"""I03 GraphQL, I04 signed webhooks, I09 MCP, I10 A2A/gRPC against local fake providers [Spec §4, §19; T21-T23].

Fake servers prove transport behaviour only; nothing here is live vendor verification.
"""

import asyncio
import base64
import json
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import mcp_types
import pytest
import uvicorn
from collections_demo import build_suite
from collections_demo.a2a_app import create_app as create_a2a_app
from collections_demo.agent import CollectionsAgent
from collections_demo.grpc_app import (
    DESCRIPTOR_B64,
    DESCRIPTOR_DIGEST,
    DESCRIPTOR_SET,
    SERVICE,
    FakeGrpcTransport,
    collections_handlers,
)
from collections_demo.mcp_server import build_server
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import ValidationError
from susanoo_connectors import mcp as mcp_module
from susanoo_connectors.a2a import A2AAgentAdapter, A2AConnection
from susanoo_connectors.graphql import GraphqlAgentAdapter, GraphqlConnection, classify_errors, operation_kind
from susanoo_connectors.grpc_connector import (
    GrpcAgentAdapter,
    GrpcClient,
    GrpcConnection,
    GrpcioTransport,
    GrpcMethod,
    GrpcStatusError,
    GrpcVersionCheck,
    classify_status,
)
from susanoo_connectors.mapping import ResponseMapping
from susanoo_connectors.mcp import (
    McpAgentAdapter,
    McpAgentToolDriver,
    McpConnection,
    McpToolClient,
    schema_digest,
)
from susanoo_connectors.webhook import (
    MemoryRelayQueue,
    MemoryTaskRegistry,
    WebhookAgentAdapter,
    WebhookCompletionWaiter,
    WebhookConnection,
    WebhookEventMapping,
    WebhookRejected,
    WebhookVerification,
    handle_callback,
    sign,
    verify_signature,
)
from susanoo_runner import AppServer, Journal, LocalSuite, SimulatorServer, run_suite
from susanoo_sdk import AdapterError, InvocationContext, Message, SessionHandle

ROOT = Path(__file__).resolve().parents[2]
SUITE = LocalSuite.model_validate(build_suite())
CASES = ["C01", "C11", "C14"]
CTX = InvocationContext(attempt_id="a1", namespace="ns", deadline=datetime.now(UTC) + timedelta(minutes=5))
SESSION = SessionHandle(session_id="s", attempt_id="a1")
TURN = {"session_id": "{{session_id}}", "message": "{{message.text}}", "context": "{{context}}"}
PYTHONPATH = ":".join(
    str(ROOT / p)
    for p in (
        "packages/evaluator",
        "packages/simulator",
        "packages/sdk-python",
        "packages/runner",
        "packages/connectors",
        "packages/contracts",
        "examples/collections",
    )
)


class LifespanServer(AppServer):
    """AppServer with the ASGI lifespan on (the MCP Streamable HTTP session manager needs it)."""

    def __init__(self, app):
        super().__init__(app)
        import threading

        self._server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
        self._thread = threading.Thread(target=self._server.run, kwargs={"sockets": [self._socket]}, daemon=True)


# --------------------------------------------------------------------------------------------------
# Fake providers
# --------------------------------------------------------------------------------------------------

DOC = "mutation Turn($session: String!, $message: String!, $context: JSON!) { turn(session: $session) { text } }"


def graphql_app() -> FastAPI:
    app = FastAPI()
    agent = CollectionsAgent("correct")
    app.state.requests = 0

    @app.post("/graphql")
    async def graphql(request: Request):
        app.state.requests += 1
        body = await request.json()
        v = body["variables"]
        reply = await agent.respond(
            v["session"], Message(text=v["message"]), InvocationContext.model_validate(v["context"])
        )
        return {"data": {"turn": {"text": reply.text}}}

    @app.post("/errors/{kind}")
    async def errors(kind: str):
        app.state.requests += 1
        code = {"auth": "UNAUTHENTICATED", "invalid": "GRAPHQL_VALIDATION_FAILED", "exec": "INTERNAL"}.get(kind)
        error = {"message": "nope", "extensions": {"code": code}}
        if kind == "partial":
            return {"data": {"turn": {"text": "half"}}, "errors": [error]}
        if kind == "invalid":
            return JSONResponse({"errors": [error]}, status_code=400)
        return {"data": None, "errors": [error]}

    @app.post("/redirect")
    async def redirect():
        return RedirectResponse("http://example.invalid/graphql", status_code=307)

    return app


