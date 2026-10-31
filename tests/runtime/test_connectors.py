"""I03-I05 connectors: real HTTP/SSE/WebSocket servers, typed mappings, no blind retries [Spec §4, §19]."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from collections_demo import build_suite
from collections_demo.http_app import REQUEST, create_app
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from susanoo_connectors import (
    AsyncHttpAgentAdapter,
    AsyncHttpConnection,
    AuthRef,
    HttpAgentAdapter,
    HttpConnection,
    ResponseMapping,
    SseAgentAdapter,
    StreamConnection,
    WebSocketAgentAdapter,
)
from susanoo_runner import AppServer, Journal, LocalSuite, SimulatorServer, run_suite
from susanoo_sdk import AdapterError, InvocationContext, Message, SessionHandle

SUITE = LocalSuite.model_validate(build_suite())
CASES = ["C01", "C11", "C14", "C15", "C19"]
CTX = InvocationContext(attempt_id="a1", namespace="ns", deadline=datetime.now(UTC) + timedelta(minutes=5))


@pytest.fixture(scope="module")
def simulator():
    with SimulatorServer() as server:
        yield server


@pytest.fixture(scope="module")
def agent_server():
    with AppServer(create_app("correct")) as server:
        yield server


def connections(base: str) -> dict:
    ws = base.replace("http://", "ws://")
    return {
        "http_sync": lambda: HttpAgentAdapter(
            HttpConnection(url=f"{base}/chat", request=REQUEST, response=ResponseMapping(text_path="reply.text"))
        ),
        "http_async": lambda: AsyncHttpAgentAdapter(
            AsyncHttpConnection(
                submit_url=f"{base}/tasks",
                status_url=f"{base}/tasks/{{task_id}}",
                request=REQUEST,
                task_id_path="task.id",
                status_path="status",
                terminal_success=["succeeded"],
                terminal_failure=["failed"],
                response=ResponseMapping(text_path="result.text"),
                poll_interval_seconds=0.02,
            )
        ),
        "sse": lambda: SseAgentAdapter(StreamConnection(kind="sse", url=f"{base}/stream", request=REQUEST)),
        "websocket": lambda: WebSocketAgentAdapter(StreamConnection(kind="websocket", url=f"{ws}/ws", request=REQUEST)),
    }


@pytest.mark.parametrize("transport", ["http_sync", "http_async", "sse", "websocket"])
async def test_suite_runs_over_every_transport(simulator, agent_server, tmp_path, transport):
    report = await run_suite(
        SUITE,
        agent_factory=connections(agent_server.base_url)[transport],
        agent_label=transport,
        journal=Journal(tmp_path / f"{transport}.sqlite"),
        simulator_url=simulator.base_url,
        simulator_admin_key=simulator.admin_key,
        case_ids=CASES,
    )
    assert {a.case_id: a.business_verdict for a in report.attempts} == dict.fromkeys(CASES, "PASS")
    assert report.gate == "READY_FOR_ACCEPTANCE"


def test_templates_are_closed_and_credentials_are_references():
    with pytest.raises(ValidationError):
        HttpConnection(
            url="http://x/chat", request={"q": "Hello {{message.text}}"}, response=ResponseMapping(text_path="t")
        )
    with pytest.raises(ValidationError):
        HttpConnection(url="http://x/chat", request={"q": "{{os.environ}}"}, response=ResponseMapping(text_path="t"))
    with pytest.raises(ValidationError):
        HttpConnection(
            url="http://x/chat",
            request={},
            headers={"Authorization": "Bearer literal"},
            response=ResponseMapping(text_path="t"),
        )
    with pytest.raises(ValidationError):
        ResponseMapping(text_path="reply.text; rm -rf /")


async def test_missing_local_secret_fails_before_sending(monkeypatch):
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    adapter = HttpAgentAdapter(
        HttpConnection(
            url="http://127.0.0.1:9/chat",
            request={"m": "{{message.text}}"},
            auth=AuthRef(kind="bearer", secret_ref="AGENT_API_KEY"),
            response=ResponseMapping(text_path="t"),
        )
    )
    with pytest.raises(AdapterError) as error:
        await adapter.invoke(SessionHandle(session_id="s", attempt_id="a1"), Message(text="hi"), CTX)
    assert error.value.code == "missing_secret" and "AGENT_API_KEY" in str(error.value)
    await adapter.aclose()


def misbehaving_app() -> FastAPI:
    app = FastAPI()
    app.state.submits = 0

    @app.post("/slow")
    async def slow():
        await asyncio.sleep(2)
        return {"t": "late"}

    @app.post("/big")
    async def big():
        return {"t": "x" * 5000}

    @app.post("/submit")
    async def submit():
        app.state.submits += 1
        await asyncio.sleep(2)
        return {"task": {"id": "t1"}}

    @app.post("/cut")
    async def cut():
        async def body():
            yield 'data: {"id": "1", "type": "delta", "text": "partial"}\n\n'

        return StreamingResponse(body(), media_type="text/event-stream")

    @app.post("/dupes")
    async def dupes():
        async def body():
            for event in (
                {"id": "1", "type": "delta", "text": "a"},
                {"id": "1", "type": "delta", "text": "a"},
                {"id": "2", "type": "delta", "text": "b"},
                {"id": "3", "type": "done"},
            ):
                yield f"data: {json.dumps(event)}\n\n"

        return StreamingResponse(body(), media_type="text/event-stream")

    return app


@pytest.fixture(scope="module")
def bad_server():
    with AppServer(misbehaving_app()) as server:
        yield server


SESSION = SessionHandle(session_id="s", attempt_id="a1")


async def test_sync_timeout_is_uncertain_and_oversize_is_rejected(bad_server):
    base = bad_server.base_url
    slow = HttpAgentAdapter(
        HttpConnection(url=f"{base}/slow", request={}, response=ResponseMapping(text_path="t"), timeout_seconds=0.3)
    )
    with pytest.raises(AdapterError) as error:
        await slow.invoke(SESSION, Message(text="x"), CTX)
    assert error.value.code == "invocation_uncertain"
    big = HttpAgentAdapter(
        HttpConnection(url=f"{base}/big", request={}, response=ResponseMapping(text_path="t"), max_response_bytes=1024)
    )
    with pytest.raises(AdapterError) as error:
        await big.invoke(SESSION, Message(text="x"), CTX)
    assert error.value.code == "output_limit"
    await slow.aclose()
    await big.aclose()


async def test_async_submit_timeout_is_never_resubmitted(bad_server):
    base = bad_server.base_url
    adapter = AsyncHttpAgentAdapter(
        AsyncHttpConnection(
            submit_url=f"{base}/submit",
            status_url=f"{base}/tasks/{{task_id}}",
            request={},
            task_id_path="task.id",
            status_path="status",
            terminal_success=["ok"],
            response=ResponseMapping(text_path="t"),
            submit_timeout_seconds=0.3,
        )
    )
    with pytest.raises(AdapterError) as error:
        await adapter.invoke(SESSION, Message(text="x"), CTX)
    assert error.value.code == "submit_uncertain"
    await asyncio.sleep(2.2)
    assert bad_server.app.state.submits == 1
    await adapter.aclose()


async def test_streams_must_complete_and_duplicates_are_ignored(bad_server):
    base = bad_server.base_url
    cut = SseAgentAdapter(StreamConnection(kind="sse", url=f"{base}/cut", request={}))
    with pytest.raises(AdapterError) as error:
        await cut.invoke(SESSION, Message(text="x"), CTX)
    assert error.value.code == "stream_interrupted"
    dupes = SseAgentAdapter(StreamConnection(kind="sse", url=f"{base}/dupes", request={}))
    assert (await dupes.invoke(SESSION, Message(text="x"), CTX)).text == "ab"
    await cut.aclose()
    await dupes.aclose()
