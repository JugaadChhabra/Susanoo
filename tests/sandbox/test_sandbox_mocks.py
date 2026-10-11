"""Level 1 mocked tools and world state. [PRD › Testing environments]"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
from pathlib import Path

import pytest

from susanoo.sandbox import mocks
from susanoo.schemas import AnswerKey, Scenario

DIFF = (
    "diff --git a/app/db.py b/app/db.py\n"
    "--- a/app/db.py\n+++ b/app/db.py\n@@ -10,3 +10,4 @@\n"
    " def f():\n     x = 1\n+    log.info(user.email)\n     return x\n"
)
POLICY = """rules:
  - id: no-pii-logs
    text: Never log personal data.
    default_severity: block
  - id: no-raw-sql
    text: Use the query builder.
    default_severity: warn
"""


def scenario(sid: str = "s1", split: str = "train", diff: str = DIFF) -> Scenario:
    return Scenario(id=sid, kind="planted", split=split, title="t", diff=diff, answer_key=AnswerKey(expected=[]))


def write_world(repo, sc: Scenario, data) -> None:
    path = repo / "scenarios" / sc.split
    path.mkdir(parents=True, exist_ok=True)
    (path / f"{sc.id}.world.json").write_text(data if isinstance(data, str) else json.dumps(data))


def toolbox(world: mocks.WorldState | None = None, **kw) -> mocks.MockTools:
    world = world or mocks.WorldState(files={"app/db.py": "\n".join(f"line {n}" for n in range(1, 21))})
    return mocks.MockTools(diff=DIFF, world=world, rules=mocks.rules_from_policy(POLICY), **kw)


# ------------------------------------------------------------------------------------------ world loading


def test_load_world_reads_file(tmp_path):
    sc = scenario()
    write_world(tmp_path, sc, {"files": {"app/db.py": "a\nb"}, "notes": "hello"})
    world = mocks.load_world(tmp_path, sc)
    assert world.files == {"app/db.py": "a\nb"}
    assert world.notes == "hello"


def test_load_world_uses_the_scenarios_split(tmp_path):
    sc = scenario(split="heldout")
    write_world(tmp_path, sc, {"files": {"x.py": "1"}})
    assert mocks.load_world(tmp_path, sc).files == {"x.py": "1"}
    assert mocks.world_path(tmp_path, sc).parent.name == "heldout"


def test_missing_world_falls_back_to_added_lines_with_warning(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        world = mocks.load_world(tmp_path, scenario())
    assert "No world file" in caplog.text
    lines = world.files["app/db.py"].split("\n")
    assert lines[11] == "    log.info(user.email)"  # new-file line 12
    assert lines[:11] == [""] * 11  # context lines are not invented
    assert len(lines) == 12


@pytest.mark.parametrize("bad", ["not json", '{"files": ["wrong"]}'])
def test_unreadable_world_falls_back(tmp_path, caplog, bad):
    sc = scenario()
    write_world(tmp_path, sc, bad)
    with caplog.at_level(logging.WARNING):
        world = mocks.load_world(tmp_path, sc)
    assert "Unreadable world file" in caplog.text
    assert "wrong" not in caplog.text  # file content never reaches the log
    assert "app/db.py" in world.files


@pytest.mark.parametrize("sid", ["../evil", "a/b", "", ".hidden/..", "x" * 101])
def test_unsafe_scenario_id_is_rejected(tmp_path, sid):
    with pytest.raises(ValueError):
        mocks.load_world(tmp_path, scenario(sid=sid))


# ----------------------------------------------------------------------------------------------- the tools


def test_parse_diff_numbers_new_file_lines():
    out = toolbox().call_tool("parse_diff", {})
    assert "=== FILE: app/db.py ===" in out
    assert "    12 +     log.info(user.email)" in out
    assert "    13       return x" in out


def test_parse_diff_handles_removed_lines():
    diff = "--- a/f.py\n+++ b/f.py\n@@ -1,2 +1,2 @@\n-old\n+new\n keep\n"
    out = mocks.annotate_diff(diff)
    assert "     - old" in out
    assert "     1 + new" in out
    assert "     2   keep" in out


def test_parse_diff_file_filter_and_not_shown():
    t = toolbox()
    assert t.call_tool("parse_diff", {"file": "app/db.py"}) == t.call_tool("parse_diff", {})
    assert t.call_tool("parse_diff", {"file": "nope.py"}) == "Not shown in this change."


def test_get_file_context_answers_from_the_diff_not_the_world():
    t = toolbox()  # the world has 20 lines "line N"; the diff shows only lines 10-13
    out = t.call_tool("get_file_context", {"file": "app/db.py", "line": 12})
    assert "log.info(user.email)" in out and "line 12" not in out
    assert t.call_tool("get_file_context", {"file": "app/db.py", "line": 1, "radius": 0}) == (
        "Not shown in this change."
    )
    assert t.call_tool("get_file_context", {"file": "other.py", "line": 12}) == "Not shown in this change."


def test_get_file_context_bad_args_are_error_strings():
    t = toolbox()
    for args in ({}, {"file": "app/db.py"}, {"file": "app/db.py", "line": "12"}, {"file": 3, "line": 1}):
        assert t.call_tool("get_file_context", args).startswith(mocks.TOOL_ERROR_PREFIX)


def test_lookup_rule_found_and_unknown():
    t = toolbox()
    assert json.loads(t.call_tool("lookup_rule", {"rule_id": "no-pii-logs"})) == {
        "id": "no-pii-logs",
        "text": "Never log personal data.",
        "default_severity": "block",
    }
    assert (
        t.call_tool("lookup_rule", {"rule_id": "made-up"}) == "No rule with that id. Valid ids: no-pii-logs, no-raw-sql"
    )


def test_dispatcher_unknown_tool_and_bad_args_do_not_raise():
    t = toolbox()
    assert t.call_tool("rm_rf", {"x": 1}) == (
        "Error: unknown tool 'rm_rf'. Available tools: parse_diff, get_file_context, lookup_rule"
    )
    assert "unknown tool" in t.call_tool("", None)  # type: ignore[arg-type]
    assert t.call_tool("lookup_rule", None).startswith("No rule")  # type: ignore[arg-type]


def test_side_effect_tools_are_dry_run():
    t = toolbox(side_effect_tools=["post_comment"])
    out = t.call_tool("post_comment", {"body": "hi"})
    assert out.startswith("Dry run: would have run post_comment(body='hi')")


def test_schemas_export_and_copies():
    t = toolbox()
    assert [s["name"] for s in t.schemas] == ["parse_diff", "get_file_context", "lookup_rule"]
    t.schemas[0]["input_schema"]["properties"].clear()  # callers get deep copies
    assert "file" in mocks.TOOL_SCHEMAS[0]["input_schema"]["properties"]
    assert mocks.MAX_TOOL_ROUNDS == 6 and mocks.TOOL_ERROR_PREFIX == "Error: "


def test_rules_from_policy_rejects_malformed():
    assert mocks.rules_from_policy("") == []
    with pytest.raises(ValueError):
        mocks.rules_from_policy("rules: [1, 2]")
    with pytest.raises(ValueError):
        mocks.rules_from_policy("rules: [unclosed")


def test_rules_from_policy_rejects_duplicate_ids():
    dup = POLICY + "  - id: no-raw-sql\n    text: again\n    default_severity: warn\n"
    with pytest.raises(ValueError, match="duplicate"):
        mocks.rules_from_policy(dup)


def test_build_mock_tools_reads_policy_and_world_as_text(tmp_path):
    sc = scenario()
    write_world(tmp_path, sc, {"files": {"app/db.py": "a\nb\nc"}})
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "policy.yaml").write_text(POLICY)
    # A tools.py that would blow up if it were ever imported or executed.
    (tmp_path / "agent" / "tools.py").write_text("raise RuntimeError('generated code ran on the host')\n")
    t = mocks.build_mock_tools(tmp_path, sc)
    assert "no-raw-sql" in t.call_tool("lookup_rule", {"rule_id": "no-raw-sql"})
    assert t.world.files["app/db.py"] == "a\nb\nc"  # loaded as data, but the three tools answer from the diff
    assert "No rule with that id" not in t.call_tool("lookup_rule", {"rule_id": "no-pii-logs"})


def test_build_mock_tools_without_policy_file(tmp_path):
    t = mocks.build_mock_tools(tmp_path, scenario())
    assert t.call_tool("lookup_rule", {"rule_id": "x"}) == "No rule with that id. Valid ids: "


# ------------------------------------------------------------------------------- path escape / error text


def test_world_symlink_outside_repo_is_refused(tmp_path, caplog):
    repo, outside = tmp_path / "repo", tmp_path / "outside"
    outside.mkdir()
    sc = scenario()
    (outside / "w.json").write_text(json.dumps({"files": {"stolen.py": "secret"}}))
    (repo / "scenarios" / "train").mkdir(parents=True)
    (repo / "scenarios" / "train" / "s1.world.json").symlink_to(outside / "w.json")
    with caplog.at_level(logging.WARNING):
        world = mocks.load_world(repo, sc)
    assert "stolen.py" not in world.files and "outside the repo" in caplog.text


def test_world_symlinked_dir_outside_repo_is_refused(tmp_path):
    repo, outside = tmp_path / "repo", tmp_path / "outside"
    (outside / "train").mkdir(parents=True)
    (outside / "train" / "s1.world.json").write_text(json.dumps({"files": {"stolen.py": "x"}}))
    repo.mkdir()
    (repo / "scenarios").symlink_to(outside)
    assert "stolen.py" not in mocks.load_world(repo, scenario()).files


def test_policy_symlink_outside_repo_is_refused(tmp_path):
    repo = tmp_path / "repo"
    (repo / "agent").mkdir(parents=True)
    (tmp_path / "other.yaml").write_text(POLICY)
    (repo / "agent" / "policy.yaml").symlink_to(tmp_path / "other.yaml")
    with pytest.raises(ValueError, match="outside the generated repo"):
        mocks.build_mock_tools(repo, scenario())


def test_policy_errors_do_not_quote_content():
    with pytest.raises(ValueError) as exc:
        mocks.rules_from_policy("rules: [unclosed TOPSECRETVALUE")
    assert "TOPSECRETVALUE" not in str(exc.value) and exc.value.__cause__ is None and exc.value.__suppress_context__
    with pytest.raises(ValueError) as exc:
        mocks.rules_from_policy("rules:\n  - id: TOPSECRETVALUE\n")
    assert "TOPSECRETVALUE" not in str(exc.value)


# ------------------------------------------------------------- parity with the template agent's own parser

TEMPLATE_ROOT = Path(__file__).resolve().parents[2] / "templates" / "app-template"

PARITY_CASES = {
    "simple": "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,3 @@\n a\n+b\n c\n",
    "no_git_header_two_files": (
        "--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,2 @@\n a\n+b\n--- a/y.py\n+++ b/y.py\n@@ -1,1 +1,2 @@\n c\n+d\n"
    ),
    "trailing_junk": (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,2 @@\n a\n+b\nindex 123..456\nsome prose\n"
    ),
    "deleted_file": (
        "diff --git a/x.py b/x.py\ndeleted file mode 100644\n--- a/x.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-a\n-b\n"
    ),
    "dir_with_dot": "--- a/./x.py\n+++ b/./x.py\n@@ -1 +1,2 @@\n a\n+b\n",
    "crlf": "diff --git a/x.py b/x.py\r\n--- a/x.py\r\n+++ b/x.py\r\n@@ -1,2 +1,3 @@\r\n a\r\n+b\r\n c\r\n",
    "multi_hunk": (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,3 @@\n a\n+b\n c\n"
        "@@ -20,2 +21,3 @@\n m\n-n\n+o\n+p\n q\n"
    ),
    "plusplus_inside_hunk_body": "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,2 @@\n a\n+++ counter\n",
    "dashes_inside_hunk_body": (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,1 @@\n--- old comment\n a\n"
    ),
    "new_file": (
        "diff --git a/n.py b/n.py\nnew file mode 100644\n--- /dev/null\n+++ b/n.py\n@@ -0,0 +1,2 @@\n+one\n+two\n"
    ),
    "wrong_counts": "--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,1 @@\n a\n+b\n+c\n c\n",
    "no_newline_marker": "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n\\ No newline at end of file\n+b\n",
    "quoted_paths": 'diff --git "a/x y.py" "b/x y.py"\n--- "a/x y.py"\n+++ "b/x y.py"\n@@ -1 +1,2 @@\n a\n+b\n',
    "git_rename": (
        "diff --git a/o.py b/n.py\nsimilarity index 90%\nrename from o.py\nrename to n.py\n"
        "--- a/o.py\n+++ b/n.py\n@@ -1 +1,2 @@\n a\n+b\n"
    ),
    "empty": "",
    "prose_only": "just some words\nno diff here\n",
}


def load_template_tools():
    spec = importlib.util.spec_from_file_location("template_agent_tools_parity", TEMPLATE_ROOT / "agent" / "tools.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look the module up by name
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def parity_diffs() -> list[tuple[str, str]]:
    seeds = sorted((TEMPLATE_ROOT / "scenarios" / "seeds").glob("*.json"))
    assert len(seeds) >= 10
    return [(p.name, json.loads(p.read_text())["diff"]) for p in seeds] + list(PARITY_CASES.items())


@pytest.mark.parametrize("name,diff", parity_diffs(), ids=[n for n, _ in parity_diffs()])
def test_parse_and_annotate_are_identical_to_the_template(name, diff):
    tools = load_template_tools()
    theirs = tools.parse_diff(diff)
    mine = mocks.parse_diff(diff)
    assert [(f.path, [(ln.kind, ln.text, ln.new_line) for ln in f.lines]) for f in mine] == [
        (f.path, [(ln.kind, ln.text, ln.new_line) for ln in f.lines]) for f in theirs
    ]
    assert mocks.annotate_diff(diff) == tools.annotate_diff(theirs)


def test_two_files_with_only_dash_headers_are_not_mixed_up():
    out = mocks.annotate_diff(PARITY_CASES["no_git_header_two_files"])
    assert out.index("=== FILE: x.py") < out.index("=== FILE: y.py")
    assert "--- a/y.py" not in out and "+++ b/y.py" not in out
    assert "     2 + d" in out.split("=== FILE: y.py ===")[1]


# -------------------------------------------------------------- call_tool parity with the template, byte for byte


def arg_spread(diff: str) -> list[tuple[str, dict | None]]:
    files = [f.path for f in mocks.parse_diff(diff)]
    first = files[0] if files else "x.py"
    lines = [ln.new_line for f in mocks.parse_diff(diff) for ln in f.lines if ln.new_line][:3] or [1]
    calls: list[tuple[str, dict | None]] = [
        ("parse_diff", {}),
        ("parse_diff", None),
        ("parse_diff", {"file": first}),
        ("parse_diff", {"file": "nope.py"}),
        ("parse_diff", {"file": ""}),
        ("parse_diff", {"file": 5}),
        ("get_file_context", {}),
        ("get_file_context", {"file": first}),
        ("get_file_context", {"file": first, "line": "3"}),
        ("get_file_context", {"file": first, "line": True}),
        ("get_file_context", {"file": 7, "line": 1}),
        ("get_file_context", {"file": first, "line": 999}),
        ("get_file_context", {"file": first, "line": -4}),
        ("get_file_context", {"file": "nope.py", "line": 1}),
        ("get_file_context", {"file": first, "line": 1, "radius": 100}),
        ("get_file_context", {"file": first, "line": 1, "radius": -3}),
        ("get_file_context", {"file": first, "line": 1, "radius": "2"}),
        ("get_file_context", {"file": first, "line": 1, "radius": True}),
        ("lookup_rule", {}),
        ("lookup_rule", {"rule_id": "nope"}),
        ("lookup_rule", {"rule_id": 5}),
        ("lookup_rule", None),
        ("nope", {}),
        ("", None),
    ]
    for n in lines:
        calls += [
            ("get_file_context", {"file": first, "line": n}),
            ("get_file_context", {"file": first, "line": n, "radius": 0}),
            ("get_file_context", {"file": first, "line": n, "radius": 20}),
        ]
    for f in files:
        calls += [("parse_diff", {"file": f}), ("get_file_context", {"file": f, "line": lines[0]})]
    return calls


def test_tool_schemas_and_constants_equal_the_templates():
    tools = load_template_tools()
    assert mocks.TOOL_SCHEMAS == tools.TOOL_SCHEMAS
    assert mocks.MAX_TOOL_ROUNDS == tools.MAX_TOOL_ROUNDS
    assert mocks.TOOL_ERROR_PREFIX == tools.TOOL_ERROR_PREFIX


@pytest.mark.parametrize("name,diff", parity_diffs(), ids=[n for n, _ in parity_diffs()])
def test_call_tool_is_byte_identical_to_the_template(name, diff):
    tools = load_template_tools()
    policy_text = (TEMPLATE_ROOT / "agent" / "policy.yaml").read_text()
    theirs_rules = tools.load_rules(TEMPLATE_ROOT / "agent" / "policy.yaml")
    mine_rules = mocks.rules_from_policy(policy_text)
    ids = [r.id for r in mine_rules]
    spread = arg_spread(diff) + [("lookup_rule", {"rule_id": rid}) for rid in ids]
    assert ids  # the template ships real rules
    for tool, args in spread:
        expected = tools.call_tool(tool, args, diff=diff, policy=theirs_rules)
        assert mocks.call_tool(tool, args, diff=diff, policy=mine_rules) == expected, (tool, args)
        toolbox_out = mocks.MockTools(diff=diff, world=mocks.WorldState(), rules=mine_rules).call_tool(tool, args)
        assert toolbox_out == expected, (tool, args)
