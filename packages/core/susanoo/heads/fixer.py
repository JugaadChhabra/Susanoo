"""Fixer head (Sonnet): one diagnosis -> the smallest patch, one commit. [PRD › The eight heads of Orochi] (Fixer),
[PRD › Improvement loop and guardrails]; IMPLEMENTATION_MUAAZ.md P4.2.

The Fixer takes the newest `Diagnosis` (`failure_ledger[-1]`) and has the coding worker change only the paths
the diagnosed layer allows (`guards/layer_allowlist.py`). The worker commits once and Entire records a
checkpoint against that commit; the commit message carries the failure, the diagnosis and the rationale.
After the worker returns, the Fixer checks the committed diff again with the layer allow-list and the patch
size cap (`guards/patch_size.py`); a violation is undone with `git revert` (history is kept, "rollback =
revert") and the head raises `FixRejected`. Whether a valid patch is KEPT is the Verifier's call, not the
Fixer's: the new `Patch` comes back with verdict "pending".

`test` layer: the HumanGate node (graph/stubs.py `human_gate`) is what pauses, with `interrupt()` and an
`approval_needed` event. The Fixer only runs after it, and refuses (`FixRefused`, nothing edited) unless
`state["gate_decision"] == "approved"` for this very diagnosis. Even then it may edit only
`scenarios/train/<id>.json` and `<id>.world.json` of the diagnosed train scenarios that the user did not approve
(`approved_by_user`) and nobody disputed. `scenarios/heldout/` and the user-approved `scenarios/acceptance/`
are never read, listed or written (the coding worker denies every tool on both), and their text is kept out of
the instruction, the commit message and the events.

Guardrails: allow-listed folders per layer, patch cap, git rollback, budget guard before the paid call,
credential scrubbing of everything that reaches a commit message or an event.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Awaitable, Callable
from pathlib import Path

import yaml

from susanoo.emit import emit
from susanoo.guards import budget, layer_allowlist, patch_size
from susanoo.integrations import coding_worker, llm, tracing
from susanoo.integrations.coding_worker import ChangeResult
from susanoo.integrations.tracing import scrub_text
from susanoo.schemas import Diagnosis, Patch, Rule, Scenario, ScenarioResult
from susanoo.schemas.state import SusanooState

HEAD = "fixer"
PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / f"{HEAD}.md"

LAYER_NAMES = {
    "code": "app's code",
    "tool": "agent's tools",
    "prompt_policy": "agent's prompt and policy",
    "test": "test",
}
RATIONALE = {
    "code": "Change only the app's code (backend/ and web/), in the smallest way that removes the cause above.",
    "tool": "Change only the agent's tools (agent/tools.py), in the smallest way that removes the cause above.",
    "prompt_policy": (
        "Change only the agent's prompt and policy (agent/prompt.md, agent/policy.yaml), in the smallest way "
        "that removes the cause above, without dropping or softening any of the user's rules."
    ),
    "test": (
        "The test itself is wrong, not the agent: change only the answer key or mocked world of the scenarios "
        "named above, which a person approved, and nothing else."
    ),
}

# Used only for our own `git revert` commits when the generated repo has no git identity configured.
FALLBACK_IDENTITY_ENV = {
    "GIT_AUTHOR_NAME": "Susanoo",
    "GIT_AUTHOR_EMAIL": "susanoo@users.noreply.github.com",
    "GIT_COMMITTER_NAME": "Susanoo",
    "GIT_COMMITTER_EMAIL": "susanoo@users.noreply.github.com",
}

# Scenario splits the Fixer must never see: held-out, and the user's approved (acceptance) scenarios, which are locked.
HIDDEN_SPLITS = ("heldout", "acceptance")
MAX_HYPOTHESIS_CHARS = 600
MAX_LISTED_SCENARIOS = 6
MAX_FINDINGS_PER_SCENARIO = 8
_SAFE_SCENARIO_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,100}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_MIN_HELDOUT_SNIPPET = 30

Worker = Callable[..., Awaitable[ChangeResult]]


class FixerError(Exception):
    """The Fixer did not produce a patch. The message is plain English and safe to show."""


class FixRefused(FixerError):
    """The Fixer refused before any edit (no approval, nothing it may edit, a bad diagnosis). Nothing changed."""


class FixRejected(FixerError):
    """The worker made a patch the guardrails reject; it was undone with `git revert`.

    `cost_usd` is the run's total cost INCLUDING the rejected attempt (the graph state is not updated when a
    head raises, so the caller can store it). `head_commit` is the repo HEAD after the revert, `cause` the
    guard that fired (OutsideLayer, PatchTooLarge, GitStateTampered, ...), or None.
    """

    def __init__(self, message: str, *, cost_usd: float, head_commit: str | None, cause: Exception | None) -> None:
        super().__init__(message)
        self.cost_usd = cost_usd
        self.head_commit = head_commit
        self.cause = cause


# --- the head ----------------------------------------------------------------------------------------------


async def run(state: SusanooState, *, worker: Worker | None = None) -> dict:
    """Make one patch for `failure_ledger[-1]`. `worker` is for tests; the default is coding_worker.run_change."""
    with tracing.span(HEAD, run_id=state["run_id"]):
        await emit(state, HEAD, "started", "Writing the smallest fix for the diagnosed cause…")
        try:
            budget.check(state)
            dx = _latest_diagnosis(state)
            editable = _editable_paths(state, dx)
            repo = Path(state["repo_path"])
            if not (repo / ".git").exists():
                raise FixRefused("The generated app's folder is not a git repository, so I changed nothing.")

            snapshot = coding_worker.snapshot_git_state(repo)  # git config/hooks run code: trusted until the worker ran
            flags = snapshot.git_flags
            base = (await _git(repo, "rev-parse", "HEAD", flags=flags)).strip()
            hide = _HeldoutText(state)
            instruction = _instruction(state, dx, editable, hide)
            message = _commit_message(state, dx, editable, hide)

            budget.check(state)  # again, right before the paid coding-worker call
            change = await (worker or coding_worker.run_change)(
                repo,
                instruction,
                list(editable),
                model=llm.HEAD_MODELS[HEAD],
                max_budget_usd=_worker_budget(state),
                commit_message=message,
            )
            cost = round(float(state.get("cost_usd", 0.0)) + change.cost_usd, 6)
            lines = await _check_patch(
                repo,
                base,
                change,
                dx.layer,
                editable,
                snapshot,
                cost,
                plan.rules if (plan := state.get("plan")) else None,
            )

            patches = list(state.get("patches", []))
            patch = Patch(
                id=f"patch-{len(patches) + 1}",
                diagnosis_id=dx.id,
                commit_sha=change.commit_sha,
                checkpoint_id=change.checkpoint_id,
                lines_changed=lines,
                verdict="pending",
            )
            where = LAYER_NAMES[dx.layer]
            checkpoint = (
                "It is recorded in an Entire checkpoint."
                if change.checkpoint_id
                else "No Entire checkpoint was recorded for it."
            )
            await emit(
                state,
                HEAD,
                "finished",
                f"Fix {len(patches) + 1} is saved ({lines} line{'s' if lines != 1 else ''} changed in the {where}). "
                f"{checkpoint}",
                {
                    "patch_id": patch.id,
                    "diagnosis_id": dx.id,
                    "layer": dx.layer,
                    "commit": change.commit_sha,
                    "checkpoint_id": change.checkpoint_id,
                    "lines_changed": lines,
                },
            )
            return {"patches": patches + [patch], "head_commit": change.commit_sha, "cost_usd": cost}
        except budget.BudgetExceeded:
            raise  # the graph ends the run cleanly
        except FixRefused as exc:
            await emit(state, HEAD, "error", str(exc), {"refused": True})
            raise
        except FixRejected as exc:
            await emit(state, HEAD, "error", str(exc), {"rejected": True, "cost_usd": exc.cost_usd})
            raise
        except coding_worker.OutsideAllowedPaths as exc:
            await emit(
                state,
                HEAD,
                "error",
                "The fixer tried to change files it is not allowed to touch, so I undid its work.",
                {"detail": scrub_text(str(exc))},
            )
            raise
        except coding_worker.GitStateTampered as exc:
            await emit(
                state,
                HEAD,
                "error",
                "The fixer tried to change git's own settings, so I undid its work.",
                {"detail": scrub_text(str(exc))},
            )
            raise
        except coding_worker.NoChange:
            await emit(
                state, HEAD, "error", "The fixer finished without changing anything, so there is no fix to test."
            )
            raise
        except Exception as exc:
            await emit(state, HEAD, "error", f"Writing the fix failed: {scrub_text(str(exc))}")
            raise


# --- what may be edited -------------------------------------------------------------------------------------


def _latest_diagnosis(state: SusanooState) -> Diagnosis:
    ledger = state.get("failure_ledger") or []
    if not ledger:
        raise FixRefused("There is no diagnosis to fix yet, so I changed nothing.")
    return ledger[-1]


def _editable_paths(state: SusanooState, dx: Diagnosis) -> list[str]:
    """The exact paths the worker may change, or FixRefused. Runs before anything is edited."""
    try:
        if dx.layer == "test":
            _require_approval(state, dx)
            paths = _test_scenario_paths(state, dx)
            layer_allowlist.check_paths(dx.layer, paths)  # exact files; the allow-list entries themselves are trusted
        else:
            paths = layer_allowlist.allowed_paths(dx.layer)
        if dx.target_files:  # the diagnosis must itself stay inside its layer; otherwise re-diagnose
            layer_allowlist.check_paths(dx.layer, dx.target_files)
    except layer_allowlist.OutsideLayer as exc:
        raise FixRefused(
            f"The diagnosis points at {scrub_text(exc.path)}, which a fix in the {LAYER_NAMES[dx.layer]} "
            "may not change, so I changed nothing. It needs to be diagnosed again."
        ) from exc
    except ValueError as exc:  # unknown layer
        raise FixRefused(f"I do not know how to fix that kind of cause ({scrub_text(str(exc))}).") from exc
    return paths


def _require_approval(state: SusanooState, dx: Diagnosis) -> None:
    """A test change needs the human's yes for THIS diagnosis, once. [PRD › Improvement loop and guardrails]"""
    if state.get("gate_decision") != "approved":  # type: ignore[typeddict-item]  # graph-internal key
        raise FixRefused("This fix changes a test, and it has not been approved by you, so I changed nothing.")
    approved_for = state.get("gate_diagnosis_id")  # type: ignore[typeddict-item]  # set by the gate when it can
    if approved_for is not None and approved_for != dx.id:
        raise FixRefused("The approval on record is for a different cause, so I did not change any test.")
    if any(p.diagnosis_id == dx.id for p in state.get("patches", [])):
        raise FixRefused("You approved this test change once and it has already been used, so I changed nothing.")


