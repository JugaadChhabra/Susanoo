"""Verifier head (code, no model). [PRD › The eight heads of Orochi] (head 8);
[PRD › Improvement loop and guardrails]; [PRD › Testing environments]; IMPLEMENTATION_MUAAZ P4.3.

Judges the Fixer's latest patch by re-running every scenario (practice, hidden and approved examples) and keeps
the patch only if ALL of these hold:

  (a) every scenario the diagnosis cited now passes;
  (b) nothing regressed: no practice or approved-example scenario that passed before the patch fails after it
      (compared with the results for the agent before the patch, at the SAME level as the re-run);
  (c) the hidden-check (held-out) F1 did not drop. Equal is fine ("doesn't drop").

Otherwise the patch commit is undone with `git revert` (never a history rewrite) and the verdict is "rolled_back".

Which level checks the patch (orchestrator decision): the Runner's Level 1 uses Susanoo-side copies of the app's
tools and output filter, so it cannot see a change to the generated repo's `agent/tools.py` or `backend/`.
So a `prompt_policy` or `test` patch is checked at Level 1, and a `code` or `tool` patch at Level 2 (the real
app in its sandbox container). If Level 2 cannot run (no app key, no Docker, build failure), the patch cannot
be verified, so it is rolled back with a plain-English reason. An unverified patch is never kept.

Same-level comparison: a Level 2 "after" is compared with a Level 2 "before", never with the Runner's Level 1
baseline. The "before" is the app at the patch's parent commit, run from a temporary `git worktree` of the
generated repo (removed afterwards). It is cached in the graph-internal state key `level2_baseline`
({"commit": sha, "results": [...]}), keyed by the commit it describes, so a later round on the same commit
(a kept patch, or the revert commit of a rolled-back one) does not pay for it twice. `prompt_policy` and `test`
patches compare Level 1 with Level 1 (the Runner's results in state).

Test-layer patches change scenario files in the generated repo. The diagnosed TRAIN scenarios (never held-out or
approved ones) are reloaded from `scenarios/train/<id>.json` and re-run in their edited form. A kept patch
returns the updated `scenarios` list; a rolled-back one returns the originals.

A patch the Fixer wrapper already marked "rolled_back" (the Fixer refused, or rejected and reverted its own
change) is not re-run or reverted: only the iteration bookkeeping happens (iteration + 1, the previous scores
repeated), with the reason read from the graph-internal key `fix_rejection_reason`.

Git identity: `git revert` makes a commit. If the generated repo has no `user.name` / `user.email`, the revert
runs with GIT_AUTHOR_* / GIT_COMMITTER_* set to "Susanoo" <susanoo@users.noreply.github.com> in its environment.
No git config is ever written, in any repo.

Iteration bookkeeping (graph/state.py): every Fixer -> Verifier cycle is one iteration; exactly one
`IterationMetrics` is recorded for it. `state["metrics"]` is the LEVEL 1 headline curve only: Level 2 numbers are
never appended to it; they live in the keep decision, the event payload and the `level2_baseline` cache.
  * kept prompt_policy/test patch: the Level 1 re-run's scores and results (stamped with the new iteration);
  * kept code/tool patch (judged at Level 2): a Level 1 re-run of all splits supplies the metrics and results
    (its cost is added), so the curve stays comparable;
  * rolled-back (or Fixer-refused) patch: the restored agent's own results, re-stamped to the new iteration, and
    the metrics computed from them. The revert brings that agent back, so its scores carry over, and the
    Diagnostician still sees what is failing (only results of `state["iteration"]` count).
The Level 1 "before" is computed from the Runner's results of the current iteration, never from `metrics[-1]`.
A scenario that passed before but has no result after counts as a regression.
The stop conditions (5 iterations, 2 rounds without improvement, the cost cap) are decided by
`graph.routing.stop_reason`; this head only reports them. When one holds, a "progress" event gives a
plain-English partial report of the scores and of every patch's verdict.

Fail closed: if anything unexpected goes wrong while checking (not just a Runner or sandbox error), the patch is
still reverted and marked rolled_back ("it couldn't be checked: ..."), an error event is emitted, and the
exception is re-raised only if the revert itself failed. Exception text put in events goes through
`tracing.scrub_text`. The Level 2 run of the pre-patch app uses a distinct run id, so its image tag and
container name differ from the post-patch run's.

Budget: `budget.check` runs before each re-run. If the cap is hit, the unverified patch is reverted and the head
returns a clean stop (`stop_reason` "budget") instead of raising, so the repo, the patch verdict and the state
stay consistent. The graph's own BudgetExceeded wrapper would otherwise leave the patch applied and "pending".

GUARDRAIL [PRD › Improvement loop and guardrails]: nothing emitted here names, quotes or diffs a held-out
scenario. Practice (train) scenarios are named by id; approved examples and hidden checks are only counted, and
the hidden checks appear only as an F1 number.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from susanoo.emit import emit
from susanoo.graph.routing import budget_stop_status, stop_reason
from susanoo.guards import budget
from susanoo.heads import runner
from susanoo.integrations import tracing
from susanoo.integrations.tracing import scrub_text
from susanoo.sandbox.docker import SandboxError
from susanoo.schemas import Diagnosis, IterationMetrics, Patch, Scenario, ScenarioResult
from susanoo.schemas.state import SusanooState
from susanoo.scoring import compute_metrics

log = logging.getLogger(__name__)

HEAD = "verifier"
GIT_TIMEOUT_S = 60
MAX_REASON_CHARS = 240
MAX_LISTED_IDS = 5

LEVEL2_LAYERS = frozenset({"code", "tool"})  # layers Level 1 cannot observe
_SHA = re.compile(r"^[0-9a-fA-F]{7,64}$")
_SAFE_SCENARIO_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,100}$")
GIT_IDENTITY = ("Susanoo", "susanoo@users.noreply.github.com")
DEFAULT_REFUSAL = "that kind of fix may not change what it tried to change"

STOP_TEXT = {
    "iteration_cap": "the round limit was reached",
    "no_gain": "two rounds in a row did not improve the hidden-check score",
    "budget": "the run reached its cost limit",
}


class VerifierError(Exception):
    """The patch could not be judged or undone; the message is plain English."""


@dataclass
class _Judgement:
    kept: bool
    reason: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    results: list[ScenarioResult] | None = None  # kept: Level 1 results to put in state (new iteration)
    metrics: IterationMetrics | None = None  # kept: Level 1 metrics for the curve
    level_results: list[ScenarioResult] | None = None  # the results the keep rule judged (at the patch's level)


def _clip(text: str, limit: int = MAX_REASON_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _ids(ids: Sequence[str]) -> str:
    shown = ", ".join(ids[:MAX_LISTED_IDS])
    return shown + (f" and {len(ids) - MAX_LISTED_IDS} more" if len(ids) > MAX_LISTED_IDS else "")


# ------------------------------------------------------------------------------------------------ git


def _git(repo: str | Path, *args: str, env: Mapping[str, str] | None = None) -> str:
    try:
        done = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
            env=None if env is None else {**os.environ, **env},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VerifierError(f"Could not run git in the app's repository ({type(exc).__name__}).") from exc
    if done.returncode != 0:
        raise VerifierError(f"git {args[0]} failed: {_clip(done.stderr or done.stdout, 200)}")
    return done.stdout.strip()


def _identity_env(repo: str | Path) -> dict[str, str]:
    """Author/committer variables for the parts of the identity this repo has not configured (never writes config)."""
    name, email = GIT_IDENTITY
    env: dict[str, str] = {}
    for key, value, var in (("user.name", name, "NAME"), ("user.email", email, "EMAIL")):
        try:
            configured = _git(repo, "config", "--get", key)
        except VerifierError:
            configured = ""
        if not configured:
            env[f"GIT_AUTHOR_{var}"] = value
            env[f"GIT_COMMITTER_{var}"] = value
    return env


def _check_sha(commit_sha: str) -> None:
    if not _SHA.match(commit_sha):
        raise VerifierError("The patch has no valid commit to undo, so it could not be rolled back.")


def _revert_sync(repo: str | Path, commit_sha: str) -> str:
    """`git revert --no-edit <sha>` and return the new HEAD. Aborts a half-done revert so the repo stays usable."""
    _check_sha(commit_sha)
    env = _identity_env(repo)
    try:
        _git(repo, "revert", "--no-edit", commit_sha, env=env)
    except VerifierError:
        try:
            _git(repo, "revert", "--abort")
        except VerifierError:
            pass  # nothing to abort when the revert never started
        raise
    return _git(repo, "rev-parse", "HEAD")


async def _revert(repo: str | Path, commit_sha: str) -> str:
    return await asyncio.to_thread(_revert_sync, repo, commit_sha)


def _parent_commit_sync(repo: str | Path, commit_sha: str) -> str:
    _check_sha(commit_sha)
    return _git(repo, "rev-parse", f"{commit_sha}^")


def _add_worktree_sync(repo: str | Path, rev: str) -> Path:
    """A detached checkout of `rev` in a temporary folder; the caller must call `_remove_worktree_sync`."""
    tmp = Path(tempfile.mkdtemp(prefix="susanoo-verify-"))
    tree = tmp / "repo"
    try:
        _git(repo, "worktree", "add", "--detach", str(tree), rev)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return tree


def _remove_worktree_sync(repo: str | Path, tree: Path) -> None:
    try:
        try:
            _git(repo, "worktree", "remove", "--force", str(tree))
        except VerifierError:
            log.warning("Could not remove the temporary checkout %s; pruning it instead.", tree)
            shutil.rmtree(tree, ignore_errors=True)
            _git(repo, "worktree", "prune")
    finally:
        shutil.rmtree(tree.parent, ignore_errors=True)


def _reload_train_sync(repo: str | Path, active: Sequence[Scenario], dx: Diagnosis) -> list[Scenario]:
    """`active` with the diagnosed TRAIN scenarios re-read from `scenarios/train/<id>.json`.

    Held-out and approved (acceptance) scenarios are never read from disk.
    """
    folder = (Path(repo) / "scenarios" / "train").resolve()
    reloaded: dict[str, Scenario] = {}
    by_id = {s.id: s for s in active}
    for sid in dict.fromkeys(dx.scenario_ids):
        current = by_id.get(sid)
        if current is None or current.split != "train" or current.approved_by_user:
            continue
        if not _SAFE_SCENARIO_ID.match(sid):
            continue
        path = (folder / f"{sid}.json").resolve()
        if path.parent != folder:
            raise VerifierError(f"the edited scenario {sid} is not inside the scenarios folder")
        try:
            loaded = Scenario.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:  # pydantic's ValidationError is a ValueError
            raise VerifierError(f"the edited scenario {sid} could not be read ({type(exc).__name__})") from exc
        if loaded.id != sid or loaded.split != "train":
            raise VerifierError(f"the edited scenario {sid} no longer has its own id or practice status")
        reloaded[sid] = loaded
    return [reloaded.get(s.id, s) for s in active]


def _latest_by_id(results: Sequence[ScenarioResult]) -> dict[str, ScenarioResult]:
    latest: dict[str, ScenarioResult] = {}
    for r in results:
        if r.scenario_id not in latest or r.iteration >= latest[r.scenario_id].iteration:
            latest[r.scenario_id] = r
    return latest


def _current_results(state: Mapping[str, Any], scenarios: Sequence[Scenario]) -> list[ScenarioResult]:
    """The Runner's results for the agent as it is now: those of the current iteration, for known scenarios."""
    known = {s.id for s in scenarios}
    mine = [r for r in state.get("results", []) if r.scenario_id in known]
    current = [r for r in mine if r.iteration == state.get("iteration", 0)]
    return current or list(_latest_by_id(mine).values())


