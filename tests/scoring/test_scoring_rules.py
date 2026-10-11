"""Tests for the locked scoring rule, written from the PRD text alone.

[PRD › Contracts (locked) › Scoring rule (decided)], rules 1-6 and 8 (rule 7 lives in test_metrics.py).
Scenarios and findings are built inline; no seed files are involved.
"""

import json

import pytest
from susanoo.schemas import AnswerKey, ExpectedIssue, Finding, Scenario, ScenarioResult
from susanoo.scoring import UnparseableOutput, parse_findings, score_scenario

POLICY = {"no-raw-sql", "no-pii-in-logs", "no-hardcoded-secrets", "needs-auth"}
FILE = "app/db.py"


def exp(line_start: int, line_end: int | None = None, *, rule: str = "no-raw-sql", sev: str = "block",
        file: str = FILE) -> ExpectedIssue:
    return ExpectedIssue(file=file, line_start=line_start, line_end=line_end or line_start, rule_id=rule,
                         severity=sev)  # type: ignore[arg-type]


def fnd(line: int, *, rule: str = "no-raw-sql", sev: str = "block", file: str = FILE, msg: str = "m") -> Finding:
    return Finding(file=file, line=line, rule_id=rule, severity=sev, message=msg)  # type: ignore[arg-type]


def scen(expected: list[ExpectedIssue], must_not_flag: list[str] | None = None) -> Scenario:
    return Scenario(
        id="s1",
        kind="planted" if expected else "clean",
        split="train",
        title="t",
        diff="",
        answer_key=AnswerKey(expected=expected, must_not_flag=must_not_flag or []),
    )


def score(scenario: Scenario, findings: list[Finding] | None, **kw) -> ScenarioResult:
    kw.setdefault("policy_rule_ids", POLICY)
    kw.setdefault("iteration", 0)
    return score_scenario(scenario, findings, **kw)


# --- Rule 1: only policy rule ids count; anything else is a false positive ---------------------------------


@pytest.mark.parametrize("sev", ["block", "warn", "nit"])
def test_rule1_unknown_rule_id_is_false_positive_at_any_severity(sev):
    r = score(scen([]), [fnd(5, rule="made-up-rule", sev=sev)])
    assert r.false_positives == 1
    assert r.matched == 0
    assert r.passed is False


def test_rule1_unknown_rule_id_never_matches_even_if_expected_issue_uses_it():
    # The answer key names a rule the policy doesn't have: the finding must not be credited as a match.
    r = score(scen([exp(10, rule="ghost-rule")]), [fnd(10, rule="ghost-rule")])
    assert r.matched == 0
    assert r.missed == 1
    assert r.false_positives == 1
    assert r.passed is False


def test_rule1_unknown_rule_id_next_to_a_real_match_still_counts_as_false_positive():
    r = score(scen([exp(10)]), [fnd(10), fnd(10, rule="invented", sev="warn")])
    assert (r.matched, r.false_positives) == (1, 1)
    assert r.passed is False


def test_rule1_policy_ids_accept_any_collection_type():
    for policy in (["no-raw-sql"], ("no-raw-sql",), frozenset({"no-raw-sql"})):
        r = score(scen([exp(10)]), [fnd(10)], policy_rule_ids=policy)
        assert r.matched == 1 and r.passed is True


# --- Rule 2: same file + same rule + line within [start-3, end+3] ------------------------------------------


@pytest.mark.parametrize(
    "line, should_match",
    [(7, True), (6, False), (10, True), (12, True), (13, True), (14, False)],
    ids=["start-3", "start-4", "inside", "inside-end", "end+3", "end+4"],
)
def test_rule2_window_boundaries_for_single_line_issue(line, should_match):
    # Issue at line 10..10: window is 7..13.
    r = score(scen([exp(10)]), [fnd(line)])
    assert (r.matched == 1) is should_match
    assert r.missed == (0 if should_match else 1)
    # A near-miss that fails to match is an unmatched block finding, hence a false positive.
    assert r.false_positives == (0 if should_match else 1)


def test_rule2_multi_line_window_is_widened_by_3_on_both_ends():
    # 20..26 widened by 3 -> 17..29
    assert score(scen([exp(20, 26)]), [fnd(29)]).matched == 1
    assert score(scen([exp(20, 26)]), [fnd(30)]).matched == 0
    assert score(scen([exp(20, 26)]), [fnd(17)]).matched == 1
    assert score(scen([exp(20, 26)]), [fnd(16)]).matched == 0
    assert score(scen([exp(20, 26)]), [fnd(23)]).matched == 1


