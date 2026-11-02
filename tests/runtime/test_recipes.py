"""I14 framework recipes: LangGraph over JSONL and HTTP/n8n-style webhooks through generic adapters [Spec §4, §19]."""

import importlib.util
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from collections_demo import build_suite
from collections_demo.http_app import create_app
from fastapi import FastAPI, Request
from susanoo_runner import AppServer, Journal, LocalSuite, SimulatorServer, run_suite
from susanoo_sdk import FrameLimits, InvocationContext, JsonlAgentProcess, Message

ROOT = Path(__file__).resolve().parents[2]
RECIPES = ROOT / "examples/recipes"
SUITE = LocalSuite.model_validate(build_suite())
CTX = InvocationContext(attempt_id="a1", namespace="ns", deadline=datetime.now(UTC) + timedelta(minutes=5))
_spec = importlib.util.spec_from_file_location("susanoo_recipe_http_webhook", RECIPES / "http_webhook/recipe.py")
assert _spec and _spec.loader
_recipe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_recipe)
adapter, n8n_chat_connection, wrapper_connection = (
    _recipe.adapter,
    _recipe.n8n_chat_connection,
    _recipe.wrapper_connection,
)

langgraph = pytest.importorskip("langgraph", reason="langgraph not installed")


@pytest.fixture(scope="module")
def simulator():
    with SimulatorServer() as server:
        yield server


def langgraph_agent() -> JsonlAgentProcess:
    env = {"PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    return JsonlAgentProcess(
        [sys.executable, str(RECIPES / "langgraph/serve.py")],
        env=env,
        limits=FrameLimits(request_deadline_seconds=60),
        name="langgraph",
    )


async def test_langgraph_recipe_passes_through_run_suite(simulator, tmp_path):
    cases = ["C01", "C04", "C11", "C12", "C13", "C14", "C15", "C17", "C19", "C20"]
    report = await run_suite(
        SUITE,
        agent_factory=langgraph_agent,
        agent_label="recipe:langgraph",
        journal=Journal(tmp_path / "langgraph.sqlite"),
        simulator_url=simulator.base_url,
        simulator_admin_key=simulator.admin_key,
        case_ids=cases,
    )
    assert {a.case_id: a.business_verdict for a in report.attempts} == dict.fromkeys(cases, "PASS")
    assert report.gate == "READY_FOR_ACCEPTANCE"


def test_langgraph_route_token_is_never_in_graph_state():
    sys.path.insert(0, str(RECIPES / "langgraph"))
    try:
        from graph_agent import Conversation, build_graph
    finally:
        sys.path.pop(0)
    assert set(Conversation.__annotations__) == {
        "text",
        "account_id",
        "amount_minor",
        "due_date",
        "promise_id",
        "handed_off",
        "reply",
    }
    assert build_graph().checkpointer is not None


async def test_http_wrapper_recipe_runs_end_to_end(simulator, tmp_path):
    with AppServer(create_app("correct")) as server:
        report = await run_suite(
            SUITE,
            agent_factory=lambda: adapter(wrapper_connection(f"{server.base_url}/chat")),
            agent_label="recipe:http",
            journal=Journal(tmp_path / "http.sqlite"),
            simulator_url=simulator.base_url,
            simulator_admin_key=simulator.admin_key,
            case_ids=["C01", "C20"],
        )
    assert {a.case_id: a.business_verdict for a in report.attempts} == {"C01": "PASS", "C20": "PASS"}


async def test_n8n_style_webhook_mapping_and_secret_reference(monkeypatch):
    app = FastAPI()
    seen: list[tuple[dict, str | None]] = []

    @app.post("/webhook/chat")
    async def chat(request: Request):
        body = await request.json()
        seen.append((body, request.headers.get("x-n8n-key")))
        return {"output": f"n8n says: {body['chatInput']}"}

    monkeypatch.setenv("N8N_WEBHOOK_KEY", "n8n-secret-canary")
    with AppServer(app) as server:
        n8n = adapter(
            n8n_chat_connection(f"{server.base_url}/webhook/chat", secret_ref="N8N_WEBHOOK_KEY", header="X-N8N-Key")
        )
        session = await n8n.start_session(CTX)
        reply = await n8n.invoke(session, Message(text="hello"), CTX)
        await n8n.aclose()
    assert reply.text == "n8n says: hello" and reply.tool_events.unavailable
    body, key = seen[0]
    assert body == {"chatInput": "hello", "sessionId": session.session_id, "action": "sendMessage"}
    assert key == "n8n-secret-canary"
    assert "n8n-secret-canary" not in n8n_chat_connection("http://x/w", secret_ref="N8N_WEBHOOK_KEY").model_dump_json()


def test_untested_framework_recipes_make_no_code_claims():
    for name in ("crewai", "autogen"):
        folder = RECIPES / name
        assert sorted(p.name for p in folder.iterdir()) == ["README.md"]
        assert "untested" in (folder / "README.md").read_text().lower()