def _restored(
    state: Mapping[str, Any], active: Sequence[Scenario], iteration: int, cost: float
) -> tuple[list[ScenarioResult], IterationMetrics]:
    """The restored (pre-patch) agent's results re-stamped to `iteration`, and the Level 1 metrics from them.

    A revert (or a refused fix) brings back the agent whose results are already in state, so its scores carry over;
    re-stamping keeps "only results of state['iteration'] count" true, so the Diagnostician still sees what fails.
    """
    results = [r.model_copy(update={"iteration": iteration}) for r in _current_results(state, active)]
    if results:
        return results, compute_metrics(results, list(active), iteration=iteration, cost_usd=cost)
    previous = list(state.get("metrics", []))
    if previous:
        return [], max(previous, key=lambda m: m.iteration).model_copy(
            update={"iteration": iteration, "cost_usd": cost}
        )
    return [], compute_metrics([], list(active), iteration=iteration, cost_usd=cost)


def _judge(
    scenarios: Sequence[Scenario],
    dx: Diagnosis,
    results: list[ScenarioResult],
    *,
    before_results: Sequence[ScenarioResult],
    before_metrics: IterationMetrics | None,
    disputed: set[str],
    iteration: int,
    cost: float,
) -> _Judgement:
    """Apply the keep rule (a), (b), (c): `results` (after) against `before_results`, both at the same level."""
    split_of = {s.id: s.split for s in scenarios}
    after = _latest_by_id(results)
    before = _latest_by_id([r for r in before_results if r.scenario_id in split_of])

    # (a) every scenario the diagnosis cited now passes
    targets = [i for i in dx.scenario_ids if i not in disputed]
    still_failing = [i for i in targets if i not in after or not after[i].passed]
    fixed_targets = bool(targets) and not still_failing

    # (b) nothing that passed before fails now (practice and approved examples)
    regressed = sorted(
        i
        for i, was in before.items()
        if was.passed
        and split_of[i] in ("train", "acceptance")
        and (i not in after or not after[i].passed)  # no result after counts as a failure
    )
    no_regressions = not regressed

    # (c) hidden-check F1 does not drop
    new_metrics = compute_metrics(results, list(scenarios), iteration=iteration, cost_usd=cost)
    heldout_before = before_metrics.heldout_f1 if before_metrics is not None else 0.0
    heldout_after = new_metrics.heldout_f1
    heldout_ok = heldout_after >= heldout_before - 1e-12

    problems: list[str] = []
    if not fixed_targets:
        if not targets:
            problems.append("there was no scenario to check the fix against")
        else:
            train = [i for i in still_failing if split_of.get(i) == "train"]
            others = len(still_failing) - len(train)
            bits = ([f"practice scenarios {_ids(train)}"] if train else []) + (
                [_plural(others, "other check")] if others else []
            )
            problems.append("the problem it was meant to fix is still there (" + " and ".join(bits) + ")")
    if not no_regressions:
        train = [i for i in regressed if split_of[i] == "train"]
        accepted = len(regressed) - len(train)
        bits = ([f"practice scenarios {_ids(train)}"] if train else []) + (
            [_plural(accepted, "approved example")] if accepted else []
        )
        problems.append("it broke something that worked before (" + " and ".join(bits) + ")")
    if not heldout_ok:
        problems.append(f"the hidden-check score dropped from {heldout_before:.2f} to {heldout_after:.2f}")

    payload = {
        "checks": {"fixed_targets": fixed_targets, "no_regressions": no_regressions, "heldout_not_lower": heldout_ok},
        "still_failing": [i for i in still_failing if split_of.get(i) == "train"],
        "regressed": [i for i in regressed if split_of[i] == "train"],
        "heldout_f1_before": round(heldout_before, 4),
        "heldout_f1_after": round(heldout_after, 4),
    }
    kept = fixed_targets and no_regressions and heldout_ok
    return _Judgement(
        kept,
        "; ".join(problems),
        payload,
        results=results,
        metrics=new_metrics if kept else None,
        level_results=results,
    )