def test_rule2_wrong_file_does_not_match():
    r = score(scen([exp(10)]), [fnd(10, file="app/other.py")])
    assert r.matched == 0
    assert r.missed == 1
    assert r.false_positives == 1


def test_rule2_wrong_rule_does_not_match():
    r = score(scen([exp(10)]), [fnd(10, rule="no-pii-in-logs")])
    assert r.matched == 0
    assert r.missed == 1
    assert r.false_positives == 1  # a valid-policy block finding that matches nothing


def test_rule2_exact_line_match_is_a_pass():
    r = score(scen([exp(10)]), [fnd(10)])
    assert (r.matched, r.missed, r.false_positives, r.severity_correct, r.passed) == (1, 0, 0, 1, True)


def test_rule2_severity_of_finding_does_not_affect_matching():
    r = score(scen([exp(10, sev="block")]), [fnd(10, sev="nit")])
    assert r.matched == 1


# --- Rule 3: one-to-one, closest wins, duplicates ignored --------------------------------------------------


def test_rule3_closest_finding_wins_when_two_candidates():
    # Candidates at 8 (distance 2) and 11 (distance 1). The closer one is paired; severity shows which.
    r = score(scen([exp(10, sev="block")]), [fnd(8, sev="warn"), fnd(11, sev="block")])
    assert r.matched == 1
    assert r.severity_correct == 1  # paired with the block finding at line 11
    assert r.false_positives == 0  # the other is a duplicate, ignored
    assert r.passed is True


def test_rule3_closest_wins_regardless_of_finding_order():
    r = score(scen([exp(10, sev="block")]), [fnd(11, sev="block"), fnd(8, sev="warn")])
    assert r.severity_correct == 1 and r.false_positives == 0 and r.passed is True


def test_rule3_distance_inside_range_is_zero():
    # Multi-line issue 10..14: line 12 is inside (distance 0) and beats line 15 (distance 1).
    r = score(scen([exp(10, 14, sev="block")]), [fnd(15, sev="warn"), fnd(12, sev="block")])
    assert r.severity_correct == 1 and r.passed is True


def test_rule3_distance_measured_to_nearest_end_of_range():
    # Issue 10..20. Finding at 22 is distance 2 from line_end; finding at 8 is distance 2 from line_start.
    # Finding at 21 (distance 1) must beat both.
    r = score(scen([exp(10, 20, sev="block")]), [fnd(8, sev="warn"), fnd(22, sev="warn"), fnd(21, sev="block")])
    assert r.matched == 1 and r.severity_correct == 1 and r.false_positives == 0


def test_rule3_exact_duplicate_findings_are_ignored_not_penalised():
    r = score(scen([exp(10)]), [fnd(10), fnd(10)])
    assert (r.matched, r.missed, r.false_positives) == (1, 0, 0)
    assert r.passed is True


def test_rule3_many_duplicates_with_mixed_severities_are_all_ignored():
    r = score(
        scen([exp(10)]),
        [fnd(10), fnd(9, sev="warn"), fnd(12, sev="nit"), fnd(13, sev="block")],
    )
    assert (r.matched, r.false_positives) == (1, 0)
    assert r.passed is True


def test_rule3_duplicate_inside_window_of_matched_issue_is_not_a_false_positive():
    r = score(scen([exp(10)]), [fnd(10), fnd(7)])
    assert r.false_positives == 0 and r.passed is True


def test_rule3_finding_outside_window_is_not_a_duplicate():
    # Line 20 is far from the matched issue at 10, so it is a genuine unmatched block finding.
    r = score(scen([exp(10)]), [fnd(10), fnd(20)])
    assert (r.matched, r.false_positives) == (1, 1)
    assert r.passed is False


def test_rule3_duplicate_of_different_file_is_not_a_duplicate():
    r = score(scen([exp(10)]), [fnd(10), fnd(10, file="app/other.py")])
    assert r.false_positives == 1


