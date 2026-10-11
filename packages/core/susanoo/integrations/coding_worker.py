"""Coding worker for Builder and Fixer: one scoped code change, one commit.

Runs the Claude Agent SDK inside the target repo, then commits with plain `git commit`
so the repo's Entire hooks record a checkpoint. Decision (Entire spike, 2026-10-11): the
Agent SDK is enough, because it runs Claude Code, which loads the repo's project hooks.
That only holds with `setting_sources=["project"]` (user-only settings record nothing),
and the repo must have `entire enable --agent claude-code` already committed.

The agent gets file tools only (no Bash), so generated code never runs on the host. Hardening,
checked against claude-agent-sdk as installed:
- `tools=` limits the built-in tools to AGENT_TOOLS and `disallowed_tools` names the dangerous ones again
  (Bash, WebFetch, WebSearch, Task...), so they stay off even if a project setting tries to enable them.
- `env=` is MERGED over the host environment by the SDK (it cannot unset a variable), so credential-like
  variables are overridden with "" except what the SDK needs to authenticate (see `agent_env`).
- Every tool call passes `check_tool_use`: Read/Edit/Write/Glob/Grep may not leave `repo_path` or touch
  `scenarios/heldout/`. It runs as a PreToolUse hook (always consulted) and as `can_use_tool` (consulted for
  calls the permission mode does not auto-approve; `acceptEdits` auto-approves edits, so the hook is the
  one that really gates them).
Writes are confined DURING the run: Write/Edit may only target `allowed_paths` (resolved, symlinks followed),
and nothing may touch `.git/`. The worker also snapshots `.git/config` and `.git/hooks/*` before the agent runs
and checks them before it runs any git command afterwards (git config and hooks execute code on the host with the
host's environment); on a difference it restores them, discards the work and raises `GitStateTampered`. Post-run
git commands also pass `-c core.fsmonitor=false -c core.hooksPath=<the original hooks dir>`; hooks stay enabled so
Entire's commit hooks still run.
`allowed_paths` is also enforced after the run from `git status`; a violation reverts the
whole change. The caller runs `guards.budget.check(state)` first and adds
`ChangeResult.cost_usd` to `state["cost_usd"]`.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    query,
)

from susanoo.integrations.llm import SONNET
from susanoo.integrations.tracing import REDACTED, redact, scrub_text

AGENT_TOOLS = ["Read", "Edit", "Write", "Glob", "Grep"]
DISALLOWED_TOOLS = ["Bash", "BashOutput", "KillShell", "WebFetch", "WebSearch", "Task", "Agent", "NotebookEdit"]
HELDOUT_DIR = "scenarios/heldout"
WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
_PATH_KEYS = ("file_path", "path", "notebook_path")
DEFAULT_MAX_TURNS = 30
DEFAULT_MAX_BUDGET_USD = 1.0

_CHECKPOINT_TRAILER = re.compile(r"^Entire-Checkpoint:\s*(\S+)\s*$", re.MULTILINE)


class CodingWorkerError(Exception):
    """The change could not be made or committed."""


class NoChange(CodingWorkerError):
    """The agent finished without modifying any file."""


class OutsideAllowedPaths(CodingWorkerError):
    """The agent touched files outside `allowed_paths`; the change was reverted."""


class GitStateTampered(CodingWorkerError):
    """`.git/config` or `.git/hooks/*` changed while the agent ran. They were restored; no git command ran first."""


@dataclass(frozen=True)
class ChangeResult:
    commit_sha: str
    cost_usd: float
    checkpoint_id: str | None  # None if Entire is not enabled in the repo


async def apply_change(
    repo_path: str | Path,
    instruction: str,
    allowed_paths: list[str],
    **kw: Any,
) -> str:
    """Make the change described by `instruction`, commit it, and return the commit SHA."""
    return (await run_change(repo_path, instruction, allowed_paths, **kw)).commit_sha


async def run_change(
    repo_path: str | Path,
    instruction: str,
    allowed_paths: list[str],
    *,
    model: str = SONNET,
    max_turns: int = DEFAULT_MAX_TURNS,
    max_budget_usd: float = DEFAULT_MAX_BUDGET_USD,
    commit_message: str | None = None,
) -> ChangeResult:
    """Like `apply_change`, but also returns the cost and the Entire checkpoint id.

    `allowed_paths` are files or folders relative to the repo root (e.g. "backend/").
    """
    repo = Path(repo_path).resolve()
    allowed = _normalise_allowed(allowed_paths)

    if await _git(repo, "status", "--porcelain", "--untracked-files=all"):
        raise CodingWorkerError(f"{repo} has uncommitted changes; commit or discard them first")

    snapshot = snapshot_git_state(repo)  # the repo is trusted until the agent has run
    flags = snapshot.git_flags
    prompt = _build_prompt(instruction, allowed)
    token = _ALLOWED.set(allowed)
    try:
        cost_usd = await _run_agent(repo, prompt, model, max_turns, max_budget_usd)
    except BaseException:
        # A failed run must not leave half-edits behind, and no git command may run on a tampered repo.
        restore_git_state(repo, snapshot)
        await _discard_changes(repo, flags)
        raise
    finally:
        _ALLOWED.reset(token)

    if verify_git_state(repo, snapshot) is not None:
        what = restore_git_state(repo, snapshot)
        await _discard_changes(repo, flags)
        raise GitStateTampered(
            f"Change reverted: the agent altered git's own settings ({what}), which is not allowed. "
            "Nothing was committed."
        )

    changed = await _changed_paths(repo, flags)
    if not changed:
        raise NoChange("The agent finished without changing any file")
    outside = [p for p in changed if not _is_allowed(p, allowed)]
    if outside:
        await _discard_changes(repo, flags)
        raise OutsideAllowedPaths(
            f"Change reverted: it touched {sorted(outside)}, outside the allowed {sorted(allowed)}"
        )

    message = commit_message or f"agent: {instruction.strip().splitlines()[0][:72]}"
    await _git(repo, "add", "--", *changed, flags=flags)
    await _git(repo, "commit", "-m", message, flags=flags)
    sha = (await _git(repo, "rev-parse", "HEAD", flags=flags)).strip()
    trailer = _CHECKPOINT_TRAILER.search(await _git(repo, "log", "-1", "--format=%B", flags=flags))
    return ChangeResult(commit_sha=sha, cost_usd=cost_usd, checkpoint_id=trailer.group(1) if trailer else None)


# --- git state that runs code on the host --------------------------------------------------------------------

_ALLOWED: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar("coding_worker_allowed", default=None)


@dataclass(frozen=True)
class GitSnapshot:
    """Contents (and modes) of `.git/config` and every file under the hooks dir, plus safe git flags."""

    files: dict[str, tuple[bytes, int]]  # path relative to .git -> (bytes, mode); config + hooks/**
    hooks_dir: str
    git_flags: tuple[str, ...]


def snapshot_git_state(repo: Path) -> GitSnapshot:
    """Record `.git/config` and `.git/hooks/**` of a TRUSTED repo (take it before the agent or worker runs)."""
    files = snapshot_git_state_files(repo)
    hooks_dir = _configured_hooks_dir(repo)
    return GitSnapshot(
        files=files,
        hooks_dir=hooks_dir,
        git_flags=("-c", "core.fsmonitor=false", "-c", f"core.hooksPath={hooks_dir}"),
    )


def _configured_hooks_dir(repo: Path) -> str:
    """core.hooksPath as configured in the trusted repo (resolved), else .git/hooks."""
    import subprocess

    out = subprocess.run(
        ["git", "config", "--get", "core.hooksPath"], cwd=repo, capture_output=True, text=True, check=False
    ).stdout.strip()
    path = Path(out).expanduser() if out else repo / ".git" / "hooks"
    return str(path if path.is_absolute() else repo / path)


def verify_git_state(repo: Path, snap: GitSnapshot) -> str | None:
    """None if config and hooks are exactly as snapshotted, else a short description of what differs.

    Reads files only; never runs git.
    """
    now = snapshot_git_state_files(repo)
    changed = sorted(
        name for name in snap.files.keys() | now.keys() if _digest(snap.files.get(name)) != _digest(now.get(name))
    )
    return ", ".join(f".git/{name}" for name in changed) if changed else None


def snapshot_git_state_files(repo: Path) -> dict[str, tuple[bytes, int]]:
    gitdir = repo / ".git"
    files: dict[str, tuple[bytes, int]] = {}
    for path in [gitdir / "config", *sorted((gitdir / "hooks").rglob("*"))]:
        if path.is_symlink():
            files[str(path.relative_to(gitdir))] = (os.readlink(path).encode(), -1)
        elif path.is_file():
            files[str(path.relative_to(gitdir))] = (path.read_bytes(), path.stat().st_mode & 0o7777)
    return files


def _digest(entry: tuple[bytes, int] | None) -> tuple[str, int] | None:
    return None if entry is None else (hashlib.sha256(entry[0]).hexdigest(), entry[1])


def restore_git_state(repo: Path, snap: GitSnapshot) -> str:
    """Put config and hooks back exactly as snapshotted (files only, no git). Returns what had changed."""
    what = verify_git_state(repo, snap) or ""
    if not what:
        return what
    gitdir = repo / ".git"
    current = snapshot_git_state_files(repo)
    for name in current.keys() - snap.files.keys():  # files the agent added
        (gitdir / name).unlink(missing_ok=True)
    for name, (data, mode) in snap.files.items():
        target = gitdir / name
        if mode == -1:
            target.unlink(missing_ok=True)
            target.symlink_to(data.decode())
            continue
        if target.is_symlink():
            target.unlink()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(mode)
    return what


# --- credentials and tool scoping --------------------------------------------------------------------------

# What the SDK needs to authenticate; everything else credential-like is blanked for the agent process.
_KEEP_FOR_AUTH = {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN"}
_CREDENTIAL_PREFIXES = ("GITHUB", "GH_", "SUPABASE", "NEATLOGS", "DATABASE")
_PLAIN_NAMES = {"GITHUB_ORG"}  # not secrets, even though they look like it
# Connection-string and password shapes that tracing's sensitive-key rule does not know: DB_PW, APP_PASS, X_URI...
_CREDENTIAL_SHAPES = re.compile(r"(_PW|_PWD|_PASS|_URI|_DB_URL|_DSN)$|^(DATABASE_URL|POSTGRES)")


def credential_like_vars(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Environment variables that hold (or may hold) a credential, with their values.

    A name qualifies when tracing's sensitive-key rule says so (KEY, TOKEN, SECRET, PASSWORD...), or it is
    DATABASE_URL / POSTGRES* / a *_DSN, *_URI, *_DB_URL, *_PW, *_PWD or *_PASS variable, or it is a
    GITHUB/SUPABASE/NEATLOGS/DATABASE variable with KEY in its name. Empty values are skipped.
    """
    env = os.environ if environ is None else environ
    found: dict[str, str] = {}
    for name, value in env.items():
        if not value or name in _PLAIN_NAMES:
            continue
        upper = name.upper()
        sensitive = redact(name, "x") == REDACTED
        connection = _CREDENTIAL_SHAPES.search(upper) is not None
        keyed = upper.startswith(_CREDENTIAL_PREFIXES) and "KEY" in upper  # e.g. SUPABASE_SERVICE_KEY
        if sensitive or connection or keyed:
            found[name] = value
    return found