def _test_scenario_paths(state: SusanooState, dx: Diagnosis) -> list[str]:
    """scenarios/train/<id>.json and .world.json for diagnosed train scenarios that are open to change."""
    by_id = {s.id: s for s in state.get("scenarios", [])}
    disputed = set(state.get("disputed_scenario_ids", []))  # type: ignore[typeddict-item]
    paths: list[str] = []
    for sid in dict.fromkeys(dx.scenario_ids):
        sc = by_id.get(sid)
        if sc is None or sc.split != "train" or sc.approved_by_user or sid in disputed:
            continue  # held-out, acceptance (user-approved), unknown and refused scenarios are never edited
        if not _SAFE_SCENARIO_ID.match(sid) or sid.endswith("."):
            continue
        paths += [f"scenarios/train/{sid}.json", f"scenarios/train/{sid}.world.json"]
    if not paths:
        raise FixRefused(
            "None of the scenarios behind this diagnosis can be changed (they are held out, approved by you, "
            "refused earlier or unknown), so I changed nothing."
        )
    return paths


def _is_editable(path: str, editable: list[str]) -> bool:
    return any(path == e or (e.endswith("/") and path.startswith(e)) for e in editable)


# --- checking the committed patch ------------------------------------------------------------------------------


async def _check_patch(
    repo: Path,
    base: str,
    change: ChangeResult,
    layer: str,
    editable: list[str],
    snapshot: coding_worker.GitSnapshot,
    cost: float,
    state_rules: list[Rule] | None = None,
) -> int:
    """Re-check the committed patch; on a violation `git revert` it and raise FixRejected. Returns lines changed."""
    flags = snapshot.git_flags
    if coding_worker.verify_git_state(repo, snapshot) is not None:
        # No git command may run first: a changed config or hook would execute with the host's environment.
        what = coding_worker.restore_git_state(repo, snapshot)
        await _git(repo, "reset", "--hard", base, flags=flags)
        await _git(repo, "clean", "-fd", flags=flags)
        raise FixRejected(
            "The fixer tried to change git's own settings, so I undid its work.",
            cost_usd=cost,
            head_commit=base,
            cause=coding_worker.GitStateTampered(f"Change reverted: git settings altered ({what})."),
        )

    if not await _is_ancestor(repo, base, flags):
        # History was rewritten under us (amend, reset, other branch): `git revert` cannot undo that.
        await _git(repo, "reset", "--hard", base, flags=flags)
        await _git(repo, "clean", "-fd", flags=flags)
        raise FixRejected(
            "The fixer rewrote the saved history instead of adding one change, so I put everything back as it was.",
            cost_usd=cost,
            head_commit=base,
            cause=None,
        )

    cause: Exception | None = None
    reason = ""
    head = (await _git(repo, "rev-parse", "HEAD", flags=flags)).strip()
    count = (await _git(repo, "rev-list", "--count", f"{base}..HEAD", flags=flags)).strip()
    if head != change.commit_sha or count != "1":
        reason = "The fixer saved more than one change, but each fix must be exactly one change, so I undid it."
    else:
        listing = await _git(repo, "diff", "--raw", "-z", "--no-renames", base, head, flags=flags)
        entries = _parse_raw(listing)
        changed = [path for path, _ in entries]
        if not changed:
            raise FixRejected(
                "The fixer saved a change that edits no file, so there is no fix to test.",
                cost_usd=cost,
                head_commit=head,
                cause=None,
            )
        try:
            layer_allowlist.check_paths(layer, changed)  # type: ignore[arg-type]
            for path in changed:
                if not _is_editable(path, editable):
                    raise layer_allowlist.OutsideLayer(
                        layer, path, f"'{path}' is not one of the files this fix was approved to change."
                    )
            for path, new_mode in entries:
                if new_mode in _LINK_MODES:  # a symlink or submodule could point out of the layer
                    raise SymlinkInPatch(path)
            lines = patch_size.check(repo, base, head)
            if "agent/policy.yaml" in changed:
                await _check_rules_kept(repo, base, head, state_rules, flags)
            return lines
        except layer_allowlist.OutsideLayer as exc:
            cause = exc
            reason = (
                f"The fix changed {scrub_text(exc.path)}, which a fix in the {LAYER_NAMES.get(layer, layer)} is not "
                "allowed to change, so I undid it."
            )
        except SymlinkInPatch as exc:
            cause = exc
            reason = (
                f"The fix turned {scrub_text(exc.path)} into a link to somewhere else, which is not allowed, "
                "so I undid it."
            )
        except RulesWeakened as exc:
            cause = exc
            reason = f"{exc} I undid it."
        except patch_size.PatchTooLarge as exc:
            cause = exc
            reason = f"{exc} I undid it."
        except patch_size.GitDiffError as exc:
            cause = exc
            reason = f"I could not measure the fix ({scrub_text(str(exc))}), so I undid it to be safe."

    new_head = await _revert(repo, base, flags, reason, cost, cause)
    raise FixRejected(reason, cost_usd=cost, head_commit=new_head, cause=cause)


