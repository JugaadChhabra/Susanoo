"""Unit tests for every conditional edge. [PRD › LangGraph flow and shared state]"""

from __future__ import annotations

import pytest

from susanoo.graph import routing as r
from susanoo.schemas import Diagnosis, IterationMetrics, Patch, ScenarioResult


@pytest.fixture(autouse=True)
def _default_cap(monkeypatch):
    monkeypatch.delenv("RUN_COST_CAP_USD", raising=False)


def result(sid: str, passed: bool, iteration: int = 1) -> ScenarioResult:
    return ScenarioResult(
        scenario_id=sid, iteration=iteration, findings=[], matched=1, missed=0, false_positives=0,
        severity_correct=1, passed=passed, trace_id="t",
    )  # fmt: skip


def dx(layer: str) -> Diagnosis:
    return Diagnosis(id="dx", scenario_ids=["s1"], layer=layer, hypothesis="h", evidence_span_ids=[], target_files=[])  # type: ignore[arg-type]


def patch(verdict: str) -> Patch:
    return Patch(id="p", diagnosis_id="dx", commit_sha="abc", checkpoint_id=None, lines_changed=1, verdict=verdict)  # type: ignore[arg-type]


def metrics(*f1s: float) -> list[IterationMetrics]:
    return [
        IterationMetrics(
            iteration=i,
            recall=0,
            precision=0,
            f1=0,
            severity_accuracy=0,
            heldout_f1=f,
            acceptance_pass_rate=0,
            cost_usd=0,
        )
        for i, f in enumerate(f1s)
    ]


def test_intake_routes_on_questions():
    assert r.after_intake({"intake_questions": [{"id": "q"}]}) == "UserQuestions"
    assert r.after_intake({"intake_questions": []}) == "ScenarioApproval"
    assert r.after_intake({}) == "ScenarioApproval"


def test_runner_all_pass_goes_to_done():
    state = {"iteration": 1, "results": [result("a", True), result("b", True)]}
    assert r.after_runner(state) == "Done"


def test_runner_failure_goes_to_diagnostician():
    state = {"iteration": 1, "results": [result("a", True), result("b", False)]}
    assert r.after_runner(state) == "Diagnostician"


def test_runner_ignores_disputed_failures_and_stale_results():
    state = {"iteration": 1, "results": [result("a", True), result("b", False)], "disputed_scenario_ids": ["b"]}
    assert r.after_runner(state) == "Done"
    stale = {"iteration": 2, "results": [result("a", True, iteration=1)]}
    assert r.after_runner(stale) == "Diagnostician"  # nothing proven for this iteration


@pytest.mark.parametrize(
    ("layer", "nxt"), [("code", "Fixer"), ("tool", "Fixer"), ("prompt_policy", "Fixer"), ("test", "HumanGate")]
)
def test_diagnostician_routes_by_layer(layer, nxt):
    assert r.after_diagnostician({"failure_ledger": [dx("code"), dx(layer)]}) == nxt


def test_diagnostician_without_diagnosis_is_an_error():
    with pytest.raises(ValueError):
        r.after_diagnostician({"failure_ledger": []})


def test_human_gate_routes_on_decision():
    assert r.after_human_gate({"gate_decision": "approved"}) == "Fixer"
    assert r.after_human_gate({"gate_decision": "rejected"}) == "Runner"
    with pytest.raises(ValueError):
        r.after_human_gate({})


def test_verifier_kept_goes_to_runner():
    assert r.after_verifier({"iteration": 2, "patches": [patch("kept")], "metrics": metrics(0.5, 0.5, 0.8)}) == "Runner"


def test_verifier_rolled_back_goes_to_diagnostician():
    assert (
        r.after_verifier({"iteration": 1, "patches": [patch("rolled_back")], "metrics": metrics(0.5, 0.5)})
        == "Diagnostician"
    )


@pytest.mark.parametrize("verdict", ["kept", "rolled_back"])
def test_verifier_stops_at_iteration_cap(verdict):
    state = {"iteration": 5, "patches": [patch(verdict)], "metrics": metrics(0.5, 0.6, 0.7, 0.8, 0.9, 0.95)}
    assert r.stop_reason(state) == "iteration_cap"
    assert r.after_verifier(state) == "Done"
    assert r.after_verifier({**state, "iteration": 4}) != "Done"


def test_verifier_stops_after_two_iterations_without_gain():
    flat = {"iteration": 2, "patches": [patch("rolled_back")], "metrics": metrics(0.5, 0.5, 0.5)}
    assert r.stop_reason(flat) == "no_gain"
    assert r.after_verifier(flat) == "Done"
    one_flat = {**flat, "metrics": metrics(0.5, 0.7, 0.7)}
    assert r.stop_reason(one_flat) is None
    assert r.after_verifier(one_flat) == "Diagnostician"
    worse = {**flat, "metrics": metrics(0.8, 0.6, 0.7)}  # below the best so far counts as no gain
    assert r.after_verifier(worse) == "Done"


def test_iterations_without_gain_counts_only_the_trailing_streak():
    assert r.iterations_without_gain(metrics(0.5)) == 0
    assert r.iterations_without_gain(metrics(0.5, 0.5, 0.9)) == 0
    assert r.iterations_without_gain(metrics(0.5, 0.9, 0.9, 0.9)) == 2


def test_verifier_stops_at_cost_cap(monkeypatch):
    state = {"iteration": 1, "patches": [patch("kept")], "metrics": metrics(0.5, 0.7), "cost_usd": 1.99}
    assert r.after_verifier(state) == "Runner"
    assert r.after_verifier({**state, "cost_usd": 2.0}) == "Done"  # default cap is $2
    monkeypatch.setenv("RUN_COST_CAP_USD", "0.5")
    assert r.stop_reason({**state, "cost_usd": 0.5}) == "budget"


def test_stop_conditions_are_checked_before_the_verdict():
    state = {"iteration": 5, "patches": [patch("kept")], "metrics": metrics(0.5, 0.9)}
    assert r.after_verifier(state) == "Done"


def test_verifier_with_undecided_patch_is_an_error():
    with pytest.raises(ValueError):
        r.after_verifier({"iteration": 1, "patches": [patch("pending")], "metrics": metrics(0.5, 0.7)})
    with pytest.raises(ValueError):
        r.after_verifier({"iteration": 1, "patches": [], "metrics": []})
