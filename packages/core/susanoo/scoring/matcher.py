"""Scenario matcher. [PRD › Contracts (locked) › Scoring rule (decided)], rules 1-6 and 8.

The rule is locked: never loosen it to improve numbers.

* Rule 1: a finding whose rule_id is not a policy rule id is a false positive and never matches.
* Rule 2: a finding matches an expected issue when file and rule_id are equal and
  ``line_start - 3 <= line <= line_end + 3``.
* Rule 3: matching is one-to-one. Candidate pairs are resolved globally by ascending distance
  (0 inside the range, otherwise distance to the nearest range end); ties go to the lower expected
  index, then the lower finding index. Unmatched findings that would also match an already-matched
  expected issue are duplicates: ignored, not penalised.
* Rule 4: false positives are unmatched block/warn findings (duplicates excluded), unknown-rule findings,
  and every finding whose rule_id is in ``must_not_flag`` (any severity, matched or not). A finding is
  counted at most once. Unmatched nits are ignored.
* Rule 5: ``severity_correct`` counts matched pairs whose severity equals the expected severity.
* Rule 6: a scenario passes when every expected issue is matched with the right severity, there are no
  false positives and there is no error.
* Rule 8: a runtime error or unparseable output fails the scenario and sets ``error``.
"""

import json
import re
from collections.abc import Collection

from pydantic import ValidationError

from susanoo.schemas import ExpectedIssue, Finding, Scenario, ScenarioResult

LINE_TOLERANCE = 3

_FENCE = re.compile(r"^```[A-Za-z0-9_-]*\s*\n(.*?)\n?```\s*$", re.DOTALL)


class UnparseableOutput(ValueError):
    """The reviewer's output could not be turned into a list of findings (rule 8)."""


def parse_findings(raw: str | list | dict) -> list[Finding]:
    """Turn raw reviewer output into findings, or raise ``UnparseableOutput``.

    Accepts a JSON string (optionally inside a fenced code block), a list of finding dicts, or a dict
    with a ``"findings"`` key.
    """
    data: object = raw
    if isinstance(raw, str):
        text = raw.strip()
        fenced = _FENCE.match(text)
        if fenced:
            text = fenced.group(1).strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise UnparseableOutput(f"output is not valid JSON: {exc}") from exc
    if isinstance(data, dict):
        if "findings" not in data:
            raise UnparseableOutput("output is a JSON object without a 'findings' key")
        data = data["findings"]
    if not isinstance(data, list):
        raise UnparseableOutput(f"expected a list of findings, got {type(data).__name__}")
    findings: list[Finding] = []
    for i, item in enumerate(data):
        if isinstance(item, Finding):
            findings.append(item)
            continue
        if not isinstance(item, dict):
            raise UnparseableOutput(f"finding {i} is not an object")
        try:
            findings.append(Finding.model_validate(item))
        except ValidationError as exc:
            raise UnparseableOutput(f"finding {i} is invalid: {exc}") from exc
    return findings


def _distance(issue: ExpectedIssue, finding: Finding) -> int | None:
    """Distance of a finding from an issue's line range, or None if it does not match (rule 2)."""
    if finding.file != issue.file or finding.rule_id != issue.rule_id:
        return None
    if finding.line < issue.line_start:
        dist = issue.line_start - finding.line
    elif finding.line > issue.line_end:
        dist = finding.line - issue.line_end
    else:
        dist = 0
    return dist if dist <= LINE_TOLERANCE else None


def score_scenario(
    scenario: Scenario,
    findings: list[Finding] | None,
    *,
    policy_rule_ids: Collection[str],
    iteration: int,
    trace_id: str = "",
    error: str | None = None,
) -> ScenarioResult:
    """Score one scenario's findings against its answer key."""
    expected = scenario.answer_key.expected
    if error is not None or findings is None:
        return ScenarioResult(
            scenario_id=scenario.id,
            iteration=iteration,
            findings=[],
            matched=0,
            missed=len(expected),
            false_positives=0,
            severity_correct=0,
            passed=False,
            trace_id=trace_id,
            error=error if error is not None else "no output",
        )

    policy = set(policy_rule_ids)
    must_not_flag = set(scenario.answer_key.must_not_flag)

    # Rules 2 and 3: candidate pairs among findings with a policy rule id, resolved globally by distance.
    candidates: list[tuple[int, int, int]] = []
    for e_idx, issue in enumerate(expected):
        for f_idx, finding in enumerate(findings):
            if finding.rule_id not in policy:
                continue
            dist = _distance(issue, finding)
            if dist is not None:
                candidates.append((dist, e_idx, f_idx))
    candidates.sort()

    pair_for_expected: dict[int, int] = {}
    matched_findings: set[int] = set()
    for _dist, e_idx, f_idx in candidates:
        if e_idx in pair_for_expected or f_idx in matched_findings:
            continue
        pair_for_expected[e_idx] = f_idx
        matched_findings.add(f_idx)

    # Duplicates: unmatched findings that would match an expected issue that is already matched.
    duplicates = {
        f_idx for _dist, e_idx, f_idx in candidates if f_idx not in matched_findings and e_idx in pair_for_expected
    }

    # Rule 4: count each finding at most once.
    false_positives = 0
    for f_idx, finding in enumerate(findings):
        if finding.rule_id in must_not_flag or finding.rule_id not in policy:
            false_positives += 1
        elif f_idx in matched_findings or f_idx in duplicates:
            continue
        elif finding.severity in ("block", "warn"):
            false_positives += 1

    matched = len(pair_for_expected)
    severity_correct = sum(
        1 for e_idx, f_idx in pair_for_expected.items() if findings[f_idx].severity == expected[e_idx].severity
    )
    passed = matched == len(expected) and severity_correct == matched and false_positives == 0

    return ScenarioResult(
        scenario_id=scenario.id,
        iteration=iteration,
        findings=list(findings),
        matched=matched,
        missed=len(expected) - matched,
        false_positives=false_positives,
        severity_correct=severity_correct,
        passed=passed,
        trace_id=trace_id,
        error=None,
    )