SECRET_ENV = "PROVIDER_WEBHOOK_SECRET"
VERIFY = WebhookVerification(secret_ref=SECRET_ENV)


def webhook_provider_app(callback_url: str, secret: str) -> FastAPI:
    """Async job provider: accepts a job, runs the agent, then posts signed callbacks (with replays)."""
    app = FastAPI()
    agent = CollectionsAgent("correct")
    app.state.submits = 0

    async def deliver(task_id: str, text: str) -> None:
        events = [
            {"event_id": f"{task_id}-1", "task_id": task_id, "sequence": 1, "status": "running"},
            {
                "event_id": f"{task_id}-2",
                "task_id": task_id,
                "sequence": 2,
                "status": "succeeded",
                "result": {"text": text},
            },
        ]
        async with httpx.AsyncClient() as client:
            # Terminal first, then the stale event, then a duplicate terminal: none may change the outcome.
            for event in (events[1], events[0], events[1]):
                body = json.dumps(event).encode()
                stamp = int(time.time())
                headers = {"X-Susanoo-Timestamp": str(stamp), "X-Susanoo-Signature": "v1=" + sign(secret, stamp, body)}
                await client.post(callback_url, content=body, headers=headers)

    @app.post("/jobs", status_code=202)
    async def submit(request: Request):
        app.state.submits += 1
        turn = await request.json()
        task_id = uuid.uuid4().hex

        async def work():
            ctx = InvocationContext.model_validate(turn["context"])
            reply = await agent.respond(turn["session_id"], Message(text=turn["message"]), ctx)
            await asyncio.sleep(0.05)  # the runner registers the job id before the callback lands
            await deliver(task_id, reply.text or "")

        asyncio.get_running_loop().create_task(work())
        return {"task": {"id": task_id}}

    return app


def relay_app(registry: MemoryTaskRegistry, queue: MemoryRelayQueue, mapping: WebhookEventMapping) -> FastAPI:
    """Stand-in for the hosted callback endpoint: it only calls the provided helper."""
    app = FastAPI()
    app.state.outcomes = []

    @app.post("/callbacks")
    async def callback(request: Request):
        try:
            outcome = await handle_callback(
                await request.body(),
                dict(request.headers),
                verification=VERIFY,
                mapping=mapping,
                registry=registry,
                queue=queue,
            )
        except WebhookRejected as error:
            app.state.outcomes.append(error.code)
            return JSONResponse({"error": error.code}, status_code=error.status_code)
        app.state.outcomes.append(outcome.disposition)
        return {"disposition": outcome.disposition}

    return app


@pytest.fixture(scope="module")
def simulator():
    with SimulatorServer() as server:
        yield server


@pytest.fixture(scope="module")
def graphql_server():
    with AppServer(graphql_app()) as server:
        yield server


@pytest.fixture(scope="module")
def a2a_server():
    with AppServer(create_a2a_app("correct")) as server:
        yield server


@pytest.fixture(scope="module")
def mcp_http():
    server = build_server("correct")
    with LifespanServer(server.streamable_http_app()) as http:
        http.mcp = server
        yield http


@pytest.fixture(scope="module")
def webhook_stack():
    import os

    secret = "whsec-test-" + uuid.uuid4().hex
    os.environ[SECRET_ENV] = secret
    registry, queue = MemoryTaskRegistry(), MemoryRelayQueue()
    connection = WebhookConnection(
        submit_url="http://placeholder/jobs",
        request=TURN,
        task_id_path="task.id",
        terminal_success=["succeeded"],
        terminal_failure=["failed"],
        response=ResponseMapping(text_path="text"),
        poll_interval_seconds=0.02,
        max_wait_seconds=10,
    )
    with AppServer(relay_app(registry, queue, connection.event_mapping())) as relay:
        with AppServer(webhook_provider_app(f"{relay.base_url}/callbacks", secret)) as provider:
            yield {
                "registry": registry,
                "queue": queue,
                "relay": relay,
                "provider": provider,
                "connection": connection.model_copy(update={"submit_url": f"{provider.base_url}/jobs"}),
            }
    os.environ.pop(SECRET_ENV, None)


