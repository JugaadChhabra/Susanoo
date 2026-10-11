"""Coding worker for Builder and Fixer: one scoped code change, one commit.

Runs the Claude Agent SDK inside the target repo, then commits with plain `git commit`
so the repo's Entire hooks record a checkpoint. Decision (Entire spike, 2026-10-11): the
Agent SDK is enough, because it runs Claude Code, which loads the repo's project hooks.
That only holds with `setting_sources=["project"]` (user-only settings record nothing),
and the repo must have `entire enable --agent claude-code` already committed.

The agent gets file tools only (no Bash), so generated code never runs on the host.
`allowed_paths` is enforced after the run from `git status`; a violation reverts the
whole change. The caller runs `guards.budget.check(state)` first and adds
`ChangeResult.cost_usd` to `state["cost_usd"]`.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from susanoo.integrations.llm import SONNET

AGENT_TOOLS = ["Read", "Edit", "Write", "Glob", "Grep"]
DEFAULT_MAX_TURNS = 30
DEFAULT_MAX_BUDGET_USD = 1.0

_CHECKPOINT_TRAILER = re.compile(r"^Entire-Checkpoint:\s*(\S+)\s*$", re.MULTILINE)


class CodingWorkerError(Exception):
    """The change could not be made or committed."""


class NoChange(CodingWorkerError):
    """The agent finished without modifying any file."""


class OutsideAllowedPaths(CodingWorkerError):
    """The agent touched files outside `allowed_paths`; the change was reverted."""


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

    prompt = _build_prompt(instruction, allowed)
    try:
        cost_usd = await _run_agent(repo, prompt, model, max_turns, max_budget_usd)
    except BaseException:
        await _discard_changes(repo)  # a failed run must not leave half-edits behind
        raise

    changed = await _changed_paths(repo)
    if not changed:
        raise NoChange("The agent finished without changing any file")
    outside = [p for p in changed if not _is_allowed(p, allowed)]
    if outside:
        await _discard_changes(repo)
        raise OutsideAllowedPaths(
            f"Change reverted: it touched {sorted(outside)}, outside the allowed {sorted(allowed)}"
        )

    message = commit_message or f"agent: {instruction.strip().splitlines()[0][:72]}"
    await _git(repo, "add", "--", *changed)
    await _git(repo, "commit", "-m", message)
    sha = (await _git(repo, "rev-parse", "HEAD")).strip()
    trailer = _CHECKPOINT_TRAILER.search(await _git(repo, "log", "-1", "--format=%B"))
    return ChangeResult(commit_sha=sha, cost_usd=cost_usd, checkpoint_id=trailer.group(1) if trailer else None)


async def _run_agent(repo: Path, prompt: str, model: str, max_turns: int, max_budget_usd: float) -> float:
    options = ClaudeAgentOptions(
        cwd=repo,
        model=model,
        allowed_tools=AGENT_TOOLS,
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
        raise CodingWorkerError(f"The coding agent failed ({result.subtype}): {result.result}")
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


async def _changed_paths(repo: Path) -> list[str]:
    """Every path git sees as changed, including both sides of a rename."""
    raw = await _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
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


async def _discard_changes(repo: Path) -> None:
    # Safe: run_change only starts on a clean tree, so everything dirty now is the agent's.
    await _git(repo, "reset", "--hard", "HEAD")
    await _git(repo, "clean", "-fd")


async def _git(repo: Path, *args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=repo, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise CodingWorkerError(f"git {args[0]} failed: {err.decode().strip()}")
    return out.decode()
