"""Checks the 10 hand-written seed scenarios.

Parses each diff on its own (no help from whatever generated it) and verifies that every
answer-key line number lands on an added line containing the code it is meant to flag.
"""

import json
import re
from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict

SEEDS = Path(__file__).resolve().parents[2] / "templates/app-template/scenarios/seeds"

# Mirror of [PRD › Contracts (locked)], stricter (extra="forbid") to catch typos.
# TODO: import from susanoo.schemas once the shared schemas land.
Severity = Literal["block", "warn", "nit"]


class ExpectedIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file: str
    line_start: int
    line_end: int
    rule_id: str
    severity: Severity


class AnswerKey(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected: list[ExpectedIssue]
    must_not_flag: list[str] = []


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    kind: Literal["planted", "edge", "adversarial", "clean"]
    split: Literal["train", "heldout", "acceptance"]
    title: str
    diff: str
    answer_key: AnswerKey
    approved_by_user: bool = False


# The five hero rules ([PRD › Hero use case]). `external-call-errors` is our id for
# "flag missing error handling on external calls"; the PRD names no id for it.
RULE_IDS = {"no-pii-logs", "auth-required", "no-raw-sql", "no-secrets", "external-call-errors"}

# For each expected issue (in answer-key order): text that must appear on the flagged line.
MARKERS = {
    "seed-01-raw-sql-fstring": ["f\"SELECT * FROM orders WHERE customer = '{customer_name}'\""],
    "seed-02-pii-in-logs": ["user.email"],
    "seed-03-hardcoded-secret": ['API_KEY = "pay_live_'],
    "seed-04-missing-auth": ["def delete_report(report_id: int):"],
    "seed-05-no-error-handling": ["requests.get(GEOCODER_URL"],
    "seed-07-adversarial-injection": ['logger.info("Updated phone'],
    "seed-10-two-problems-two-files": ["SMTP_PASSWORD =", "% domain"],
}

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_added_lines(diff: str) -> dict[str, dict[int, str]]:
    """Map file -> {new-file line number: text} for every added line.

    Hunks are consumed by their declared line counts, so a removed line that happens to
    start with "-- " (or similar) cannot be mistaken for a file header.
    """
    added: dict[str, dict[int, str]] = {}
    lines = diff.split("\n")
    path = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("+++ "):
            target = line[4:]
            path = None if target == "/dev/null" else target.removeprefix("b/")
            added.setdefault(path, {})
            i += 1
        elif (m := _HUNK.match(line)):
            old_left = int(m.group(2) or 1)
            new_left = int(m.group(4) or 1)
            new_no = int(m.group(3))
            i += 1
            while old_left > 0 or new_left > 0:
                body = lines[i]
                tag, text = body[:1], body[1:]
                if tag == "+":
                    added[path][new_no] = text
                    new_no += 1
                    new_left -= 1
                elif tag == "-":
                    old_left -= 1
                elif tag == " ":
                    new_no += 1
                    old_left -= 1
                    new_left -= 1
                else:
                    raise AssertionError(f"hunk ended early in {path}: {body!r}")
                i += 1
        else:
            i += 1
    return added


def load_all():
    return [(p.stem, Scenario.model_validate(json.loads(p.read_text()))) for p in sorted(SEEDS.glob("*.json"))]


SCENARIOS = load_all()


def test_there_are_ten_seeds_and_names_match_ids():
    assert len(SCENARIOS) == 10
    for stem, s in SCENARIOS:
        assert s.id == stem
    assert len({s.id for _, s in SCENARIOS}) == 10


def test_every_kind_in_the_scenario_table_is_covered():
    kinds = {s.kind for _, s in SCENARIOS}
    assert kinds == {"planted", "edge", "adversarial", "clean"}
    assert sum(s.kind == "clean" for _, s in SCENARIOS) >= 2


def test_every_hero_rule_has_a_planted_scenario():
    planted = {i.rule_id for _, s in SCENARIOS for i in s.answer_key.expected}
    assert planted == RULE_IDS


def test_edge_case_has_must_not_flag_and_no_expected_issues():
    edges = [s for _, s in SCENARIOS if s.kind == "edge"]
    assert edges
    for s in edges:
        assert s.answer_key.expected == []
        assert s.answer_key.must_not_flag


def test_clean_scenarios_expect_nothing():
    for _, s in SCENARIOS:
        if s.kind == "clean":
            assert s.answer_key.expected == []


def test_adversarial_diff_really_contains_an_injection_and_a_real_issue():
    (s,) = [s for _, s in SCENARIOS if s.kind == "adversarial"]
    assert "ignore all previous instructions" in s.diff.lower()
    assert s.answer_key.expected


@pytest.mark.parametrize(("stem", "s"), SCENARIOS, ids=[stem for stem, _ in SCENARIOS])
class TestEachScenario:
    def test_rule_ids_are_known(self, stem, s):
        used = {i.rule_id for i in s.answer_key.expected} | set(s.answer_key.must_not_flag)
        assert used <= RULE_IDS

    def test_title_is_plain_english(self, stem, s):
        assert 20 <= len(s.title) <= 160
        assert not any(rule in s.title for rule in RULE_IDS)
        assert "`" not in s.title

    def test_seeds_are_unapproved_training_cases(self, stem, s):
        assert s.split == "train"
        assert s.approved_by_user is False

    def test_diff_parses_and_has_added_lines(self, stem, s):
        added = parse_added_lines(s.diff)
        assert added
        assert all(lines for lines in added.values())

    def test_answer_key_lines_are_on_added_code_that_matches(self, stem, s):
        added = parse_added_lines(s.diff)
        expected = s.answer_key.expected
        markers = MARKERS.get(stem, [])
        assert len(markers) == len(expected), "every expected issue needs a marker"
        for issue, marker in zip(expected, markers, strict=True):
            assert issue.file in added, f"{issue.file} is not in the diff"
            assert 1 <= issue.line_start <= issue.line_end
            span = range(issue.line_start, issue.line_end + 1)
            missing = [n for n in span if n not in added[issue.file]]
            assert not missing, f"{issue.file} lines {missing} are not added lines"
            assert any(marker in added[issue.file][n] for n in span), (
                f"{marker!r} not found in {issue.file}:{issue.line_start}-{issue.line_end}"
            )


def test_parser_handles_removed_lines_that_look_like_headers():
    diff = (
        "diff --git a/x.sql b/x.sql\n--- a/x.sql\n+++ b/x.sql\n@@ -1,2 +1,2 @@\n"
        "--- a comment\n+-- b comment\n keep\n"
    )
    assert parse_added_lines(diff) == {"x.sql": {1: "-- b comment"}}


def test_parser_numbers_lines_across_multiple_hunks_and_files():
    diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        "@@ -1,2 +1,3 @@\n one\n+two\n three\n"
        "@@ -10 +11,2 @@\n ten\n+eleven\n"
        "diff --git a/b.py b/b.py\nnew file mode 100644\n--- /dev/null\n+++ b/b.py\n"
        "@@ -0,0 +1 @@\n+only\n"
    )
    assert parse_added_lines(diff) == {"a.py": {2: "two", 12: "eleven"}, "b.py": {1: "only"}}