def test_rule3_one_finding_cannot_satisfy_two_expected_issues():
    # Two issues of the same rule 4 lines apart; a single finding at 12 is inside both windows.
    r = score(scen([exp(10), exp(14)]), [fnd(12)])
    assert r.matched == 1
    assert r.missed == 1
    assert r.passed is False


def test_rule3_two_expected_one_finding_tie_goes_to_lower_expected_index():
    # Finding at 12 is distance 2 from both. The tie goes to expected[0] (warn); the finding is warn.
    # If it were paired with expected[1] (block) severity_correct would be 0 instead.
    r = score(scen([exp(10, sev="warn"), exp(14, sev="block")]), [fnd(12, sev="warn")])
    assert r.matched == 1 and r.missed == 1
    assert r.severity_correct == 1


def test_rule3_two_expected_one_finding_closer_expected_wins():
    # Finding at 13: distance 3 from expected[0] (line 10), distance 1 from expected[1] (line 14).
    r = score(scen([exp(10, sev="warn"), exp(14, sev="block")]), [fnd(13, sev="block")])
    assert r.matched == 1 and r.missed == 1
    assert r.severity_correct == 1  # paired with the block issue at 14


def test_rule3_tie_between_findings_goes_to_lower_finding_index():
    # Findings at 8 and 12 are both distance 2 from the issue at 10. The first listed is paired.
    first_warn = score(scen([exp(10, sev="block")]), [fnd(8, sev="warn"), fnd(12, sev="block")])
    assert first_warn.matched == 1 and first_warn.severity_correct == 0
    first_block = score(scen([exp(10, sev="block")]), [fnd(8, sev="block"), fnd(12, sev="warn")])
    assert first_block.matched == 1 and first_block.severity_correct == 1


def test_rule3_global_resolution_lets_both_issues_match():
    # Issues at 10 and 12. f0@11 is distance 1 from both, f1@13 is distance 1 from the second only.
    # Ascending distance, ties by expected index then finding index: (A,f0), then (B,f1). Both matched.
    r = score(scen([exp(10), exp(12)]), [fnd(11), fnd(13)])
    assert (r.matched, r.missed, r.false_positives) == (2, 0, 0)
    assert r.passed is True


def test_rule3_global_resolution_prefers_the_smaller_distance_over_expected_order():
    # Issues A@10 and B@12. f0@13 (A:3, B:1) and f1@8 (A:2, B:out of window).
    # Smallest distance first: (B,f0)=1, then (A,f1)=2. Both matched.
    r = score(scen([exp(10), exp(12)]), [fnd(13), fnd(8)])
    assert (r.matched, r.missed, r.false_positives) == (2, 0, 0)


def test_rule3_two_distinct_issues_each_match_their_own_finding():
    r = score(scen([exp(10), exp(40, rule="no-pii-in-logs", sev="warn")]),
              [fnd(10), fnd(41, rule="no-pii-in-logs", sev="warn")])
    assert (r.matched, r.missed, r.false_positives, r.severity_correct) == (2, 0, 0, 2)
    assert r.passed is True


# --- Rule 4: false positives ------------------------------------------------------------------------------


def test_rule4_unmatched_block_finding_is_false_positive():
    r = score(scen([]), [fnd(30, sev="block")])
    assert r.false_positives == 1
    assert r.passed is False


def test_rule4_unmatched_warn_finding_is_false_positive():
    r = score(scen([]), [fnd(30, sev="warn")])
    assert r.false_positives == 1
    assert r.passed is False


def test_rule4_unmatched_nit_is_ignored():
    r = score(scen([exp(10)]), [fnd(10), fnd(50, sev="nit")])
    assert r.false_positives == 0
    assert r.passed is True


def test_rule4_nits_on_a_clean_diff_still_pass():
    r = score(scen([]), [fnd(3, sev="nit"), fnd(9, rule="no-pii-in-logs", sev="nit")])
    assert (r.matched, r.missed, r.false_positives) == (0, 0, 0)
    assert r.passed is True


def test_rule4_clean_diff_with_no_findings_passes():
    r = score(scen([]), [])
    assert (r.matched, r.missed, r.false_positives, r.severity_correct) == (0, 0, 0, 0)
    assert r.passed is True
    assert r.error is None


def test_rule4_must_not_flag_hit_fails_and_counts_as_false_positive():
    r = score(scen([], must_not_flag=["no-raw-sql"]), [fnd(10, sev="block")])
    assert r.false_positives == 1
    assert r.passed is False