def agent_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """`ClaudeAgentOptions.env` for the agent process: blank every credential it does not need.

    The SDK merges this over the host environment, so blanking ("") is the only way to hide a variable.
    ANTHROPIC_API_KEY and the other auth variables are kept because Claude Code cannot run without them.
    """
    return {name: "" for name in credential_like_vars(environ) if name not in _KEEP_FOR_AUTH}


def check_tool_use(
    repo: Path, tool_name: str, tool_input: Mapping[str, Any], allowed: list[str] | None = None
) -> str | None:
    """Why this tool call must be refused, or None if it may run. Plain English, safe to show.

    Only the file tools exist. Every path must stay inside `repo`, out of scenarios/heldout/ and out of `.git/`
    (compared case-insensitively, since macOS disks are). When `allowed` is given, writing tools may only
    target those repo-relative paths, judged on the fully resolved target so a symlink cannot lead out.
    """
    if tool_name not in AGENT_TOOLS and tool_name not in WRITE_TOOLS:
        return f"The tool {tool_name} is not available here."
    root = repo.resolve()
    heldout = root / HELDOUT_DIR
    gitdir = root / ".git"
    for key in _PATH_KEYS:
        raw = tool_input.get(key)
        if not raw:
            continue
        target = _resolve(root, str(raw))
        if not _inside(target, root):
            return "That path is outside the project folder, so it is off limits."
        if _inside_ci(target, heldout):
            return "The held-out scenarios are off limits."
        if _inside_ci(target, gitdir):
            return "Git's own files are off limits."
        if tool_name in WRITE_TOOLS and allowed is not None:
            if not any(_inside(target, root / a) for a in allowed):
                return f"You may only change files in {', '.join(allowed)}; that file is somewhere else."
    if tool_name in WRITE_TOOLS and allowed is not None and not any(tool_input.get(k) for k in _PATH_KEYS):
        return "A file path is required."
    if tool_name in ("Glob", "Grep"):
        pattern = tool_input.get("pattern") if tool_name == "Glob" else tool_input.get("glob")
        if isinstance(pattern, str) and (pattern.startswith(("/", "~")) or ".." in PurePosixPath(pattern).parts):
            return "Search patterns must stay inside the project folder."
        search_root = _resolve(root, str(tool_input.get("path") or root))
        if _inside_ci(heldout, search_root) and _has_real_files(heldout):
            return "Search a specific subfolder instead; the held-out scenarios are off limits."
    return None


