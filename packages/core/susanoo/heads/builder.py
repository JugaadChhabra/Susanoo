"""Builder head (Sonnet): template -> generated repo. [PRD › The eight heads of Orochi] (Builder),
[PRD › Repository structure › Generated product repo]; IMPLEMENTATION_MUAAZ.md P2.4.

Steps: copy `templates/app-template` into a run workdir, make the first commit, have the coding worker fill
only `backend/` and `web/` from the Plan (it commits and records an Entire checkpoint), then create the repo
in `GITHUB_ORG` and push. With no GitHub org or token configured the run stays local and says so.

Entire: when the `entire` CLI is installed, `entire enable --agent claude-code` runs in the new repo before the
worker, so the worker's commit carries an Entire checkpoint. (Copying the template cannot carry git hooks.)

Guardrails: the coding worker may only touch `backend/` and `web/` (it reverts and raises
OutsideAllowedPaths otherwise); nothing inside `scenarios/heldout/` is ever copied from the template, so
there is nothing held-out for the worker to read or edit; credentials are never written into the repo: every file
the worker changed is scanned for secrets before any push, and a hit resets the repo and stops the run.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import Any

from susanoo.emit import emit
from susanoo.guards import budget
from susanoo.integrations import coding_worker, github, llm
from susanoo.integrations.coding_worker import ChangeResult
from susanoo.integrations.tracing import scrub_text
from susanoo.schemas.state import SusanooState

try:  # tracing lands with task 0.3; heads still run without it
    from susanoo.integrations import tracing

    _span: Callable[..., AbstractContextManager[Any]] = tracing.span
except ImportError:  # pragma: no cover

    def _span(name: str, **attrs: Any) -> AbstractContextManager[Any]:
        return nullcontext()


HEAD = "builder"
WORKDIR_ENV = "SUSANOO_WORKDIR"
ALLOWED_PATHS = ["backend/", "web/"]  # [PRD › Repository structure]: the `code` layer, and all the Builder fills
TEMPLATE_DIR = Path(__file__).resolve().parents[4] / "templates" / "app-template"
PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "builder.md"

_COPY_IGNORE = shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".pytest_cache", ".DS_Store")
_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_GIT_IDENTITY = {"user.name": "Susanoo Builder", "user.email": "builder@susanoo.invalid"}

Worker = Callable[..., Awaitable[ChangeResult]]
CommandRunner = Callable[[list[str], Path], Awaitable[None]]  # (argv, cwd); raises on a non-zero exit

_which = shutil.which  # patched in tests
ENTIRE_ENABLE = ["entire", "enable", "--agent", "claude-code", "--project", "--skip-push-sessions", "--telemetry=false"]

_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_TOKEN_SPLIT = re.compile(r"[\s\"'`=,()\[\]{}<>;]+")
_MIN_SECRET_LEN = 8
_MAX_SCAN_BYTES = 5_000_000


class SecretInGeneratedCode(Exception):
    """The generated code contains a credential. The repo was reset and nothing was pushed.

    `files` lists the offending paths; the secret value is never kept in the exception.
    """

    def __init__(self, files: list[str]) -> None:
        self.files = files
        super().__init__(f"A credential was found in the generated code ({', '.join(files)}); nothing was pushed.")


async def run(
    state: SusanooState,
    *,
    worker: Worker | None = None,
    github_client: github.GitHubClient | None = None,
    template_dir: Path | None = None,
    command_runner: CommandRunner | None = None,
) -> dict:
    """Generate the product repo. `worker`, `github_client`, `template_dir` and `command_runner` are for tests."""
    with _span(HEAD, run_id=state["run_id"]):
        await emit(state, HEAD, "started", "Setting up your app from the template…")
        try:
            budget.check(state)
            plan = state["plan"]
            repo = _prepare_repo(state["run_id"], template_dir or TEMPLATE_DIR)
            await _git(repo, "init", "-q", "-b", "main")
            for key, value in _GIT_IDENTITY.items():
                await _git(repo, "config", key, value)
            await _git(repo, "config", "commit.gpgsign", "false")
            await _git(repo, "add", "-A")
            await _git(repo, "commit", "-q", "-m", "chore: start from the app template")
            await _enable_entire(state, repo, command_runner or _run_command)
            base = (await _git(repo, "rev-parse", "HEAD")).strip()
            snapshot = coding_worker.snapshot_git_state(repo)  # git config/hooks run code: trusted until the worker ran
            await emit(state, HEAD, "progress", "Template copied. Now writing the app's code and page…")

            budget.check(state)  # again, right before the paid coding-worker call
            change = await (worker or coding_worker.run_change)(
                repo,
                _instruction(plan),
                list(ALLOWED_PATHS),
                model=llm.HEAD_MODELS[HEAD],
                max_budget_usd=_worker_budget(state),
                commit_message=f"feat: build {plan.product_name} from the plan",
            )
            cost = round(float(state.get("cost_usd", 0.0)) + change.cost_usd, 6)
            await _refuse_tampering(repo, base, snapshot)  # before ANY further git command runs on the host
            await _refuse_secrets(repo, base, snapshot.git_flags)  # before anything leaves this machine

            payload: dict[str, Any] = {"commit": change.commit_sha, "checkpoint_id": change.checkpoint_id}
            remote_note = await _publish(state, repo, plan.product_name, plan.summary, github_client, payload)
            await emit(state, HEAD, "finished", f"Your app is built and saved. {remote_note}", payload)
            return {"repo_path": str(repo), "head_commit": change.commit_sha, "cost_usd": cost}
        except budget.BudgetExceeded:
            raise  # the graph ends the run cleanly
        except coding_worker.OutsideAllowedPaths as exc:
            await emit(
                state,
                HEAD,
                "error",
                "The app builder tried to change files it is not allowed to touch, so I undid its work.",
                {"detail": scrub_text(str(exc))},
            )
            raise
        except coding_worker.GitStateTampered as exc:
            await emit(
                state,
                HEAD,
                "error",
                "The app builder tried to change git's own settings, so I undid its work and did not upload anything.",
                {"detail": scrub_text(str(exc))},
            )
            raise
        except SecretInGeneratedCode as exc:
            await emit(
                state,
                HEAD,
                "error",
                f"I stopped before saving anything: {_name_files(exc.files)} looks like it contains a password or "
                "secret key. I undid the generated code and did not upload it.",
                {"files": exc.files},
            )
            raise
        except Exception as exc:
            await emit(state, HEAD, "error", f"Building the app failed: {scrub_text(str(exc))}")
            raise


def _name_files(files: list[str]) -> str:
    shown = ", ".join(scrub_text(f) for f in files[:3])
    return shown + (f" and {len(files) - 3} more files" if len(files) > 3 else "")


async def _enable_entire(state: SusanooState, repo: Path, runner: CommandRunner) -> None:
    """Turn on Entire in the new repo (needs its git hooks, which copying cannot carry) and commit its files."""
    if not _which("entire"):
        await emit(state, HEAD, "progress", "Entire isn't installed here, so fixes won't have checkpoints.")
        return
    try:
        await runner(list(ENTIRE_ENABLE), repo)
    except Exception as exc:
        await _git(repo, "reset", "--hard", "HEAD")  # drop any half-written Entire files
        await _git(repo, "clean", "-fd")
        await emit(
            state,
            HEAD,
            "progress",
            f"Entire could not be set up ({scrub_text(str(exc))}), so fixes won't have checkpoints.",
        )
        return
    await _git(repo, "add", "-A")
    if (await _git(repo, "status", "--porcelain")).strip():
        await _git(repo, "commit", "-q", "-m", "chore: enable Entire")
    await emit(state, HEAD, "progress", "Entire is switched on, so each fix will get a checkpoint.")


async def _run_command(args: list[str], cwd: Path) -> None:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        detail = (err or out).decode(errors="replace").strip().splitlines()
        raise RuntimeError(f"{args[0]} exited with {proc.returncode}: {detail[-1] if detail else 'no output'}")


# --- secret scan ---------------------------------------------------------------------------------------------


async def _refuse_tampering(repo: Path, base: str, snapshot: coding_worker.GitSnapshot) -> None:
    """If `.git/config` or `.git/hooks` changed while the worker ran, restore them, reset the repo and raise.

    No git command runs before the restore: a changed config or hook would execute with the host's environment.
    """
    if coding_worker.verify_git_state(repo, snapshot) is None:
        return
    what = coding_worker.restore_git_state(repo, snapshot)
    await _git(repo, "reset", "--hard", base, flags=snapshot.git_flags)
    await _git(repo, "clean", "-fd", flags=snapshot.git_flags)
    raise coding_worker.GitStateTampered(
        f"Change reverted: the app builder altered git's own settings ({what}), which is not allowed."
    )


async def _refuse_secrets(repo: Path, base: str, flags: tuple[str, ...] = ()) -> None:
    """Scan everything the worker changed since `base`; on a hit reset the repo to `base` and raise."""
    hits = await _files_with_secrets(repo, base, flags)
    if hits:
        await _git(repo, "reset", "--hard", base, flags=flags)
        await _git(repo, "clean", "-fd", flags=flags)
        raise SecretInGeneratedCode(hits)


async def _files_with_secrets(repo: Path, base: str, flags: tuple[str, ...] = ()) -> list[str]:
    """Changed files (committed, staged, edited or untracked) that hold a credential. Never returns values."""
    changed = set((await _git(repo, "diff", "--name-only", "-z", "--diff-filter=d", base, flags=flags)).split("\0"))
    changed |= set((await _git(repo, "ls-files", "-z", "--others", "--exclude-standard", flags=flags)).split("\0"))
    values = list(coding_worker.credential_like_vars().values())
    hits = []
    for rel in sorted(p for p in changed if p):
        path = repo / rel
        if path.is_file() and path.stat().st_size <= _MAX_SCAN_BYTES and has_secret(_read(path), values):
            hits.append(rel)
    messages = await _git(repo, "log", f"{base}..HEAD", "--format=%B", flags=flags)
    if has_secret(messages, values):
        hits.append("(a commit message)")
    return hits


def _read(path: Path) -> str:
    return path.read_bytes().decode("latin-1")


def has_secret(text: str, env_values: list[str]) -> bool:
    """True if `text` holds a host credential (plain or base64) or something shaped like a secret key.

    Detection by value and by shape only: tracing's `scrub_text` runs on single tokens, so its
    value patterns (Anthropic/OpenAI/Stripe keys, JWTs, AWS and GitHub tokens, URL passwords) apply but its
    `password=...` assignment heuristic does not (that one would flag `api_key = os.environ[...]`).
    """
    for value in env_values:
        if len(value) < _MIN_SECRET_LEN:
            continue
        raw = value.encode()
        forms = {value}
        for encoder in (base64.b64encode, base64.urlsafe_b64encode):
            encoded = encoder(raw).decode()
            forms.update({encoded, encoded.rstrip("=")})
        if any(form in text for form in forms):
            return True
    if _PEM.search(text):
        return True
    return any(len(tok) >= _MIN_SECRET_LEN and scrub_text(tok) != tok for tok in _TOKEN_SPLIT.split(text))


async def _publish(
    state: SusanooState,
    repo: Path,
    product_name: str,
    summary: str,
    client: github.GitHubClient | None,
    payload: dict[str, Any],
) -> str:
    """Create the org repo and push. Returns a short sentence for the finished event."""
    config = github.github_config_from_env()
    if client is None and config is None:
        await emit(
            state,
            HEAD,
            "progress",
            "GitHub is not set up (no organisation or token), so the app stays on this machine for now.",
        )
        payload["remote"] = None
        return "It was kept locally (not uploaded to GitHub)."
    org = config[0] if config else os.environ.get(github.ORG_ENV, "").strip()
    if not org:
        await emit(state, HEAD, "progress", "No GitHub organisation is set, so the app stays on this machine for now.")
        payload["remote"] = None
        return "It was kept locally (not uploaded to GitHub)."

    name = github.repo_name(product_name, state["run_id"])
    await emit(state, HEAD, "progress", f"Creating the repo {org}/{name} on GitHub…")
    owns_client = client is None
    gh = client or github.GitHubClient(config[1])  # type: ignore[index]  # config is set when client is None
    try:
        info = await gh.create_and_push(org, name, repo, description=summary)
    finally:
        if owns_client:
            await gh.aclose()
    payload["remote"] = info.html_url
    return f"It is on GitHub at {info.html_url}."


def _prepare_repo(run_id: str, template_dir: Path) -> Path:
    if not _SAFE_RUN_ID.match(run_id):
        raise ValueError(f"Unsafe run id {run_id!r}")
    if not template_dir.is_dir():
        raise FileNotFoundError(f"The app template was not found at {template_dir}")
    workdir = Path(os.environ.get(WORKDIR_ENV) or tempfile.mkdtemp(prefix="susanoo-")).resolve()
    repo = workdir / run_id / "repo"
    if repo.exists():
        shutil.rmtree(repo)  # a retried run starts clean; this folder belongs to this run only
    shutil.copytree(template_dir, repo, ignore=_COPY_IGNORE)
    # Held-out scenarios never enter a repo the coding worker can see: keep the folder, drop its contents.
    heldout = repo / "scenarios" / "heldout"
    shutil.rmtree(heldout, ignore_errors=True)
    heldout.mkdir(parents=True)
    (heldout / ".gitkeep").touch()
    return repo


def _instruction(plan: Any) -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8")
    template = re.sub(r"<!--.*?-->\s*", "", template, count=1, flags=re.DOTALL)
    files = "\n".join(f"- {f}" for f in plan.files_to_generate) or "- (none listed; decide from the plan)"
    values = {
        "product_name": plan.product_name,
        "files": files,
        "plan": plan.model_dump_json(indent=2),
    }
    # One pass, so text inside a value is never substituted again.
    return re.sub(r"\{\{(\w+)\}\}", lambda m: values.get(m.group(1), m.group(0)), template).strip()


def _worker_budget(state: SusanooState) -> float:
    remaining = budget.run_cost_cap_usd() - float(state.get("cost_usd", 0.0))
    return round(max(0.05, min(coding_worker.DEFAULT_MAX_BUDGET_USD, remaining)), 4)


async def _git(repo: Path, *args: str, flags: tuple[str, ...] = ()) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", *flags, *args, cwd=repo, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise coding_worker.CodingWorkerError(f"git {args[0]} failed: {scrub_text(err.decode().strip())}")
    return out.decode()
