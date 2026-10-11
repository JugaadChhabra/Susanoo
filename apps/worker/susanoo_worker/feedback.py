"""Feed the user's feedback scenarios into the loop. [PRD > Non-technical users] ("it should have..." -> a new test)

The API saves a thumbs-down as a `train` row in `public.scenarios` (approved_by_user false) and never touches the
agent. This module is the other half: the graph's Runner node is wrapped so that, right before it scores a round,
it loads those rows and adds them to the run, once.

What "add" means, per row, in order:
  1. skip it if its id is already in `state["scenarios"]` (added earlier; this is what makes it idempotent) or an
     earlier pass already refused it (a `feedback_scenario_id` event of this run; so a refusal is announced once);
  2. validate it with the Simulator's own checks against this run's plan (rule ids, the lines it points at, a
     plain-English title), and force split="train" and approved_by_user=False;
  3. write `scenarios/train/<id>.json` and `<id>.world.json` into the generated repo (world rebuilt from the diff
     alone) and commit them; the files of other scenarios are not touched.
The valid ones are returned as a state update (`scenarios`, `head_commit`) merged into the Runner's own update, so
the Diagnostician, Fixer and Verifier see them from this round on, exactly like any other practice scenario.

Why the Runner and not `aupdate_state`: the Runner starts every round (after the Simulator, after a kept patch,
after a rejected gate), so no patch is half-judged and no pause is pending. `aupdate_state` would write a new
checkpoint "as" some node and re-derive the next step from it, which would drop a pending HumanGate interrupt.

Reach:
  - a run that is still going picks feedback up at its next Runner pass (a run paused for approval: after the answer);
  - a run that has finished is never reopened or rerun. Its feedback stays in `public.scenarios` and is picked up by
    the next run of the same project (the query is by project, not by run), whose first Runner pass loads it. A
    scenario written for rules the new plan no longer has is skipped with a plain-English event.
  - split heldout / acceptance rows are never loaded (the query filters them out and the split is forced anyway).
Scores: a new scenario changes what the next round's recall/precision are computed over. That is the point of the
feature, and the scoring rule itself is untouched.
"""

from __future__ import annotations

import dataclasses
import logging
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from susanoo.db import emit_event, session
from susanoo.graph import Heads
from susanoo.heads import simulator
from susanoo.schemas import Scenario

log = logging.getLogger("susanoo_worker.feedback")

ADDED_ONE = "Added 1 new check from your feedback; it will be used from the next round."
ADDED_MANY = "Added {n} new checks from your feedback; they will be used from the next round."
SKIPPED = "One check from your feedback could not be used, so it was left out ({reason})."
NOT_ADDED = "Your feedback could not be added as a check this time. The run carries on without it."

_PENDING = """
select s.id::text as id, s.kind, s.title, s.diff, s.answer_key
  from public.scenarios s
  join public.runs r on r.id = s.run_id
 where r.project_id = (select project_id from public.runs where id = %(run)s)
   and s.split = 'train' and not s.approved_by_user
 order by s.id
"""
_SEEN = """
select payload from public.run_events
 where run_id = %(run)s
   and (payload ? 'feedback_scenario_id' or payload ? 'feedback_scenario_ids')
"""

Node = Callable[[dict], Awaitable[dict]]


async def _pending_rows(run_id: str) -> list[dict[str, Any]]:
    async with session() as conn:
        cur = await conn.execute(_PENDING, {"run": run_id})
        return list(await cur.fetchall())


async def _announced(run_id: str) -> tuple[set[str], set[str]]:
    """(ids this run already announced as added, ids it already announced as refused)."""
    async with session() as conn:
        cur = await conn.execute(_SEEN, {"run": run_id})
        rows = await cur.fetchall()
    added: set[str] = set()
    refused: set[str] = set()
    for row in rows:
        payload = row["payload"] or {}
        added.update(payload.get("feedback_scenario_ids") or [])
        if payload.get("feedback_scenario_id"):
            refused.add(payload["feedback_scenario_id"])
    return added, refused


