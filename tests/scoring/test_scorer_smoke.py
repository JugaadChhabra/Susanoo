"""Smoke tests for the scorer (implementer's own sanity checks; rule tests live elsewhere)."""

import pytest

from susanoo.schemas import AnswerKey, ExpectedIssue, Finding, Scenario
from susanoo.scoring import UnparseableOutput, compute_metrics, f1, parse_findings, score_scenario

POLICY = {"no-raw-sql", "no-secrets"}


def _scenario(sid: str = "s1", split: str = "train", expected: list[ExpectedIssue] | None = None) -> Scenario:
    return Scenario(
        id=sid,
        kind="planted",
        split=split,  # type: ignore[arg-type]
        title="t",
        diff="",
        answer_key=AnswerKey(
            expected=expected
            if expected is not None
            else [ExpectedIssue(file="a.py", line_start=10, line_end=10, rule_id="no-raw-sql", severity="block")]
        ),
    )


def _finding(line: int = 10, rule_id: str = "no-raw-sql", severity: str = "block") -> Finding:
    return Finding(file="a.py", line=line, rule_id=rule_id, severity=severity, message="m")  # type: ignore[arg-type]


def test_perfect_match_passes() -> None:
    res = score_scenario(_scenario(), [_finding()], policy_rule_ids=POLICY, iteration=1)
    assert (res.matched, res.missed, res.false_positives, res.severity_correct, res.passed) == (1, 0, 0, 1, True)


def test_tolerance_edge_and_duplicate() -> None:
    res = score_scenario(_scenario(), [_finding(13), _finding(10)], policy_rule_ids=POLICY, iteration=1)
    assert res.matched == 1 and res.false_positives == 0 and res.passed
    out = score_scenario(_scenario(), [_finding(14)], policy_rule_ids=POLICY, iteration=1)
    assert out.matched == 0 and out.false_positives == 1 and not out.passed


def test_unknown_rule_and_error() -> None:
    res = score_scenario(
        _scenario(), [_finding(rule_id="made-up", severity="nit")], policy_rule_ids=POLICY, iteration=1
    )
    assert res.false_positives == 1 and res.missed == 1
    err = score_scenario(_scenario(), None, policy_rule_ids=POLICY, iteration=1)
    assert err.error == "no output" and not err.passed and err.missed == 1


def test_parse_findings() -> None:
    raw = '```json\n{"findings": [{"file": "a.py", "line": 1, "rule_id": "r", "severity": "nit", "message": "m"}]}\n```'
    assert len(parse_findings(raw)) == 1
    with pytest.raises(UnparseableOutput):
        parse_findings("not json")
    with pytest.raises(UnparseableOutput):
        parse_findings([{"file": "a.py"}])


def test_metrics() -> None:
    assert f1(0, 0, 0) == (1.0, 1.0, 1.0)
    assert f1(0, 2, 1) == (0.0, 0.0, 0.0)
    scenarios = [_scenario("a"), _scenario("b", "heldout"), _scenario("c", "acceptance")]
    results = [score_scenario(s, [_finding()], policy_rule_ids=POLICY, iteration=2) for s in scenarios]
    m = compute_metrics(results, scenarios, iteration=2, cost_usd=0.5)
    assert (m.recall, m.precision, m.f1, m.heldout_f1, m.acceptance_pass_rate) == (1.0, 1.0, 1.0, 1.0, 1.0)
    with pytest.raises(ValueError):
        compute_metrics(results, scenarios[:1], iteration=2, cost_usd=0.0)
