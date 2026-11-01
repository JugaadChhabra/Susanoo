"""T35: shared-state collision schedules catch lost updates/duplicates; isolated suites stay separate [Spec §21]."""

import asyncio
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from collections_demo.collision_agents import claimer, payer
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError
from susanoo_runner.collision import (
    CollisionSchedule,
    DelayedAck,
    ReadBeforeWrite,
    Revocation,
    run_collision,
    run_isolated_control,
)
from susanoo_simulator import Actor, FaultProfile, FaultRule, FixtureManager, FixtureSeed
from susanoo_simulator.domains import collections, shared_ledger

T0 = datetime(2026, 11, 2, 4, 30, tzinfo=UTC)


def schedule(**overrides) -> CollisionSchedule:
    base = {
        "id": "two-payers",
        "namespace": "collide",
        "intentionally_shared": True,
        "actors": ["alice", "bob"],
        "barriers": [ReadBeforeWrite(actors=["alice", "bob"])],
        "repetitions": 2,
    }
    return CollisionSchedule(**{**base, **overrides})


def seed(balance: int = 100_000):
    return shared_ledger.seed(T0, ["alice", "bob"], [shared_ledger.account("ACC-1", balance=balance)])


def payers(mode_a, mode_b, amount_a=10_000, amount_b=20_000):
    return {
        "alice": payer(mode_a, account_id="ACC-1", amount_minor=amount_a, payment_ref="A-1"),
        "bob": payer(mode_b, account_id="ACC-1", amount_minor=amount_b, payment_ref="B-1"),
    }


def run(sched, actors, tmp_path, balance=100_000, faults=None, isolated=False):
    runner = run_isolated_control if isolated else run_collision
    kwargs = {"domain": shared_ledger.DOMAIN, "seed": seed(balance), "root": tmp_path}
    if faults is not None:
        kwargs["faults"] = faults
    return asyncio.run(runner(sched, actors, **kwargs))


def kinds(rep):
    return sorted({d.kind for d in rep.observation.defects})


def test_read_before_write_barrier_catches_seeded_lost_update(tmp_path):
    report = run(schedule(), payers("lost_update", "lost_update"), tmp_path)
    assert report.caught and report.defects_found == ["lost_update"] and report.shared_namespace
    for rep in report.repetitions:
        assert rep.outcome == "settled" and kinds(rep) == ["lost_update"]
        assert rep.observation.record_counts["payment"] == 2
        assert rep.observation.expected_balances == {"ACC-1": 70_000}
        assert rep.observation.final_balances["ACC-1"] in {80_000, 90_000}
        calls = [(e.actor, e.operation) for e in rep.evidence if e.event == "call"]
        # Both actors read before either writes: the barrier ordering is preserved in evidence.
        assert {calls[0][1], calls[1][1]} == {"read_account"} and {calls[0][0], calls[1][0]} == {"alice", "bob"}
    # Priority rotates across repetitions, so each actor writes first once.
    assert [rep.priority[0] for rep in report.repetitions] == ["alice", "bob"]


def test_versioned_actors_pass_the_same_schedule(tmp_path):
    report = run(schedule(), payers("correct", "correct"), tmp_path)
    assert not report.caught and report.defects_found == []
    for rep in report.repetitions:
        assert rep.observation.final_balances == {"ACC-1": 70_000}
        assert rep.observation.record_counts["payment"] == 2
        codes = [e.code for e in rep.evidence if e.event == "call"]
        assert "version_conflict" in codes  # the loser re-read and retried instead of overwriting


def test_delayed_ack_with_fresh_key_duplicates_and_same_key_replays(tmp_path):
    sched = schedule(barriers=[], delayed_acks=[DelayedAck(actor="alice", nth_mutation=1, late_by_steps=1)])
    bad = run(sched, payers("duplicate_retry", "correct"), tmp_path / "bad")
    assert bad.defects_found == ["duplicate_record"]
    assert all(rep.observation.payments_by_ref["ACC-1:A-1"] == 2 for rep in bad.repetitions)
    good = run(sched, payers("correct", "correct"), tmp_path / "good")
    assert not good.caught
    for rep in good.repetitions:
        events = [e.event for e in rep.evidence]
        assert "ack_withheld" in events and "late_ack" in events
        assert any(e.status == "replayed" and e.actor == "alice" for e in rep.evidence)
        assert rep.observation.payments_by_ref == {"ACC-1:A-1": 1, "ACC-1:B-1": 1}


def test_stale_absolute_write_reopens_a_settled_account(tmp_path):
    report = run(schedule(repetitions=1), payers("lost_update", "lost_update", 30_000, 10_000), tmp_path, 30_000)
    rep = report.repetitions[0]
    assert set(kinds(rep)) == {"invalid_transition", "lost_update"}
    assert rep.observation.status_history["ACC-1"] == ["open", "settled", "open"]


def test_ownership_steal_is_caught_and_versioned_claim_is_exclusive(tmp_path):
    steal = {name: claimer("ownership_steal", account_id="ACC-1") for name in ("alice", "bob")}
    bad = run(schedule(), steal, tmp_path / "bad")
    assert bad.defects_found == ["ownership_conflict"]
    fair = {name: claimer("correct", account_id="ACC-1") for name in ("alice", "bob")}
    good = run(schedule(), fair, tmp_path / "good")
    assert not good.caught
    assert [rep.observation.final_owner["ACC-1"] for rep in good.repetitions] == ["alice", "bob"]


