"""Feedback scenarios join the loop through the worker. [PRD › Non-technical users]

Stub heads, a throwaway Postgres and a real (tiny) git repo per run. The API's half (saving the row) is tested in
tests/api/test_feedback.py; here the row is inserted directly, as `save_feedback_scenario` does.
"""

from __future__ import annotations

import asyncio
import subprocess
import uuid
from pathlib import Path

from conftest import wait_for
from helpers import Ledger, answer_until_terminal, instrumented, start, stop
from psycopg.types.json import Jsonb
from susanoo_worker import feedback as fb

from susanoo.graph import Heads, stubs
from susanoo.schemas import Scenario

DIFF = "--- a/app/log.py\n+++ b/app/log.py\n@@ -1,1 +1,2 @@\n x = 1\n+log.info(user.phone)\n"
RULE = "no-pii-in-logs"
ADDED = fb.ADDED_ONE


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def make_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "commit.gpgsign", "false")
    (path / "README.md").write_text("generated app\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    return str(path)


def add_feedback(db, run_id, *, sid=None, split="train", approved=False, rule=RULE, kind="planted", title=None, line=2):
    """Insert a row the way the API's save_feedback_scenario does (split/approved are overridable to test refusals)."""
    sid = sid or f"fb-{uuid.uuid4().hex[:8]}"
    expected = [] if kind == "clean" else [
        {"file": "app/log.py", "line_start": line, "line_end": line, "rule_id": rule, "severity": "block"}
    ]  # fmt: skip
    db.conn.execute(
        "insert into public.scenarios (id, run_id, kind, split, title, diff, answer_key, approved_by_user) "
        "values (%s, %s, %s, %s, %s, %s, %s, %s)",
        (
            _uuid(sid),
            run_id,
            kind,
            split,
            title or "The agent missed a phone number written to the log.",
            DIFF,
            Jsonb({"expected": expected, "must_not_flag": []}),
            approved,
        ),
    )
    return _uuid(sid)


def _uuid(sid: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, sid))


class Recorder:
    """A Runner that records the scenario ids it was given on each pass, then does what the stub does."""

    def __init__(self) -> None:
        self.passes: list[list[str]] = []

    async def __call__(self, state: dict) -> dict:
        self.passes.append([s.id for s in state.get("scenarios", [])])
        return await stubs.runner(state)


def heads_with_repo(tmp_path: Path, recorder: Recorder) -> Heads:
    async def builder(state: dict) -> dict:
        update = await stubs.builder(state)
        return {**update, "repo_path": make_repo(tmp_path / state["run_id"])}

    return Heads(builder=builder, runner=recorder)


def scenario_files(tmp_path: Path, run_id: str) -> list[str]:
    folder = tmp_path / run_id / "scenarios" / "train"
    return sorted(p.name for p in folder.glob("*")) if folder.exists() else []


def messages(db, run_id: str) -> list[str]:
    return [e["message"] for e in db.events(run_id)]


async def wait_for_gate(db, run_id: str) -> None:
    """Answer the intake pauses like a human would, and stop at the test-change approval (the gate)."""
    from helpers import ANSWERS
    from susanoo_worker import store

    async def at_gate() -> bool:
        row = db.run(run_id)
        if row["status"] != "awaiting_approval" or not row["paused_checkpoint_id"]:
            return False
        kind = db.pending_kind(run_id)
        if kind == "gate":
            return True
        await store.store_resume_value(run_id, ANSWERS[kind], row["paused_checkpoint_id"])
        return False

    deadline = asyncio.get_running_loop().time() + 60
    while not await at_gate():
        assert asyncio.get_running_loop().time() < deadline, "the run never reached the gate"
        await asyncio.sleep(0.1)


