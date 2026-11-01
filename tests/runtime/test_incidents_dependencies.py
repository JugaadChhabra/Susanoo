"""Failure-to-test drafts and dependency validity; trigger dedupe and budgets [Spec §18; T33, T34]."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from collections_demo import CollectionsAgent
from pydantic import ValidationError
from susanoo_runner.ci import RunDir, execute_run, load_suite, prepare_run
from susanoo_runner.dependencies import (
    AutomaticRunBudget,
    ChangeEvent,
    Conclusion,
    DependencyGraph,
    TriggerLedger,
    check_automatic_run,
    conclusions_from_run,
    observed_dependencies,
    trigger_dedupe_key,
)
from susanoo_runner.imports import Importer
from susanoo_runner.incidents import (
    DraftNotApproved,
    DraftScenario,
    approve_draft,
    group_incidents,
    incidents_from_records,
    propose_draft,
)
from susanoo_runner.journal import Journal
from susanoo_runner.suite import LocalProfile
from susanoo_sdk import InProcessAgentAdapter

ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_IDS = ["ACC-88231", "ACC-99412", "ACC-10077", "ACC-55555", "priya@corp.example"]


def conversation(conv: str, account: str, amount: int, error: str, email: str = "priya@corp.example") -> dict:
    return {
        "conversation_id": conv,
        "messages": [
            {"role": "user", "text": f"Account {account}: I will pay {amount} tomorrow. Write to {email}"},
            {"role": "assistant", "text": "Let me record that."},
            {
                "role": "tool",
                "tool_name": "create_promise",
                "arguments": {"account_id": account},
                "status": "error",
                "error": error,
            },
        ],
        "outcome": {"kind": "promise", "status": "failed"},
    }


@pytest.fixture
def records():
    lines = [
        conversation("conv-1", "ACC-88231", 5000, "Upstream timeout for ACC-88231 after 3000 ms"),
        conversation("conv-2", "ACC-99412", 7250, "Upstream timeout for ACC-99412 after 2950 ms"),
        conversation("conv-3", "ACC-10077", 100, "Upstream timeout for ACC-10077 after 3100 ms"),
        conversation("conv-4", "ACC-55555", 900, "Validation failed: due date 2026-12-31 beyond policy"),
    ]
    data = "\n".join(json.dumps(line) for line in lines).encode()
    return Importer("proj").import_jsonl(data, "prod.jsonl").records


# --- failure-to-test -----------------------------------------------------------------------------


def test_incidents_group_by_stable_signature(records):
    incidents = incidents_from_records(records)
    assert {i.kind for i in incidents} == {"tool_error", "reported_outcome"}
    groups = group_incidents([i for i in incidents if i.kind == "tool_error"])
    assert [g.count for g in groups] == [3, 1]
    timeout = groups[0]
    assert timeout.signature_basis["tool"] == "create_promise" and "<id>" in timeout.signature_basis["message"]
    assert timeout.representative.user_turns  # representative evidence carries the conversation
    outcomes = group_incidents([i for i in incidents if i.kind == "reported_outcome"])
    assert [g.count for g in outcomes] == [4]  # same failing outcome everywhere -> one signature
    again = group_incidents(list(reversed([i for i in incidents if i.kind == "tool_error"])))
    assert [g.signature for g in again] == [g.signature for g in groups]


def test_incident_to_draft_is_draft_only_and_synthetic(records):
    group = group_incidents([i for i in incidents_from_records(records) if i.kind == "tool_error"])[0]
    draft = propose_draft(group, domain="collections")
    blob = draft.model_dump_json()
    for production in PRODUCTION_IDS:
        assert production not in blob
    assert draft.status == "draft" and draft.requires_approval and not draft.auto_replay
    assert draft.case["checks"] == [] and draft.case["requirement_ids"] == [] and draft.case["required"] is False
    assert "SYN-" in draft.case["script"][0]["message"]
    assert draft.fixture["synthetic"] is True and draft.fixture["recipe"]["seed"]["entities"] == {}
    assert all(len(d) == 64 for d in draft.alias_digests.values())
    with pytest.raises(ValidationError):
        DraftScenario.model_validate({**draft.model_dump(), "status": "approved"})
    with pytest.raises(ValidationError):
        DraftScenario.model_validate({**draft.model_dump(), "auto_replay": True})


def test_draft_needs_human_approval_with_authored_checks(records):
    group = group_incidents(incidents_from_records(records))[0]
    draft = propose_draft(group, domain="collections")
    suite = load_suite(ROOT / "examples/collections/suite.json")
    template = suite.cases[0].model_dump(mode="json")
    with pytest.raises(DraftNotApproved):
        approve_draft(
            draft,
            approver="",
            requirement_ids=["PTP-001"],
            checks=template["checks"],
            observations=template["observations"],
        )
    with pytest.raises(DraftNotApproved):
        approve_draft(draft, approver="lead", requirement_ids=["PTP-001"], checks=[], observations=[])
    with pytest.raises(DraftNotApproved):  # the proposed fixture is a skeleton until authored
        approve_draft(
            draft,
            approver="lead",
            requirement_ids=["PTP-001"],
            checks=template["checks"],
            observations=template["observations"],
        )
    case = approve_draft(
        draft,
        approver="lead",
        requirement_ids=["PTP-001"],
        checks=template["checks"],
        observations=template["observations"],
        fixture=template["fixture"],
    )
    assert case.id.startswith("INC-") and case.required and case.fixture.synthetic


# --- dependency graph ----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def local_run(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("deps")
    suite = load_suite(ROOT / "examples/collections/suite.json")
    run_dir = RunDir.create(tmp / "runs")
    profile = LocalProfile()
    prepare_run(
        run_dir,
        suite,
        agent_label="inprocess:correct",
        agent_spec="inprocess:correct",
        agent_cmd=None,
        profile=profile,
        case_ids=["C01", "C15"],
    )
    report = execute_run(
        run_dir, suite, agent_factory=lambda: InProcessAgentAdapter(CollectionsAgent("correct")), profile=profile
    )
    return run_dir, report, suite


def build_graph(local_run):
    run_dir, report, suite = local_run
    journal = Journal(run_dir.journal_path)
    try:
        conclusions, edges = conclusions_from_run(report, run_dir.manifest(), suite, journal)
    finally:
        journal.close()
    graph = DependencyGraph()
    for conclusion in conclusions:
        graph.add(conclusion)
    for node, on in edges:
        graph.depends(node, on)
    by_case = {c.case_id: c.conclusion_id for c in conclusions}
    return graph, by_case


def event(kind, node_id, reason="changed", **kw) -> ChangeEvent:
    return ChangeEvent(
        event_id=f"{kind}:{node_id}:{kw.get('new_version', 'v2')}", kind=kind, node_id=node_id, reason=reason, **kw
    )


def test_observed_dependencies_come_from_the_journal(local_run):
    run_dir, report, _ = local_run
    journal = Journal(run_dir.journal_path)
    try:
        observed = observed_dependencies(journal, report.attempts[0].attempt_id)
    finally:
        journal.close()
    assert set(observed.observations) == {"promises", "accounts", "handoffs"}
    assert "promise_created_once" in observed.checks
    assert {"fixture_prepare", "agent_invoke", "cleanup"} <= set(observed.action_kinds)
    assert all(s.startswith("simulator:") for s in observed.observation_sources)
    assert observed.tool_operations is None  # adapter tool events are not journaled: unknown, not "none"


def test_change_marks_only_affected_conclusions_with_path(local_run):
    graph, by_case = build_graph(local_run)
    check = graph.apply(event("check", "C01/promise_created_once", "expected count changed"))
    assert [m.conclusion_id for m in check.affected] == [by_case["C01"]]
    mark = check.affected[0]
    assert mark.status == "scope_changed" and "expected count changed" in mark.reason
    assert mark.dependency_path == [f"conclusion:{by_case['C01']}", "check:C01/promise_created_once"]
    assert by_case["C15"] in check.unaffected and check.coverage_incomplete == []

    sim = graph.apply(event("simulator", "collections@1.0"))
    assert {m.conclusion_id for m in sim.affected} == set(by_case.values())
    assert sim.affected[0].status == "retest_recommended" and sim.affected[0].dependency_path[-2].startswith("fixture:")

    tool = graph.apply(event("tool_schema", "create_promise"))
    assert {m.status for m in tool.affected} == {"retest_recommended"}
    unrelated = graph.apply(event("tool_schema", "send_sms"))
    assert unrelated.affected == [] and unrelated.coverage_incomplete == []  # declared change elsewhere

    agent = graph.apply(event("agent", "inprocess:correct", new_version="inprocess:correct"))
    assert agent.affected == []  # already at that version


def test_missing_dependency_map_is_coverage_incomplete_not_unaffected(local_run):
    graph, by_case = build_graph(local_run)
    impact = graph.apply(event("reference", "core-banking-sandbox"))
    assert impact.affected == [] and impact.unaffected == []
    assert sorted(impact.coverage_incomplete) == sorted(by_case.values())
    assert graph.status(by_case["C01"]) == "coverage_incomplete"
    policy = graph.apply(event("policy", "data-retention"))
    assert sorted(policy.coverage_incomplete) == sorted(by_case.values())


def test_reference_changes_mark_reference_unverified_and_history_is_append_only():
    graph = DependencyGraph()
    conclusion = Conclusion(
        conclusion_id="run-1:C01",
        run_id="run-1",
        case_id="C01",
        verdict="PASS",
        recorded_at=datetime.now(UTC),
        dependencies={"reference:bank-sandbox": "r1", "case:C01": "d1"},
        covered_kinds=["reference", "case"],
    )
    graph.add(conclusion)
    first = graph.apply(event("reference", "bank-sandbox", new_version="r2"))
    assert first.affected[0].status == "reference_unverified"
    graph.apply(event("reference", "bank-sandbox", new_version="r2"))  # same event id: idempotent
    graph.apply(event("case", "C01", new_version="d2"))
    assert [m.status for m in graph.history("run-1:C01")] == ["reference_unverified", "scope_changed"]
    assert graph.conclusions["run-1:C01"].verdict == "PASS"  # prior results never erased
    with pytest.raises(ValueError):
        graph.add(conclusion)


# --- automatic runs ------------------------------------------------------------------------------


def test_trigger_dedupe_key_and_ledger():
    key = trigger_dedupe_key("proj", "github:push", "abc123", "collections-ptp-v1@3")
    assert key == trigger_dedupe_key("proj", "github:push", "abc123", "collections-ptp-v1@3")
    assert key != trigger_dedupe_key("proj", "github:push", "abc123", "collections-ptp-v1@4")
    assert key != trigger_dedupe_key("other", "github:push", "abc123", "collections-ptp-v1@3")
    ledger = TriggerLedger()
    assert ledger.admit(key, "run-1") == (True, "run-1")
    assert ledger.admit(key, "run-2") == (False, "run-1")


def test_automatic_runs_must_fit_budget_and_scope():
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    budget = AutomaticRunBudget(
        max_runs_per_window=2,
        window_seconds=3600,
        max_cases_per_run=20,
        max_agent_invocations=100,
        max_cost_minor=5000,
        allowed_suites=["collections-ptp-v1"],
    )
    ok = check_automatic_run(
        budget,
        suite_id="collections-ptp-v1",
        recent_runs=[],
        cases=20,
        agent_invocations=40,
        estimated_cost_minor=100,
        now=now,
    )
    assert ok.allowed and ok.secrets_allowed
    recent = [now - timedelta(minutes=50), now - timedelta(minutes=10)]
    limited = check_automatic_run(
        budget,
        suite_id="collections-ptp-v1",
        recent_runs=recent,
        cases=20,
        agent_invocations=40,
        estimated_cost_minor=100,
        now=now,
    )
    assert not limited.allowed and limited.delayed_until == now + timedelta(minutes=10)
    denied = check_automatic_run(budget, suite_id="other", recent_runs=[], cases=21, agent_invocations=101, now=now)
    assert len(denied.reasons) == 4 and not denied.allowed  # scope, cases, invocations, missing cost estimate
    fork = check_automatic_run(
        budget,
        suite_id="collections-ptp-v1",
        recent_runs=[],
        cases=1,
        agent_invocations=1,
        estimated_cost_minor=1,
        untrusted_source=True,
        now=now,
    )
    assert fork.allowed and not fork.secrets_allowed
