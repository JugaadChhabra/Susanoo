"""Runtime half of the M2 vertical slice against a real HTTP simulator and the durable journal.

M2 exit gate (runtime side): correct write -> PASS, false completion -> FAIL, observer outage -> UNKNOWN.
Integration with the platform API is a separate gate and is not claimed here.
"""

import json
from pathlib import Path

import pytest
from collections_demo import CollectionsAgent, build_suite
from susanoo_runner import CancelToken, Journal, LocalProfile, LocalSuite, SimulatorServer, run_suite
from susanoo_sdk import InProcessAgentAdapter, Reply, SusanooAgent

ROOT = Path(__file__).resolve().parents[2]
SUITE = LocalSuite.model_validate(build_suite())


@pytest.fixture(scope="module")
def simulator():
    with SimulatorServer() as server:
        yield server


async def run(simulator, tmp_path, mode="correct", agent=None, **kwargs):
    factory = (
        (lambda: InProcessAgentAdapter(agent)) if agent else (lambda: InProcessAgentAdapter(CollectionsAgent(mode)))
    )
    journal = Journal(tmp_path / f"{mode}.sqlite")
    report = await run_suite(
        kwargs.pop("suite", SUITE),
        agent_factory=factory,
        agent_label=mode,
        journal=journal,
        simulator_url=simulator.base_url,
        simulator_admin_key=simulator.admin_key,
        **kwargs,
    )
    return report, journal


def verdicts(report):
    return {a.case_id: a.business_verdict for a in report.attempts}


def test_checked_in_suite_matches_the_generator():
    checked_in = json.loads((ROOT / "examples/collections/suite.json").read_text(encoding="utf-8"))
    assert checked_in == build_suite()
    assert len(SUITE.cases) == 20


@pytest.mark.parametrize("truncated", [False, True])
async def test_conditional_script_uses_complete_reply(simulator, tmp_path, truncated):
    class BranchAgent(SusanooAgent):
        def __init__(self):
            self.messages = []
        async def respond(self, session_id, message, context):
            self.messages.append(message.text)
            return Reply(text="continue" + ("x" * 262144 if truncated else ""))
    agent = BranchAgent()
    case = SUITE.cases[0].model_dump(mode="json")
    case["script"] = [{"message": "first"},
                      {"message": "branch", "when": "reply_contains", "value": "continue"},
                      {"message": "never", "when": "reply_contains", "value": "not-in-reply"},
                      {"message": "last"}]
    suite = SUITE.model_copy(update={"cases": [type(SUITE.cases[0]).model_validate(case)]})
    report, journal = await run(simulator, tmp_path, agent=agent, suite=suite)
    try:
        assert agent.messages == (["first", "last"] if truncated else ["first", "branch", "last"])
        steps = journal.steps(report.attempts[0].attempt_id)
        assert all(step.step_id != "turn-003" for step in steps)
    finally:
        journal.close()


async def test_m2_correct_agent_passes_every_case(simulator, tmp_path):
    report, journal = await run(simulator, tmp_path)
    assert set(verdicts(report).values()) == {"PASS"}
    assert report.gate == "READY_FOR_ACCEPTANCE"
    assert report.counts.passed == 20 and report.counts.execution_errors == 0
    assert all(a.cleanup_state == "succeeded" for a in report.attempts)
    assert journal.unfinished_attempts() == []


async def test_m2_false_completion_fails_from_independent_evidence(simulator, tmp_path):
    report, _ = await run(simulator, tmp_path, "false_completion")
    c01 = next(a for a in report.attempts if a.case_id == "C01")
    assert "recorded your promise" in c01.turns[0].reply  # the agent claims success...
    assert c01.business_verdict == "FAIL"  # ...the observed state says otherwise
    failing = {o.check_id for o in c01.outcomes if o.verdict == "FAIL"}
    assert "promise_created_once" in failing
    assert report.gate == "BLOCKED"


async def test_m2_observer_outage_is_unknown_not_pass(simulator, tmp_path):
    report, _ = await run(simulator, tmp_path, observer_outage=True, case_ids=["C01", "C15"])
    assert set(verdicts(report).values()) == {"UNKNOWN"}
    assert all(o.reason_code == "observation_unavailable" for a in report.attempts for o in a.outcomes)
    assert report.gate == "NEEDS_REVIEW"