def _resolve(root: Path, raw: str) -> Path:
    path = Path(raw).expanduser()
    return (path if path.is_absolute() else root / path).resolve()


def _inside(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _inside_ci(path: Path, parent: Path) -> bool:
    """Like `_inside`, ignoring case. Used for deny-lists, where wrongly matching is the safe error."""
    a, b = [part.casefold() for part in path.parts], [part.casefold() for part in parent.parts]
    return a[: len(b)] == b


def _has_real_files(folder: Path) -> bool:
    return folder.is_dir() and any(p.is_file() and p.name != ".gitkeep" for p in folder.rglob("*"))


def _tool_guards(repo: Path, allowed: list[str] | None = None) -> tuple[Any, dict[str, list[HookMatcher]]]:
    """(can_use_tool, hooks) that deny any call `check_tool_use` refuses."""

    async def can_use_tool(tool_name: str, tool_input: dict[str, Any], context: Any) -> Any:
        reason = check_tool_use(repo, tool_name, tool_input, allowed)
        return PermissionResultDeny(message=reason) if reason else PermissionResultAllow()

    async def pre_tool_use(input_data: Any, tool_use_id: str | None, context: Any) -> dict[str, Any]:
        reason = check_tool_use(repo, input_data.get("tool_name", ""), input_data.get("tool_input") or {}, allowed)
        if not reason:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }

    return can_use_tool, {"PreToolUse": [HookMatcher(matcher=None, hooks=[pre_tool_use])]}


