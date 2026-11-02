"""Synchronous (I03) and asynchronous submit/poll (I04) HTTP agent adapters [Spec §4, §19].

Sync: one request per turn through a typed template; JSON REST or GraphQL are both just templates.
Async: submit once, then poll reads until a declared terminal state. A submit that times out or
loses its response is *uncertain* and is never resubmitted; cancellation is attempted only when the
connection declares a cancel endpoint, and its acknowledgement is not proof the remote work stopped.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Literal

import httpx
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

from .mapping import AuthRef, Model, ResponseMapping, extract, render, validate_template


class HttpConnection(Model):
    kind: Literal["http_sync"] = "http_sync"
    url: str = Field(pattern=r"^https?://")
    method: Literal["POST", "PUT"] = "POST"
    headers: dict[str, str] = Field(default_factory=dict)
    auth: AuthRef = Field(default_factory=AuthRef)
    request: dict[str, Any]
    response: ResponseMapping
    session_mode: Literal["client", "server", "none"] = "client"
    timeout_seconds: float = Field(default=60, gt=0, le=300)
    max_response_bytes: int = Field(default=262_144, ge=1024)
    capabilities: Capabilities = Field(
        default_factory=lambda: Capabilities(invoke=True, sessions=True, multi_turn=True)
    )

    @model_validator(mode="after")
    def typed_template(self) -> HttpConnection:
        validate_template(self.request)
        if any(name.lower() in {"authorization", "cookie"} for name in self.headers):
            raise ValueError("Credentials go through auth secret references, never literal headers")
        if self.session_mode == "server" and not self.response.session_path:
            raise ValueError("Server-issued sessions need response.session_path")
        return self


class AsyncHttpConnection(Model):
    kind: Literal["http_async"] = "http_async"
    submit_url: str = Field(pattern=r"^https?://")
    status_url: str = Field(pattern=r"^https?://", description="Must contain {task_id}")
    cancel_url: str | None = Field(default=None, pattern=r"^https?://")
    auth: AuthRef = Field(default_factory=AuthRef)
    request: dict[str, Any]
    task_id_path: str = Field(pattern=PATH_PATTERN)
    status_path: str = Field(pattern=PATH_PATTERN)
    terminal_success: list[str] = Field(min_length=1)
    terminal_failure: list[str] = Field(default_factory=list)
    response: ResponseMapping
    poll_interval_seconds: float = Field(default=0.5, gt=0, le=60)
    submit_timeout_seconds: float = Field(default=30, gt=0, le=300)
    max_poll_seconds: float = Field(default=300, gt=0, le=3600)

    @model_validator(mode="after")
    def typed(self) -> AsyncHttpConnection:
        validate_template(self.request)
        if "{task_id}" not in self.status_url or (self.cancel_url and "{task_id}" not in self.cancel_url):
            raise ValueError("status_url and cancel_url must contain {task_id}")
        return self


def _reply(body: Any, mapping: ResponseMapping, invocation_id: str, session_id: str) -> AgentReply:
    text = extract(body, mapping.text_path)
    structured = extract(body, mapping.structured_path)
    if text is not None and not isinstance(text, str):
        raise AdapterError("response_mapping", "Mapped text is not a string")
    if structured is not None and not isinstance(structured, dict):
        raise AdapterError("response_mapping", "Mapped structured value is not an object")
    return AgentReply(
        invocation_id=invocation_id,
        session_id=session_id,
        text=text,
        structured=structured,
        usage=Unavailable(reason="HTTP connection does not report usage"),
        tool_events=Unavailable(reason="HTTP connection does not report tool events"),
    )


class HttpAgentAdapter:
    def __init__(self, connection: HttpConnection, *, transport: httpx.AsyncBaseTransport | None = None):
        self.connection = connection
        self._client = httpx.AsyncClient(timeout=connection.timeout_seconds, transport=transport)

    async def preflight(self) -> CapabilityReport:
        return CapabilityReport(
            adapter="http_sync",
            adapter_version="1.0",
            capabilities=self.connection.capabilities,
            notes=["capabilities are declared by the connection, not inferred"],
        )

    async def start_session(self, context: InvocationContext) -> SessionHandle:
        return SessionHandle(session_id=f"s-{uuid.uuid4().hex[:16]}", attempt_id=context.attempt_id)

    async def invoke(self, session: SessionHandle, message: Message, context: InvocationContext) -> AgentReply:
        c = self.connection
        invocation_id = f"inv-{uuid.uuid4().hex[:12]}"
        body = render(c.request, message, context, session.session_id)
        headers = {**c.headers, **c.auth.headers(), "Idempotency-Key": invocation_id}
        try:
            async with self._client.stream(c.method, c.url, json=body, headers=headers) as response:
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > c.max_response_bytes:
                        raise AdapterError("output_limit", "Agent response exceeded the configured size")
        except httpx.TimeoutException:
            raise AdapterError(
                "invocation_uncertain", "No response before the timeout; the agent may have acted"
            ) from None
        except httpx.TransportError as error:
            raise AdapterError("invocation_uncertain", f"Transport failed: {type(error).__name__}") from None
        if response.status_code >= 400:
            raise AdapterError(f"http_{response.status_code}", "Agent endpoint returned an error status")
        try:
            data = httpx.Response(200, content=bytes(raw)).json()
        except ValueError:
            raise AdapterError("malformed_response", "Agent response is not JSON") from None
        session_id = session.session_id
        if c.session_mode == "server":
            session_id = str(extract(data, c.response.session_path))
        return _reply(data, c.response, invocation_id, session_id)

    async def cancel(self, invocation_id: str) -> CancelResult:
        return CancelResult(invocation_id=invocation_id, acknowledged=False, note="sync HTTP has no remote cancel")

    async def close_session(self, session: SessionHandle) -> None:
        return None

    async def aclose(self) -> None:
        await self._client.aclose()


class AsyncHttpAgentAdapter:
    def __init__(self, connection: AsyncHttpConnection, *, transport: httpx.AsyncBaseTransport | None = None):
        self.connection = connection
        self._client = httpx.AsyncClient(transport=transport, follow_redirects=False, trust_env=False)
        self._tasks: dict[str, str] = {}

    async def preflight(self) -> CapabilityReport:
        caps = Capabilities(
            invoke=True,
            async_jobs=True,
            sessions=True,
            multi_turn=True,
            cancel_remote=self.connection.cancel_url is not None,
        )
        return CapabilityReport(adapter="http_async", adapter_version="1.0", capabilities=caps)

    async def start_session(self, context: InvocationContext) -> SessionHandle:
        return SessionHandle(session_id=f"s-{uuid.uuid4().hex[:16]}", attempt_id=context.attempt_id)

    async def invoke(self, session: SessionHandle, message: Message, context: InvocationContext) -> AgentReply:
        c = self.connection
        invocation_id = f"inv-{uuid.uuid4().hex[:12]}"
        headers = {**c.auth.headers(), "Idempotency-Key": invocation_id}
        try:
            submitted = await self._client.post(
                c.submit_url,
                json=render(c.request, message, context, session.session_id),
                headers=headers,
                timeout=c.submit_timeout_seconds,
            )
        except (httpx.TimeoutException, httpx.TransportError):
            raise AdapterError("submit_uncertain", "Submit may have been accepted; never resubmitted") from None
        if submitted.status_code >= 400:
            raise AdapterError(f"http_{submitted.status_code}", "Submit was rejected")
        task_id = str(extract(submitted.json(), c.task_id_path))
        self._tasks[invocation_id] = task_id
        deadline = time.monotonic() + c.max_poll_seconds
        while True:
            try:
                polled = await self._client.get(
                    c.status_url.format(task_id=task_id), headers=c.auth.headers(), timeout=c.submit_timeout_seconds
                )
            except (httpx.TimeoutException, httpx.TransportError):
                polled = None  # a failed read is retried within the poll budget; reads are safe
            if polled is not None and polled.status_code < 400:
                body = polled.json()
                status = str(extract(body, c.status_path))
                if status in c.terminal_success:
                    self._tasks.pop(invocation_id, None)
                    return _reply(body, c.response, invocation_id, session.session_id)
                if status in c.terminal_failure:
                    self._tasks.pop(invocation_id, None)
                    raise AdapterError("remote_task_failed", f"Remote task ended {status}")
            if time.monotonic() > deadline:
                raise AdapterError("poll_deadline", f"Task {task_id} not terminal before the poll deadline")
            await asyncio.sleep(c.poll_interval_seconds)

    async def cancel(self, invocation_id: str) -> CancelResult:
        task_id = self._tasks.get(invocation_id)
        if not self.connection.cancel_url or task_id is None:
            return CancelResult(invocation_id=invocation_id, acknowledged=False, note="no declared remote cancel")
        response = await self._client.post(
            self.connection.cancel_url.format(task_id=task_id), headers=self.connection.auth.headers()
        )
        return CancelResult(
            invocation_id=invocation_id,
            acknowledged=response.status_code < 400,
            remote_cancel_attempted=True,
            note="acknowledgement is not proof the work stopped",
        )

    async def close_session(self, session: SessionHandle) -> None:
        return None

    async def aclose(self) -> None:
        await self._client.aclose()
