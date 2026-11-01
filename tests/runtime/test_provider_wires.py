"""Optional real local transports and SDK vectors, not live customer/provider qualification."""

import os
from datetime import UTC, datetime, timedelta

import pytest


async def test_real_grpcio_loopback_transport():
    grpc = pytest.importorskip("grpc")
    from susanoo_connectors.grpc_connector import GrpcioTransport

    async def echo(value, context):
        assert dict(context.invocation_metadata())["x-susanoo-test"] == "synthetic"
        return b"reply:" + value

    server = grpc.aio.server()
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                "susanoo.integration.Echo",
                {
                    "Run": grpc.unary_unary_rpc_method_handler(echo),
                },
            ),
        )
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    transport = GrpcioTransport(f"127.0.0.1:{port}", tls=False)
    try:
        assert (
            await transport.unary_unary(
                "/susanoo.integration.Echo/Run", b"request", timeout=3, metadata=[("x-susanoo-test", "synthetic")]
            )
            == b"reply:request"
        )
    finally:
        await transport.aclose()
        await server.stop(0)


def test_twilio_signature_matches_installed_provider_sdk():
    validator_module = pytest.importorskip("twilio.request_validator")
    from susanoo_connectors.media.twilio import compute_signature, validate_signature

    secret = "synthetic-not-a-provider-credential"
    url = "https://callbacks.example.test/voice/status"
    params = {"CallSid": "CA" + "1" * 32, "CallStatus": "completed"}
    validator = validator_module.RequestValidator(secret)
    signature = validator.compute_signature(url, params)
    assert signature == compute_signature(secret, url, params)
    assert validate_signature(secret, url, signature, params=params).valid
    assert validator.validate(url, params, signature)
    assert not validate_signature(secret, url + "/wrong", signature, params=params).valid


@pytest.mark.skipif(os.environ.get("SUSANOO_BROWSER_TESTS") != "1", reason="Opt in with installed Chromium")
async def test_real_playwright_browser_adapter():
    pytest.importorskip("playwright.async_api")
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse
    from susanoo_connectors.browser import BrowserAgentAdapter, BrowserConnection, PlaywrightDriver
    from susanoo_runner.local import AppServer
    from susanoo_sdk import InvocationContext, Message

    app = FastAPI()

    @app.get("/", response_class=HTMLResponse)
    def page():
        return """<!doctype html><title>Synthetic browser agent</title>
        <label>Message<input id="message"></label><button id="send">Send</button><p id="reply"></p>
        <script>document.querySelector('#send').onclick=()=>{
        document.querySelector('#reply').textContent='Recorded: '+document.querySelector('#message').value;
        document.querySelector('#reply').dataset.complete='yes';};</script>"""

    with AppServer(app) as server:
        adapter = BrowserAgentAdapter(
            BrowserConnection(
                start_url=server.base_url,
                allowed_origins=[server.base_url],
                selectors={
                    "message": "#message",
                    "send": "#send",
                    "done": "#reply[data-complete=yes]",
                    "reply": "#reply",
                },
                turn=[
                    {"action": "fill", "selector": "message", "value_from": "message_text"},
                    {"action": "click", "selector": "send"},
                ],
                completion_selector="done",
                reply_selector="reply",
            ),
            PlaywrightDriver(),
        )
        context = InvocationContext(
            attempt_id="browser-wire", namespace="synthetic", deadline=datetime.now(UTC) + timedelta(seconds=30)
        )
        try:
            session = await adapter.start_session(context)
            reply = await adapter.invoke(session, Message(text="Synthetic test"), context)
            assert reply.text == "Recorded: Synthetic test"
            assert reply.usage.unavailable and reply.tool_events.unavailable
            await adapter.close_session(session)
        finally:
            await adapter.aclose()