def test_actor_losing_permission_mid_run_is_stopped_at_the_router(tmp_path):
    sched = schedule(revocations=[Revocation(actor="bob", after_step=2)])
    report = run(sched, payers("correct", "correct"), tmp_path)
    assert not report.caught
    for rep in report.repetitions:
        assert any(e.event == "revoked" and e.actor == "bob" for e in rep.evidence)
        rejected = [e for e in rep.evidence if e.event == "router_rejected"]
        assert rejected and all(e.actor == "bob" and e.code == "permission_revoked" for e in rejected)
        assert rep.observation.payments_by_ref == {"ACC-1:A-1": 1}
        assert rep.observation.final_balances == {"ACC-1": 90_000}


def test_injected_fault_is_preserved_in_evidence_and_reconciled_by_same_key(tmp_path):
    faults = FaultProfile(
        name="lost-ack", rules=[FaultRule(kind="failure_after_commit", operation="post_payment", on_calls=[1])]
    )
    report = run(schedule(repetitions=1), payers("correct", "correct"), tmp_path, faults=faults)
    rep = report.repetitions[0]
    assert rep.fault_profile == "lost-ack" and not report.caught
    fault = next(e for e in rep.evidence if e.event == "fault")
    assert fault.code == "connection_reset" and "failure_after_commit" in fault.faults
    assert rep.observation.final_balances == {"ACC-1": 70_000}


def test_normal_suites_keep_namespace_isolation(tmp_path):
    # The same defective actors in isolated namespaces cannot see each other, so nothing collides.
    report = run(schedule(), payers("lost_update", "lost_update"), tmp_path, isolated=True)
    assert report.isolated and not report.shared_namespace and not report.caught
    namespaces = [rep.namespace for rep in report.repetitions]
    assert len(namespaces) == len(set(namespaces)) == 4
    for rep in report.repetitions:
        assert rep.observation.record_counts["payment"] == 1
        assert {e.actor for e in rep.evidence} == {rep.priority[0]}
    # The fixture manager used by normal suites never shares state or tokens across namespaces.
    manager = FixtureManager(tmp_path / "suite")
    case_seed = FixtureSeed(
        domain="collections",
        domain_version=collections.VERSION,
        clock=T0,
        entities={"account": [collections.account("ACC-1")]},
        actors={"agent": Actor(permissions=collections.AGENT_PERMISSIONS, operations=collections.AGENT_OPERATIONS)},
    )
    one, two = manager.create("case-1", case_seed), manager.create("case-2", case_seed)
    manager.get("case-1").execute("agent", "request_handoff", {"account_id": "ACC-1", "reason": "dispute"})
    assert len(manager.get("case-1").observe("handoff")) == 1 and manager.get("case-2").observe("handoff") == []
    with pytest.raises(PermissionError):
        manager.authorize(one.agent_token, "case-2", "agent")
    assert two.agent_token != one.agent_token
    with pytest.raises(FileExistsError):
        manager.create("case-1", case_seed)


def test_shared_fixture_must_be_declared_and_limits_are_bounded():
    with pytest.raises(ValidationError, match="intentionally shared"):
        schedule(intentionally_shared=False)
    with pytest.raises(ValidationError):
        schedule(repetitions=21)
    with pytest.raises(ValidationError):
        schedule(deadline_seconds=600)
    with pytest.raises(ValidationError):
        schedule(max_calls_per_actor=10_000)
    with pytest.raises(ValidationError, match="unknown actors"):
        schedule(revocations=[Revocation(actor="mallory", after_step=1)])
    default = schedule()
    assert default.deadline_seconds == 10 and default.max_calls_per_actor == 50


def test_runaway_actors_hit_call_limit_and_deadline(tmp_path):
    async def spin(tools):
        while True:
            await tools.call("read_account", {"account_id": "ACC-1"})

    async def hang(tools):
        await asyncio.sleep(3600)

    idle = payer("correct", account_id="ACC-1", amount_minor=1, payment_ref="X")
    looping = run(schedule(repetitions=1, max_calls_per_actor=5), {"alice": spin, "bob": idle}, tmp_path / "a")
    assert looping.repetitions[0].outcome == "call_limit_exceeded"
    stuck = run(schedule(repetitions=1, deadline_seconds=0.2), {"alice": hang, "bob": idle}, tmp_path / "b")
    assert stuck.repetitions[0].outcome == "deadline_exceeded"


def test_actor_scripts_must_match_schedule(tmp_path):
    with pytest.raises(ValueError, match="one script per"):
        run(schedule(), {"alice": payer("correct", account_id="ACC-1", amount_minor=1, payment_ref="A")}, tmp_path)


def _order(report):
    return [
        [(e.step, e.actor, e.event, e.operation, e.status, e.code, e.sim_seq) for e in rep.evidence]
        for rep in report.repetitions
    ]


def test_schedule_is_deterministic_across_runs(tmp_path):
    first = run(schedule(), payers("lost_update", "correct"), tmp_path / "1")
    second = run(schedule(), payers("lost_update", "correct"), tmp_path / "2")
    assert _order(first) == _order(second)
    assert [r.fixture_digest for r in first.repetitions] == [r.fixture_digest for r in second.repetitions]


@settings(max_examples=15, deadline=None)
@given(
    amounts=st.tuples(st.integers(1, 40_000), st.integers(1, 40_000)),
    ack=st.booleans(),
)
def test_correct_actors_never_produce_collision_defects(amounts, ack):
    delayed = [DelayedAck(actor="bob", nth_mutation=1)] if ack else []
    with tempfile.TemporaryDirectory() as root:
        report = run(schedule(repetitions=2, delayed_acks=delayed), payers("correct", "correct", *amounts), Path(root))
    assert not report.caught
    for rep in report.repetitions:
        assert rep.observation.final_balances == {"ACC-1": 100_000 - sum(amounts)}