def grpc_connection(**overrides) -> GrpcConnection:
    methods = {
        "chat": GrpcMethod(
            service=SERVICE,
            method="Chat",
            request_type="susanoo.demo.v1.Turn",
            response_type="susanoo.demo.v1.TurnReply",
            mutating=True,
            request=TURN,
            response=ResponseMapping(text_path="text"),
        ),
        "version": GrpcMethod(
            service=SERVICE,
            method="GetVersion",
            request_type="susanoo.demo.v1.VersionRequest",
            response_type="susanoo.demo.v1.VersionReply",
            mutating=False,
        ),
    }
    values = {
        "target": "127.0.0.1:50051",
        "tls": False,
        "descriptor_set_b64": DESCRIPTOR_B64,
        "descriptor_digest": DESCRIPTOR_DIGEST,
        "methods": methods,
        "invoke_method": "chat",
        "version_check": GrpcVersionCheck(method="version", version_path="api_version", expected="v1"),
    }
    return GrpcConnection(**{**values, **overrides})


def mcp_http_connection(base: str, **overrides) -> McpConnection:
    values = {
        "protocol_revision": "2026-07-28",
        "transport": "streamable_http",
        "url": f"{base}/mcp",
        "allowed_tools": ["lookup_account", "chat", "fail"],
    }
    return McpConnection(**{**values, **overrides})


def mcp_stdio_connection(**overrides) -> McpConnection:
    values = {
        "protocol_revision": "2025-11-25",
        "transport": "stdio",
        "command": [sys.executable, "-m", "collections_demo.mcp_server", "stdio"],
        "env": {"PYTHONPATH": PYTHONPATH},
        "allowed_tools": ["lookup_account", "ask_model", "ask_user"],
    }
    return McpConnection(**{**values, **overrides})


# --------------------------------------------------------------------------------------------------
# T21: the collections suite end to end over every new protocol connector
# --------------------------------------------------------------------------------------------------


def factories(graphql_base, a2a_base, mcp_base, webhook):
    return {
        "graphql": lambda: GraphqlAgentAdapter(
            GraphqlConnection(
                url=f"{graphql_base}/graphql",
                query=DOC,
                variables={"session": "{{session_id}}", "message": "{{message.text}}", "context": "{{context}}"},
                response=ResponseMapping(text_path="turn.text"),
            )
        ),
        "a2a": lambda: A2AAgentAdapter(
            A2AConnection(
                card_url=f"{a2a_base}/.well-known/agent-card.json",
                metadata={"susanoo_context": "{{context}}"},
                required_skills=["collections"],
            )
        ),
        "a2a_stream": lambda: A2AAgentAdapter(
            A2AConnection(
                card_url=f"{a2a_base}/.well-known/agent-card.json",
                methods=["message/stream", "tasks/get"],
                metadata={"susanoo_context": "{{context}}"},
            )
        ),
        "grpc": lambda: GrpcAgentAdapter(grpc_connection(), transport=FakeGrpcTransport(collections_handlers())),
        "mcp_agent_tool": lambda: McpAgentAdapter(
            mcp_http_connection(mcp_base),
            McpAgentToolDriver(tool="chat", arguments=TURN, response=ResponseMapping(text_path="text")),
        ),
        "webhook": lambda: WebhookAgentAdapter(
            webhook["connection"], registrar=webhook["registry"], queue=webhook["queue"]
        ),
    }


@pytest.mark.parametrize("transport", ["graphql", "a2a", "a2a_stream", "grpc", "mcp_agent_tool", "webhook"])
async def test_suite_runs_over_protocol_connectors(
    simulator, graphql_server, a2a_server, mcp_http, webhook_stack, tmp_path, transport
):
    make = factories(graphql_server.base_url, a2a_server.base_url, mcp_http.base_url, webhook_stack)[transport]
    report = await run_suite(
        SUITE,
        agent_factory=make,
        agent_label=transport,
        journal=Journal(tmp_path / f"{transport}.sqlite"),
        simulator_url=simulator.base_url,
        simulator_admin_key=simulator.admin_key,
        case_ids=CASES,
    )
    assert {a.case_id: a.business_verdict for a in report.attempts} == dict.fromkeys(CASES, "PASS")
    assert report.gate == "READY_FOR_ACCEPTANCE"


# --------------------------------------------------------------------------------------------------
# I03 GraphQL
# --------------------------------------------------------------------------------------------------


