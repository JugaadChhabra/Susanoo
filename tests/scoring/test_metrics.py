"""Tests for F1 and per-iteration metrics, written from the PRD text alone.

[PRD › Contracts (locked) › Scoring rule (decided)], rule 7: recall = matched / expected;
precision = matched / (matched + false positives); F1 combines both; the headline numbers are
held-out F1 and the acceptance-set pass rate.
"""

import pytest
from susanoo.schemas import AnswerKey, ExpectedIssue, IterationMetrics, Scenario, ScenarioResult
from susanoo.scoring import compute_metrics, f1


def scenario(sid: str, split: str, n_expected: int = 1) -> Scenario:
    expected = [
        ExpectedIssue(file="a.py", line_start=10 * (i + 1), line_end=10 * (i + 1), rule_id="r", severity="block")
        for i in range(n_expected)
    ]
    return Scenario(
        id=sid,
        kind="planted" if n_expected else "clean",
        split=split,  # type: ignore[arg-type]
        title="t",
        diff="",
        answer_key=AnswerKey(expected=expected),
    )


def result(sid: str, *, matched: int = 0, missed: int = 0, fp: int = 0, sev: int | None = None,
           passed: bool | None = None, error: str | None = None) -> ScenarioResult:
    sev = matched if sev is None else sev
    if passed is None:
        passed = missed == 0 and fp == 0 and sev == matched and error is None
    return ScenarioResult(scenario_id=sid, iteration=1, findings=[], matched=matched, missed=missed,
                          false_positives=fp, severity_correct=sev, passed=passed, trace_id="", error=error)


def metrics(results, scenarios, *, iteration: int = 1, cost: float = 0.0) -> IterationMetrics:
    return compute_metrics(results, scenarios, iteration=iteration, cost_usd=cost)


def f1_of(p: float, r: float) -> float:
    return 2 * p * r / (p + r)


# --- f1() --------------------------------------------------------------------------------------------------


def test_rule7_f1_formulas():
    recall, precision, score = f1(matched=3, expected=4, false_positives=1)
    assert recall == pytest.approx(0.75)
    assert precision == pytest.approx(0.75)
    assert score == pytest.approx(0.75)


def test_rule7_f1_is_harmonic_mean_of_unequal_precision_and_recall():
    recall, precision, score = f1(matched=2, expected=4, false_positives=0)  # R = 0.5, P = 1.0
    assert recall == pytest.approx(0.5)
    assert precision == pytest.approx(1.0)
    assert score == pytest.approx(f1_of(1.0, 0.5))
    recall, precision, score = f1(matched=2, expected=2, false_positives=6)  # R = 1.0, P = 0.25
    assert (recall, precision) == (pytest.approx(1.0), pytest.approx(0.25))
    assert score == pytest.approx(f1_of(0.25, 1.0))


def test_rule7_f1_perfect_and_total_miss():
    assert f1(5, 5, 0) == (1.0, 1.0, 1.0)
    recall, precision, score = f1(0, 4, 3)
    assert (recall, precision, score) == (0.0, 0.0, 0.0)


def test_rule7_f1_zero_division_recall_is_one_when_nothing_expected():
    recall, precision, score = f1(0, 0, 0)
    assert recall == 1.0
    assert precision == 1.0
    assert score == 1.0


def test_rule7_f1_zero_division_precision_is_one_when_nothing_matched_and_no_false_positives():
    recall, precision, score = f1(0, 3, 0)
    assert recall == 0.0
    assert precision == 1.0
    assert score == 0.0  # 2 * 1.0 * 0.0 / 1.0


def test_rule7_f1_zero_when_precision_plus_recall_is_zero():
    assert f1(0, 3, 2)[2] == 0.0


def test_rule7_f1_nothing_expected_but_false_positives_hurts_precision_only():
    recall, precision, score = f1(0, 0, 2)
    assert recall == 1.0
    assert precision == 0.0
    assert score == pytest.approx(0.0)


# --- compute_metrics: train split --------------------------------------------------------------------------


