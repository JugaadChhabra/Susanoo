import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from agent.tools import MAX_TOOL_ROUNDS, TOOL_SCHEMAS, call_tool, load_rules
from backend.main import create_app
from backend.reviewer import Reviewer, ReviewFailed
from tests.conftest import (
    SQL_DIFF,
    SQL_FINDING,
    FakeClient,
    ScriptedClient,
    text_response,
    tool_response,
)

RULES = load_rules()
FINAL = text_response(json.dumps({"findings": [SQL_FINDING]}))


def run(coro):
    return asyncio.run(coro)


def test_contract_names_are_stable():
    assert MAX_TOOL_ROUNDS == 6
    assert [t["name"] for t in TOOL_SCHEMAS] == ["parse_diff", "get_file_context", "lookup_rule"]
    for t in TOOL_SCHEMAS:
        assert t["input_schema"]["type"] == "object" and t["description"]


def test_call_tool_answers_from_the_diff():
    args = {"file": "app/db/queries.py", "line": 11, "radius": 1}
    ctx = call_tool("get_file_context", args, diff=SQL_DIFF, policy=RULES)
    assert "query = f" in ctx and ctx.count("\n") == 2
    assert call_tool("get_file_context", {"file": "other.py", "line": 1}, diff=SQL_DIFF, policy=RULES) == (
        "Not shown in this change."
    )
    assert "=== FILE: app/db/queries.py ===" in call_tool("parse_diff", {}, diff=SQL_DIFF, policy=RULES)
    rule = json.loads(call_tool("lookup_rule", {"rule_id": "no-secrets"}, diff=SQL_DIFF, policy=RULES))
    assert rule["id"] == "no-secrets" and rule["default_severity"] == "block"
    assert "Valid ids" in call_tool("lookup_rule", {"rule_id": "zzz"}, diff=SQL_DIFF, policy=RULES)


def test_call_tool_never_raises():
    assert call_tool("rm_rf", {}, diff=SQL_DIFF, policy=RULES).startswith("Error: unknown tool")
    assert call_tool("get_file_context", {"file": 3}, diff=SQL_DIFF, policy=RULES).startswith("Error: ")
    assert call_tool("lookup_rule", None, diff=SQL_DIFF, policy=RULES).startswith("No rule")


def test_tool_round_trip():
    client = ScriptedClient(
        [
            tool_response(
                ("t1", "lookup_rule", {"rule_id": "no-raw-sql"}),
                ("t2", "get_file_context", {"file": "app/db/queries.py", "line": 11}),
            ),
            FINAL,
        ]
    )
    findings = run(Reviewer(client).review(SQL_DIFF))
    assert [f.rule_id for f in findings] == ["no-raw-sql"]
    first, second = client.messages.calls
    assert first["tools"] == TOOL_SCHEMAS and second["tools"] == TOOL_SCHEMAS
    assert len(first["messages"]) == 1 and len(second["messages"]) == 3
    assert second["messages"][1]["role"] == "assistant"
    results = second["messages"][2]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]
    assert all(r["type"] == "tool_result" and "is_error" not in r for r in results)
    assert "no-raw-sql" in results[0]["content"] and "query = f" in results[1]["content"]
    for call in (first, second):
        for banned in ("temperature", "top_p", "top_k"):
            assert banned not in call


def test_no_tools_requested_behaves_as_before():
    client = ScriptedClient([FINAL])
    plain = FakeClient([SQL_FINDING])
    a = run(Reviewer(client).review(SQL_DIFF))
    b = run(Reviewer(plain).review(SQL_DIFF))
    assert a == b and len(client.messages.calls) == 1
    assert client.messages.calls[0]["system"] == plain.messages.calls[0]["system"]
    assert client.messages.calls[0]["messages"] == plain.messages.calls[0]["messages"]


def test_overflow_is_a_plain_english_error():
    client = ScriptedClient([tool_response(("t", "lookup_rule", {"rule_id": "no-raw-sql"}))])
    with pytest.raises(ReviewFailed, match="too many steps"):
        run(Reviewer(client).review(SQL_DIFF))
    assert len(client.messages.calls) == MAX_TOOL_ROUNDS + 1


def test_exactly_max_rounds_then_answer_is_fine():
    looping = [tool_response((f"t{i}", "parse_diff", {})) for i in range(MAX_TOOL_ROUNDS)]
    client = ScriptedClient(looping + [FINAL])
    assert len(run(Reviewer(client).review(SQL_DIFF))) == 1