def test_graphql_document_is_fixed_and_variables_are_closed():
    ok = {"url": "http://x/graphql", "query": DOC, "response": ResponseMapping(text_path="turn.text")}
    with pytest.raises(ValidationError):
        GraphqlConnection(**ok, variables={"m": "Hi {{message.text}}"})
    with pytest.raises(ValidationError):
        GraphqlConnection(**{**ok, "query": 'query { turn(m: "{{message.text}}") { text } }'})
    with pytest.raises(ValidationError):
        GraphqlConnection(**{**ok, "query": "subscription { events { id } }"})
    with pytest.raises(ValidationError):
        GraphqlConnection(**ok, headers={"Cookie": "literal"})
    assert operation_kind("# comment\n{ a }") == "query" and GraphqlConnection(**ok).mutating
    assert classify_errors([{"extensions": {"code": "FORBIDDEN"}}, {}]) == "graphql_auth"
    assert classify_errors([{"message": "x"}]) == "graphql_execution_error"


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("errors/auth", "graphql_auth"),
        ("errors/invalid", "graphql_request_invalid"),
        ("errors/exec", "graphql_execution_error"),
        ("errors/partial", "graphql_partial"),
        ("redirect", "redirect_blocked"),
    ],
)
async def test_graphql_errors_are_classified_never_passing(graphql_server, path, code):
    adapter = GraphqlAgentAdapter(
        GraphqlConnection(
            url=f"{graphql_server.base_url}/{path}", query=DOC, response=ResponseMapping(text_path="turn.text")
        )
    )
    with pytest.raises(AdapterError) as error:
        await adapter.invoke(SESSION, Message(text="x"), CTX)
    assert error.value.code == code
    await adapter.aclose()


async def test_graphql_partial_data_only_when_accepted_and_oversize_requests_are_not_sent(graphql_server):
    base = graphql_server.base_url
    partial = GraphqlAgentAdapter(
        GraphqlConnection(
            url=f"{base}/errors/partial",
            query=DOC,
            accept_partial_data=True,
            response=ResponseMapping(text_path="turn.text"),
        )
    )
    assert (await partial.invoke(SESSION, Message(text="x"), CTX)).text == "half"
    before = graphql_server.app.state.requests
    big = GraphqlAgentAdapter(
        GraphqlConnection(
            url=f"{base}/graphql",
            query=DOC,
            variables={"message": "{{message.text}}"},
            max_request_bytes=256,
            response=ResponseMapping(text_path="turn.text"),
        )
    )
    with pytest.raises(AdapterError) as error:
        await big.invoke(SESSION, Message(text="y" * 2000), CTX)
    assert error.value.code == "request_limit" and graphql_server.app.state.requests == before
    await partial.aclose()
    await big.aclose()


# --------------------------------------------------------------------------------------------------
# I04 signed webhook completion (T22)
# --------------------------------------------------------------------------------------------------


def signed(secret: str, event: dict, *, stamp: int | None = None) -> tuple[bytes, dict]:
    body = json.dumps(event).encode()
    stamp = int(time.time()) if stamp is None else stamp
    return body, {"X-Susanoo-Timestamp": str(stamp), "X-Susanoo-Signature": "v1=" + sign(secret, stamp, body)}


MAPPING = WebhookEventMapping(terminal_states=["succeeded", "failed"])


def test_signature_verification(monkeypatch):
    monkeypatch.setenv("WH_NEW", "new-secret")
    monkeypatch.setenv("WH_OLD", "old-secret")
    config = WebhookVerification(secret_ref="WH_NEW", previous_secret_ref="WH_OLD", tolerance_seconds=60)
    body, headers = signed("new-secret", {"a": 1})
    verify_signature(body, headers, config)
    verify_signature(*signed("old-secret", {"a": 1}), config)  # rotation window
    cases = {
        "bad_signature": (body + b" ", headers),
        "missing_signature": (body, {}),
        "stale_timestamp": signed("new-secret", {"a": 1}, stamp=int(time.time()) - 600),
    }
    wrong = {**headers, "X-Susanoo-Signature": "v1=" + "0" * 64}
    cases["bad_signature_wrong_key"] = (body, wrong)
    for expected, (b, h) in cases.items():
        with pytest.raises(WebhookRejected) as error:
            verify_signature(b, h, config)
        assert error.value.code == expected.removesuffix("_wrong_key") and error.value.status_code == 401