def test_rule7_metrics_use_pooled_counts_over_the_train_split():
    scenarios = [scenario("t1", "train", 4), scenario("t2", "train", 1)]
    results = [
        result("t1", matched=3, missed=1, fp=2, sev=2),
        result("t2", matched=1, missed=0, fp=0, sev=1),
    ]
    m = metrics(results, scenarios)
    # pooled: matched 4, expected 5, fp 2
    assert m.recall == pytest.approx(4 / 5)
    assert m.precision == pytest.approx(4 / 6)
    assert m.f1 == pytest.approx(f1_of(4 / 6, 4 / 5))
    assert m.severity_accuracy == pytest.approx(3 / 4)
    # pooled F1 is not the average of the per-scenario F1s
    per_scenario = (f1_of(3 / 5, 3 / 4) + 1.0) / 2
    assert m.f1 != pytest.approx(per_scenario)


def test_rule7_metrics_return_iteration_and_cost_and_model_type():
    m = metrics([result("t1", matched=1)], [scenario("t1", "train")], iteration=4, cost=1.25)
    assert isinstance(m, IterationMetrics)
    assert m.iteration == 4
    assert m.cost_usd == pytest.approx(1.25)


def test_rule7_expected_per_scenario_is_matched_plus_missed():
    # 2 matched + 3 missed -> 5 expected, whatever the answer key length says about other scenarios.
    m = metrics([result("t1", matched=2, missed=3)], [scenario("t1", "train", 5)])
    assert m.recall == pytest.approx(2 / 5)


def test_rule7_train_metrics_ignore_heldout_and_acceptance_results():
    scenarios = [scenario("t1", "train", 2), scenario("h1", "heldout", 2), scenario("a1", "acceptance", 2)]
    results = [
        result("t1", matched=2),
        result("h1", matched=0, missed=2, fp=5),
        result("a1", matched=0, missed=2, fp=5),
    ]
    m = metrics(results, scenarios)
    assert (m.recall, m.precision, m.f1, m.severity_accuracy) == (1.0, 1.0, 1.0, 1.0)


def test_rule7_false_positives_lower_precision_and_not_recall():
    m = metrics([result("t1", matched=2, fp=2)], [scenario("t1", "train", 2)])
    assert m.recall == pytest.approx(1.0)
    assert m.precision == pytest.approx(0.5)


def test_rule7_severity_accuracy_counts_only_matched_pairs():
    # 1 matched, wrong severity, plus a missed issue: accuracy = 0/1, not 0/2.
    m = metrics([result("t1", matched=1, missed=1, sev=0)], [scenario("t1", "train", 2)])
    assert m.severity_accuracy == pytest.approx(0.0)
    m = metrics([result("t1", matched=2, missed=3, sev=2)], [scenario("t1", "train", 5)])
    assert m.severity_accuracy == pytest.approx(1.0)


def test_rule7_errored_scenario_counts_its_expected_issues_as_missed():
    scenarios = [scenario("t1", "train", 2), scenario("t2", "train", 2)]
    results = [result("t1", matched=2), result("t2", missed=2, error="crash", passed=False)]
    m = metrics(results, scenarios)
    assert m.recall == pytest.approx(0.5)
    assert m.precision == pytest.approx(1.0)


# --- compute_metrics: held-out F1 and acceptance pass rate -------------------------------------------------


def test_rule7_heldout_f1_is_computed_over_heldout_scenarios_only():
    scenarios = [scenario("t1", "train", 2), scenario("h1", "heldout", 3), scenario("h2", "heldout", 1)]
    results = [
        result("t1", matched=2),
        result("h1", matched=2, missed=1, fp=1),
        result("h2", matched=1),
    ]
    m = metrics(results, scenarios)
    # heldout pooled: matched 3, expected 4, fp 1 -> R = 0.75, P = 0.75
    assert m.heldout_f1 == pytest.approx(0.75)
    assert m.f1 == pytest.approx(1.0)  # train is unaffected by held-out results


