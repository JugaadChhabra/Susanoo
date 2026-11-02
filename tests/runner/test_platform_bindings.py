import asyncio
import base64
import hashlib
import importlib.util
import json
import sys
import socketserver
import threading
import uuid
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler

import httpx
import pytest

from susanoo_contracts import content_hash
from susanoo_runner.journal import JournalError
from susanoo_runner.platform_bindings import check_egress_url, factory_for_connection
from susanoo_sdk import AdapterError, InvocationContext, Message


PRIVATE_LOOPBACKS = ["127.0.0.0/8", "::1/128"]


@pytest.mark.parametrize("family,adapter", [("I06", "python_sdk"), ("I07", "typescript_sdk")])
def test_sdk_bindings_require_a_local_jsonl_process_not_an_implicit_demo(family, adapter):
    connection = {"family": family, "adapter": adapter}
    digest = content_hash(connection)
    binding = {digest: {"adapter": adapter, "config": {"kind": "cli", "argv": [sys.executable, "-m", "agent.serve"]}}}
    actual = factory_for_connection(connection, digest, binding)()
    assert actual.profile.argv == [sys.executable, "-m", "agent.serve"]
    with pytest.raises(JournalError, match="No local binding"):
        factory_for_connection(connection, digest, {})


def http_connection():
    return {"family": "I03", "adapter": "http_sync"}


def http_binding(data, url, **overrides):
    host = url.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0].strip("[]")
    return {
        content_hash(data): {
            "adapter": "http_sync",
            "config": {
                "kind": "http_sync",
                "url": url,
                "request": {"message": "{{message.text}}"},
                "response": {"text_path": "text"},
                "max_response_bytes": 1024,
            },
            "allowed_hosts": [host],
            "allow_private_cidrs": PRIVATE_LOOPBACKS,
            **overrides,
        }
    }


def test_binding_requires_exact_hash_adapter_and_family():
    data = http_connection()
    digest = content_hash(data)
    approved = http_binding(data, "http://127.0.0.1/run")
    adapter = factory_for_connection(data, digest, approved)()
    assert adapter.connection.url == "http://127.0.0.1/run"
    with pytest.raises(JournalError, match="hash"):
        factory_for_connection(data, "0" * 64, approved)
    bad = {digest: {**approved[digest], "adapter": "graphql"}}
    with pytest.raises(JournalError, match="adapter"):
        factory_for_connection(data, digest, bad)
    mismatch = {**data, "family": "I04"}
    mismatch_bindings = {**approved, content_hash(mismatch): approved[digest]}
    with pytest.raises(JournalError, match="family"):
        factory_for_connection(mismatch, content_hash(mismatch), mismatch_bindings)


async def test_http_output_transport_is_bounded():
    data = http_connection()
    approved = http_binding(data, "http://127.0.0.1/run")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 2048))
    adapter = factory_for_connection(data, content_hash(data), approved, egress_transport=transport)()
    context = InvocationContext(attempt_id=str(uuid.uuid4()), namespace="test", deadline=datetime.now(UTC))
    session = await adapter.start_session(context)
    try:
        with pytest.raises(AdapterError, match="configured size"):
            await adapter.invoke(session, Message(text="hello"), context)
    finally:
        await adapter.aclose()


def test_egress_rejects_unapproved_hosts_before_resolution():
    with pytest.raises(JournalError, match="not explicitly allowed"):
        check_egress_url("https://169.254.169.254/latest", allowed_hosts=["agent.example"])