@pytest.mark.parametrize(
    "mode,case_id,failed_check",
    [
        ("wrong_amount", "C01", "promise_amount_matches"),
        ("wrong_account", "C01", "promise_created_once"),
        ("wrong_account", "C01", "no_promise_on_other_accounts"),
        ("duplicate", "C01", "promise_created_once"),
        ("duplicate", "C13", "promise_created_once"),
        ("forbidden_change", "C19", "fees_and_balance_unchanged"),
        ("handoff", "C01", "promise_created_once"),
        ("lost_ack", "C14", "promise_created_once"),
        ("false_completion", "C15", "handoff_requested"),
    ],
)
async def test_faulty_modes_are_caught(simulator, tmp_path, mode, case_id, failed_check):
    report, _ = await run(simulator, tmp_path, mode, case_ids=[case_id])
    attempt = report.attempts[0]
    assert attempt.business_verdict == "FAIL"
    assert failed_check in {o.check_id for o in attempt.outcomes if o.verdict == "FAIL"}


async def test_lost_ack_with_idempotency_key_is_safe(simulator, tmp_path):
    report, _ = await run(simulator, tmp_path, case_ids=["C14"])
    assert report.attempts[0].business_verdict == "PASS"
    assert report.attempts[0].injected_faults == ["failure_after_commit"]


async def test_timeout_after_write_is_uncertain_then_reconciled(simulator, tmp_path):
    profile = LocalProfile(invocation_timeout_seconds=0.5)
    report, journal = await run(
        simulator, tmp_path, agent=CollectionsAgent("timeout_after_write"), profile=profile, case_ids=["C01"]
    )
    attempt = report.attempts[0]
    assert attempt.execution_state == "timed_out"
    assert attempt.turns[0].dispatch_state == "uncertain"
    assert attempt.reconciliations[0]["finding"] == "effect_present"
    assert attempt.reconciliation_state == "resolved"
    assert attempt.business_verdict == "PASS"  # the write was right; the execution still timed out
    assert report.gate == "BLOCKED"
    step = journal.step(attempt.attempt_id, "turn-001")
    assert (step.dispatch_state, step.side_effect_state) == ("uncertain", "confirmed_by_readback")


async def test_cleanup_failure_blocks_the_gate(simulator, tmp_path):
    report, _ = await run(simulator, tmp_path, cleanup_failure=True, case_ids=["C01"])
    assert report.attempts[0].business_verdict == "PASS"
    assert report.attempts[0].cleanup_state == "failed"
    assert report.gate == "BLOCKED" and report.blockers == ["C01:cleanup_failed"]


async def test_cancellation_starts_no_new_action(simulator, tmp_path):
    token = CancelToken()
    token.cancel()
    report, journal = await run(simulator, tmp_path, cancel=token, case_ids=["C01"])
    attempt = report.attempts[0]
    assert attempt.execution_state == "cancelled"
    events = [e["type"] for e in journal.events(attempt.attempt_id)]
    assert "cancel_acknowledged" in events
    assert not [s for s in journal.steps(attempt.attempt_id) if s.action_kind == "agent_invoke"]


async def test_local_profile_is_the_ceiling(simulator, tmp_path):
    with pytest.raises(PermissionError):
        await run(simulator, tmp_path, profile=LocalProfile(allowed_operations=["read_account"]), case_ids=["C01"])


class Spy(SusanooAgent):
    def __init__(self):
        self.seen: list[str] = []

    async def respond(self, session_id, message, context):
        self.seen.append(context.model_dump_json() + message.model_dump_json())
        return Reply(text="ok")


async def test_agent_never_receives_checks_or_fixture_truth(simulator, tmp_path):
    spy = Spy()
    await run(simulator, tmp_path, agent=spy, case_ids=["C01", "C07"])
    seen = " ".join(spy.seen)
    for secret in ("promise_created_once", "expected", "fixture.primary_account", "requires_complete", "checks"):
        assert secret not in seen
    assert "**********" in seen  # the route token is a SecretStr even inside the agent's own context dump


async def test_engineer_view_hides_held_out_cases(simulator, tmp_path):
    report, _ = await run(simulator, tmp_path, case_ids=["C06", "C07"])
    hidden = next(a for a in report.view("engineer")["attempts"] if a["case_id"] == "C07")
    assert hidden["outcomes"] == [] and hidden["turns"] == [] and hidden["title"] == "Held-out case"
    assert hidden["injected_faults"] == [] and hidden["reconciliations"] == []
    assert hidden["business_verdict"] == "PASS"
    operator = next(a for a in report.view("operator")["attempts"] if a["case_id"] == "C07")
    assert operator["outcomes"]