async def test_callbacks_correlate_dedupe_order_and_reject_unknown(monkeypatch):
    monkeypatch.setenv("WH", "s3cret")
    config = WebhookVerification(secret_ref="WH")
    registry, queue = MemoryTaskRegistry(), MemoryRelayQueue()
    await registry.register("t1", attempt_id="a1", invocation_id="inv-1")

    async def deliver(event):
        return await handle_callback(
            *signed("s3cret", event), verification=config, mapping=MAPPING, registry=registry, queue=queue
        )

    with pytest.raises(WebhookRejected) as error:
        await deliver({"event_id": "x", "task_id": "never-submitted", "sequence": 1, "status": "succeeded"})
    assert error.value.code == "unknown_task" and error.value.status_code == 404
    assert (await queue.poll("never-submitted", 0))[0] == []
    outcomes = [
        (await deliver(e)).disposition
        for e in (
            {"event_id": "e2", "task_id": "t1", "sequence": 2, "status": "running"},
            {"event_id": "e2", "task_id": "t1", "sequence": 2, "status": "running"},
            {"event_id": "e1", "task_id": "t1", "sequence": 1, "status": "succeeded", "result": {"text": "stale"}},
            {"event_id": "e3", "task_id": "t1", "sequence": 3, "status": "succeeded", "result": {"text": "done"}},
            {"event_id": "e4", "task_id": "t1", "sequence": 4, "status": "failed"},
        )
    ]
    assert outcomes == ["accepted", "duplicate", "out_of_order", "accepted", "after_terminal"]
    events, _ = await queue.poll("t1", 0)
    assert [e.event_id for e in events] == ["e2", "e3"]
    final = await WebhookCompletionWaiter(queue, poll_interval_seconds=0.01).wait("t1", timeout_seconds=1)
    assert final.result == {"text": "done"}
    with pytest.raises(WebhookRejected) as error:
        await handle_callback(
            b"not json", signed("s3cret", {})[1], verification=config, mapping=MAPPING, registry=registry, queue=queue
        )
    assert error.value.code in {"bad_signature", "malformed_event"}


async def test_missing_callback_is_a_deadline_never_a_resubmission(webhook_stack):
    app = FastAPI()
    app.state.submits = 0

    @app.post("/jobs")
    async def jobs():
        app.state.submits += 1
        return {"task": {"id": "silent"}}

    with AppServer(app) as silent:
        connection = webhook_stack["connection"].model_copy(
            update={"submit_url": f"{silent.base_url}/jobs", "max_wait_seconds": 0.3}
        )
        adapter = WebhookAgentAdapter(connection, registrar=MemoryTaskRegistry(), queue=MemoryRelayQueue())
        with pytest.raises(AdapterError) as error:
            await adapter.invoke(SESSION, Message(text="x"), CTX)
        assert error.value.code == "callback_deadline"
        assert app.state.submits == 1 and adapter.submissions == 1
        await adapter.aclose()


def test_relay_saw_replays_but_only_one_submission_per_turn(webhook_stack):
    outcomes = webhook_stack["relay"].app.state.outcomes
    if not outcomes:
        pytest.skip("end-to-end webhook case did not run in this session")
    # Each turn: terminal accepted, then the stale event lands after terminal, then a duplicate terminal.
    assert set(outcomes) == {"accepted", "after_terminal", "duplicate"}
    registrations = webhook_stack["registry"].registrations
    assert webhook_stack["provider"].app.state.submits == len(registrations)


# --------------------------------------------------------------------------------------------------
# I09 MCP (T23)
# --------------------------------------------------------------------------------------------------


async def test_mcp_tool_only_connection_is_not_an_agent(mcp_http):
    connection = mcp_http_connection(mcp_http.base_url)
    with pytest.raises(AdapterError) as error:
        McpAgentAdapter(connection)
    assert error.value.code == "not_an_agent"
    async with McpToolClient(connection) as tools:
        report = await tools.preflight()
    assert report.capabilities.invoke is False and report.capabilities.modalities == ["tool"]
    assert tools.negotiated_revision == "2026-07-28"