def _report(patches: Sequence[Patch], metrics: Sequence[IterationMetrics], reason: str | None) -> str:
    """Plain-English partial report: hidden-check score per round and each patch's verdict."""
    words = {"kept": "kept", "rolled_back": "rolled back", "pending": "not judged", "awaiting_approval": "waiting"}
    scores = ", ".join(
        f"{'start' if m.iteration == 0 else f'round {m.iteration}'}: {m.heldout_f1:.2f}"
        for m in sorted(metrics, key=lambda m: m.iteration)
    )
    fixes = ", ".join(f"fix {i + 1} {words[p.verdict]}" for i, p in enumerate(patches))
    why = f"Stopping because {STOP_TEXT.get(reason or '', 'a stop condition was reached')}. "
    return f"{why}Hidden-check score by round ({scores or 'none yet'}). Fixes: {fixes or 'none'}."


# ------------------------------------------------------------------------------------------------ the node


def _safe(exc: BaseException) -> str:
    """Exception text that is safe to put in an event: credentials scrubbed, whitespace collapsed, clipped."""
    return _clip(scrub_text(str(exc)))


def _stop_text(
    state: Mapping[str, Any], update: dict[str, Any], budget_stop: bool
) -> tuple[str | None, dict[str, Any]]:
    merged = {**state, **update}
    reason = "budget" if budget_stop else stop_reason(merged)
    extra: dict[str, Any] = {}
    if budget_stop:
        extra = {"stop_reason": "budget", "status": budget_stop_status(merged)}
    return reason, extra