async def test_feedback_saved_at_the_gate_joins_the_next_round_once_and_lands_in_the_repo(db, tmp_path):
    run_id = db.make_run()
    recorder = Recorder()
    worker, task = start(instrumented(Ledger(), base=heads_with_repo(tmp_path, recorder)), "w1")
    try:
        await wait_for_gate(db, run_id)
        sid = add_feedback(db, run_id)
        assert len(recorder.passes) == 1 and sid not in recorder.passes[0]
        final = await answer_until_terminal(db, run_id)
    finally:
        await stop(worker, task)
    assert final["status"] == "done"
    assert len(recorder.passes) >= 2
    assert all(sid in p for p in recorder.passes[1:])  # in every round after the one it was saved in
    assert [p.count(sid) for p in recorder.passes[1:]] == [1] * (len(recorder.passes) - 1)  # never twice
    assert scenario_files(tmp_path, run_id) == sorted([f"{sid}.json", f"{sid}.world.json"])
    saved = Scenario.model_validate_json((tmp_path / run_id / "scenarios" / "train" / f"{sid}.json").read_text())
    assert saved.split == "train" and not saved.approved_by_user and saved.kind == "planted"
    assert git(tmp_path / run_id, "status", "--porcelain") == ""  # committed, nothing left dirty
    assert f"scenarios/train/{sid}.json" in git(tmp_path / run_id, "ls-files")
    assert messages(db, run_id).count(ADDED) == 1  # announced once, however many rounds followed


async def test_an_invalid_feedback_scenario_is_skipped_once_with_a_plain_english_event(db, tmp_path):
    run_id = db.make_run()
    recorder = Recorder()
    worker, task = start(instrumented(Ledger(), base=heads_with_repo(tmp_path, recorder)), "w1")
    try:
        await wait_for_gate(db, run_id)
        bad = add_feedback(db, run_id, sid="bad", rule="rule-the-plan-lacks")
        await answer_until_terminal(db, run_id)
    finally:
        await stop(worker, task)
    assert all(bad not in p for p in recorder.passes)
    assert scenario_files(tmp_path, run_id) == []
    skipped = [m for m in messages(db, run_id) if m.startswith("One check from your feedback could not be used")]
    assert skipped == [fb.SKIPPED.format(reason="using a rule that is not in the plan")]  # said once, over many rounds
    assert not any(ADDED in m for m in messages(db, run_id))


async def test_heldout_and_acceptance_rows_are_never_loaded(db, tmp_path):
    run_id = db.make_run()
    recorder = Recorder()
    worker, task = start(instrumented(Ledger(), base=heads_with_repo(tmp_path, recorder)), "w1")
    try:
        await wait_for_gate(db, run_id)
        held = add_feedback(db, run_id, sid="held", split="heldout")
        acc = add_feedback(db, run_id, sid="acc", split="acceptance", approved=True)
        approved_train = add_feedback(db, run_id, sid="approved", approved=True)
        await answer_until_terminal(db, run_id)
    finally:
        await stop(worker, task)
    for sid in (held, acc, approved_train):
        assert all(sid not in p for p in recorder.passes)
    assert scenario_files(tmp_path, run_id) == []
    assert not (tmp_path / run_id / "scenarios" / "heldout").exists()
    assert not (tmp_path / run_id / "scenarios" / "acceptance").exists()


async def test_a_finished_run_is_left_alone_and_its_feedback_waits_for_the_projects_next_run(db, tmp_path):
    first = db.make_run()
    recorder = Recorder()
    worker, task = start(instrumented(Ledger(), base=heads_with_repo(tmp_path, recorder)), "w1")
    try:
        await answer_until_terminal(db, first)
        passes_before = len(recorder.passes)
        sid = add_feedback(db, first)  # thumbs down on a finished run
        events = len(db.events(first))
        # the finished run is claimed again (its status write was lost): it is only closed, never rerun or touched
        db.conn.execute(
            "update public.runs set status = 'running', claimed_by = 'dead', heartbeat_at = now() - interval '1 hour' "
            "where id = %s",
            (first,),
        )
        await wait_for(lambda: db.run(first)["status"] == "done")
        assert len(db.events(first)) == events and len(recorder.passes) == passes_before
        assert scenario_files(tmp_path, first) == []

        # the next run of the same project picks it up at its first Runner pass
        project = db.one("select project_id from public.runs where id = %s", first)["project_id"]
        row = db.one("insert into public.runs (project_id, status) values (%s, 'queued') returning id", project)
        second = str(row["id"])
        await answer_until_terminal(db, second)
    finally:
        await stop(worker, task)
    assert all(sid in p for p in recorder.passes[passes_before:]) and len(recorder.passes) > passes_before
    assert sid in "".join(scenario_files(tmp_path, second))
    assert messages(db, second).count(ADDED) == 1
    assert scenario_files(tmp_path, first) == []