async def test_mcp_allowlist_and_schema_validation_happen_before_sending(mcp_http):
    calls = mcp_http.mcp.calls
    async with McpToolClient(mcp_http_connection(mcp_http.base_url)) as tools:
        assert "close_account" in tools.server_tools and "close_account" not in tools.tools
        result = await tools.call_tool("lookup_account", {"account_id": "ACC-1001"})
        assert result.structured == {"account_id": "ACC-1001", "status": "overdue", "overdue_minor": 450000}
        before = list(calls)
        with pytest.raises(AdapterError) as error:
            await tools.call_tool("close_account", {"account_id": "ACC-1001"})
        assert error.value.code == "tool_not_allowlisted"
        with pytest.raises(AdapterError) as error:
            await tools.call_tool("lookup_account", {"account_id": 7})
        assert error.value.code == "tool_arguments_invalid"
        assert calls[len(before) :] == []
        assert "close_account" not in calls
        failed = await tools.call_tool("fail", {})
        assert failed.is_error and tools.events[-1].status == "error"


async def test_mcp_schema_digest_pins_reviewed_tools(mcp_http):
    connection = mcp_http_connection(mcp_http.base_url, tool_schema_digests={"lookup_account": "0" * 64})
    with pytest.raises(AdapterError) as error:
        await McpToolClient(connection).connect()
    assert error.value.code == "tool_schema_changed"
    with pytest.raises(AdapterError) as error:
        await McpToolClient(mcp_http_connection(mcp_http.base_url, allowed_tools=["no_such_tool"])).connect()
    assert error.value.code == "preflight_failed"
    async with McpToolClient(mcp_http_connection(mcp_http.base_url)) as tools:
        tool = tools.tools["lookup_account"]
        assert tool.digest == schema_digest("lookup_account", tool.input_schema, tool.output_schema)


async def test_mcp_revision_is_pinned(mcp_http, monkeypatch):
    # Force the client to negotiate the handshake-era revision while the connection pins 2026-07-28.
    monkeypatch.setitem(mcp_module._MODE, "2026-07-28", "legacy")
    with pytest.raises(AdapterError) as error:
        await McpToolClient(mcp_http_connection(mcp_http.base_url)).connect()
    assert error.value.code == "protocol_version_mismatch"
    with pytest.raises(ValidationError):
        mcp_http_connection(mcp_http.base_url, protocol_revision="2025-06-18")
    with pytest.raises(ValidationError):
        McpConnection(protocol_revision="2025-11-25", transport="stdio", allowed_tools=["x"])


async def test_mcp_stdio_denies_sampling_and_elicitation_unless_configured():
    async with McpToolClient(mcp_stdio_connection()) as tools:
        assert tools.negotiated_revision == "2025-11-25"
        assert (await tools.call_tool("lookup_account", {"account_id": "ACC-1001"})).structured["status"] == "overdue"
        for name, args in (("ask_model", {"question": "q"}), ("ask_user", {})):
            with pytest.raises(AdapterError) as error:
                await tools.call_tool(name, args)
            assert error.value.code == "mcp_error"

    seen = []

    async def sampling(context, params):
        seen.append("sampling")
        return mcp_types.CreateMessageResult(
            role="assistant", content=mcp_types.TextContent(type="text", text="sampled"), model="fake"
        )

    async def elicitation(context, params):
        seen.append("elicitation")
        return mcp_types.ElicitResult(action="decline")

    with pytest.raises(ValueError):
        McpToolClient(mcp_stdio_connection(), sampling_handler=sampling)
    with pytest.raises(ValueError):
        McpToolClient(mcp_stdio_connection(allow_sampling=True))
    allowed = mcp_stdio_connection(allow_sampling=True, allow_elicitation=True)
    async with McpToolClient(allowed, sampling_handler=sampling, elicitation_handler=elicitation) as tools:
        assert (await tools.call_tool("ask_model", {"question": "q"})).text == "sampled"
        assert (await tools.call_tool("ask_user", {})).text == "decline"
    assert seen == ["sampling", "elicitation"]


# --------------------------------------------------------------------------------------------------
# I10 A2A (T23)
# --------------------------------------------------------------------------------------------------


def a2a(base: str, **overrides) -> A2AAgentAdapter:
    values = {"card_url": f"{base}/.well-known/agent-card.json", "metadata": {"susanoo_context": "{{context}}"}}
    return A2AAgentAdapter(A2AConnection(**{**values, **overrides}))


async def test_a2a_version_mismatch_is_an_explicit_preflight_failure():
    with AppServer(create_a2a_app(protocol_version="0.2.5")) as server:
        adapter = a2a(server.base_url)
        with pytest.raises(AdapterError) as error:
            await adapter.preflight()
        assert error.value.code == "protocol_version_mismatch"
        with pytest.raises(AdapterError):
            await adapter.invoke(SESSION, Message(text="hi"), CTX)
        assert server.app.state.methods == []
        await adapter.aclose()


