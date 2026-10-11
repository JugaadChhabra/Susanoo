"""Queue mechanics against a throwaway Postgres: claim, fencing, cancel sweep, migration. [PRD > Tech stack]"""

from __future__ import annotations

import asyncio

import psycopg
import pytest
from helpers import FAST
from pg_container import ROOT
from susanoo_worker import store
from susanoo_worker.config import WorkerConfig, require_session_connection

STALE = 1.0


async def test_two_workers_never_claim_the_same_run(db):
    ids = {db.make_run() for _ in range(8)}
    claims = await asyncio.gather(*(store.claim(f"w{i}", STALE) for i in range(12)))
    got = [c.id for c in claims if c]
    assert len(got) == 8 and set(got) == ids  # every run claimed, none twice
    assert sum(c is None for c in claims) == 4


async def test_single_run_goes_to_exactly_one_of_many_claimers(db):
    run_id = db.make_run()
    claims = await asyncio.gather(*(store.claim(f"w{i}", STALE) for i in range(10)))
    winners = [c for c in claims if c]
    assert [w.id for w in winners] == [run_id]
    row = db.run(run_id)
    assert row["status"] == "running" and row["claimed_by"] and row["heartbeat_at"] and row["attempts"] == 1


async def test_claim_returns_the_spec_oldest_first(db):
    first = db.make_run(spec="first spec")
    second = db.make_run(spec="second spec")
    one = await store.claim("w", STALE)
    assert (one.id, one.spec, one.prev_status) == (first, "first spec", "queued")
    two = await store.claim("w", STALE)
    assert two.id == second


async def test_awaiting_approval_done_failed_cancelled_are_never_claimed(db):
    for status in ("awaiting_approval", "done", "failed", "cancelled"):
        db.make_run(status=status)
    await asyncio.sleep(STALE + 0.3)  # even past the stale window
    assert await store.claim("w", STALE) is None


async def test_running_run_is_claimable_only_after_its_heartbeat_goes_stale(db):
    run_id = db.make_run()
    first = await store.claim("a", STALE)
    assert first and first.prev_status == "queued"
    assert await store.claim("b", STALE) is None  # fresh heartbeat: leave it alone
    assert await store.heartbeat(run_id, "a") is False
    db.conn.execute("update public.runs set heartbeat_at = now() - interval '1 hour' where id = %s", (run_id,))
    taken = await store.claim("b", STALE)
    assert taken and taken.id == run_id and taken.prev_status == "running" and taken.attempts == 2
    assert db.run(run_id)["claimed_by"] == "b"


async def test_a_worker_that_lost_its_run_cannot_write_to_it(db):
    run_id = db.make_run()
    await store.claim("a", STALE)
    db.conn.execute("update public.runs set heartbeat_at = now() - interval '1 hour' where id = %s", (run_id,))
    await store.claim("b", STALE)
    assert await store.heartbeat(run_id, "a") is None  # fenced out
    assert await store.finish(run_id, "a", "done", 1.0) is False
    assert await store.pause(run_id, "a", "ck", 1.0) is False
    assert await store.requeue(run_id, "a") is False
    row = db.run(run_id)
    assert row["status"] == "running" and row["claimed_by"] == "b"
    assert await store.finish(run_id, "b", "done", 1.5) is True
    assert float(db.run(run_id)["cost_usd"]) == 1.5


async def test_cancelled_runs_are_not_claimed_and_are_swept(db):
    queued = db.make_run(cancel_requested=True)
    parked = db.make_run(status="awaiting_approval", cancel_requested=True)
    orphan = db.make_run(status="running", cancel_requested=True, heartbeat_at="2000-01-01")
    alive = db.make_run(status="running", cancel_requested=True)
    db.conn.execute("update public.runs set heartbeat_at = now() where id = %s", (alive,))
    assert await store.claim("w", STALE) is None
    swept = set(await store.sweep_cancelled(STALE))
    assert swept == {queued, parked, orphan}  # a live worker cancels its own run
    assert {db.run(r)["status"] for r in swept} == {"cancelled"}
    assert db.run(alive)["status"] == "running"


