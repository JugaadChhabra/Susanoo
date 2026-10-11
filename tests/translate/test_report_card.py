"""Report card (translate, pure code). [PRD › Non-technical users]"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from susanoo.schemas import AnswerKey, IterationMetrics, Scenario, ScenarioResult
from susanoo.scoring import compute_metrics
from susanoo.translate import SetScore, previous_label, report_card, score_from_results


def sc(sid: str, split: str) -> Scenario:
    return Scenario(id=sid, kind="clean", split=split, title="t", diff="", answer_key=AnswerKey(expected=[]))


def res(sid: str, matched=0, missed=0, fp=0, passed=False, error=None) -> ScenarioResult:
    return ScenarioResult(
        scenario_id=sid,
        iteration=1,
        findings=[],
        matched=matched,
        missed=missed,
        false_positives=fp,
        severity_correct=matched,
        passed=passed,
        trace_id="t",
        error=error,
    )


def metrics(scenarios, results) -> IterationMetrics:
    return compute_metrics(results, scenarios, iteration=1, cost_usd=0.1)


def card(scenarios, results, **kw):
    return report_card(metrics(scenarios, results), results, scenarios, **kw)


SCENARIOS = [
    sc("h1", "heldout"),
    sc("h2", "heldout"),
    sc("t1", "train"),
    sc("a1", "acceptance"),
    sc("a2", "acceptance"),
]


def test_the_prd_example_sentences():
    results = [
        res("h1", matched=5, missed=1, fp=1),
        res("h2", matched=4, missed=0, fp=0),
        res("t1", matched=1),
        res("a1", passed=True),
        res("a2"),
    ]
    prev = SetScore(basis="heldout", caught=6, problems=10, false_alarms=3)
    c = card(SCENARIOS, results, previous=prev, previous_label_text="Last week")
    assert c.headline == "Caught 9 of 10 problems, 1 false alarm."
    assert c.comparison == "Last week: 6 of 10."
    assert c.trend == "better"
    assert c.basis == "heldout" and "hidden examples" in c.basis_note
    assert c.acceptance == "Passed 1 of 2 of the examples you approved."
    assert c.lines[0] == c.headline and c.lines[2] == c.comparison
    assert c.score_percent == round(c.score_percent)  # int


def test_headline_set_is_heldout_not_train():
    results = [res("h1", matched=1, missed=1), res("t1", matched=10)]
    c = card(SCENARIOS, results)
    assert c.headline == "Caught 1 of 2 problems, no false alarms."
    assert c.score_percent == round(metrics(SCENARIOS, results).heldout_f1 * 100)


def test_all_caught_and_no_false_alarms():
    c = card(SCENARIOS, [res("h1", matched=10)])
    assert c.headline == "Caught 10 of 10 problems, no false alarms."
    assert c.score_percent == 100
    assert c.comparison is None and c.trend == "new"


def test_single_problem_is_singular():
    assert card(SCENARIOS, [res("h1", matched=1)]).headline == "Caught 1 of 1 problem, no false alarms."


def test_no_problems_in_the_set():
    c = card(SCENARIOS, [res("h1"), res("h2", fp=2)])
    assert c.headline == "There were no problems to catch, and 2 false alarms."
    c = card(SCENARIOS, [res("h1", passed=True)])
    assert c.headline == "There were no problems to catch, and no false alarms."


def test_false_alarm_plural_and_singular():
    assert "1 false alarm." in card(SCENARIOS, [res("h1", matched=3, fp=1)]).headline
    assert "3 false alarms." in card(SCENARIOS, [res("h1", matched=3, fp=3)]).headline


def test_falls_back_to_practice_set_and_says_so():
    c = card(SCENARIOS, [res("t1", matched=2, missed=2)])
    assert c.basis == "train"
    assert c.headline == "Caught 2 of 4 problems, no false alarms."
    assert "practice examples" in c.basis_note


def test_nothing_scored():
    c = card(SCENARIOS, [])
    assert c.basis == "none" and c.headline == "Nothing has been scored yet."
    assert c.score_percent == 0 and c.acceptance is None


@pytest.mark.parametrize(
    ("before", "trend"),
    [
        (SetScore(basis="heldout", caught=6, problems=10, false_alarms=0), "better"),
        (SetScore(basis="heldout", caught=9, problems=10, false_alarms=1), "same"),
        (SetScore(basis="heldout", caught=9, problems=10, false_alarms=3), "better"),
        (SetScore(basis="heldout", caught=10, problems=10, false_alarms=0), "worse"),
        (SetScore(basis="train", caught=1, problems=10, false_alarms=0), "new"),
    ],
)
def test_trend(before, trend):
    c = card(SCENARIOS, [res("h1", matched=9, missed=1, fp=1)], previous=before)
    assert c.trend == trend
    assert (c.comparison is None) == (trend == "new")  # different set: no comparison line


def test_previous_can_be_an_earlier_report_card():
    older = card(SCENARIOS, [res("h1", matched=6, missed=4)])
    c = card(SCENARIOS, [res("h1", matched=9, missed=1)], previous=older, previous_label_text="Yesterday")
    assert c.comparison == "Yesterday: 6 of 10."
    assert c.trend == "better"


def test_previous_with_no_problems():
    prev = SetScore(basis="heldout", caught=0, problems=0, false_alarms=0)
    c = card(SCENARIOS, [res("h1", matched=1)], previous=prev)
    assert c.comparison == "Before: no problems to catch."


def test_errors_are_reported_in_words():
    c = card(SCENARIOS, [res("h1", missed=2, error="unparseable")])
    assert c.unscored == "1 example could not be checked because the agent's answer was unreadable or it failed."
    assert c.lines[-1] == c.unscored


def test_unknown_scenario_id_raises_like_the_scorer():
    with pytest.raises(ValueError, match="unknown scenario_id"):
        score_from_results([res("zzz")], SCENARIOS)


def test_no_jargon_in_sentences():
    c = card(SCENARIOS, [res("h1", matched=9, missed=1, fp=1), res("a1", passed=True)])
    text = " ".join(c.lines).lower()
    for word in ("f1", "recall", "precision", "heldout", "held-out", "split"):
        assert word not in text


def test_previous_label_wording():
    now = datetime(2026, 10, 12, 12)
    assert previous_label(now - timedelta(hours=3), now) == "Earlier today"
    assert previous_label(now - timedelta(hours=30), now) == "Yesterday"
    assert previous_label(now - timedelta(days=4), now) == "4 days ago"
    assert previous_label(now - timedelta(days=8), now) == "Last week"
    assert previous_label(now - timedelta(days=30), now) == "Earlier"