def test_overflow_is_502_from_the_route():
    client = ScriptedClient([tool_response(("t", "parse_diff", {}))])
    r = TestClient(create_app(client=client)).post("/review", json={"diff": SQL_DIFF})
    assert r.status_code == 502 and "too many steps" in r.json()["detail"]


def test_unknown_tool_gets_an_error_tool_result():
    client = ScriptedClient([tool_response(("t1", "delete_everything", {"x": 1})), FINAL])
    assert len(run(Reviewer(client).review(SQL_DIFF))) == 1
    (result,) = client.messages.calls[1]["messages"][2]["content"]
    assert result["is_error"] is True and "unknown tool" in result["content"]


def test_post_filter_unchanged_after_tool_use():
    invented = {**SQL_FINDING, "rule_id": "no-tabs"}
    bad_sev = {**SQL_FINDING, "severity": "critical"}
    final = text_response(json.dumps({"findings": [SQL_FINDING, invented, bad_sev, {"file": "x"}]}))
    reviewer = Reviewer(ScriptedClient([tool_response(("t", "parse_diff", {})), final]))
    assert [f.rule_id for f in run(reviewer.review(SQL_DIFF))] == ["no-raw-sql"]
    assert reviewer.last_dropped == 3


def test_model_error_mid_loop_is_plain_english():
    class Boom(ScriptedClient):
        pass

    client = Boom([tool_response(("t", "parse_diff", {}))])
    original = client.messages.create

    async def flaky(**kw):
        if client.messages.calls:
            raise RuntimeError("down")
        return await original(**kw)

    client.messages.create = flaky
    with pytest.raises(ReviewFailed, match="try again"):
        run(Reviewer(client).review(SQL_DIFF))


def test_prompt_mentions_tools():
    assert "lookup_rule" in Reviewer(FakeClient([])).system_prompt()


# --- connector tools appended by Agent Smith -------------------------------------------------

SECRET = "tok-super-secret-123"
CONNECTOR_SCHEMA = {
    "name": "post_comment",
    "description": "Post a comment.",
    "input_schema": {"type": "object", "properties": {"body": {"type": "string"}}, "required": ["body"]},
}


@pytest.fixture
def connector(monkeypatch):
    import agent.tools as tools

    def post_comment(body: str) -> dict:
        return {"ok": True, "message": f"posted {body}", "data": None, "dry_run": True}

    def leaky(**_kwargs) -> dict:
        raise RuntimeError(f"401 for token {SECRET}")

    monkeypatch.setattr(tools, "CONNECTOR_TOOLS", {"post_comment": post_comment, "leaky": leaky}, raising=False)
    monkeypatch.setattr(
        tools,
        "CONNECTOR_TOOL_SCHEMAS",
        [CONNECTOR_SCHEMA, dict(CONNECTOR_SCHEMA, name="parse_diff")],  # a base-name clash is ignored
        raising=False,
    )
    monkeypatch.setenv("SOME_TOKEN", SECRET)


def test_no_connector_block_changes_nothing():
    from agent.tools import all_tool_schemas

    assert all_tool_schemas() == TOOL_SCHEMAS
    assert all_tool_schemas() is not TOOL_SCHEMAS


def test_connector_schemas_are_offered_without_touching_base(connector):
    from agent.tools import all_tool_schemas

    names = [t["name"] for t in all_tool_schemas()]
    assert names == ["parse_diff", "get_file_context", "lookup_rule", "post_comment"]
    assert [t["name"] for t in TOOL_SCHEMAS] == names[:3]


def test_connector_dispatch_and_errors(connector):
    out = json.loads(call_tool("post_comment", {"body": "hi"}, diff=SQL_DIFF, policy=RULES))
    assert out == {"ok": True, "message": "posted hi", "data": None, "dry_run": True}
    bad = call_tool("post_comment", {"nope": 1}, diff=SQL_DIFF, policy=RULES)
    assert bad.startswith("Error: ")
    boom = call_tool("leaky", {}, diff=SQL_DIFF, policy=RULES)
    assert boom.startswith("Error: ") and SECRET not in boom
    assert "post_comment" in call_tool("nope", {}, diff=SQL_DIFF, policy=RULES)


def test_reviewer_offers_and_calls_connector_tools(connector):
    client = ScriptedClient([tool_response(("t1", "post_comment", {"body": "x"})), FINAL])
    assert run(Reviewer(client).review(SQL_DIFF))
    assert "post_comment" in [t["name"] for t in client.messages.calls[0]["tools"]]