def _raw(row: dict[str, Any]) -> dict[str, Any]:
    """A table row in the shape `simulator.validate_scenario` takes."""
    key = row["answer_key"] or {}
    return {
        "id": row["id"],
        "kind": row["kind"],
        "title": row["title"],
        "diff": row["diff"],
        "expected": key.get("expected") or [],
        "must_not_flag": key.get("must_not_flag") or [],
    }


def _write_into_repo(repo: Path, scenarios: list[Scenario]) -> None:
    """Add scenarios/train/<id>.json and .world.json to the repo without touching any other scenario file.

    `simulator.write_scenarios` empties the split folders first, so it writes into a scratch folder and the new
    files are copied across.
    """
    worlds = {s.id: simulator.world_from_diff(s.diff) for s in scenarios}
    with tempfile.TemporaryDirectory() as tmp:
        simulator.write_scenarios(Path(tmp), scenarios, worlds)
        target = repo / simulator.SCENARIOS_DIR / "train"
        target.mkdir(parents=True, exist_ok=True)
        for sc in scenarios:
            for name in (f"{sc.id}.json", f"{sc.id}.world.json"):
                shutil.copyfile(Path(tmp) / simulator.SCENARIOS_DIR / "train" / name, target / name)


async def feedback_update(state: dict) -> dict:
    """The state update that adds this run's not-yet-added feedback scenarios, or {} when there is nothing to add."""
    plan = state.get("plan")
    if plan is None:
        return {}
    run_id = state["run_id"]
    rows = await _pending_rows(run_id)
    have = {s.id for s in state.get("scenarios", [])}
    rows = [r for r in rows if r["id"] not in have]
    if not rows:
        return {}

    announced_added, announced_refused = await _announced(run_id)
    rule_ids = {r.id for r in plan.rules}
    repo = Path(state.get("repo_path") or "")
    taken = set(have)
    good: list[Scenario] = []
    for row in rows:
        try:
            scenario, _ = simulator.validate_scenario(_raw(row), rule_ids, taken, world_required=False)
        except simulator.ScenarioDrop as drop:
            log.warning(
                "feedback scenario %s skipped for run %s: %s (%s)", row["id"], run_id, drop.category, drop.detail
            )
            if row["id"] not in announced_refused:
                reason = simulator.DROP_LABELS.get(drop.category, "it did not check out")
                await emit_event(
                    run_id, "system", "progress", SKIPPED.format(reason=reason), {"feedback_scenario_id": row["id"]}
                )
            continue
        # Whatever the row says, a feedback scenario is a practice scenario the user has not approved.
        good.append(scenario.model_copy(update={"split": "train", "approved_by_user": False}))
        taken.add(scenario.id)
    if not good:
        return {}

    if not (repo / ".git").exists():
        await emit_event(
            run_id,
            "system",
            "progress",
            SKIPPED.format(reason="the agent's files could not be found"),
            {"feedback_scenario_id": good[0].id},
        )
        return {}
    _write_into_repo(repo, good)
    head_commit = await simulator.commit_scenarios(repo)

    fresh = [s for s in good if s.id not in announced_added]
    if fresh:
        message = ADDED_ONE if len(fresh) == 1 else ADDED_MANY.format(n=len(fresh))
        await emit_event(run_id, "system", "progress", message, {"feedback_scenario_ids": [s.id for s in fresh]})
    return {"scenarios": [*state.get("scenarios", []), *good], "head_commit": head_commit}


def _with_feedback(runner: Node) -> Node:
    async def node(state: dict) -> dict:
        try:
            update = await feedback_update(state)
        except Exception as exc:  # feedback must never break the run: say so, log the type (text may be private)
            log.error("Adding feedback scenarios to run %s failed", state.get("run_id"), exc_info=exc)
            await emit_event(state["run_id"], "system", "progress", NOT_ADDED, {"error_type": type(exc).__name__})
            update = {}
        result = await runner({**state, **update})
        return {**update, **result}

    return node


def with_feedback(heads: Heads) -> Heads:
    """The same heads, with the Runner loading feedback scenarios before it scores a round."""
    return dataclasses.replace(heads, runner=_with_feedback(heads.runner))