def test_rule7_heldout_f1_with_unequal_precision_and_recall():
    scenarios = [scenario("h1", "heldout", 4)]
    m = metrics([result("h1", matched=2, missed=2, fp=0)], scenarios)
    assert m.heldout_f1 == pytest.approx(f1_of(1.0, 0.5))


def test_rule7_empty_heldout_pool_gives_heldout_f1_of_one():
    m = metrics([result("t1", matched=1)], [scenario("t1", "train")])
    assert m.heldout_f1 == 1.0


def test_rule7_acceptance_pass_rate_is_passed_over_total_acceptance_scenarios():
    scenarios = [scenario(f"a{i}", "acceptance") for i in range(4)] + [scenario("t1", "train")]
    results = [
        result("a0", matched=1),
        result("a1", matched=1),
        result("a2", matched=1),
        result("a3", missed=1),
        result("t1", missed=1),  # a failing train scenario must not affect the acceptance rate
    ]
    m = metrics(results, scenarios)
    assert m.acceptance_pass_rate == pytest.approx(0.75)


def test_rule7_acceptance_pass_rate_uses_the_passed_flag():
    # Matched everything but severity was wrong -> passed False -> counts as a failure.
    scenarios = [scenario("a1", "acceptance"), scenario("a2", "acceptance")]
    results = [result("a1", matched=1, sev=0, passed=False), result("a2", matched=1)]
    assert metrics(results, scenarios).acceptance_pass_rate == pytest.approx(0.5)


def test_rule7_acceptance_pass_rate_is_zero_without_acceptance_scenarios():
    m = metrics([result("t1", matched=1)], [scenario("t1", "train")])
    assert m.acceptance_pass_rate == 0.0


def test_rule7_acceptance_pass_rate_can_be_zero_and_one():
    scenarios = [scenario("a1", "acceptance")]
    assert metrics([result("a1", missed=1)], scenarios).acceptance_pass_rate == 0.0
    assert metrics([result("a1", matched=1)], scenarios).acceptance_pass_rate == 1.0


# --- compute_metrics: zero-division and input handling -----------------------------------------------------


def test_rule7_empty_inputs_use_the_zero_division_conventions():
    m = metrics([], [], iteration=0, cost=0.0)
    assert m.recall == 1.0
    assert m.precision == 1.0
    assert m.f1 == 1.0
    assert m.severity_accuracy == 1.0
    assert m.heldout_f1 == 1.0
    assert m.acceptance_pass_rate == 0.0


def test_rule7_all_misses_gives_zero_recall_and_full_severity_accuracy_by_convention():
    m = metrics([result("t1", missed=2)], [scenario("t1", "train", 2)])
    assert m.recall == 0.0
    assert m.precision == 1.0  # matched + fp == 0
    assert m.f1 == 0.0
    assert m.severity_accuracy == 1.0  # nothing matched


def test_rule7_clean_scenarios_only_with_no_false_positives_score_perfectly():
    scenarios = [scenario("t1", "train", 0), scenario("h1", "heldout", 0)]
    m = metrics([result("t1"), result("h1")], scenarios)
    assert (m.recall, m.precision, m.f1, m.heldout_f1) == (1.0, 1.0, 1.0, 1.0)


def test_rule7_false_positives_on_clean_scenarios_zero_the_precision():
    m = metrics([result("t1", fp=2)], [scenario("t1", "train", 0)])
    assert m.recall == 1.0
    assert m.precision == 0.0
    assert m.f1 == 0.0


def test_rule7_unknown_scenario_id_raises_value_error():
    with pytest.raises(ValueError):
        metrics([result("ghost", matched=1)], [scenario("t1", "train")])


def test_rule7_result_order_does_not_matter():
    scenarios = [scenario("t1", "train", 2), scenario("h1", "heldout", 2), scenario("a1", "acceptance", 1)]
    results = [result("t1", matched=1, missed=1), result("h1", matched=2), result("a1", matched=1)]
    forward = metrics(results, scenarios)
    backward = metrics(list(reversed(results)), list(reversed(scenarios)))
    assert forward == backward