async def test_factory_uses_numeric_pinned_transport_for_real_loopback_http():
    async def handle(reader, writer):
        try:
            await reader.readline()
            headers = {}
            while line := await reader.readline():
                if line == b"\r\n":
                    break
                name, value = line.decode("latin-1").split(":", 1)
                headers[name.lower()] = value.strip()
            body = await reader.readexactly(int(headers.get("content-length", "0")))
            message = json.loads(body)["message"]
            payload = json.dumps({"text": f"loopback:{message}"}).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                + str(len(payload)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + payload
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    data = http_connection()
    adapter = factory_for_connection(data, content_hash(data), http_binding(data, f"http://127.0.0.1:{port}/run"))()
    context = InvocationContext(
        attempt_id=str(uuid.uuid4()), namespace="loopback", deadline=datetime.now(UTC) + timedelta(seconds=5)
    )
    try:
        session = await adapter.start_session(context)
        reply = await adapter.invoke(session, Message(text="hello"), context)
        assert reply.text == "loopback:hello"
    finally:
        await adapter.aclose()
        server.close()
        await server.wait_closed()


async def test_websocket_binding_uses_pinned_loopback_socket():
    import websockets

    from susanoo_connectors.streaming import StreamConnection

    async def echo(websocket):
        await websocket.recv()
        await websocket.send(json.dumps({"id": "1", "type": "delta", "text": "websocket:hello"}))
        await websocket.send(json.dumps({"id": "2", "type": "done"}))

    server = await websockets.serve(echo, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    connection = StreamConnection(
        kind="websocket",
        url=f"ws://localhost:{port}/agent",
        request={"message": "{{message.text}}"},
    )
    data = {"family": "I05", "adapter": "websocket"}
    digest = content_hash(data)
    binding = {
        digest: {
            "adapter": "websocket",
            "config": connection.model_dump(mode="json"),
            "allowed_hosts": ["localhost"],
            "allow_private_cidrs": PRIVATE_LOOPBACKS,
        }
    }
    adapter = factory_for_connection(data, digest, binding)()
    context = InvocationContext(
        attempt_id="websocket-loopback", namespace="test", deadline=datetime.now(UTC) + timedelta(seconds=5)
    )
    try:
        session = await adapter.start_session(context)
        reply = await adapter.invoke(session, Message(text="hello"), context)
        assert reply.text == "websocket:hello"
        assert "127.0.0.1" in adapter.pinned_addresses
    finally:
        await adapter.aclose()
        server.close()
        await server.wait_closed()


def test_mcp_tool_only_cannot_be_bound_as_agent():
    data = {"family": "I09", "adapter": "mcp_tools"}
    digest = content_hash(data)
    binding = {
        digest: {
            "adapter": "mcp_tools",
            "config": {
                "kind": "mcp",
                "protocol_revision": "2025-11-25",
                "transport": "stdio",
                "command": ["python", "server.py"],
                "allowed_tools": ["lookup"],
            },
            "command_approved": True,
            "approved_argv": ["python", "server.py"],
        }
    }
    with pytest.raises(JournalError, match="tool-only"):
        factory_for_connection(data, digest, binding)


async def test_mcp_http_binding_uses_pinned_transport_and_typed_agent_driver():
    import uvicorn
    from collections_demo.mcp_server import build_server
    from susanoo_connectors.mcp import McpConnection, McpToolClient
    from susanoo_runner import AppServer

    class McpHttpServer(AppServer):
        def __init__(self, app):
            super().__init__(app)
            self._server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
            self._thread = threading.Thread(target=self._server.run, kwargs={"sockets": [self._socket]}, daemon=True)

    provider = build_server("correct")
    with McpHttpServer(provider.streamable_http_app()) as server:
        connection = McpConnection(
            protocol_revision="2026-07-28",
            transport="streamable_http",
            url=f"{server.base_url}/mcp",
            allowed_tools=["chat"],
        )
        data = {"family": "I09", "adapter": "mcp_agent"}
        digest = content_hash(data)
        binding = {
            digest: {
                "adapter": "mcp_agent",
                "config": connection.model_dump(mode="json"),
                "allowed_hosts": ["127.0.0.1"],
                "allow_private_cidrs": PRIVATE_LOOPBACKS,
                "agent_driver": {
                    "kind": "agent_tool",
                    "tool": "chat",
                    "arguments": {
                        "session_id": "{{session_id}}",
                        "message": "{{message.text}}",
                        "context": "{{context}}",
                    },
                    "response": {"text_path": "text"},
                },
            }
        }
        adapter = factory_for_connection(data, digest, binding)()
        assert isinstance(adapter.tools, McpToolClient)
        assert adapter.tools._http_transport is not None
        context = InvocationContext(
            attempt_id="mcp-loopback", namespace="test", deadline=datetime.now(UTC) + timedelta(seconds=10)
        )
        try:
            session = await adapter.start_session(context)
            reply = await adapter.invoke(session, Message(text="hello"), context)
            assert reply.text
        finally:
            await adapter.aclose()


async def test_grpc_binding_pins_numeric_target_and_preserves_authority():
    grpc = pytest.importorskip("grpc")
    from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

    from susanoo_connectors.grpc_connector import GrpcConnection, GrpcMethod

    file = descriptor_pb2.FileDescriptorProto(name="binding.proto", package="binding", syntax="proto3")
    for name in ("ChatRequest", "ChatReply"):
        message = file.message_type.add(name=name)
        message.field.add(name="text", number=1, label=1, type=9)
    service = file.service.add(name="ChatService")
    service.method.add(name="Chat", input_type=".binding.ChatRequest", output_type=".binding.ChatReply")
    descriptor_set = descriptor_pb2.FileDescriptorSet()
    descriptor_set.file.add().CopyFrom(file)
    raw_descriptors = descriptor_set.SerializeToString()
    pool = descriptor_pool.DescriptorPool()
    pool.Add(file)
    reply_type = message_factory.GetMessageClass(pool.FindMessageTypeByName("binding.ChatReply"))

    def echo(raw, context):
        request_type = message_factory.GetMessageClass(pool.FindMessageTypeByName("binding.ChatRequest"))
        request = request_type.FromString(raw)
        return reply_type(text=f"grpc:{request.text}").SerializeToString()

    server = grpc.aio.server()
    handler = grpc.unary_unary_rpc_method_handler(
        echo, request_deserializer=lambda value: value, response_serializer=lambda value: value
    )
    server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler("binding.ChatService", {"Chat": handler}),))
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()

    connection = GrpcConnection(
        target=f"localhost:{port}",
        tls=False,
        descriptor_set_b64=base64.b64encode(raw_descriptors).decode(),
        descriptor_digest=hashlib.sha256(raw_descriptors).hexdigest(),
        methods={
            "chat": GrpcMethod(
                service="binding.ChatService",
                method="Chat",
                request_type="binding.ChatRequest",
                response_type="binding.ChatReply",
                mutating=True,
                request={"text": "{{message.text}}"},
                response={"text_path": "text"},
            )
        },
        invoke_method="chat",
    )
    data = {"family": "I10", "adapter": "grpc"}
    digest = content_hash(data)
    binding = {
        digest: {
            "adapter": "grpc",
            "config": connection.model_dump(mode="json"),
            "allowed_hosts": ["localhost"],
            "allow_private_cidrs": PRIVATE_LOOPBACKS,
        }
    }
    adapter = factory_for_connection(data, digest, binding)()
    try:
        transport = adapter.client.transport
        assert transport.target == f"127.0.0.1:{port}"
        assert transport.authority == "localhost"
        context = InvocationContext(
            attempt_id="grpc-loopback", namespace="test", deadline=datetime.now(UTC) + timedelta(seconds=5)
        )
        session = await adapter.start_session(context)
        reply = await adapter.invoke(session, Message(text="hello"), context)
        assert reply.text == "grpc:hello"
    finally:
        await adapter.aclose()
        await server.stop(0)