def _with_metrics(state: Mapping[str, Any], new: IterationMetrics) -> list[IterationMetrics]:
    """The state's metrics with `new` in place of any entry for the same iteration (so: once per iteration)."""
    return [m for m in state.get("metrics", []) if m.iteration != new.iteration] + [new]


async def _measure(
    state: SusanooState,
    active: list[Scenario],
    after_scenarios: list[Scenario],
    dx: Diagnosis,
    patch: Patch,
    iteration: int,
    cost: float,
) -> tuple[_Judgement, float, int, list[ScenarioResult] | None]:
    """Re-run at the layer's level and judge against a same-level "before".

    Returns (judgement, model cost in USD, level, Level 2 baseline results when one was measured). When the patch
    is kept, `judgement.results` / `.metrics` are always Level 1 (the headline curve is Level 1 only).
    """
    budget.check(state)
    disputed = set(state.get("disputed_scenario_ids", []))  # type: ignore[call-overload]
    if dx.layer not in LEVEL2_LAYERS:
        results, spent = await runner.run_level1(state, after_scenarios, iteration=iteration)
        before = _current_results(state, active)
        before_metrics = (
            compute_metrics(before, active, iteration=state.get("iteration", 0), cost_usd=cost) if before else None
        )
        judgement = _judge(
            after_scenarios,
            dx,
            results,
            before_results=before,
            before_metrics=before_metrics,
            disputed=disputed,
            iteration=iteration,
            cost=round(cost + spent, 6),
        )
        return judgement, spent, 1, None

    baseline = await _level2_baseline(state, active, patch)
    results = await runner.run_level2(state, after_scenarios, iteration=iteration)
    judgement = _judge(
        after_scenarios,
        dx,
        results,
        before_results=baseline,
        before_metrics=compute_metrics(baseline, active, iteration=state.get("iteration", 0), cost_usd=cost),
        disputed=disputed,
        iteration=iteration,
        cost=cost,
    )
    spent = 0.0
    if judgement.kept:  # Level 2 said yes: record this round's scores from a Level 1 rerun, so the curve is comparable
        budget.check({**state, "cost_usd": cost})
        l1_results, spent = await runner.run_level1(state, after_scenarios, iteration=iteration)
        judgement.results = l1_results
        judgement.metrics = compute_metrics(
            l1_results, after_scenarios, iteration=iteration, cost_usd=round(cost + spent, 6)
        )
    return judgement, spent, 2, baseline


