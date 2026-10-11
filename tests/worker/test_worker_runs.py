"""The worker running real graph runs on the Postgres checkpointer, with stub heads (no model calls)."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from dataclasses import replace

from conftest import wait_for
from helpers import (
    FAST,
    Ledger,
    SimulatedCrash,
    answer_until_terminal,
    forever,
    instrumented,
    play_human,
    start,
    stop,
)
from susanoo_worker import store
from susanoo_worker.main import MAX_ATTEMPTS_MESSAGE, Worker
from psycopg.types.json import Jsonb
from susanoo_worker.run_driver import CANCELLED_MESSAGE, FAILED_MESSAGE, STALE_ANSWER_MESSAGE

from susanoo.graph import Heads, stubs


@contextlib.asynccontextmanager
async def human(db, run_id):
    task = play_human(db, run_id)
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_a_run_goes_end_to_end_pausing_for_each_human_answer(db):
    run_id = db.make_run()
    ledger = Ledger()
    worker, task = start(instrumented(ledger), "w1")
    try:
        final = await answer_until_terminal(db, run_id)
    finally:
        await stop(worker, task)
    assert final["status"] == "done" and final["error"] is None and final["claimed_by"] is None
    assert float(final["cost_usd"]) > 0
    kinds = [e["payload"].get("kind") for e in db.events(run_id) if e["type"] in ("question_needed", "approval_needed")]
    assert kinds == ["questions", "scenarios", "gate"]  # each pause announced exactly once, in order
    types = db.event_types(run_id)
    assert types[-1] == ("system", "finished")
    assert [e["seq"] for e in db.events(run_id)] == list(range(1, len(types) + 1))
    for node in ("Planner", "Builder", "AgentSmith", "Simulator"):
        assert ledger.count(node) == 1


async def test_awaiting_approval_is_left_alone_until_the_api_stores_an_answer(db):
    run_id = db.make_run()
    worker, task = start(instrumented(Ledger()), "w1")
    try:
        row = await wait_for(lambda: (r := db.run(run_id))["status"] == "awaiting_approval" and r)
        assert row["paused_checkpoint_id"] and row["claimed_by"] is None and row["attempts"] == 0
        before = len(db.events(run_id))
        await asyncio.sleep(FAST.stale_seconds + 0.5)  # past the stale window, still not claimed
        assert db.run(run_id)["status"] == "awaiting_approval" and len(db.events(run_id)) == before
    finally:
        await stop(worker, task)


async def test_an_answer_for_an_earlier_pause_is_never_applied(db):
    """Backstop: an answer whose token no longer names the live checkpoint (a crash lost the status write)."""
    run_id = db.make_run()
    worker, task = start(instrumented(Ledger()), "w1")
    try:
        await wait_for(lambda: db.run(run_id)["status"] == "awaiting_approval")
        # bypass store_resume_value's check, as a crash between a pause and its status write could
        db.conn.execute(
            "update public.runs set status = 'queued', resume_value = %s, paused_checkpoint_id = 'an-older-checkpoint'"
            " where id = %s",
            (Jsonb({"answers": {}}), run_id),
        )
        row = await wait_for(
            lambda: (
                (r := db.run(run_id))["status"] == "awaiting_approval"
                and r["paused_checkpoint_id"] != "an-older-checkpoint"
                and r
            )
        )
        assert row["resume_value"] is None
        assert [t for t in db.event_types(run_id) if t[1] == "finished"] == [("planner", "finished")]  # only Intake
        assert db.event_types(run_id).count(("planner", "question_needed")) == 1  # the pause was not re-announced
        notes = [e["message"] for e in db.events(run_id) if e["head"] == "system"]
        assert notes == [STALE_ANSWER_MESSAGE]  # said out loud, not cleared silently
    finally:
        await stop(worker, task)


async def test_a_stale_tab_cannot_approve_a_later_test_change_gate(db):
    """[PRD > Improvement loop and guardrails]: the gate for diagnosis 2 needs its own human decision."""
    run_id = db.make_run()
    ledger = Ledger()

    async def always_test_layer(state):
        out = await stubs.diagnostician(state)
        dx = out["failure_ledger"][-1]
        out["failure_ledger"][-1] = dx.model_copy(update={"layer": "test", "scenario_ids": dx.scenario_ids or ["tr-1"]})
        out["status"] = "awaiting_approval"
        return out

    base = replace(Heads(), diagnostician=always_test_layer)
    worker, task = start(instrumented(ledger, base=base), "w1")
    answers = {"questions": {"answers": {}}, "scenarios": {"approved_ids": []}}
    gates: list[tuple[str, str]] = []  # (diagnosis_id, token) of each gate the human saw
    try:
        for _ in range(300):
            row = db.run(run_id)
            if row["status"] == "awaiting_approval" and row["paused_checkpoint_id"]:
                pending = await store.pending_pause(run_id)
                assert pending and pending.pause_token == row["paused_checkpoint_id"]
                if pending.kind in answers:
                    await store.store_resume_value(run_id, answers.pop(pending.kind), pending.pause_token)
                elif pending.kind == "gate":
                    gates.append((pending.payload["diagnosis_id"], pending.pause_token))
                    if len(gates) == 1:  # the human approves gate 1; the next diagnosis then needs its own decision
                        assert await store.store_resume_value(run_id, {"approved": True}, pending.pause_token)
                    else:
                        break
            await asyncio.sleep(0.1)
        assert len(gates) == 2 and gates[0][0] != gates[1][0] and gates[0][1] != gates[1][1]
        # a stale tab, rendered for gate 1, now sends "approve" with gate 1's token
        late = await store.store_resume_value(run_id, {"approved": True}, gates[0][1])
        assert not late and "earlier step" in late.message
        await asyncio.sleep(1.0)  # give the worker every chance to (wrongly) act on it
        row = db.run(run_id)
        assert row["status"] == "awaiting_approval" and row["resume_value"] is None
        assert row["paused_checkpoint_id"] == gates[1][1]
        decisions = [
            e["payload"]["decision"] for e in db.events(run_id) if e["head"] == "fixer" and "decision" in e["payload"]
        ]
        assert decisions == ["approved"]  # only gate 1; gate 2 was never approved
        commits = [e for e in db.events(run_id) if e["head"] == "fixer" and "committed" in e["message"]]
        assert len(commits) == 1  # gate 1's fix; nothing was committed for gate 2
        # the real human, with the right token, still can
        assert await store.store_resume_value(run_id, {"approved": True}, gates[1][1])
    finally:
        await stop(worker, task)


async def test_a_malformed_answer_is_refused_in_plain_english_and_the_run_keeps_waiting(db):
    run_id = db.make_run()
    worker, task = start(instrumented(Ledger()), "w1")
    try:
        row = await wait_for(lambda: (r := db.run(run_id))["status"] == "awaiting_approval" and r)
        db.conn.execute("update public.runs set status = 'queued', resume_value = '5' where id = %s", (run_id,))
        await wait_for(lambda: any(e["type"] == "error" for e in db.events(run_id)))
        again = await wait_for(lambda: (r := db.run(run_id))["status"] == "awaiting_approval" and r)
        assert again["paused_checkpoint_id"] == row["paused_checkpoint_id"] and again["resume_value"] is None
        error = next(e for e in db.events(run_id) if e["type"] == "error")
        assert "still waiting" in error["message"] and "must be an object" in error["message"]
    finally:
        await stop(worker, task)


async def test_a_crashed_run_resumes_from_its_checkpoint_on_another_worker(db):
    run_id = db.make_run()
    ledger = Ledger()
    armed = {"crash": True}

    async def die_once(_state):
        if armed["crash"]:
            armed["crash"] = False
            raise SimulatedCrash  # the worker "dies" while the Builder is working

    factory = instrumented(ledger, {"Builder": die_once})
    a, task_a = start(factory, "worker-A")
    try:
        async with human(db, run_id):
            await wait_for(lambda: not armed["crash"])
            await wait_for(lambda: not a.active_run_ids)  # A's run task ended: it crashed
    finally:
        await stop(a, task_a)
    stuck = db.run(run_id)
    assert stuck["status"] == "running" and stuck["claimed_by"] == "worker-A"  # a crash writes nothing
    assert ledger.count("Builder") == 1 and ledger.count("Planner") == 1

    b, task_b = start(factory, "worker-B")
    try:
        final = await answer_until_terminal(db, run_id)
    finally:
        await stop(b, task_b)
    assert final["status"] == "done"
    assert ledger.count("Builder") == 2  # only the node that was in flight ran twice
    for node in ("Intake", "Planner", "AgentSmith", "Simulator"):
        assert ledger.count(node) == 1, node  # finished nodes were not repeated
    resumed = [e for e in db.events(run_id) if e["type"] == "progress" and "back up after" in e["message"]]
    assert len(resumed) == 1 and resumed[0]["payload"]["resumed_at"] == ["Builder"]
    assert resumed[0]["payload"]["attempt"] == 2
    # nothing the finished nodes announced was announced again
    assert db.event_types(run_id).count(("planner", "question_needed")) == 1
    assert db.event_types(run_id).count(("simulator", "started")) == 1


async def test_a_crash_in_the_very_first_node_is_also_resumed(db):
    run_id = db.make_run()
    ledger = Ledger()
    armed = {"crash": True}

    async def die_once(_state):
        if armed["crash"]:
            armed["crash"] = False
            raise SimulatedCrash

    factory = instrumented(ledger, {"Intake": die_once})
    a, task_a = start(factory, "A")
    try:
        await wait_for(lambda: not armed["crash"] and not a.active_run_ids)
    finally:
        await stop(a, task_a)
    assert db.run(run_id)["status"] == "running"
    b, task_b = start(factory, "B")
    try:
        final = await answer_until_terminal(db, run_id)
    finally:
        await stop(b, task_b)
    assert final["status"] == "done" and ledger.count("Intake") == 2 and ledger.count("Planner") == 1


async def test_a_run_that_keeps_crashing_is_given_up_on(db):
    run_id = db.make_run(status="running", attempts=FAST.max_attempts, heartbeat_at="2000-01-01", claimed_by="dead")
    ledger = Ledger()
    worker, task = start(instrumented(ledger), "w")
    try:
        row = await wait_for(lambda: (r := db.run(run_id))["status"] == "failed" and r)
    finally:
        await stop(worker, task)
    assert row["error"] == MAX_ATTEMPTS_MESSAGE and ledger.started == []
    assert [e["type"] for e in db.events(run_id)] == ["error"]


async def test_cancel_while_a_node_is_running_stops_it_within_a_heartbeat(db):
    run_id = db.make_run()
    ledger = Ledger()
    worker, task = start(instrumented(ledger, {"Builder": forever}), "w")
    try:
        async with human(db, run_id):
            await wait_for(lambda: ledger.count("Builder") == 1)  # the Builder now blocks forever
            db.conn.execute("update public.runs set cancel_requested = true where id = %s", (run_id,))
            row = await wait_for(lambda: (r := db.run(run_id))["status"] == "cancelled" and r)
    finally:
        await stop(worker, task)
    assert row["claimed_by"] is None and row["resume_value"] is None
    last = db.events(run_id)[-1]
    assert (last["head"], last["type"], last["message"]) == ("system", "finished", CANCELLED_MESSAGE)
    assert ledger.count("AgentSmith") == 0


async def test_cancel_is_seen_between_graph_steps(db):
    run_id = db.make_run()
    ledger = Ledger()

    async def cancel_during_planner(state):
        db.conn.execute("update public.runs set cancel_requested = true where id = %s", (state["run_id"],))

    slow_beat = replace(FAST, heartbeat_seconds=5.0, stale_seconds=20.0)  # the heartbeat cannot be what catches it
    worker, task = start(instrumented(ledger, {"Planner": cancel_during_planner}), "w", slow_beat)
    try:
        async with human(db, run_id):
            await wait_for(lambda: db.run(run_id)["status"] == "cancelled")
    finally:
        await stop(worker, task)
    assert ledger.count("Planner") == 1 and ledger.count("Builder") == 0  # stopped right after the Planner step


async def test_cancelling_a_queued_run_never_starts_it(db):
    run_id = db.make_run(cancel_requested=True)
    ledger = Ledger()
    worker, task = start(instrumented(ledger), "w")
    try:
        await wait_for(lambda: db.run(run_id)["status"] == "cancelled")
    finally:
        await stop(worker, task)
    assert ledger.started == []
    assert db.events(run_id)[-1]["message"] == CANCELLED_MESSAGE


async def test_a_node_exception_fails_the_run_with_a_plain_english_error_and_no_leak(db):
    run_id = db.make_run()
    ledger = Ledger()

    async def explode(_state):
        raise RuntimeError("connection string postgres://u:hunter2@host leaked")

    worker, task = start(instrumented(ledger, {"Builder": explode}), "w")
    try:
        async with human(db, run_id):
            row = await wait_for(lambda: (r := db.run(run_id))["status"] == "failed" and r)
    finally:
        await stop(worker, task)
    assert row["error"] == FAILED_MESSAGE and row["claimed_by"] is None
    err = [e for e in db.events(run_id) if e["type"] == "error"]
    assert len(err) == 1 and err[0]["head"] == "system" and err[0]["message"] == FAILED_MESSAGE
    assert err[0]["payload"] == {"error_type": "RuntimeError"}
    assert "hunter2" not in repr(db.events(run_id)) and "hunter2" not in repr(row)
    assert ledger.count("Builder") == 1  # a failed run is not retried


async def test_budget_stop_before_any_score_is_failed(db, monkeypatch):
    monkeypatch.setenv("RUN_COST_CAP_USD", "0.01")
    run_id = db.make_run()
    worker, task = start(instrumented(Ledger()), "w")
    try:
        final = await answer_until_terminal(db, run_id)
    finally:
        await stop(worker, task)
    assert final["status"] == "failed" and "cost limit" in final["error"]
    assert float(final["cost_usd"]) == 0.05
    assert any(e["type"] == "error" and "cost cap" in e["message"] for e in db.events(run_id))  # from the graph


async def test_budget_stop_after_the_baseline_score_is_done(db, monkeypatch):
    monkeypatch.setenv("RUN_COST_CAP_USD", "0.36")
    run_id = db.make_run()
    worker, task = start(instrumented(Ledger()), "w")
    try:
        final = await answer_until_terminal(db, run_id)
    finally:
        await stop(worker, task)
    assert final["status"] == "done" and final["error"] is None
    assert float(final["cost_usd"]) == 0.37


async def test_two_workers_share_the_queue_and_run_each_run_once(db):
    run_ids = [db.make_run() for _ in range(4)]
    ledger = Ledger()
    factory = instrumented(ledger)
    a, task_a = start(factory, "A")
    b, task_b = start(factory, "B")
    try:
        finals = await asyncio.gather(*(answer_until_terminal(db, r) for r in run_ids))
    finally:
        await stop(a, task_a)
        await stop(b, task_b)
    assert [f["status"] for f in finals] == ["done"] * 4
    for r in run_ids:
        assert ledger.count("Builder", r) == 1


async def test_shutdown_hands_a_running_run_back_and_another_worker_finishes_it(db):
    run_id = db.make_run()
    ledger = Ledger()
    hold = {"on": True}

    async def hold_builder(state):
        if hold["on"]:
            await forever(state)

    factory = instrumented(ledger, {"Builder": hold_builder})
    a, task_a = start(factory, "A")
    async with human(db, run_id):
        await wait_for(lambda: ledger.count("Builder") == 1)
    a.request_stop()  # SIGTERM: grace is 0.5 s and the Builder never ends by itself
    await asyncio.wait_for(task_a, 30)
    row = db.run(run_id)
    assert (row["status"], row["claimed_by"], row["attempts"]) == ("queued", None, 0)
    assert any("restarting" in e["message"] for e in db.events(run_id))
    hold["on"] = False
    b, task_b = start(factory, "B")
    try:
        final = await answer_until_terminal(db, run_id)
    finally:
        await stop(b, task_b)
    assert final["status"] == "done" and ledger.count("Builder") == 2 and ledger.count("Planner") == 1


async def test_an_idle_worker_stops_promptly_and_sigterm_requests_a_stop(db):
    worker = Worker(FAST, instrumented(Ledger()), worker_id="idle")
    task = asyncio.create_task(worker.run())
    worker.install_signal_handlers()
    try:
        await asyncio.sleep(0.3)
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(task, 5)
    finally:
        worker.remove_signal_handlers()
    assert worker.active_run_ids == []


async def test_a_finished_run_whose_status_write_was_lost_is_closed_without_rerunning(db):
    """Crash between the Done node and the status write: the claim goes stale, the next worker only records it."""
    run_id = db.make_run()
    ledger = Ledger()
    worker, task = start(instrumented(ledger), "A")
    try:
        await answer_until_terminal(db, run_id)
    finally:
        await stop(worker, task)
    nodes, events = len(ledger.started), len(db.events(run_id))
    db.conn.execute(
        "update public.runs set status = 'running', claimed_by = 'dead', heartbeat_at = now() - interval '1 hour' "
        "where id = %s",
        (run_id,),
    )
    worker, task = start(instrumented(ledger), "B")
    try:
        row = await wait_for(lambda: (r := db.run(run_id))["status"] == "done" and r)
    finally:
        await stop(worker, task)
    assert len(ledger.started) == nodes and len(db.events(run_id)) == events
    assert row["claimed_by"] is None


async def test_the_cancel_event_is_retried_once_if_recording_it_fails(db, monkeypatch):
    import susanoo_worker.main as worker_main

    run_id = db.make_run(cancel_requested=True)
    real_emit, calls = worker_main.emit_event, {"n": 0}

    async def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("blip")
        return await real_emit(*args, **kwargs)

    monkeypatch.setattr(worker_main, "emit_event", flaky)
    worker, task = start(instrumented(Ledger()), "w")
    try:
        await wait_for(lambda: db.run(run_id)["status"] == "cancelled")
        await wait_for(lambda: db.events(run_id))
    finally:
        await stop(worker, task)
    assert calls["n"] == 2 and db.events(run_id)[-1]["message"] == CANCELLED_MESSAGE
