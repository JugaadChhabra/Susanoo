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


def rounds(*verdicts: str, **extra) -> dict:
    """State after one Fixer -> Verifier round per verdict (the last patch is the latest)."""
    return {"iteration": len(verdicts), "patches": [patch(v) for v in verdicts], **extra}


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
    stale = {**rounds("kept", "kept"), "results": [result("a", True, iteration=1)]}
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
    assert r.after_verifier(rounds("rolled_back", "kept", metrics=metrics(0.5, 0.5, 0.8))) == "Runner"


def test_verifier_rolled_back_goes_to_diagnostician():
    assert r.after_verifier(rounds("kept", "rolled_back")) == "Diagnostician"


@pytest.mark.parametrize("verdict", ["kept", "rolled_back"])
def test_verifier_stops_at_iteration_cap(verdict):
    # a kept patch before the last round, so only the cap (not "no_gain") can stop the run
    state = rounds("kept", "kept", "kept", "kept", verdict)
    assert r.stop_reason(state) == ("iteration_cap")
    assert r.after_verifier(state) == "Done"
    assert r.after_verifier(rounds("kept", "kept", "kept", verdict)) != "Done"


def test_verifier_stops_after_two_rounds_in_a_row_without_a_kept_patch():
    flat = rounds("rolled_back", "rolled_back")
    assert r.stop_reason(flat) == "no_gain"
    assert r.after_verifier(flat) == "Done"
    one = rounds("kept", "rolled_back")
    assert r.stop_reason(one) is None
    assert r.after_verifier(one) == "Diagnostician"
    assert r.stop_reason(rounds("rolled_back", "kept")) is None  # a kept patch resets the streak
    assert r.stop_reason(rounds("kept", "rolled_back", "rolled_back")) == "no_gain"


def test_held_out_f1_is_not_the_improvement_signal():
    # F1 values may come from different test levels: a rising curve does not make a rolled-back round a gain
    state = rounds("rolled_back", "rolled_back", metrics=metrics(0.1, 0.5, 0.9))
    assert r.stop_reason(state) == "no_gain"
    assert r.stop_reason(rounds("kept", "kept", metrics=metrics(0.5, 0.5, 0.5))) is None


def test_a_rejected_gate_round_counts_as_a_round_without_a_kept_patch():
    # round 1 rolled back, round 2 = rejected gate (no patch)
    assert (
        r.iterations_without_gain({"iteration": 2, "patches": [patch("rolled_back")], "gate_rejected_rounds": [2]}) == 2
    )
    # round 1 = rejected gate, round 2 = kept patch, round 3 = rejected gate
    state = {"iteration": 3, "patches": [patch("kept")], "gate_rejected_rounds": [1, 3]}
    assert r.iterations_without_gain(state) == 1
    assert r.stop_reason(state) is None
    state = {"iteration": 2, "patches": [], "gate_rejected_rounds": [1, 2]}
    assert r.stop_reason(state) == "no_gain"


def test_iterations_without_gain_counts_only_the_trailing_streak():
    assert r.iterations_without_gain({}) == 0
    assert r.iterations_without_gain(rounds("kept")) == 0
    assert r.iterations_without_gain(rounds("rolled_back")) == 1
    assert r.iterations_without_gain(rounds("rolled_back", "kept")) == 0
    assert r.iterations_without_gain(rounds("kept", "rolled_back", "rolled_back")) == 2
    pending = {"iteration": 1, "patches": [patch("kept"), patch("pending")]}  # a patch not yet judged is no round
    assert r.iterations_without_gain(pending) == 0


def test_verifier_stops_at_cost_cap(monkeypatch):
    state = rounds("kept", cost_usd=1.99)
    assert r.after_verifier(state) == "Runner"
    assert r.after_verifier({**state, "cost_usd": 2.0}) == "Done"  # default cap is $2
    monkeypatch.setenv("RUN_COST_CAP_USD", "0.5")
    assert r.stop_reason({**state, "cost_usd": 0.5}) == "budget"


def test_stop_conditions_are_checked_before_the_verdict():
    state = rounds("kept", "kept", "kept", "kept", "kept")
    assert r.after_verifier(state) == "Done"


def test_verifier_with_undecided_patch_is_an_error():
    with pytest.raises(ValueError):
        r.after_verifier({"iteration": 1, "patches": [patch("pending")], "metrics": metrics(0.5, 0.7)})
    with pytest.raises(ValueError):
        r.after_verifier({"iteration": 1, "patches": [], "metrics": []})


# --- head-set stops ------------------------------------------------------------------------------


def test_diagnostician_no_diagnosis_goes_to_done_and_ignores_a_stale_ledger():
    state = {"failure_ledger": [dx("test")], "stop_reason": "no_diagnosis"}  # dx("test") is an old diagnosis
    assert r.after_diagnostician(state) == "Done"
    assert r.after_diagnostician({"failure_ledger": [dx("test")]}) == "HumanGate"


def test_head_stop_status_is_done_only_with_a_score():
    assert r.head_stop_status({"metrics": metrics(0.5)}, "no_diagnosis") == "done"
    assert r.head_stop_status({"metrics": []}, "no_diagnosis") == "failed"
    assert r.head_stop_status({}, "budget") == "failed"
    with pytest.raises(ValueError):
        r.head_stop_status({}, "no_gain")