def _baseline_run_id(run_id: str) -> str:
    """A distinct run id (so image tag and container name differ) for the Level 2 run of the pre-patch app."""
    candidate = f"{run_id[:30].rstrip('-')}-before"
    return candidate if candidate != run_id else f"{candidate}-2"


async def _level2_baseline(state: SusanooState, active: list[Scenario], patch: Patch) -> list[ScenarioResult]:
    """Level 2 results for the app as it was before the patch (cached by commit in `level2_baseline`)."""
    repo = state["repo_path"]
    parent = await asyncio.to_thread(_parent_commit_sync, repo, patch.commit_sha)
    known = {s.id for s in active}
    cached = state.get("level2_baseline")  # type: ignore[typeddict-item]  # graph-internal key
    if isinstance(cached, dict) and cached.get("commit") == parent:
        have = [r for r in cached.get("results", []) if r.scenario_id in known]
        if {r.scenario_id for r in have} == known:
            return have
    tree = await asyncio.to_thread(_add_worktree_sync, repo, parent)
    try:
        run_id = state["run_id"]
        before_state = {**state, "repo_path": str(tree), "run_id": _baseline_run_id(run_id)}

        async def connections(_baseline_id: str) -> dict[str, str]:  # the real run's, not the baseline id's
            from susanoo.connectors import runtime_env  # lazy: needs the db

            return await runtime_env.run_connection_env(run_id)

        return await runner.run_level2(
            before_state, active, iteration=state.get("iteration", 0), connection_env=connections
        )
    finally:
        await asyncio.to_thread(_remove_worktree_sync, repo, tree)


