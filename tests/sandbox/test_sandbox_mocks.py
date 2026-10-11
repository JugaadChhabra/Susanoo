"""Level 1 mocked tools and world state. [PRD › Testing environments]"""

from __future__ import annotations

import json
import logging

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


def test_parse_diff_empty():
    tools = mocks.MockTools(diff="", world=mocks.WorldState())
    assert tools.call_tool("parse_diff", {}) == "The diff is empty."


def test_get_file_context_returns_numbered_lines():
    out = toolbox().call_tool("get_file_context", {"path": "app/db.py", "line_start": 2, "line_end": 4})
    assert out.split("\n") == ["     2 | line 2", "     3 | line 3", "     4 | line 4"]


def test_get_file_context_clamps_and_accepts_prefixed_path():
    out = toolbox().call_tool("get_file_context", {"path": "b/app/db.py", "line_start": -5, "line_end": 2})
    assert out.split("\n") == ["     1 | line 1", "     2 | line 2"]
    out = toolbox().call_tool("get_file_context", {"path": "app/db.py", "line_start": 19, "line_end": 999})
    assert out.split("\n") == ["    19 | line 19", "    20 | line 20"]


def test_get_file_context_out_of_range_and_bad_args():
    t = toolbox()
    assert "has 20 lines" in t.call_tool("get_file_context", {"path": "app/db.py", "line_start": 50, "line_end": 60})
    assert "whole numbers" in t.call_tool("get_file_context", {"path": "app/db.py", "line_start": "x", "line_end": 2})
    assert "whole numbers" in t.call_tool("get_file_context", {"path": "app/db.py", "line_start": True, "line_end": 2})


def test_get_file_context_unknown_path():
    out = toolbox().call_tool("get_file_context", {"path": "nope.py", "line_start": 1, "line_end": 2})
    assert out == "file not found in this scenario"


def test_get_file_context_caps_range():
    big = mocks.WorldState(files={"f.py": "\n".join(str(n) for n in range(1000))})
    out = toolbox(big).call_tool("get_file_context", {"path": "f.py", "line_start": 1, "line_end": 1000})
    assert len(out.split("\n")) == mocks.MAX_CONTEXT_LINES


def test_lookup_rule_found_and_unknown():
    t = toolbox()
    assert t.call_tool("lookup_rule", {"rule_id": "no-pii-logs"}) == (
        "no-pii-logs: Never log personal data. (default severity: block)"
    )
    out = t.call_tool("lookup_rule", {"rule_id": "made-up"})
    assert "No policy rule" in out and "no-pii-logs, no-raw-sql" in out


def test_dispatcher_unknown_tool_and_bad_args_do_not_raise():
    t = toolbox()
    assert "Unknown tool 'rm_rf'" in t.call_tool("rm_rf", {"x": 1})
    assert "Unknown tool" in t.call_tool("", None)  # type: ignore[arg-type]
    assert "No policy rule" in t.call_tool("lookup_rule", None)  # type: ignore[arg-type]


def test_side_effect_tools_are_dry_run():
    t = toolbox(side_effect_tools=["post_comment"])
    out = t.call_tool("post_comment", {"body": "hi"})
    assert out.startswith("Dry run: would have run post_comment(body='hi')")


def test_schemas_match_dispatcher_and_anthropic_shape():
    t = toolbox()
    names = {s["name"] for s in t.schemas}
    assert names == {"parse_diff", "get_file_context", "lookup_rule"}
    for s in t.schemas:
        assert set(s) == {"name", "description", "input_schema"}
        assert s["input_schema"]["type"] == "object"
        assert "Unknown tool" not in t.call_tool(s["name"], {})
    gfc = next(s for s in t.schemas if s["name"] == "get_file_context")
    assert gfc["input_schema"]["required"] == ["path", "line_start", "line_end"]
    t.schemas[0]["name"] = "mutated"  # callers get copies
    assert mocks.TOOL_SCHEMAS[0]["name"] == "parse_diff"


def test_rules_from_policy_rejects_malformed():
    assert mocks.rules_from_policy("") == []
    with pytest.raises(ValueError):
        mocks.rules_from_policy("rules: [1, 2]")
    with pytest.raises(ValueError):
        mocks.rules_from_policy("rules: [unclosed")


def test_build_mock_tools_reads_policy_and_world_as_text(tmp_path):
    sc = scenario()
    write_world(tmp_path, sc, {"files": {"app/db.py": "a\nb\nc"}})
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "policy.yaml").write_text(POLICY)
    # A tools.py that would blow up if it were ever imported or executed.
    (tmp_path / "agent" / "tools.py").write_text("raise RuntimeError('generated code ran on the host')\n")
    t = mocks.build_mock_tools(tmp_path, sc)
    assert "no-raw-sql" in t.call_tool("lookup_rule", {"rule_id": "no-raw-sql"})
    assert t.call_tool("get_file_context", {"path": "app/db.py", "line_start": 3, "line_end": 3}) == "     3 | c"


def test_build_mock_tools_without_policy_file(tmp_path):
    t = mocks.build_mock_tools(tmp_path, scenario())
    assert "Known rule ids: none" in t.call_tool("lookup_rule", {"rule_id": "x"})


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