async def test_a2a_card_validation_and_advertised_features():
    with AppServer(create_a2a_app(streaming=False)) as server:
        adapter = a2a(server.base_url, methods=["message/stream"])
        with pytest.raises(AdapterError) as error:
            await adapter.preflight()
        assert error.value.code == "unsupported_feature"
        missing = a2a(server.base_url, required_skills=["refunds"])
        with pytest.raises(AdapterError) as error:
            await missing.preflight()
        assert "refunds" in str(error.value)
        report = await a2a(server.base_url, methods=["message/send", "tasks/get", "tasks/cancel"]).preflight()
        assert report.capabilities.cancel_remote and not report.capabilities.stream
    with AppServer(create_a2a_app(rpc_url="http://elsewhere.invalid/a2a")) as server:
        with pytest.raises(AdapterError) as error:
            await a2a(server.base_url).preflight()
        assert "different origin" in str(error.value)
    with pytest.raises(ValidationError):
        A2AConnection(card_url="http://x/card", methods=["tasks/get"])
    with pytest.raises(ValidationError):
        A2AConnection(card_url="http://x/card", protocol_version="1.0.0")


async def test_a2a_task_states_polling_and_failures():
    with AppServer(create_a2a_app(working_polls=2)) as server:
        reply = await a2a(server.base_url, poll_interval_seconds=0.01).invoke(SESSION, Message(text="hello"), CTX)
        assert reply.structured == {"a2a_state": "completed", "task_id": reply.structured["task_id"]}
        assert server.app.state.methods.count("tasks/get") == 2
        no_get = a2a(server.base_url, methods=["message/send"])
        with pytest.raises(AdapterError) as error:
            await no_get.invoke(SESSION, Message(text="hello"), CTX)
        assert error.value.code == "task_not_terminal"
    with AppServer(create_a2a_app(fail_with="failed")) as server:
        with pytest.raises(AdapterError) as error:
            await a2a(server.base_url).invoke(SESSION, Message(text="hello"), CTX)
        assert error.value.code == "remote_task_failed"


async def test_a2a_cancel_only_when_mapped_and_send_is_never_resent():
    with AppServer(create_a2a_app(working_polls=10_000)) as server:
        adapter = a2a(
            server.base_url, methods=["message/send", "tasks/get", "tasks/cancel"], poll_interval_seconds=0.02
        )
        running = asyncio.create_task(adapter.invoke(SESSION, Message(text="hello"), CTX))
        while not adapter._invocations:
            await asyncio.sleep(0.01)
        invocation_id = next(iter(adapter._invocations))
        result = await adapter.cancel(invocation_id)
        assert result.acknowledged and result.remote_cancel_attempted
        with pytest.raises(AdapterError) as error:
            await running
        assert error.value.code == "remote_task_canceled"
        unmapped = a2a(server.base_url)
        assert not (await unmapped.cancel("inv-x")).acknowledged and "tasks/cancel" not in unmapped.sent
        await adapter.aclose()
    with AppServer(create_a2a_app(rpc_path="/slow-a2a")) as server:
        slow = a2a(server.base_url, timeout_seconds=0.3)
        with pytest.raises(AdapterError) as error:
            await slow.invoke(SESSION, Message(text="hello"), CTX)
        assert error.value.code == "invocation_uncertain" and slow.sent == ["message/send"]
        await slow.aclose()


# --------------------------------------------------------------------------------------------------
# I10 gRPC (T23)
# --------------------------------------------------------------------------------------------------


class ScriptedTransport(FakeGrpcTransport):
    def __init__(self, failures: list[str], handlers):
        super().__init__(handlers)
        self.failures = failures

    async def unary_unary(self, path, request, *, timeout, metadata):
        if self.failures:
            self.calls.append(path)
            raise GrpcStatusError(self.failures.pop(0), "scripted")
        return await super().unary_unary(path, request, timeout=timeout, metadata=metadata)


def test_grpc_status_classification():
    assert classify_status("INVALID_ARGUMENT", mutating=True) == "rejected"
    assert classify_status("UNAVAILABLE", mutating=True) == "uncertain"
    assert classify_status("DEADLINE_EXCEEDED", mutating=True) == "uncertain"
    assert classify_status("UNAVAILABLE", mutating=False) == "retryable"
    assert classify_status("INTERNAL", mutating=False) == "failed"


