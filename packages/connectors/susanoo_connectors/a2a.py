"""A2A agent adapter over JSON-RPC 2.0 / HTTP (I10) [Spec §4 I10, §19 A2A; T21-T23].

Pinned to A2A specification **0.3.0** (JSON-RPC binding). Preflight fetches and validates the Agent
Card: ``protocolVersion`` must equal the pinned version (a mismatch is an explicit preflight
failure, never a best-effort call), the JSON-RPC interface must be on the configured origin, the
required skills must be declared, and ``message/stream`` is used only when the card advertises
``capabilities.streaming``. Only the methods the connection maps are ever sent.

Each turn is one ``message/send`` (or ``message/stream``); the A2A ``contextId`` carries the session
and an ``input-required`` task is continued by ``taskId``. Non-terminal tasks are followed with
``tasks/get`` reads. A send that times out is uncertain and never resent; ``tasks/cancel``
acknowledgement is not proof the remote work stopped.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from susanoo_contracts import Capabilities
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

from .mapping import AuthRef, Model, render, validate_template

A2A_PROTOCOL_VERSION = "0.3.0"
AGENT_CARD_PATH = "/.well-known/agent-card.json"
Method = Literal["message/send", "message/stream", "tasks/get", "tasks/cancel"]
TaskState = Literal[
    "submitted", "working", "input-required", "completed", "canceled", "failed", "rejected", "auth-required", "unknown"
]
TERMINAL = {"completed", "canceled", "failed", "rejected"}
TURN_COMPLETE = {"completed", "input-required"}
JSONRPC_ERRORS = {
    -32700: "a2a_parse_error",
    -32600: "a2a_invalid_request",
    -32601: "a2a_method_not_found",
    -32602: "a2a_invalid_params",
    -32603: "a2a_internal_error",
    -32001: "a2a_task_not_found",
    -32002: "a2a_task_not_cancelable",
    -32003: "a2a_push_not_supported",
    -32004: "a2a_unsupported_operation",
    -32005: "a2a_content_type_not_supported",
    -32006: "a2a_invalid_agent_response",
}


class _Card(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class AgentSkill(_Card):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = ""
    tags: list[str] = Field(default_factory=list)


class AgentCapabilities(_Card):
    streaming: bool = False
    push_notifications: bool = Field(default=False, alias="pushNotifications")
    state_transition_history: bool = Field(default=False, alias="stateTransitionHistory")


class AgentInterface(_Card):
    url: str
    transport: str


class AgentCard(_Card):
    """The fields Susanoo relies on; unknown card fields are kept but never acted upon."""

    protocol_version: str = Field(alias="protocolVersion")
    name: str = Field(min_length=1)
    description: str = ""
    url: str = Field(pattern=r"^https?://")
    version: str = Field(min_length=1)
    preferred_transport: str = Field(default="JSONRPC", alias="preferredTransport")
    additional_interfaces: list[AgentInterface] = Field(default_factory=list, alias="additionalInterfaces")
    capabilities: AgentCapabilities
    default_input_modes: list[str] = Field(alias="defaultInputModes", min_length=1)
    default_output_modes: list[str] = Field(alias="defaultOutputModes", min_length=1)
    skills: list[AgentSkill] = Field(min_length=1)

    def jsonrpc_url(self) -> str | None:
        if self.preferred_transport == "JSONRPC":
            return self.url
        return next((i.url for i in self.additional_interfaces if i.transport == "JSONRPC"), None)


class A2AConnection(Model):
    kind: Literal["a2a"] = "a2a"
    protocol_version: Literal["0.3.0"] = A2A_PROTOCOL_VERSION
    card_url: str = Field(pattern=r"^https?://")
    auth: AuthRef = Field(default_factory=AuthRef)
    methods: list[Method] = Field(default_factory=lambda: ["message/send", "tasks/get"], min_length=1)
    required_skills: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict, description="Closed placeholders only")
    accepted_output_modes: list[str] = Field(default_factory=lambda: ["text/plain"], min_length=1)
    timeout_seconds: float = Field(default=60, gt=0, le=300)
    max_task_seconds: float = Field(default=300, gt=0, le=3600)
    poll_interval_seconds: float = Field(default=0.2, gt=0, le=60)
    max_response_bytes: int = Field(default=1_048_576, ge=1024)

    @model_validator(mode="after")
    def typed(self) -> A2AConnection:
        validate_template(self.metadata)
        if "message/send" not in self.methods and "message/stream" not in self.methods:
            raise ValueError("Map message/send or message/stream; an A2A agent needs one send method")
        return self


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = httpx.URL(url)
    return parsed.scheme, parsed.host, parsed.port


class A2AAgentAdapter:
    def __init__(self, connection: A2AConnection, *, transport: httpx.AsyncBaseTransport | None = None):
        self.connection = connection
        self._client = httpx.AsyncClient(
            timeout=connection.timeout_seconds, transport=transport, follow_redirects=False
        )
        self.card: AgentCard | None = None
        self._endpoint: str | None = None
        self._contexts: dict[str, str] = {}
        self._open_tasks: dict[str, str] = {}
        self._invocations: dict[str, str] = {}
        self.sent: list[str] = []

    # -- preflight -------------------------------------------------------------------------------

    async def _load_card(self) -> AgentCard:
        if self.card is not None:
            return self.card
        c = self.connection
        try:
            response = await self._client.get(c.card_url, headers=c.auth.headers())
        except (httpx.TimeoutException, httpx.TransportError) as error:
            raise AdapterError("preflight_failed", f"Agent Card unreachable: {type(error).__name__}") from None
        if response.status_code != 200:
            raise AdapterError("preflight_failed", f"Agent Card returned HTTP {response.status_code}")
        try:
            raw = response.json()
        except ValueError:
            raise AdapterError("preflight_failed", "Agent Card is not JSON") from None
        version = raw.get("protocolVersion") if isinstance(raw, dict) else None
        if version != c.protocol_version:
            raise AdapterError(
                "protocol_version_mismatch",
                f"Agent Card declares A2A {version!r}; this connector is pinned to {c.protocol_version}",
            )
        try:
            card = AgentCard.model_validate(raw)
        except ValidationError as error:
            raise AdapterError("preflight_failed", f"Agent Card is invalid: {error.error_count()} problem(s)") from None
        endpoint = card.jsonrpc_url()
        if endpoint is None:
            raise AdapterError("preflight_failed", "Agent Card offers no JSON-RPC interface")
        if _origin(endpoint) != _origin(c.card_url):
            raise AdapterError("preflight_failed", "Agent Card points the RPC endpoint at a different origin")
        missing = set(c.required_skills) - {skill.id for skill in card.skills}
        if missing:
            raise AdapterError("preflight_failed", f"Agent Card lacks required skills: {sorted(missing)}")
        if "message/stream" in c.methods and not card.capabilities.streaming:
            raise AdapterError("unsupported_feature", "message/stream is mapped but the agent does not advertise it")
        if not set(c.accepted_output_modes) & set(card.default_output_modes):
            raise AdapterError("preflight_failed", "Agent produces none of the accepted output modes")
        self.card, self._endpoint = card, endpoint
        return card

    async def preflight(self) -> CapabilityReport:
        card = await self._load_card()
        c = self.connection
        caps = Capabilities(
            invoke=True,
            sessions=True,
            multi_turn=True,
            stream="message/stream" in c.methods,
            async_jobs="tasks/get" in c.methods,
            cancel_remote="tasks/cancel" in c.methods,
        )
        return CapabilityReport(
            adapter="a2a",
            adapter_version=c.protocol_version,
            capabilities=caps,
            checks={"agent_card": True, "streaming_advertised": card.capabilities.streaming},
            notes=[f"agent {card.name} {card.version}; methods {sorted(c.methods)}"],
        )

    # -- JSON-RPC --------------------------------------------------------------------------------

    def _require(self, method: Method) -> None:
        if method not in self.connection.methods:
            raise AdapterError("method_not_mapped", f"{method} is not mapped for this connection")

    async def _rpc(self, method: Method, params: dict[str, Any], *, mutating: bool) -> Any:
        self._require(method)
        await self._load_card()
        request_id = uuid.uuid4().hex
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        self.sent.append(method)
        raw = bytearray()
        try:
            async with self._client.stream(
                "POST", self._endpoint, json=payload, headers=self.connection.auth.headers()
            ) as response:
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > self.connection.max_response_bytes:
                        raise AdapterError("output_limit", "A2A response exceeded the configured size")
        except httpx.TimeoutException:
            code = "invocation_uncertain" if mutating else "read_timeout"
            raise AdapterError(code, f"{method} timed out; never resent") from None
        except httpx.TransportError as error:
            code = "invocation_uncertain" if mutating else "transport_error"
            raise AdapterError(code, f"{method} failed: {type(error).__name__}") from None
        if 300 <= response.status_code < 400:
            raise AdapterError("redirect_blocked", "A2A endpoint redirected")
        if response.status_code >= 400:
            raise AdapterError(f"http_{response.status_code}", f"{method} returned an error status")
        return self._unwrap(bytes(raw), request_id, method)

    def _unwrap(self, raw: bytes | str, request_id: str, method: str) -> Any:
        try:
            body = json.loads(raw)
        except ValueError:
            raise AdapterError("malformed_response", f"{method} response is not JSON") from None
        if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or body.get("id") != request_id:
            raise AdapterError("malformed_response", f"{method} response is not a correlated JSON-RPC 2.0 reply")
        if "error" in body:
            error = body["error"] if isinstance(body["error"], dict) else {}
            code = JSONRPC_ERRORS.get(error.get("code"), "a2a_error")
            raise AdapterError(code, str(error.get("message", "A2A error"))[:500])
        if "result" not in body:
            raise AdapterError("malformed_response", f"{method} reply has neither result nor error")
        return body["result"]

    # -- AgentAdapter ----------------------------------------------------------------------------

    async def start_session(self, context: InvocationContext) -> SessionHandle:
        await self._load_card()
        return SessionHandle(session_id=f"s-{uuid.uuid4().hex[:16]}", attempt_id=context.attempt_id)

    def _message(self, session: SessionHandle, message: Message, context: InvocationContext) -> dict[str, Any]:
        parts: list[dict[str, Any]] = [{"kind": "text", "text": message.text}]
        if message.structured is not None:
            parts.append({"kind": "data", "data": message.structured})
        body: dict[str, Any] = {"kind": "message", "role": "user", "messageId": uuid.uuid4().hex, "parts": parts}
        if session.session_id in self._contexts:
            body["contextId"] = self._contexts[session.session_id]
        if session.session_id in self._open_tasks:
            body["taskId"] = self._open_tasks[session.session_id]
        metadata = render(self.connection.metadata, message, context, session.session_id)
        if metadata:
            body["metadata"] = metadata
        return body

    async def invoke(self, session: SessionHandle, message: Message, context: InvocationContext) -> AgentReply:
        c = self.connection
        invocation_id = f"inv-{uuid.uuid4().hex[:12]}"
        params = {
            "message": self._message(session, message, context),
            "configuration": {"blocking": True, "acceptedOutputModes": c.accepted_output_modes},
        }
        if "message/stream" in c.methods:
            result = await self._stream(params, invocation_id, session)
        else:
            result = await self._rpc("message/send", params, mutating=True)
        return await self._settle(result, invocation_id, session)

    async def _settle(self, result: Any, invocation_id: str, session: SessionHandle) -> AgentReply:
        if not isinstance(result, dict) or result.get("kind") not in {"task", "message"}:
            raise AdapterError("malformed_response", "A2A result is neither a Task nor a Message")
        if result["kind"] == "message":
            if result.get("contextId"):
                self._contexts[session.session_id] = result["contextId"]
            return self._reply(_text(result.get("parts")), invocation_id, session, None)
        task = result
        self._track(task, invocation_id, session)
        deadline = time.monotonic() + self.connection.max_task_seconds
        while _state(task) not in TERMINAL | TURN_COMPLETE:
            if _state(task) == "auth-required":
                raise AdapterError("auth_required", "A2A task needs authentication the runner does not hold")
            if "tasks/get" not in self.connection.methods:
                raise AdapterError("task_not_terminal", f"Task is {_state(task)} and tasks/get is not mapped")
            if time.monotonic() > deadline:
                raise AdapterError("task_deadline", f"Task {task['id']} not settled before the deadline")
            await asyncio.sleep(self.connection.poll_interval_seconds)
            try:
                task = await self._rpc("tasks/get", {"id": task["id"]}, mutating=False)
            except AdapterError as error:
                if error.code not in {"read_timeout", "transport_error"}:
                    raise
                continue  # reads are safe to repeat within the deadline
            if not isinstance(task, dict) or task.get("kind") != "task":
                raise AdapterError("malformed_response", "tasks/get did not return a Task")
            self._track(task, invocation_id, session)
        return self._finish(task, invocation_id, session)

    def _track(self, task: dict[str, Any], invocation_id: str, session: SessionHandle) -> None:
        if not isinstance(task.get("id"), str) or not isinstance(task.get("contextId"), str):
            raise AdapterError("malformed_response", "A2A Task lacks id or contextId")
        self._contexts[session.session_id] = task["contextId"]
        self._invocations[invocation_id] = task["id"]

    def _finish(self, task: dict[str, Any], invocation_id: str, session: SessionHandle) -> AgentReply:
        state = _state(task)
        self._invocations.pop(invocation_id, None)
        if state == "input-required":
            self._open_tasks[session.session_id] = task["id"]
        else:
            self._open_tasks.pop(session.session_id, None)
        if state == "failed" or state == "rejected":
            raise AdapterError("remote_task_failed", f"A2A task ended {state}")
        if state == "canceled":
            raise AdapterError("remote_task_canceled", "A2A task was canceled")
        status_text = _text(((task.get("status") or {}).get("message") or {}).get("parts"))
        artifact_text = "".join(_text(a.get("parts")) or "" for a in task.get("artifacts") or [] if isinstance(a, dict))
        text = artifact_text or status_text
        return self._reply(text, invocation_id, session, {"a2a_state": state, "task_id": task["id"]})

    def _reply(
        self, text: str | None, invocation_id: str, session: SessionHandle, structured: dict[str, Any] | None
    ) -> AgentReply:
        return AgentReply(
            invocation_id=invocation_id,
            session_id=session.session_id,
            text=text,
            structured=structured,
            usage=Unavailable(reason="A2A does not report usage"),
            tool_events=Unavailable(reason="A2A does not report tool events"),
        )

    async def _stream(self, params: dict[str, Any], invocation_id: str, session: SessionHandle) -> dict[str, Any]:
        self._require("message/stream")
        await self._load_card()
        request_id = uuid.uuid4().hex
        payload = {"jsonrpc": "2.0", "id": request_id, "method": "message/stream", "params": params}
        self.sent.append("message/stream")
        task: dict[str, Any] | None = None
        artifacts: dict[str, list[dict[str, Any]]] = {}
        total = 0
        headers = {**self.connection.auth.headers(), "Accept": "text/event-stream"}
        try:
            async with self._client.stream("POST", self._endpoint, json=payload, headers=headers) as response:
                if response.status_code >= 300:
                    raise AdapterError(f"http_{response.status_code}", "message/stream returned an error status")
                data: list[str] = []
                async for line in response.aiter_lines():
                    total += len(line)
                    if total > self.connection.max_response_bytes:
                        raise AdapterError("output_limit", "A2A stream exceeded the configured size")
                    if line.startswith("data:"):
                        data.append(line[5:].lstrip())
                        continue
                    if line or not data:
                        continue
                    event = self._unwrap("\n".join(data), request_id, "message/stream")
                    data = []
                    kind = event.get("kind") if isinstance(event, dict) else None
                    if kind == "message":
                        return event
                    if kind == "task":
                        task = event
                        self._track(task, invocation_id, session)
                    elif kind == "status-update" and task is not None:
                        task = {**task, "status": event.get("status") or {}}
                        if event.get("final") or _state(task) in TERMINAL | TURN_COMPLETE:
                            task["artifacts"] = [{"parts": parts} for parts in artifacts.values()]
                            return task
                    elif kind == "artifact-update" and task is not None:
                        artifact = event.get("artifact") or {}
                        key = str(artifact.get("artifactId", ""))
                        parts = list(artifact.get("parts") or [])
                        artifacts[key] = [*artifacts.get(key, []), *parts] if event.get("append") else parts
                    else:
                        raise AdapterError("malformed_event", "Unexpected A2A stream event")
        except (httpx.TimeoutException, httpx.TransportError) as error:
            raise AdapterError("stream_interrupted", f"A2A stream failed: {type(error).__name__}") from None
        raise AdapterError("stream_interrupted", "A2A stream ended before a final status; not resent")

    async def cancel(self, invocation_id: str) -> CancelResult:
        task_id = self._invocations.get(invocation_id)
        if "tasks/cancel" not in self.connection.methods or task_id is None:
            return CancelResult(invocation_id=invocation_id, acknowledged=False, note="no mapped tasks/cancel")
        try:
            task = await self._rpc("tasks/cancel", {"id": task_id}, mutating=False)
        except AdapterError as error:
            return CancelResult(
                invocation_id=invocation_id, acknowledged=False, remote_cancel_attempted=True, note=error.code
            )
        return CancelResult(
            invocation_id=invocation_id,
            acknowledged=isinstance(task, dict) and _state(task) == "canceled",
            remote_cancel_attempted=True,
            note="acknowledgement is not proof the work stopped",
        )

    async def close_session(self, session: SessionHandle) -> None:
        self._contexts.pop(session.session_id, None)
        self._open_tasks.pop(session.session_id, None)

    async def aclose(self) -> None:
        await self._client.aclose()


def _state(task: dict[str, Any]) -> str:
    state = (task.get("status") or {}).get("state")
    if state not in TaskState.__args__:  # type: ignore[attr-defined]
        raise AdapterError("malformed_response", f"Unknown A2A task state {state!r}")
    return state


def _text(parts: Any) -> str | None:
    if not isinstance(parts, list):
        return None
    texts = [
        p["text"] for p in parts if isinstance(p, dict) and p.get("kind") == "text" and isinstance(p.get("text"), str)
    ]
    return "".join(texts) if texts else None
