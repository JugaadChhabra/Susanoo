import json
from pathlib import Path

from agent.tools import annotate_diff, get_file_context, load_rules, lookup_rule, parse_diff
from tests.conftest import SQL_DIFF

SEEDS = Path(__file__).resolve().parent.parent / "scenarios" / "seeds"


def test_parse_diff_gives_new_file_line_numbers():
    (f,) = parse_diff(SQL_DIFF)
    assert f.path == "app/db/queries.py"
    added = {ln.new_line: ln.text for ln in f.added}
    assert sorted(added) == [7, 8, 9, 10, 11, 12]
    assert "customer_name" in added[11] and added[11].lstrip().startswith("query = f")


def test_parse_diff_multiple_files_and_removed_lines():
    diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,3 +1,3 @@\n x = 1\n-y = 2\n+y = 3\n z = 4\n"
        "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -10,2 +10,3 @@\n keep\n+new\n keep2\n"
    )
    a, b = parse_diff(diff)
    assert (a.path, b.path) == ("a.py", "b.py")
    assert [ln.new_line for ln in a.lines if ln.kind == "add"] == [2]
    assert [ln.new_line for ln in a.lines if ln.kind == "remove"] == [None]
    assert [ln.new_line for ln in b.lines if ln.kind == "add"] == [11]


def test_parse_diff_new_file_and_wrong_hunk_counts():
    diff = "diff --git a/n.py b/n.py\nnew file mode 100644\n--- /dev/null\n+++ b/n.py\n@@ -0,0 +1,1 @@\n+a\n+b\n+c\n"
    (f,) = parse_diff(diff)
    assert f.path == "n.py"
    assert [ln.new_line for ln in f.added] == [1, 2, 3]  # tolerates counts that undercount


def test_annotate_diff_prefixes_new_line_numbers():
    text = annotate_diff(parse_diff(SQL_DIFF))
    assert "=== FILE: app/db/queries.py ===" in text
    assert "    11 +     query = f" in text


def test_get_file_context_returns_nearby_lines():
    files = parse_diff(SQL_DIFF)
    ctx = get_file_context(files, "app/db/queries.py", 11, radius=1)
    assert [ln.new_line for ln in ctx] == [10, 11, 12]
    assert get_file_context(files, "nope.py", 1) == []


def test_policy_has_the_five_hero_rules():
    ids = {r.id for r in load_rules()}
    assert ids == {"no-pii-logs", "auth-required", "no-raw-sql", "no-secrets", "external-call-errors"}
    assert lookup_rule("no-raw-sql").default_severity == "block"
    assert lookup_rule("external-call-errors").default_severity == "warn"
    assert lookup_rule("made-up-rule") is None


def test_seed_answer_keys_agree_with_policy_and_parser():
    seeds = sorted(SEEDS.glob("*.json")) if SEEDS.exists() else []
    if not seeds:
        return
    for path in seeds:
        data = json.loads(path.read_text())
        files = {f.path: {ln.new_line for ln in f.lines if ln.new_line} for f in parse_diff(data["diff"])}
        for issue in data["answer_key"]["expected"]:
            rule = lookup_rule(issue["rule_id"])
            assert rule is not None, path.name
            assert rule.default_severity == issue["severity"], path.name
            lines = files[issue["file"]]
            assert issue["line_start"] in lines and issue["line_end"] in lines, path.name