_LINK_MODES = frozenset({"120000", "160000"})  # symlink, submodule
POLICY_PATH = "agent/policy.yaml"


class SymlinkInPatch(Exception):
    """The patch adds a symbolic link (or submodule), which could point outside the allowed files."""

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(f"'{path}' is a link")


class RulesWeakened(Exception):
    """The patch deletes policy.yaml, drops one of the user's rules or changes a severity the user set."""


def _parse_raw(listing: str) -> list[tuple[str, str]]:
    """(path, new file mode) per entry of `git diff --raw -z --no-renames` output."""
    parts = listing.split("\0")
    entries = []
    for i in range(0, len(parts) - 1, 2):
        meta = parts[i].split()  # ":oldmode newmode oldsha newsha status"
        if len(meta) < 2 or not parts[i].startswith(":") or not parts[i + 1]:
            raise coding_worker.CodingWorkerError(f"Could not read the list of changed files: {parts[i]!r}")
        entries.append((parts[i + 1], meta[1]))
    return entries


async def _is_ancestor(repo: Path, base: str, flags: tuple[str, ...]) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *flags,
        "merge-base",
        "--is-ancestor",
        base,
        "HEAD",
        cwd=repo,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode in (0, 1):
        return proc.returncode == 0
    raise coding_worker.CodingWorkerError(f"git merge-base failed: {scrub_text(err.decode().strip())}")