def test_rule4_must_not_flag_hit_counts_even_as_a_nit():
    r = score(scen([], must_not_flag=["no-raw-sql"]), [fnd(10, sev="nit")])
    assert r.false_positives == 1
    assert r.passed is False


def test_rule4_must_not_flag_each_finding_is_counted_once():
    r = score(scen([], must_not_flag=["no-raw-sql"]), [fnd(10, sev="block"), fnd(11, sev="warn")])
    assert r.false_positives == 2


def test_rule4_must_not_flag_finding_is_not_double_counted_as_unmatched_block():
    # It is both an unmatched block finding and a must_not_flag hit: still one false positive.
    r = score(scen([exp(40, rule="no-pii-in-logs")], must_not_flag=["no-raw-sql"]),
              [fnd(10, rule="no-raw-sql", sev="block"), fnd(40, rule="no-pii-in-logs")])
    assert r.false_positives == 1
    assert r.matched == 1


def test_rule4_must_not_flag_does_not_penalise_other_rules():
    r = score(scen([exp(40, rule="no-pii-in-logs")], must_not_flag=["no-raw-sql"]),
              [fnd(40, rule="no-pii-in-logs")])
    assert r.false_positives == 0 and r.passed is True


def test_rule4_unknown_rule_that_is_also_must_not_flag_is_one_false_positive():
    r = score(scen([], must_not_flag=["bogus"]), [fnd(10, rule="bogus", sev="nit")])
    assert r.false_positives == 1


# --- Rule 5: severity accuracy is measured on matched pairs only ------------------------------------------


def test_rule5_severity_correct_counts_matched_pairs_with_expected_severity():
    r = score(
        scen([exp(10, sev="block"), exp(40, rule="no-pii-in-logs", sev="warn")]),
        [fnd(10, sev="block"), fnd(40, rule="no-pii-in-logs", sev="block")],
    )
    assert r.matched == 2
    assert r.severity_correct == 1


def test_rule5_unmatched_findings_do_not_contribute_to_severity_correct():
    r = score(scen([exp(10, sev="block")]), [fnd(10, sev="block"), fnd(90, sev="block"), fnd(95, sev="nit")])
    assert r.matched == 1
    assert r.severity_correct == 1


def test_rule5_severity_mismatch_is_matched_but_not_passed():
    r = score(scen([exp(10, sev="block")]), [fnd(10, sev="warn")])
    assert r.matched == 1
    assert r.missed == 0
    assert r.false_positives == 0
    assert r.severity_correct == 0
    assert r.passed is False


def test_rule5_no_matches_means_no_severity_credit():
    r = score(scen([exp(10)]), [])
    assert r.severity_correct == 0


# --- Rule 6: pass criteria ---------------------------------------------------------------------------------


def test_rule6_passes_when_everything_matched_with_right_severity_and_no_false_positives():
    r = score(scen([exp(10), exp(40, rule="no-pii-in-logs", sev="warn")]),
              [fnd(10), fnd(40, rule="no-pii-in-logs", sev="warn")])
    assert r.passed is True


def test_rule6_fails_when_an_expected_issue_is_missed():
    r = score(scen([exp(10), exp(40, rule="no-pii-in-logs", sev="warn")]), [fnd(10)])
    assert (r.matched, r.missed) == (1, 1)
    assert r.passed is False


def test_rule6_fails_when_there_is_a_false_positive():
    r = score(scen([exp(10)]), [fnd(10), fnd(80, rule="needs-auth", sev="warn")])
    assert r.matched == 1 and r.false_positives == 1
    assert r.passed is False


def test_rule6_fails_when_a_matched_severity_is_wrong():
    r = score(scen([exp(10, sev="warn")]), [fnd(10, sev="block")])
    assert r.passed is False


def test_rule6_no_findings_on_a_scenario_with_expected_issues_fails():
    r = score(scen([exp(10), exp(20)]), [])
    assert (r.matched, r.missed, r.false_positives) == (0, 2, 0)
    assert r.passed is False


def test_rule6_clean_diff_with_a_block_finding_fails():
    r = score(scen([]), [fnd(5)])
    assert r.passed is False


# --- Result bookkeeping ------------------------------------------------------------------------------------