async def run(state: SusanooState) -> dict:
    """Judge `patches[-1]`: keep it, or `git revert` it. Returns patches, iteration, metrics, cost, head_commit."""
    with tracing.span(HEAD, run_id=state["run_id"]):
        await emit(state, HEAD, "started", "Re-running the scenarios to check the fix…")
        try:
            return await _verify(state)
        except Exception as exc:
            if not isinstance(exc, VerifierError):
                log.exception("verifier failed")
            await emit(state, HEAD, "error", f"The fix could not be checked: {_safe(exc)}")
            raise


async def _already_refused(state: SusanooState, patch: Patch, active: list[Scenario]) -> dict:
    """The Fixer wrapper already rolled this patch back: no re-run, no revert, only the round's bookkeeping."""
    iteration = state.get("iteration", 0) + 1
    cost = float(state.get("cost_usd", 0.0))
    results, new = _restored(state, active, iteration, cost)
    metrics = _with_metrics(state, new)
    update: dict[str, Any] = {"iteration": iteration, "metrics": metrics}
    if results:
        update["results"] = results
    raw = state.get("fix_rejection_reason")  # type: ignore[typeddict-item]  # graph-internal key
    reason = _clip(scrub_text(raw) if isinstance(raw, str) and raw.strip() else DEFAULT_REFUSAL).rstrip(".")
    await emit(state, HEAD, "metrics", f"Score after round {iteration}.", new.model_dump())
    await emit(
        state,
        HEAD,
        "finished",
        f"The fix was refused before it could be checked (round {iteration}): {reason}.",
        {"verdict": "rolled_back", "patch_id": patch.id, "refused": True},
    )
    stop, _ = _stop_text(state, update, False)
    if stop is not None:
        await emit(
            state, HEAD, "progress", _report(list(state.get("patches", [])), metrics, stop), {"stop_reason": stop}
        )
    return update


