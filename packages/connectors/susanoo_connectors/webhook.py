"""Signed webhook completion for asynchronous HTTP agents (I04) [Spec §4 I04, §19 Async webhook; T22].

Three pieces, split along the hosted/customer boundary:

* :func:`handle_callback` is the provider-verification helper the hosted callback endpoint calls.
  It verifies an HMAC-SHA256 signature over ``"{timestamp}.{raw body}"`` in constant time within a
  timestamp tolerance, normalizes the event through typed paths, correlates it to a job the runner
  already registered, drops duplicates and out-of-order events, rejects unknown tasks and queues a
  minimal :class:`RelayEvent`. The endpoint owns tenancy, policy and persistence through the
  :class:`TaskRegistry` and :class:`RelayQueue` protocols.
* :class:`WebhookCompletionWaiter` is the runner side: it polls the relay queue (outbound only) and
  returns the first terminal event for a task in sequence order.
* :class:`WebhookAgentAdapter` submits once, registers the job id, then waits. A callback is never a
  fresh task: nothing on the callback path can submit, and a missed deadline is never resubmitted.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

import httpx
from pydantic import Field, model_validator
from susanoo_contracts import Capabilities
from susanoo_evaluator.dsl import PATH_PATTERN
from susanoo_evaluator.engine import _MISSING, get_path
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

from .mapping import SECRET_REF, AuthRef, Model, ResponseMapping, extract, render, validate_template

WEBHOOK_CONTRACT_VERSION = "1.0"
Disposition = Literal["accepted", "duplicate", "out_of_order", "unknown_task", "after_terminal"]


class WebhookRejected(Exception):
    """A callback failed verification or normalization. ``status_code`` is what the endpoint returns."""

    def __init__(self, code: str, message: str, status_code: int = 401):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class WebhookVerification(Model):
    """How a provider signs callbacks. Secrets are local references; a second ref allows rotation."""

    secret_ref: str = Field(pattern=SECRET_REF)
    previous_secret_ref: str | None = Field(default=None, pattern=SECRET_REF)
    signature_header: str = "X-Susanoo-Signature"
    timestamp_header: str = "X-Susanoo-Timestamp"
    signature_prefix: str = "v1="
    tolerance_seconds: int = Field(default=300, ge=1, le=3600)
    max_body_bytes: int = Field(default=65_536, ge=256, le=1_048_576)


class WebhookEventMapping(Model):
    event_id_path: str = Field(default="event_id", pattern=PATH_PATTERN)
    task_id_path: str = Field(default="task_id", pattern=PATH_PATTERN)
    status_path: str = Field(default="status", pattern=PATH_PATTERN)
    sequence_path: str | None = Field(default="sequence", pattern=PATH_PATTERN)
    result_path: str | None = Field(default="result", pattern=PATH_PATTERN)
    terminal_states: list[str] = Field(min_length=1)


class RelayEvent(Model):
    """The minimal approved event queued for runner polling. No headers, no signature, no raw body."""

    contract_version: Literal["1.0"] = WEBHOOK_CONTRACT_VERSION
    task_id: str = Field(min_length=1, max_length=200)
    event_id: str = Field(min_length=1, max_length=200)
    sequence: int | None = Field(default=None, ge=0)
    status: str = Field(min_length=1, max_length=100)
    terminal: bool
    result: dict[str, Any] | None = None
    received_at: datetime


class CallbackOutcome(Model):
    status_code: int
    disposition: Disposition
    event_id: str
    task_id: str


def sign(secret: str, timestamp: int, body: bytes) -> str:
    """Hex HMAC-SHA256 over ``"{timestamp}.{body}"`` (also used by fake providers in tests)."""
    return hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


def _header(headers: Mapping[str, str], name: str) -> str | None:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def verify_signature(
    body: bytes, headers: Mapping[str, str], config: WebhookVerification, *, now: float | None = None
) -> None:
    """Raise :class:`WebhookRejected` unless a configured secret signed this body recently."""
    if len(body) > config.max_body_bytes:
        raise WebhookRejected("oversize", "Callback body exceeds the configured size", 413)
    signature = _header(headers, config.signature_header)
    stamp = _header(headers, config.timestamp_header)
    if not signature or not stamp:
        raise WebhookRejected("missing_signature", "Callback is not signed")
    try:
        timestamp = int(stamp)
    except ValueError:
        raise WebhookRejected("bad_timestamp", "Callback timestamp is not an integer") from None
    if abs((time.time() if now is None else now) - timestamp) > config.tolerance_seconds:
        raise WebhookRejected("stale_timestamp", "Callback timestamp is outside the tolerance")
    secrets = [os.environ.get(config.secret_ref)]
    if config.previous_secret_ref:
        secrets.append(os.environ.get(config.previous_secret_ref))
    if not any(secrets):
        raise WebhookRejected("missing_secret", f"Local secret {config.secret_ref} is not configured", 500)
    offered = [part.strip().removeprefix(config.signature_prefix) for part in signature.split(",")]
    matched = False
    for secret in filter(None, secrets):
        expected = sign(secret, timestamp, body)
        for candidate in offered:
            # Every comparison runs: no early exit that leaks which secret or position matched.
            matched |= hmac.compare_digest(expected.encode(), candidate.encode())
    if not matched:
        raise WebhookRejected("bad_signature", "Callback signature does not match")


def normalize(body: bytes, mapping: WebhookEventMapping, *, received_at: datetime | None = None) -> RelayEvent:
    try:
        event = json.loads(body)
    except ValueError:
        raise WebhookRejected("malformed_event", "Callback body is not JSON", 400) from None

    def read(path: str | None, required: bool = True) -> Any:
        if path is None:
            return None
        value = get_path(event, path)
        if value is _MISSING:
            if required:
                raise WebhookRejected("malformed_event", f"Callback has no value at {path}", 400)
            return None
        return value

    sequence = read(mapping.sequence_path, required=False)
    if sequence is not None and (type(sequence) is not int or sequence < 0):
        raise WebhookRejected("malformed_event", "Callback sequence is not a non-negative integer", 400)
    result = read(mapping.result_path, required=False)
    if result is not None and not isinstance(result, dict):
        raise WebhookRejected("malformed_event", "Callback result is not an object", 400)
    status = str(read(mapping.status_path))
    try:
        return RelayEvent(
            task_id=str(read(mapping.task_id_path)),
            event_id=str(read(mapping.event_id_path)),
            sequence=sequence,
            status=status,
            terminal=status in mapping.terminal_states,
            result=result,
            received_at=received_at or datetime.now(UTC),
        )
    except ValueError:
        raise WebhookRejected("malformed_event", "Callback fields are out of bounds", 400) from None


class TaskRegistry(Protocol):
    """Correlation and dedupe store owned by the hosted endpoint (tenant-scoped in production)."""

    async def is_registered(self, task_id: str) -> bool: ...

    async def record(self, event: RelayEvent) -> Disposition: ...


class RelayQueue(Protocol):
    async def put(self, event: RelayEvent) -> None: ...

    async def poll(self, task_id: str, after: int, limit: int = 100) -> tuple[list[RelayEvent], int]:
        """Events for ``task_id`` past cursor ``after``; returns them with the next cursor."""
        ...


class JobRegistrar(Protocol):
    """Runner -> relay: declare a submitted job so its callbacks are correlated."""

    async def register(self, task_id: str, *, attempt_id: str, invocation_id: str) -> None: ...


@dataclass
class _TaskState:
    seen: set[str] = field(default_factory=set)
    high: int = -1
    terminal: bool = False


class MemoryTaskRegistry:
    """Reference :class:`TaskRegistry` and :class:`JobRegistrar` for tests and private-mode relays."""

    def __init__(self) -> None:
        self._tasks: dict[str, _TaskState] = {}
        self.registrations: list[tuple[str, str, str]] = []

    async def register(self, task_id: str, *, attempt_id: str, invocation_id: str) -> None:
        self._tasks.setdefault(task_id, _TaskState())
        self.registrations.append((task_id, attempt_id, invocation_id))

    async def is_registered(self, task_id: str) -> bool:
        return task_id in self._tasks

    async def record(self, event: RelayEvent) -> Disposition:
        state = self._tasks.get(event.task_id)
        if state is None:
            return "unknown_task"
        if event.event_id in state.seen:
            return "duplicate"
        state.seen.add(event.event_id)
        if state.terminal:
            return "after_terminal"
        if event.sequence is not None:
            if event.sequence <= state.high:
                return "out_of_order"
            state.high = event.sequence
        state.terminal = event.terminal
        return "accepted"


class MemoryRelayQueue:
    def __init__(self) -> None:
        self._events: dict[str, list[RelayEvent]] = {}

    async def put(self, event: RelayEvent) -> None:
        self._events.setdefault(event.task_id, []).append(event)

    async def poll(self, task_id: str, after: int, limit: int = 100) -> tuple[list[RelayEvent], int]:
        events = self._events.get(task_id, [])[after : after + limit]
        return events, after + len(events)


async def handle_callback(
    body: bytes,
    headers: Mapping[str, str],
    *,
    verification: WebhookVerification,
    mapping: WebhookEventMapping,
    registry: TaskRegistry,
    queue: RelayQueue,
    now: float | None = None,
) -> CallbackOutcome:
    """Verify, correlate and relay one provider callback. Raises :class:`WebhookRejected` on bad input.

    Duplicates and stale events are acknowledged (200) so providers stop retrying but are not queued.
    An unknown task is rejected (404) and nothing is queued or submitted.
    """
    verify_signature(body, headers, verification, now=now)
    event = normalize(body, mapping)
    if not await registry.is_registered(event.task_id):
        raise WebhookRejected("unknown_task", "Callback refers to no submitted job", 404)
    disposition = await registry.record(event)
    if disposition == "unknown_task":
        raise WebhookRejected("unknown_task", "Callback refers to no submitted job", 404)
    if disposition == "accepted":
        await queue.put(event)
    return CallbackOutcome(status_code=200, disposition=disposition, event_id=event.event_id, task_id=event.task_id)


class WebhookCompletionWaiter:
    """Polls the relay for a task's terminal event; dedupes by event id and ignores lower sequences."""

    def __init__(self, queue: RelayQueue, *, poll_interval_seconds: float = 0.2):
        self.queue = queue
        self.poll_interval_seconds = poll_interval_seconds

    async def wait(self, task_id: str, *, timeout_seconds: float) -> RelayEvent:
        deadline = time.monotonic() + timeout_seconds
        cursor, high, seen = 0, -1, set[str]()
        while True:
            events, cursor = await self.queue.poll(task_id, cursor)
            for event in events:
                if event.task_id != task_id or event.event_id in seen:
                    continue
                seen.add(event.event_id)
                if event.sequence is not None:
                    if event.sequence <= high:
                        continue
                    high = event.sequence
                if event.terminal:
                    return event
            if time.monotonic() > deadline:
                raise AdapterError(
                    "callback_deadline", f"No terminal callback for task {task_id}; the job is not resubmitted"
                )
            await asyncio.sleep(self.poll_interval_seconds)