async def test_pause_requeue_and_resume_value_round_trip(db):
    run_id = db.make_run()
    await store.claim("a", STALE)
    assert await store.pause(run_id, "a", "ck-1", 0.4)
    row = db.run(run_id)
    assert (row["status"], row["claimed_by"], row["paused_checkpoint_id"], row["attempts"]) == (
        "awaiting_approval",
        None,
        "ck-1",
        0,
    )
    assert await store.claim("a", STALE) is None
    assert await store.store_resume_value(run_id, {"approved": True}, "ck-1")
    assert not await store.store_resume_value(run_id, {"approved": False}, "ck-1")  # only while awaiting an answer
    claimed = await store.claim("b", STALE)
    assert claimed and claimed.resume_value == {"approved": True} and claimed.paused_checkpoint_id == "ck-1"
    assert await store.requeue(run_id, "b")
    row = db.run(run_id)
    assert (row["status"], row["claimed_by"], row["attempts"]) == ("queued", None, 0)


def test_the_proposal_is_idempotent_and_adds_the_new_statuses(db):
    proposal = (ROOT / "docs" / "proposals" / "006_run_queue.sql").read_text()
    db.conn.execute(proposal)
    db.conn.execute(proposal)
    for status in ("queued", "running", "awaiting_approval", "done", "failed", "cancelled"):
        db.make_run(status=status)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.make_run(status="bogus")
    run_id = db.make_run(status="queued")
    row = db.run(run_id)
    assert row["cancel_requested"] is False and row["attempts"] == 0 and row["claimed_by"] is None


def test_config_rejects_a_stale_window_shorter_than_two_heartbeats():
    with pytest.raises(ValueError):
        WorkerConfig(heartbeat_seconds=10, stale_seconds=15)
    assert FAST.stale_seconds >= 2 * FAST.heartbeat_seconds


def test_config_reads_the_environment(monkeypatch):
    monkeypatch.setenv("WORKER_CONCURRENCY", "4")
    monkeypatch.setenv("WORKER_STALE_SECONDS", "90")
    cfg = WorkerConfig.from_env()
    assert (cfg.concurrency, cfg.stale_seconds) == (4, 90.0)
    monkeypatch.setenv("WORKER_POLL_SECONDS", "soon")
    with pytest.raises(ValueError):
        WorkerConfig.from_env()


async def test_store_resume_value_fails_closed_on_a_stale_or_missing_token(db):
    run_id = db.make_run()
    await store.claim("a", STALE)
    await store.pause(run_id, "a", "ck-2", 0.0)
    stale = await store.store_resume_value(run_id, {"approved": True}, "ck-1")
    assert not stale and "earlier step" in stale.message and "Nothing was changed" in stale.message
    row = db.run(run_id)
    assert row["status"] == "awaiting_approval" and row["resume_value"] is None  # untouched
    ok = await store.store_resume_value(run_id, {"approved": True}, "ck-2")
    assert ok and db.run(run_id)["status"] == "queued"
    late = await store.store_resume_value(run_id, {"approved": True}, "ck-2")  # a duplicate submit
    assert not late and "not waiting for an answer" in late.message
    missing = await store.store_resume_value("00000000-0000-0000-0000-000000000000", {}, "ck-2")
    assert not missing and "could not be found" in missing.message


async def test_pending_pause_exposes_the_token_and_the_question(db):
    run_id = db.make_run()
    assert await store.pending_pause(run_id) is None
    await store.claim("a", STALE)
    from susanoo.db import emit_event

    await emit_event(run_id, "planner", "question_needed", "Please answer", {"kind": "questions", "questions": []})
    await store.pause(run_id, "a", "ck-9", 0.0)
    pending = await store.pending_pause(run_id)
    assert pending and pending.pause_token == "ck-9" and pending.kind == "questions"
    assert pending.payload["questions"] == []


def test_the_supabase_transaction_pooler_is_refused_with_a_clear_message():
    with pytest.raises(RuntimeError, match="transaction pooler.*5432"):
        require_session_connection("postgresql://postgres.abc:pw@aws-0-eu.pooler.supabase.com:6543/postgres")
    require_session_connection("postgresql://postgres:pw@db.abc.supabase.co:5432/postgres")
    require_session_connection("postgresql://postgres:pw@127.0.0.1:55001/postgres")


async def test_a_worker_refuses_to_start_on_port_6543(monkeypatch):
    from helpers import FAST, Ledger, instrumented
    from susanoo_worker.main import Worker

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@pooler.example.com:6543/postgres")
    with pytest.raises(RuntimeError, match="port 6543"):
        await Worker(FAST, instrumented(Ledger()), worker_id="w").run()