async def _verify(state: SusanooState) -> dict:
    patches: list[Patch] = list(state.get("patches", []))
    if not patches:
        raise VerifierError("There is no fix to check.")
    patch = patches[-1]
    disputed = set(state.get("disputed_scenario_ids", []))  # type: ignore[call-overload]
    active = [s for s in state.get("scenarios", []) if s.id not in disputed]
    if patch.verdict == "rolled_back":
        return await _already_refused(state, patch, active)
    if patch.verdict != "pending":
        raise VerifierError(f"The latest fix was already judged ({patch.verdict}).")
    dx = next((d for d in state.get("failure_ledger", []) if d.id == patch.diagnosis_id), None)
    if dx is None:
        raise VerifierError("The latest fix has no diagnosis on record, so it cannot be checked.")

    iteration = state.get("iteration", 0) + 1
    cost = float(state.get("cost_usd", 0.0))
    repo = state["repo_path"]
    level = 2 if dx.layer in LEVEL2_LAYERS else 1
    stopped_by_budget = False
    unexpected: Exception | None = None
    after_scenarios = active
    baseline: list[ScenarioResult] | None = None

    try:
        if dx.layer == "test":
            after_scenarios = await asyncio.to_thread(_reload_train_sync, repo, active, dx)
        judgement, spent, level, baseline = await _measure(state, active, after_scenarios, dx, patch, iteration, cost)
        cost = round(cost + spent, 6)
    except budget.BudgetExceeded as exc:
        stopped_by_budget = True
        cost = max(cost, float(exc.cost_usd))
        judgement = _Judgement(False, "the run reached its cost limit before the fix could be checked")
    except (runner.RunnerError, SandboxError, VerifierError) as exc:
        judgement = _Judgement(False, f"it couldn't be checked: {_safe(exc)}", {"unverified": True})
    except Exception as exc:  # anything unexpected: the patch must still not stay applied and pending
        log.exception("verifier: unexpected failure while checking a fix")
        unexpected = exc
        judgement = _Judgement(False, f"it couldn't be checked: {_safe(exc)}", {"unverified": True})

    update: dict[str, Any] = {"iteration": iteration}
    if judgement.kept:
        verdict = "kept"
        new_metrics = judgement.metrics
        if judgement.results is not None:
            update["results"] = judgement.results  # Level 1, stamped with the new iteration
        if dx.layer == "test":
            reloaded = {s.id: s for s in after_scenarios}
            update["scenarios"] = [reloaded.get(s.id, s) for s in state.get("scenarios", [])]
        if level == 2 and baseline is not None:
            update["level2_baseline"] = {"commit": patch.commit_sha, "results": judgement.level_results}
    else:
        verdict = "rolled_back"
        head_commit = await _revert(repo, patch.commit_sha)  # if this fails the error propagates (loudly)
        update["head_commit"] = head_commit
        restored, new_metrics = _restored(state, active, iteration, cost)
        if restored:
            update["results"] = restored
        if dx.layer == "test":
            update["scenarios"] = list(state.get("scenarios", []))  # the originals
        if baseline is not None:  # the restored tree is the baseline's tree
            update["level2_baseline"] = {"commit": head_commit, "results": baseline}
    assert new_metrics is not None
    new_metrics = new_metrics.model_copy(update={"iteration": iteration, "cost_usd": cost})

    patches[-1] = patch.model_copy(update={"verdict": verdict})
    metrics = _with_metrics(state, new_metrics)
    update.update({"patches": patches, "metrics": metrics, "cost_usd": cost})

    if unexpected is not None:
        await emit(state, HEAD, "error", f"The fix could not be checked: {_safe(unexpected)}")
    await emit(state, HEAD, "metrics", f"Score after round {iteration}.", new_metrics.model_dump())
    payload = {"verdict": verdict, "patch_id": patch.id, "level": level, **judgement.payload}
    if judgement.kept:
        message = f"The fix helped, so I kept it (round {iteration})."
    else:
        message = f"The fix did not help, so I rolled it back (round {iteration}): {judgement.reason.rstrip('.')}."
    await emit(state, HEAD, "finished", message, payload)

    reason, extra = _stop_text(state, update, stopped_by_budget)
    update.update(extra)
    if reason is not None:
        await emit(state, HEAD, "progress", _report(patches, metrics, reason), {"stop_reason": reason})
    return update


__all__ = ["HEAD", "VerifierError", "run"]