class WebhookConnection(Model):
    kind: Literal["http_webhook"] = "http_webhook"
    submit_url: str = Field(pattern=r"^https?://")
    auth: AuthRef = Field(default_factory=AuthRef)
    request: dict[str, Any]
    task_id_path: str = Field(pattern=PATH_PATTERN)
    terminal_success: list[str] = Field(min_length=1)
    terminal_failure: list[str] = Field(default_factory=list)
    response: ResponseMapping = Field(description="Paths are read from the relayed event's result object")
    submit_timeout_seconds: float = Field(default=30, gt=0, le=300)
    max_wait_seconds: float = Field(default=300, gt=0, le=3600)
    poll_interval_seconds: float = Field(default=0.2, gt=0, le=60)

    @model_validator(mode="after")
    def typed(self) -> WebhookConnection:
        validate_template(self.request)
        if set(self.terminal_success) & set(self.terminal_failure):
            raise ValueError("A state cannot be both terminal success and terminal failure")
        return self

    def event_mapping(self, **paths: Any) -> WebhookEventMapping:
        return WebhookEventMapping(terminal_states=[*self.terminal_success, *self.terminal_failure], **paths)


class WebhookAgentAdapter:
    def __init__(
        self,
        connection: WebhookConnection,
        *,
        registrar: JobRegistrar,
        queue: RelayQueue,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.connection = connection
        self.registrar = registrar
        self.waiter = WebhookCompletionWaiter(queue, poll_interval_seconds=connection.poll_interval_seconds)
        self._client = httpx.AsyncClient(transport=transport, follow_redirects=False)
        self.submissions = 0

    async def preflight(self) -> CapabilityReport:
        caps = Capabilities(invoke=True, async_jobs=True, sessions=True, multi_turn=True)
        return CapabilityReport(
            adapter="http_webhook",
            adapter_version=WEBHOOK_CONTRACT_VERSION,
            capabilities=caps,
            notes=["completion arrives by signed callback through the relay; callbacks never submit"],
        )

    async def start_session(self, context: InvocationContext) -> SessionHandle:
        return SessionHandle(session_id=f"s-{uuid.uuid4().hex[:16]}", attempt_id=context.attempt_id)

    async def invoke(self, session: SessionHandle, message: Message, context: InvocationContext) -> AgentReply:
        c = self.connection
        invocation_id = f"inv-{uuid.uuid4().hex[:12]}"
        self.submissions += 1
        try:
            submitted = await self._client.post(
                c.submit_url,
                json=render(c.request, message, context, session.session_id),
                headers={**c.auth.headers(), "Idempotency-Key": invocation_id},
                timeout=c.submit_timeout_seconds,
            )
        except (httpx.TimeoutException, httpx.TransportError):
            raise AdapterError("submit_uncertain", "Submit may have been accepted; never resubmitted") from None
        if 300 <= submitted.status_code < 400:
            raise AdapterError("redirect_blocked", "Submit endpoint redirected")
        if submitted.status_code >= 400:
            raise AdapterError(f"http_{submitted.status_code}", "Submit was rejected")
        try:
            task_id = str(extract(submitted.json(), c.task_id_path))
        except ValueError:
            raise AdapterError("submit_uncertain", "Submit response is not JSON; job id unknown") from None
        await self.registrar.register(task_id, attempt_id=context.attempt_id, invocation_id=invocation_id)
        event = await self.waiter.wait(task_id, timeout_seconds=c.max_wait_seconds)
        if event.status in c.terminal_failure:
            raise AdapterError("remote_task_failed", f"Remote task ended {event.status}")
        body = event.result or {}
        text = extract(body, c.response.text_path)
        structured = extract(body, c.response.structured_path)
        if text is not None and not isinstance(text, str):
            raise AdapterError("response_mapping", "Mapped text is not a string")
        if structured is not None and not isinstance(structured, dict):
            raise AdapterError("response_mapping", "Mapped structured value is not an object")
        return AgentReply(
            invocation_id=invocation_id,
            session_id=session.session_id,
            text=text,
            structured=structured,
            usage=Unavailable(reason="webhook connection does not report usage"),
            tool_events=Unavailable(reason="webhook connection does not report tool events"),
        )

    async def cancel(self, invocation_id: str) -> CancelResult:
        return CancelResult(invocation_id=invocation_id, acknowledged=False, note="no declared remote cancel")

    async def close_session(self, session: SessionHandle) -> None:
        return None

    async def aclose(self) -> None:
        await self._client.aclose()
