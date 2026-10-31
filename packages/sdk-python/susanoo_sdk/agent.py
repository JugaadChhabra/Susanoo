"""Agent-author API: subclass ``SusanooAgent``, call tools through ``ToolClient``, serve over JSONL.

An agent only receives an ``InvocationContext`` and the user's message. It reaches the environment
through the approved tool route in that context; it never sees fixture truth or checks.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
from pydantic import Field
from susanoo_contracts import Capabilities

from .models import (
    AgentReply,
    CancelResult,
    CapabilityReport,
    InvocationContext,
    Message,
    SessionHandle,
    ToolEvent,
    ToolRoute,
    Unavailable,
    Usage,
    Wire,
)


class Reply(Wire):
    text: str | None = None
    structured: dict[str, Any] | None = None
    tool_events: list[ToolEvent] | None = None
    usage: Usage | None = None


class SusanooAgent:
    """Base class for agents under test. Override ``respond``; the rest has safe defaults."""

    name = "agent"
    version = "0.0.0"
    capabilities = Capabilities(invoke=True, sessions=True, multi_turn=True, tool_events=True)

    async def start_session(self, context: InvocationContext) -> str:
        return f"session-{uuid.uuid4().hex[:12]}"

    async def respond(self, session_id: str, message: Message, context: InvocationContext) -> Reply:
        raise NotImplementedError

    async def close_session(self, session_id: str) -> None:
        return None


def to_agent_reply(reply: Reply, invocation_id: str, session_id: str, limit: int = 262144) -> AgentReply:
    text, truncated = reply.text, False
    if text is not None and len(text.encode()) > limit:
        text, truncated = text.encode()[:limit].decode(errors="ignore"), True
    return AgentReply(
        invocation_id=invocation_id,
        session_id=session_id,
        text=text,
        structured=reply.structured,
        usage=reply.usage or Unavailable(reason="agent did not report usage"),
        tool_events=reply.tool_events if reply.tool_events is not None else Unavailable(reason="not reported"),
        truncated=truncated,
    )


class InProcessAgentAdapter:
    """Same-process adapter for trusted examples and unit tests. Real customer code uses ``JsonlAgentProcess``."""

    def __init__(self, agent: SusanooAgent):
        self.agent = agent

    async def preflight(self) -> CapabilityReport:
        return CapabilityReport(
            adapter="in_process",
            adapter_version=self.agent.version,
            capabilities=self.agent.capabilities,
            notes=["in-process adapter: not an isolation boundary"],
        )

    async def start_session(self, context: InvocationContext) -> SessionHandle:
        return SessionHandle(session_id=await self.agent.start_session(context), attempt_id=context.attempt_id)

    async def invoke(self, session: SessionHandle, message: Message, context: InvocationContext) -> AgentReply:
        invocation_id = f"inv-{uuid.uuid4().hex[:12]}"
        reply = await self.agent.respond(session.session_id, message, context)
        return to_agent_reply(reply, invocation_id, session.session_id)

    async def cancel(self, invocation_id: str) -> CancelResult:
        return CancelResult(invocation_id=invocation_id, acknowledged=False, note="in-process agent has no cancel")

    async def close_session(self, session: SessionHandle) -> None:
        await self.agent.close_session(session.session_id)


class ToolTransportError(Exception):
    """The environment did not answer cleanly. The write may or may not have happened."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class ToolCall(Wire):
    operation: str
    status: str
    code: str | None = None
    response: dict[str, Any] = Field(default_factory=dict)
    seq: int | None = None


class ToolClient:
    """Calls environment operations through the approved route. One client per invocation context."""

    def __init__(self, route: ToolRoute, *, timeout: float = 30.0, transport: httpx.AsyncBaseTransport | None = None):
        self.route = route
        self.events: list[ToolEvent] = []
        self._client = httpx.AsyncClient(
            base_url=route.base_url,
            timeout=timeout,
            transport=transport,
            headers={"Authorization": f"Bearer {route.token.get_secret_value()}"},
        )

    async def call(self, operation: str, request: dict[str, Any], idempotency_key: str | None = None) -> ToolCall:
        response = await self._client.post(
            f"/fixtures/{self.route.namespace}/operations/{operation}",
            json={"request": request, "idempotency_key": idempotency_key},
        )
        if response.status_code >= 500:
            self.events.append(ToolEvent(operation=operation, status="transport_error"))
            raise ToolTransportError(response.status_code, response.json().get("detail", "environment error"))
        if response.status_code >= 400:
            raise ToolTransportError(response.status_code, response.text)
        body = response.json()
        self.events.append(ToolEvent(operation=operation, status=body["status"], seq=body.get("seq")))
        return ToolCall(
            operation=operation,
            status=body["status"],
            code=body.get("code"),
            response=body["response"],
            seq=body.get("seq"),
        )

    async def aclose(self) -> None:
        await self._client.aclose()