@pytest.mark.skipif(importlib.util.find_spec("playwright") is None, reason="playwright is optional")
async def test_browser_binding_uses_pinned_playwright_for_loopback():
    from pathlib import Path

    from playwright.async_api import async_playwright

    playwright = await async_playwright().start()
    try:
        if not Path(playwright.chromium.executable_path).exists():
            pytest.skip("Chromium is not installed")
    finally:
        await playwright.stop()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"""<!doctype html><input id=message><button id=send>Send</button><p id=reply></p>
            <script>document.querySelector('#send').onclick=()=>{const p=document.querySelector('#reply');
            p.textContent='browser:'+document.querySelector('#message').value;p.dataset.complete='yes';};</script>"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
        allow_reuse_address = True
        daemon_threads = True

    server = Server(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    data = {"family": "I11", "adapter": "browser"}
    digest = content_hash(data)
    binding = {
        digest: {
            "adapter": "browser",
            "config": {
                "kind": "browser",
                "start_url": base,
                "allowed_origins": [base],
                "selectors": {
                    "message": "#message",
                    "send": "#send",
                    "done": "#reply[data-complete=yes]",
                    "reply": "#reply",
                },
                "turn": [
                    {"action": "fill", "selector": "message", "value_from": "message_text"},
                    {"action": "click", "selector": "send"},
                ],
                "completion_selector": "done",
                "reply_selector": "reply",
            },
            "allowed_hosts": ["127.0.0.1"],
            "allow_private_cidrs": PRIVATE_LOOPBACKS,
        }
    }
    adapter = factory_for_connection(data, digest, binding)()
    context = InvocationContext(
        attempt_id="browser-loopback", namespace="test", deadline=datetime.now(UTC) + timedelta(seconds=20)
    )
    try:
        session = await adapter.start_session(context)
        reply = await adapter.invoke(session, Message(text="hello"), context)
        assert reply.text == "browser:hello"
    finally:
        await adapter.aclose()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