def _policy_rules(text: str | None) -> dict[str, str] | None:
    """{rule id: default_severity} from a policy.yaml text, or None if it is missing or unreadable."""
    if text is None:
        return None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    rules = data.get("rules") if isinstance(data, dict) else None
    if not isinstance(rules, list):
        return None
    return {
        str(r["id"]): str(r.get("default_severity")) for r in rules if isinstance(r, dict) and r.get("id") is not None
    }


async def _blob(repo: Path, rev: str, flags: tuple[str, ...]) -> str | None:
    try:
        return await _git(repo, "show", f"{rev}:{POLICY_PATH}", flags=flags)
    except coding_worker.CodingWorkerError:
        return None


async def _check_rules_kept(repo: Path, base: str, head: str, rules: list[Rule] | None, flags: tuple[str, ...]) -> None:
    """The patch must not delete policy.yaml, drop a user rule or change a severity the user set.

    "Broken" means a rule from the plan that is missing or has another default_severity. Only rules the patch
    newly breaks count, so a repo that already differed from the plan can still be fixed.
    """
    after_text = await _blob(repo, head, flags)
    if after_text is None:
        raise RulesWeakened("The fix deleted the agent's policy file, which holds the user's rules.")
    after = _policy_rules(after_text)
    if after is None:
        raise RulesWeakened("The fix left the agent's policy file unreadable.")
    before = _policy_rules(await _blob(repo, base, flags)) or {}
    for rule in rules or []:
        was_ok = before.get(rule.id) == rule.default_severity
        if was_ok and after.get(rule.id) != rule.default_severity:
            what = "removed" if rule.id not in after else "changed the severity of"
            raise RulesWeakened(f"The fix {what} the rule '{scrub_text(rule.id)}', which the user set.")