async def test_jsonl_agent_process_end_to_end(simulator, tmp_path):
    from susanoo_runner.cli import agent_factory

    factory, label = agent_factory("jsonl:correct", None)
    journal = Journal(tmp_path / "jsonl.sqlite")
    report = await run_suite(
        SUITE,
        agent_factory=factory,
        agent_label=label,
        journal=journal,
        simulator_url=simulator.base_url,
        simulator_admin_key=simulator.admin_key,
        case_ids=["C01", "C11", "C14", "C19"],
    )
    assert set(verdicts(report).values()) == {"PASS"}
    assert all(a.reason == "" for a in report.attempts)


async def test_reported_tool_events_are_journaled_for_dependency_extraction(simulator, tmp_path):
    from susanoo_runner.dependencies import observed_dependencies

    report, journal = await run(simulator, tmp_path, case_ids=["C01"])
    observed = observed_dependencies(journal, report.attempts[0].attempt_id)
    assert observed.tool_operations and "create_promise" in observed.tool_operations


@pytest.mark.parametrize("failure", ["evaluation", "reconciliation"])
async def test_evidence_processing_failure_still_cleans_up(simulator, tmp_path, monkeypatch, failure):
    from susanoo_runner.executor import AttemptExecutor
    from susanoo_simulator.adapter import SimulatorFixtureAdapter

    async def broken(*args, **kwargs):
        raise RuntimeError("synthetic evidence processing failure")

    cleanup_calls = []
    original_cleanup = SimulatorFixtureAdapter.cleanup

    async def cleanup(adapter, fixture):
        cleanup_calls.append(fixture.namespace)
        return await original_cleanup(adapter, fixture)

    monkeypatch.setattr(SimulatorFixtureAdapter, "cleanup", cleanup)
    monkeypatch.setattr(
        AttemptExecutor, "_observe_and_evaluate" if failure == "evaluation" else "_reconcile", broken
    )
    report, journal = await run(simulator, tmp_path, case_ids=["C01"])
    try:
        attempt = report.attempts[0]
        assert attempt.execution_state == "error"
        assert attempt.cleanup_state == "succeeded"
        assert cleanup_calls == [attempt.namespace]
        assert "Evidence processing failed" in attempt.reason
        assert report.gate == "BLOCKED"
        if failure == "evaluation":
            assert attempt.business_verdict == "UNKNOWN"
        assert journal.unfinished_attempts() == []
    finally:
        journal.close()


async def test_uncertain_fixture_preparation_is_not_reported_as_no_cleanup_needed(simulator, tmp_path, monkeypatch):
    from susanoo_simulator.adapter import SimulatorFixtureAdapter

    async def lost_ack(*args, **kwargs):
        raise TimeoutError("synthetic lost fixture acknowledgement")

    monkeypatch.setattr(SimulatorFixtureAdapter, "prepare", lost_ack)
    report, journal = await run(simulator, tmp_path, case_ids=["C01"])
    try:
        attempt = report.attempts[0]
        assert attempt.execution_state == "error"
        assert attempt.business_verdict == "UNKNOWN"
        assert attempt.cleanup_state == "failed"
        assert attempt.reconciliation_state == "unresolved"
        assert report.gate == "BLOCKED"
        assert journal.step(attempt.attempt_id, "fixture-prepare").dispatch_state == "uncertain"
        assert not [step for step in journal.steps(attempt.attempt_id) if step.action_kind == "cleanup"]
    finally:
        journal.close()


async def test_insufficient_housekeeping_reserve_refuses_before_fixture_creation(tmp_path):
    from susanoo_runner.executor import AttemptExecutor
    from susanoo_runner.journal import JournalError
    from susanoo_runner.suite import Limits

    journal = Journal(tmp_path / "insufficient-reserve.sqlite")
    try:
        executor = AttemptExecutor(
            journal, run_id="synthetic", limits=Limits(cleanup_reserve=2), agent_factory=lambda: None, fixtures=None
        )
        with pytest.raises(JournalError, match="Reserve fixture preparation"):
            await executor.run(SUITE.cases[0])
        assert journal.unfinished_attempts() == []
    finally:
        journal.close()