async def _run_agent(repo: Path, prompt: str, model: str, max_turns: int, max_budget_usd: float) -> float:
    can_use_tool, hooks = _tool_guards(repo, _ALLOWED.get())
    options = ClaudeAgentOptions(
        cwd=repo,
        model=model,
        tools=AGENT_TOOLS,
        allowed_tools=AGENT_TOOLS,
        disallowed_tools=DISALLOWED_TOOLS,
        env=agent_env(),
        can_use_tool=can_use_tool,
        hooks=hooks,
        permission_mode="acceptEdits",
        setting_sources=["project"],  # required: loads the repo's Entire hooks
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
    )
    result: ResultMessage | None = None
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, ResultMessage):
            result = message
    if result is None:
        raise CodingWorkerError("The coding agent ended without a result")
    if result.is_error or result.subtype != "success":
        raise CodingWorkerError(f"The coding agent failed ({result.subtype}): {scrub_text(str(result.result))}")
    return result.total_cost_usd or 0.0


def _build_prompt(instruction: str, allowed: list[str]) -> str:
    folders = "\n".join(f"- {p}" for p in allowed)
    return (
        f"{instruction.strip()}\n\n"
        f"You may only create or modify files in these paths (relative to the repo root):\n{folders}\n"
        "Make the smallest change that does the job. Do not commit; the caller commits."
    )


def _normalise_allowed(allowed_paths: list[str]) -> list[str]:
    if not allowed_paths:
        raise ValueError("allowed_paths must list at least one file or folder")
    out = []
    for raw in allowed_paths:
        path = PurePosixPath(raw)
        if path.is_absolute() or ".." in path.parts or str(path) in ("", "."):
            raise ValueError(f"allowed_paths entries must be repo-relative, got {raw!r}")
        out.append(str(path))
    return out


def _is_allowed(path: str, allowed: list[str]) -> bool:
    return any(path == a or path.startswith(a + "/") for a in allowed)


async def _changed_paths(repo: Path, flags: tuple[str, ...] = ()) -> list[str]:
    """Every path git sees as changed, including both sides of a rename."""
    raw = await _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all", flags=flags)
    entries = raw.split("\0")
    paths: list[str] = []
    i = 0
    while i < len(entries) and entries[i]:
        status, path = entries[i][:2], entries[i][3:]
        paths.append(path)
        if "R" in status or "C" in status:  # -z lists the rename source as the next entry
            i += 1
            paths.append(entries[i])
        i += 1
    return paths


async def _discard_changes(repo: Path, flags: tuple[str, ...] = ()) -> None:
    # Safe: run_change only starts on a clean tree, so everything dirty now is the agent's.
    await _git(repo, "reset", "--hard", "HEAD", flags=flags)
    await _git(repo, "clean", "-fd", flags=flags)


async def _git(repo: Path, *args: str, flags: tuple[str, ...] = ()) -> str:
    """Run git in `repo`. `flags` go before the subcommand (e.g. -c core.fsmonitor=false)."""
    proc = await asyncio.create_subprocess_exec(
        "git", *flags, *args, cwd=repo, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise CodingWorkerError(f"git {args[0]} failed: {scrub_text(err.decode().strip())}")
    return out.decode()