async def test_two_new_checks_get_one_plural_event_and_leave_other_scenario_files_alone(db, tmp_path):
    run_id = db.make_run()
    state = {
        "run_id": run_id,
        "plan": stubs_plan(),
        "repo_path": make_repo(tmp_path / "r"),
        "scenarios": [],
    }
    # a scenario file that is already in the repo (from the Simulator) must survive
    train = tmp_path / "r" / "scenarios" / "train"
    train.mkdir(parents=True)
    (train / "existing.json").write_text("{}")
    (train / "existing.world.json").write_text("{}")
    a = add_feedback(db, run_id, sid="a")
    b = add_feedback(db, run_id, sid="b", kind="clean", title="A harmless log line was flagged by mistake.")
    update = await fb.feedback_update(state)
    assert {s.id for s in update["scenarios"]} == {a, b}
    assert all(s.split == "train" and not s.approved_by_user for s in update["scenarios"])
    assert update["head_commit"] == git(tmp_path / "r", "rev-parse", "HEAD")
    assert sorted(p.name for p in train.glob("*")) == sorted(
        ["existing.json", "existing.world.json", f"{a}.json", f"{a}.world.json", f"{b}.json", f"{b}.world.json"]
    )
    assert messages(db, run_id) == [fb.ADDED_MANY.format(n=2)]


async def test_a_rerun_of_the_runner_after_a_crash_rewrites_the_same_files_and_announces_nothing_twice(db, tmp_path):
    run_id = db.make_run()
    state = {"run_id": run_id, "plan": stubs_plan(), "repo_path": make_repo(tmp_path / "r"), "scenarios": []}
    sid = add_feedback(db, run_id)
    first = await fb.feedback_update(state)
    head = git(tmp_path / "r", "rev-parse", "HEAD")
    again = await fb.feedback_update(state)  # the node died before its update was checkpointed: state is unchanged
    assert [s.id for s in again["scenarios"]] == [s.id for s in first["scenarios"]] == [sid]
    assert git(tmp_path / "r", "rev-parse", "HEAD") == head  # identical files: no second commit
    assert messages(db, run_id) == [ADDED]
    assert await fb.feedback_update({**state, "scenarios": first["scenarios"]}) == {}  # once in state: nothing to do


async def test_a_problem_adding_feedback_never_stops_the_run(db, tmp_path):
    run_id = db.make_run()
    recorder = Recorder()
    base = heads_with_repo(tmp_path, recorder)

    async def break_repo(state: dict) -> dict:
        # the generated app's folder disappears between the Builder and the Runner
        update = await base.builder(state)
        add_feedback(db, run_id)
        return {**update, "repo_path": str(tmp_path / "gone")}

    worker, task = start(instrumented(Ledger(), base=Heads(builder=break_repo, runner=recorder)), "w1")
    try:
        final = await answer_until_terminal(db, run_id)
    finally:
        await stop(worker, task)
    assert final["status"] == "done"
    assert fb.SKIPPED.format(reason="the agent's files could not be found") in messages(db, run_id)
    assert not any(ADDED in m for m in messages(db, run_id))


def stubs_plan():
    from susanoo.schemas import Plan, Rule

    return Plan(
        product_name="Code Review Agent",
        summary="Reviews diffs.",
        rules=[Rule(id=RULE, text="Never log customer personal data.", default_severity="block")],
        intake_answers={},
        files_to_generate=[],
        acceptance_criteria=[],
    )