def test_result_carries_scenario_id_iteration_trace_id_and_findings():
    findings = [fnd(10), fnd(11, sev="nit")]
    r = score(scen([exp(10)]), findings, iteration=3, trace_id="trace-abc")
    assert r.scenario_id == "s1"
    assert r.iteration == 3
    assert r.trace_id == "trace-abc"
    assert r.findings == findings
    assert r.error is None


def test_result_trace_id_defaults_to_empty_string():
    assert score(scen([]), []).trace_id == ""


def test_missed_equals_expected_minus_matched():
    r = score(scen([exp(10), exp(20), exp(30)]), [fnd(20)])
    assert r.matched == 1
    assert r.missed == 2


# --- Rule 8: runtime error or unparseable output fails the scenario and logs `error` ----------------------


def test_rule8_runtime_error_fails_the_scenario_and_sets_error():
    r = score(scen([exp(10), exp(20)]), None, error="reviewer crashed: timeout", iteration=2, trace_id="t9")
    assert r.passed is False
    assert r.error == "reviewer crashed: timeout"
    assert r.findings == []
    assert (r.matched, r.missed, r.false_positives, r.severity_correct) == (0, 2, 0, 0)
    assert r.iteration == 2 and r.trace_id == "t9"


def test_rule8_error_on_a_clean_diff_still_fails():
    r = score(scen([]), None, error="boom")
    assert r.passed is False
    assert r.error == "boom"
    assert r.missed == 0


def test_rule8_error_discards_any_findings_that_were_supplied():
    r = score(scen([exp(10)]), [fnd(10)], error="partial output then crash")
    assert r.passed is False
    assert r.error == "partial output then crash"
    assert r.findings == []
    assert (r.matched, r.missed, r.false_positives, r.severity_correct) == (0, 1, 0, 0)


def test_rule8_no_findings_and_no_error_reports_no_output():
    r = score(scen([exp(10)]), None)
    assert r.passed is False
    assert r.error == "no output"
    assert r.findings == []
    assert r.missed == 1


def test_rule8_unparseable_output_is_logged_as_an_error_failure():
    # The runner's job: catch UnparseableOutput and pass its text as `error`.
    try:
        parse_findings("this is not json at all")
    except UnparseableOutput as exc:
        r = score(scen([exp(10)]), None, error=str(exc))
    else:  # pragma: no cover
        pytest.fail("expected UnparseableOutput")
    assert r.passed is False
    assert r.error
    assert r.missed == 1


# --- parse_findings ----------------------------------------------------------------------------------------

GOOD = {"file": FILE, "line": 10, "rule_id": "no-raw-sql", "severity": "block", "message": "raw sql"}


def test_unparseable_output_is_a_value_error():
    assert issubclass(UnparseableOutput, ValueError)


def test_parse_accepts_json_string_list():
    out = parse_findings(json.dumps([GOOD]))
    assert out == [Finding(**GOOD)]


def test_parse_accepts_json_string_object_with_findings_key():
    out = parse_findings(json.dumps({"findings": [GOOD, GOOD]}))
    assert len(out) == 2 and all(isinstance(f, Finding) for f in out)


def test_parse_accepts_fenced_json_block():
    raw = "```json\n" + json.dumps([GOOD]) + "\n```"
    assert parse_findings(raw) == [Finding(**GOOD)]


def test_parse_accepts_python_list_of_dicts():
    assert parse_findings([GOOD]) == [Finding(**GOOD)]


def test_parse_accepts_python_dict_with_findings_key():
    assert parse_findings({"findings": [GOOD]}) == [Finding(**GOOD)]


def test_parse_empty_list_means_no_findings():
    assert parse_findings([]) == []
    assert parse_findings("[]") == []
    assert parse_findings({"findings": []}) == []


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not json",
        "[{",
        "```json\n{broken\n```",
        "42",
        '"just a string"',
        "null",
        {"results": [GOOD]},
        {"findings": "nope"},
        [1, 2, 3],
        ["a string"],
        [{"file": FILE}],
        [{**GOOD, "severity": "critical"}],
        [{**GOOD, "line": "ten"}],
        [GOOD, {"file": FILE}],
    ],
    ids=lambda v: repr(v)[:30],
)
def test_unparseable_output_is_rejected(raw):
    with pytest.raises(UnparseableOutput):
        parse_findings(raw)