async def test_grpc_mutating_failures_after_send_are_uncertain_and_never_retried():
    transport = ScriptedTransport(["UNAVAILABLE"], collections_handlers())
    adapter = GrpcAgentAdapter(grpc_connection(read_retries=3), transport=transport)
    with pytest.raises(AdapterError) as error:
        await adapter.invoke(SESSION, Message(text="hi"), CTX)
    assert error.value.code == "invocation_uncertain" and transport.calls == [f"/{SERVICE}/Chat"]
    transport = ScriptedTransport(["DEADLINE_EXCEEDED"], collections_handlers())
    with pytest.raises(AdapterError) as error:
        await GrpcAgentAdapter(grpc_connection(), transport=transport).invoke(SESSION, Message(text="hi"), CTX)
    assert error.value.code == "invocation_uncertain" and len(transport.calls) == 1
    transport = ScriptedTransport(["INVALID_ARGUMENT"], collections_handlers())
    with pytest.raises(AdapterError) as error:
        await GrpcAgentAdapter(grpc_connection(), transport=transport).invoke(SESSION, Message(text="hi"), CTX)
    assert error.value.code == "grpc_invalid_argument"
    reads = ScriptedTransport(["UNAVAILABLE"], collections_handlers())
    assert (await GrpcAgentAdapter(grpc_connection(), transport=reads).preflight()).checks["api_version"]
    assert reads.calls == [f"/{SERVICE}/GetVersion"] * 2


async def test_grpc_only_reviewed_methods_and_typed_messages():
    transport = FakeGrpcTransport(collections_handlers())
    client = GrpcClient(grpc_connection(), transport=transport)
    with pytest.raises(AdapterError) as error:
        await client.call("PurgeAccounts", {"confirm": True})
    assert error.value.code == "method_not_mapped"
    with pytest.raises(AdapterError) as error:
        await client.call("chat", {"session_id": "s", "message": "m", "drop_tables": True})
    assert error.value.code == "request_invalid"
    with pytest.raises(AdapterError) as error:
        await client.call("chat", {"session_id": "s"}, deadline=datetime.now(UTC) - timedelta(seconds=1))
    assert error.value.code == "deadline_exceeded"
    assert transport.calls == []


async def test_grpc_descriptor_review_and_version_mismatch():
    with pytest.raises(AdapterError) as error:
        await GrpcAgentAdapter(grpc_connection(descriptor_digest="0" * 64), transport=FakeGrpcTransport({})).preflight()
    assert error.value.code == "descriptor_digest_mismatch"
    stream = grpc_connection().methods["chat"].model_copy(update={"method": "StreamChat"})
    with pytest.raises(AdapterError) as error:
        await GrpcAgentAdapter(
            grpc_connection(methods={**grpc_connection().methods, "chat": stream}), transport=FakeGrpcTransport({})
        ).preflight()
    assert error.value.code == "unsupported_feature"
    retyped = grpc_connection().methods["chat"].model_copy(update={"response_type": "susanoo.demo.v1.PurgeReply"})
    with pytest.raises(AdapterError) as error:
        await GrpcAgentAdapter(
            grpc_connection(methods={**grpc_connection().methods, "chat": retyped}), transport=FakeGrpcTransport({})
        ).preflight()
    assert error.value.code == "preflight_failed"
    newer = FakeGrpcTransport(collections_handlers(api_version="v2"))
    with pytest.raises(AdapterError) as error:
        await GrpcAgentAdapter(grpc_connection(), transport=newer).preflight()
    assert error.value.code == "protocol_version_mismatch"
    with pytest.raises(ValidationError):
        grpc_connection(version_check=GrpcVersionCheck(method="chat", version_path="text", expected="v1"))
    assert base64.b64decode(DESCRIPTOR_B64) == DESCRIPTOR_SET


async def test_grpcio_transport_fails_clearly_when_grpcio_is_absent(monkeypatch):
    monkeypatch.setitem(sys.modules, "grpc", None)
    transport = GrpcioTransport("127.0.0.1:1", tls=False)
    with pytest.raises(AdapterError) as error:
        await transport.unary_unary(f"/{SERVICE}/GetVersion", b"", timeout=1, metadata=[])
    assert error.value.code == "dependency_missing" and "grpcio" in str(error.value)