async def _revert(
    repo: Path, base: str, flags: tuple[str, ...], reason: str, cost: float, cause: Exception | None
) -> str:
    """`git revert` everything after `base` (history is kept: rollback = revert). Returns the new HEAD."""
    try:
        env = None if await _has_identity(repo, flags) else dict(FALLBACK_IDENTITY_ENV)
        await _git(repo, "revert", "--no-edit", f"{base}..HEAD", flags=flags, env=env)
    except coding_worker.CodingWorkerError as exc:
        try:
            await _git(repo, "revert", "--abort", flags=flags)
        except coding_worker.CodingWorkerError:
            pass
        raise FixRejected(
            f"{reason} I could not undo it automatically ({scrub_text(str(exc))}); the app needs a manual rollback.",
            cost_usd=cost,
            head_commit=None,
            cause=cause,
        ) from exc
    return (await _git(repo, "rev-parse", "HEAD", flags=flags)).strip()


# --- the instruction and the commit message -------------------------------------------------------------------


class _HeldoutText:
    """Removes anything that came from a held-out or acceptance scenario (id, title, diff lines) from our text.

    The Diagnostician is blind to the held-out set, so this should never find anything; it is the last wall
    before text reaches the coding worker, a commit message or an event.
    """

    def __init__(self, state: SusanooState) -> None:
        self._snippets: list[str] = []
        for sc in state.get("scenarios", []):
            if sc.split not in HIDDEN_SPLITS:
                continue
            for text in [sc.id, sc.title, *sc.diff.splitlines()]:
                text = text.strip()
                if len(text) >= (4 if text in (sc.id, sc.title) else _MIN_HELDOUT_SNIPPET):
                    self._snippets.append(text)
        self._snippets.sort(key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for snippet in self._snippets:
            if snippet in text:
                text = text.replace(snippet, "[held-out]")
        return text


def _clean_line(text: str, hide: _HeldoutText, limit: int | None = None) -> str:
    """One line of plain text: held-out parts and credentials removed, control characters dropped."""
    text = " ".join(_CONTROL.sub("", scrub_text(hide(str(text)))).split())
    return text if limit is None or len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _visible_scenarios(state: SusanooState, dx: Diagnosis) -> list[str]:
    hidden = {s.id for s in state.get("scenarios", []) if s.split in HIDDEN_SPLITS}
    return [sid for sid in dict.fromkeys(dx.scenario_ids) if sid not in hidden and not layer_allowlist.is_heldout(sid)]


def _latest_results(state: SusanooState) -> dict[str, ScenarioResult]:
    latest: dict[str, ScenarioResult] = {}
    for r in state.get("results", []):
        if r.scenario_id not in latest or r.iteration >= latest[r.scenario_id].iteration:
            latest[r.scenario_id] = r
    return latest


def _plural(n: int, word: str, plural: str | None = None) -> str:
    return f"{n} {word if n == 1 else (plural or word + 's')}"


def _what_went_wrong(r: ScenarioResult | None, sc: Scenario | None) -> str:
    """A short plain-English summary from counts only (never from agent output)."""
    if r is None:
        return "did not pass"
    if r.error:
        return "could not be checked"
    bits = []
    if r.missed:
        bits.append(f"missed {_plural(r.missed, 'expected problem')}")
    if r.false_positives:
        bits.append(f"raised {_plural(r.false_positives, 'false alarm')}")
    wrong_severity = max(r.matched - r.severity_correct, 0)
    if wrong_severity:
        bits.append(f"got {_plural(wrong_severity, 'severity', 'severities')} wrong")
    return ", ".join(bits) or "did not pass"


def _failure_line(state: SusanooState, dx: Diagnosis, hide: _HeldoutText) -> str:
    """'sim-train-01 (missed 1 expected problem); sim-train-02 (raised 1 false alarm)' for the commit message."""
    results = _latest_results(state)
    by_id = {s.id: s for s in state.get("scenarios", [])}
    ids = _visible_scenarios(state, dx)
    parts = [
        f"{_clean_line(sid, hide, 80)} ({_what_went_wrong(results.get(sid), by_id.get(sid))})"
        for sid in ids[:MAX_LISTED_SCENARIOS]
    ]
    if len(ids) > MAX_LISTED_SCENARIOS:
        parts.append(f"and {len(ids) - MAX_LISTED_SCENARIOS} more")
    return "; ".join(parts) or "scenarios that failed"


def _commit_message(state: SusanooState, dx: Diagnosis, editable: list[str], hide: _HeldoutText) -> str:
    """Carries the failure, the diagnosis and the rationale; Entire records the session against this commit."""
    ids = _visible_scenarios(state, dx)
    if not ids:
        subject = f"fix the cause of {_plural(len(dx.scenario_ids), 'failing scenario')}"
    elif len(ids) == 1:
        subject = f"fix the cause of {_clean_line(ids[0], hide, 40)}"
    else:
        subject = f"fix the cause behind {_clean_line(ids[0], hide, 40)} and {len(ids) - 1} more"
    hypothesis = _clean_line(dx.hypothesis, hide, MAX_HYPOTHESIS_CHARS)
    where = ", ".join(_clean_line(p, hide, 80) for p in editable[:6]) + (" and more" if len(editable) > 6 else "")
    return (
        f"fix({dx.layer}): {subject}\n\n"
        f"Failure: {_failure_line(state, dx, hide)}.\n"
        f"Diagnosis ({dx.layer} layer, {dx.id}): {hypothesis}\n"
        f"Rationale: {RATIONALE[dx.layer]} Files this fix may change: {where}.\n"
    )


def _instruction(state: SusanooState, dx: Diagnosis, editable: list[str], hide: _HeldoutText) -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8")
    template = re.sub(r"<!--.*?-->\s*", "", template, count=1, flags=re.DOTALL)
    targets = "\n".join(f"- {_clean_line(p, hide, 120)}" for p in dx.target_files) or "- (none named)"
    values = {
        # Untrusted text sits inside tags: break any closing tag so it cannot end the block early.
        "layer": dx.layer,
        "hypothesis": _escape_tags(_clean_line(dx.hypothesis, hide, 2000)),
        "target_files": _escape_tags(targets),
        "failures": _escape_tags(_failures_text(state, dx, hide)),
        "rules": _escape_tags(_rules_text(state, hide)),
        "editable": "\n".join(f"- {p}" for p in editable),
    }
    # One pass, so text inside a value is never substituted again.
    return re.sub(r"\{\{(\w+)\}\}", lambda m: values.get(m.group(1), m.group(0)), template).strip()


def _escape_tags(text: str) -> str:
    return text.replace("</", "<\\/")


def _rules_text(state: SusanooState, hide: _HeldoutText) -> str:
    plan = state.get("plan")
    if plan is None or not plan.rules:
        return "(no rules were given)"
    return "\n".join(
        f"- {_clean_line(r.id, hide, 80)} ({r.default_severity}): {_clean_line(r.text, hide, 300)}" for r in plan.rules
    )


def _failures_text(state: SusanooState, dx: Diagnosis, hide: _HeldoutText) -> str:
    """Per failing scenario: what happened, what the agent reported and what the answer key expected."""
    results = _latest_results(state)
    by_id = {s.id: s for s in state.get("scenarios", [])}
    blocks = []
    for sid in _visible_scenarios(state, dx)[:MAX_LISTED_SCENARIOS]:
        sc, r = by_id.get(sid), results.get(sid)
        head = f"Scenario {_clean_line(sid, hide, 80)}"
        if sc is not None:
            head += f" ({_clean_line(sc.title, hide, 160)})"
        lines = [f"{head}: {_what_went_wrong(r, sc)}."]
        if r is not None:
            if r.error:
                lines.append(f"  The check ended with an error: {_clean_line(r.error, hide, 300)}")
            for f in r.findings[:MAX_FINDINGS_PER_SCENARIO]:
                lines.append(
                    f"  Agent reported: {_clean_line(f.file, hide, 80)}:{f.line} {_clean_line(f.rule_id, hide, 60)} "
                    f"[{f.severity}] {_clean_line(f.message, hide, 200)}"
                )
        if sc is not None:
            for e in sc.answer_key.expected:
                lines.append(
                    f"  Expected: {_clean_line(e.file, hide, 80)}:{e.line_start}-{e.line_end} "
                    f"{_clean_line(e.rule_id, hide, 60)} [{e.severity}]"
                )
            if sc.answer_key.must_not_flag:
                lines.append(
                    "  Must not flag: " + ", ".join(_clean_line(x, hide, 60) for x in sc.answer_key.must_not_flag)
                )
            if not sc.answer_key.expected:
                lines.append("  Expected: nothing (a clean change).")
        blocks.append("\n".join(lines))
    return "\n".join(blocks) or "(the failing scenarios are not listed)"


def _worker_budget(state: SusanooState) -> float:
    """min(worker default, what is left of the run's cap)."""
    remaining = budget.cap_for(state) - float(state.get("cost_usd", 0.0))
    return round(max(0.0001, min(coding_worker.DEFAULT_MAX_BUDGET_USD, remaining)), 4)


async def _has_identity(repo: Path, flags: tuple[str, ...]) -> bool:
    """True if git can name a committer for `repo` from its config (repo, global or system level)."""
    for key in ("user.name", "user.email"):
        try:
            if not (await _git(repo, "config", "--get", key, flags=flags)).strip():
                return False
        except coding_worker.CodingWorkerError:  # `git config --get` exits 1 when the key is not set
            return False
    return True


async def _git(repo: Path, *args: str, flags: tuple[str, ...] = (), env: dict[str, str] | None = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *flags,
        *args,
        cwd=repo,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, **env} if env else None,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise coding_worker.CodingWorkerError(f"git {args[0]} failed: {scrub_text(err.decode().strip())}")
    return out.decode()
