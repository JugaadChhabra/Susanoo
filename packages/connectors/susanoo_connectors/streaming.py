"""Streaming agent adapters (I05): Server-Sent Events and WebSocket [Spec §4, §19].

Incremental events need stable message IDs; the final response is assembled from deltas until the
declared completion event. Duplicate event IDs are ignored, total bytes are bounded, and a stream
that ends before completion is an adapter error. Reconnecting is never treated as a safe replay.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Literal

import httpx
import websockets
from pydantic import Field, model_validator
from susanoo_contracts import Capabilities
from susanoo_evaluator.dsl import PATH_PATTERN
from susanoo_sdk import (
    AdapterError,
    AgentReply,
    CancelResult,
    CapabilityReport,
    InvocationContext,
    Message,
    SessionHandle,
    Unavailable,
)

from .mapping import AuthRef, Model, extract, render, validate_template


class StreamConnection(Model):
    kind: Literal["sse", "websocket"]
    url: str = Field(pattern=r"^(https?|wss?)://")
    auth: AuthRef = Field(default_factory=AuthRef)
    request: dict[str, Any]
    event_id_path: str = Field(default="id", pattern=PATH_PATTERN)
    event_type_path: str = Field(default="type", pattern=PATH_PATTERN)
    delta_type: str = "delta"
    done_type: str = "done"
    delta_text_path: str = Field(default="text", pattern=PATH_PATTERN)
    max_total_bytes: int = Field(default=1_048_576, ge=1024)
    timeout_seconds: float = Field(default=60, gt=0, le=300)

    @model_validator(mode="after")
    def typed(self) -> StreamConnection:
        validate_template(self.request)
        return self


class _Assembler:
    def __init__(self, connection: StreamConnection):
        self.c = connection
        self.seen: set[str] = set()
        self.parts: list[str] = []
        self.total = 0
        self.done = False

    def feed(self, raw: str) -> None:
        self.total += len(raw.encode())
        if self.total > self.c.max_total_bytes:
            raise AdapterError("output_limit", "Stream exceeded the configured size")
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            raise AdapterError("malformed_event", "Stream event is not JSON") from None
        event_id = str(extract(event, self.c.event_id_path))
        if event_id in self.seen:
            return  # duplicates after reconnection are ignored, not double-counted
        self.seen.add(event_id)
        kind = extract(event, self.c.event_type_path)
        if kind == self.c.delta_type:
            text = extract(event, self.c.delta_text_path)
            if not isinstance(text, str):
                raise AdapterError("malformed_event", "Delta text is not a string")
            self.parts.append(text)
        elif kind == self.c.done_type:
            self.done = True

    def reply(self, invocation_id: str, session_id: str) -> AgentReply:
        if not self.done:
            raise AdapterError("stream_interrupted", "Stream ended before completion; not replayed automatically")
        return AgentReply(
            invocation_id=invocation_id,
            session_id=session_id,
            text="".join(self.parts),
            usage=Unavailable(reason="stream does not report usage"),
            tool_events=Unavailable(reason="stream does not report tool events"),
        )


class _Base:
    def __init__(self, connection: StreamConnection):
        self.connection = connection

    async def preflight(self) -> CapabilityReport:
        caps = Capabilities(invoke=True, stream=True, sessions=True, multi_turn=True)
        return CapabilityReport(adapter=self.connection.kind, adapter_version="1.0", capabilities=caps)

    async def start_session(self, context: InvocationContext) -> SessionHandle:
        return SessionHandle(session_id=f"s-{uuid.uuid4().hex[:16]}", attempt_id=context.attempt_id)

    async def cancel(self, invocation_id: str) -> CancelResult:
        return CancelResult(invocation_id=invocation_id, acknowledged=False, note="closing the stream is not a cancel")

    async def close_session(self, session: SessionHandle) -> None:
        return None


class SseAgentAdapter(_Base):
    def __init__(self, connection: StreamConnection, *, transport: httpx.AsyncBaseTransport | None = None):
        if connection.kind != "sse":
            raise ValueError("SseAgentAdapter needs an sse connection")
        super().__init__(connection)
        self._client = httpx.AsyncClient(timeout=connection.timeout_seconds, transport=transport)

    async def invoke(self, session: SessionHandle, message: Message, context: InvocationContext) -> AgentReply:
        c = self.connection
        invocation_id = f"inv-{uuid.uuid4().hex[:12]}"
        assembler = _Assembler(c)
        body = render(c.request, message, context, session.session_id)
        headers = {**c.auth.headers(), "Accept": "text/event-stream", "Idempotency-Key": invocation_id}
        try:
            async with self._client.stream("POST", c.url, json=body, headers=headers) as response:
                if response.status_code >= 400:
                    raise AdapterError(f"http_{response.status_code}", "Stream endpoint returned an error")
                data: list[str] = []
                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        data.append(line[5:].lstrip())
                    elif line == "" and data:
                        assembler.feed("\n".join(data))
                        data = []
                        if assembler.done:
                            break
        except (httpx.TimeoutException, httpx.TransportError) as error:
            raise AdapterError("stream_interrupted", f"Stream failed: {type(error).__name__}") from None
        return assembler.reply(invocation_id, session.session_id)

    async def aclose(self) -> None:
        await self._client.aclose()


class WebSocketAgentAdapter(_Base):
    def __init__(self, connection: StreamConnection):
        if connection.kind != "websocket":
            raise ValueError("WebSocketAgentAdapter needs a websocket connection")
        super().__init__(connection)

    async def invoke(self, session: SessionHandle, message: Message, context: InvocationContext) -> AgentReply:
        c = self.connection
        invocation_id = f"inv-{uuid.uuid4().hex[:12]}"
        assembler = _Assembler(c)
        try:
            async with websockets.connect(
                c.url, additional_headers=c.auth.headers(), max_size=c.max_total_bytes, open_timeout=c.timeout_seconds
            ) as socket:
                await socket.send(json.dumps(render(c.request, message, context, session.session_id)))
                async for frame in socket:
                    assembler.feed(frame if isinstance(frame, str) else frame.decode())
                    if assembler.done:
                        break
        except (OSError, websockets.WebSocketException) as error:
            raise AdapterError("stream_interrupted", f"WebSocket failed: {type(error).__name__}") from None
        return assembler.reply(invocation_id, session.session_id)

    async def aclose(self) -> None:
        return None
