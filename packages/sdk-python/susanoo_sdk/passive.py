"""Opt-in passive production evidence: redacted traces and outcome events over OTLP/HTTP JSON [Spec §18; T34].

Safety properties, each covered by tests:

- Off by default: ``PassiveConfig()`` is disabled with ``sample_rate=0``; nothing is recorded or sent.
- Redaction happens in the caller *before* enqueue; raw values never reach the queue or the wire.
- Bounded: a fixed-size queue; when it is full the event is dropped and counted, never blocking.
- Fails open: export runs on a daemon thread with a timeout; exporter errors, timeouts and a dead
  collector are counted, never raised into the application, and never delay the instrumented call.
  Every recorder entry point swallows its own errors (``internal_errors``).
- Sampling is per conversation (stable hash), so a sampled conversation keeps its outcome events.
- Coverage shows sampled conversations with and without outcome evidence; missing is never "fine".
- Enabling tracing authorizes no production action: this module only observes and exports.
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import json
import queue
import re
import threading
import time
import urllib.request
import uuid
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr

# --- redaction ----------------------------------------------------------------------------------

SENSITIVE_KEY_SUFFIXES = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "apikey",
    "accesskey",
    "privatekey",
    "authorization",
    "cookie",
    "ssn",
    "aadhaar",
    "pan",
    "cvv",
    "otp",
    "pin",
    "card",
    "cardnumber",
    "iban",
)

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("[SECRET]", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")),
    ("[SECRET]", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("[SECRET]", re.compile(r"\b(?:sk|pk|rk|ghp|gho|xox[abp])[-_][A-Za-z0-9_-]{16,}")),
    ("[SECRET]", re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S+")),
    ("[EMAIL]", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("[CARD]", re.compile(r"\b(?:[0-9][ -]?){12,18}[0-9]\b")),
    ("[GOVT_ID]", re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b")),  # PAN
    ("[GOVT_ID]", re.compile(r"\b[2-9][0-9]{3}[ -]?[0-9]{4}[ -]?[0-9]{4}\b")),  # Aadhaar shape
    ("[PHONE]", re.compile(r"(?<![\w-])\+?[0-9](?:[ ()-]{0,2}[0-9]){9,14}\b")),
]


def _luhn(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for index, char in enumerate(digits):
        value = int(char)
        if index % 2 == parity:
            value *= 2
            value -= 9 if value > 9 else 0
        total += value
    return total % 10 == 0


class Redactor:
    """Key- and pattern-based minimization. Conservative: when unsure, it removes."""

    def __init__(self, extra_keys: tuple[str, ...] = (), max_chars: int = 4096):
        self.keys = SENSITIVE_KEY_SUFFIXES + tuple(k.lower() for k in extra_keys)
        self.max_chars = max_chars

    def sensitive_key(self, key: str) -> bool:
        flat = re.sub(r"[^a-z0-9]", "", key.lower())
        return any(flat.endswith(suffix) for suffix in self.keys)

    def text(self, value: str) -> tuple[str, int]:
        count = 0
        if len(value) > self.max_chars:
            value, count = value[: self.max_chars] + "...[TRUNCATED]", count + 1
        for label, pattern in _PATTERNS:

            def replace(match: re.Match[str], label: str = label) -> str:
                nonlocal count
                if label == "[CARD]" and not _luhn(re.sub(r"\D", "", match.group(0))):
                    return match.group(0)
                count += 1
                return label

            value = pattern.sub(replace, value)
        return value, count

    def value(self, value: Any, _depth: int = 0) -> tuple[Any, int]:
        if _depth > 20:
            return "[TOO_DEEP]", 1
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            out, count = {}, 0
            for key, item in list(value.items())[:200]:
                if self.sensitive_key(str(key)):
                    out[str(key)], count = "[REDACTED]", count + 1
                else:
                    out[str(key)], n = self.value(item, _depth + 1)
                    count += n
            return out, count
        if isinstance(value, list | tuple):
            out_list, count = [], 0
            for item in list(value)[:200]:
                redacted, n = self.value(item, _depth + 1)
                out_list.append(redacted)
                count += n
            return out_list, count
        if value is None or isinstance(value, bool | int | float):
            return value, 0
        return self.text(repr(value))


# --- configuration and transport ----------------------------------------------------------------


class PassiveConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    sample_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    endpoint: str | None = Field(default=None, description="collector base URL; spans go to <endpoint>/v1/traces")
    project_id: str | None = None
    token: SecretStr | None = None
    service_name: str = "agent"
    max_queue: int = Field(default=1000, ge=1, le=100_000)
    batch_size: int = Field(default=100, ge=1, le=1000)
    export_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    flush_interval_seconds: float = Field(default=1.0, gt=0, le=60)
    linger_seconds: float = Field(default=0.2, ge=0, le=10, description="wait to fill a batch after the first span")
    max_tracked_conversations: int = Field(default=10_000, ge=1)
    max_chars: int = Field(default=4096, ge=64, le=65536)

    @property
    def active(self) -> bool:
        return self.enabled and self.sample_rate > 0 and self.endpoint is not None


Transport = Callable[[bytes, dict[str, str], float], None]


def http_transport(endpoint: str) -> Transport:
    url = endpoint.rstrip("/") + "/v1/traces"

    def send(body: bytes, headers: dict[str, str], timeout: float) -> None:
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - configured collector
            if response.status >= 300:
                raise OSError(f"collector answered {response.status}")

    return send


class CoverageReport(BaseModel):
    enabled: bool
    sample_rate: float
    conversations_seen: int
    conversations_sampled: int
    sampled_with_outcome: int
    sampled_missing_outcome: int
    outcome_events: int
    spans_enqueued: int
    dropped_queue_full: int
    exported_spans: int
    export_failures: int
    internal_errors: int
    redactions: int
    note: str = "Sampled scope only; this is not production accuracy."


def _attr(key: str, value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, default=str)
    return {"key": key, "value": {"stringValue": value}}


# --- recorder -----------------------------------------------------------------------------------


class PassiveRecorder:
    def __init__(
        self,
        config: PassiveConfig | None = None,
        *,
        transport: Transport | None = None,
        redactor: Redactor | None = None,
    ):
        self.config = config or PassiveConfig()
        self.redactor = redactor or Redactor(max_chars=self.config.max_chars)
        self._transport = transport or (http_transport(self.config.endpoint) if self.config.endpoint else None)
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=self.config.max_queue)
        self._lock = threading.Lock()
        self._conversations: OrderedDict[str, bool] = OrderedDict()  # sampled id -> has outcome
        self._evicted_with, self._evicted_missing = 0, 0
        self._stats = dict.fromkeys(
            ("seen", "sampled", "outcomes", "enqueued", "dropped", "exported", "failures", "internal", "redactions"), 0
        )
        self._seen_ids: OrderedDict[str, None] = OrderedDict()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- sampling --

    def sampled(self, conversation_id: str) -> bool:
        if not self.config.active:
            return False
        bucket = int.from_bytes(hashlib.sha256(conversation_id.encode()).digest()[:8], "big") / 2**64
        return bucket < self.config.sample_rate

    def _track(self, conversation_id: str) -> bool:
        with self._lock:
            if conversation_id not in self._seen_ids:
                self._seen_ids[conversation_id] = None
                self._stats["seen"] += 1
                if len(self._seen_ids) > self.config.max_tracked_conversations:
                    self._seen_ids.popitem(last=False)
            if not self.sampled(conversation_id):
                return False
            if conversation_id not in self._conversations:
                self._conversations[conversation_id] = False
                self._stats["sampled"] += 1
                if len(self._conversations) > self.config.max_tracked_conversations:
                    _, had = self._conversations.popitem(last=False)
                    if had:
                        self._evicted_with += 1
                    else:
                        self._evicted_missing += 1
            return True

    # -- public API: never raises --

    def trace(
        self,
        conversation_id: str,
        name: str,
        *,
        input: Any = None,
        output: Any = None,
        tool: str | None = None,
        arguments: Any = None,
        error: str | None = None,
        started_ns: int | None = None,
        ended_ns: int | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> bool:
        """Record one turn or tool call. Returns whether it was sampled and enqueued."""
        try:
            if not self.config.active or not self._track(conversation_id):
                return False
            attrs: dict[str, Any] = {"susanoo.event": "tool" if tool else "turn"}
            if input is not None:
                attrs["gen_ai.prompt"] = input
            if output is not None:
                attrs["gen_ai.completion"] = output
            if tool:
                attrs["gen_ai.tool.name"] = tool
                attrs["gen_ai.operation.name"] = "execute_tool"
            if arguments is not None:
                attrs["gen_ai.tool.call.arguments"] = arguments
            if error:
                attrs["error.type"] = error
            attrs.update(attributes or {})
            return self._enqueue(conversation_id, name, attrs, error is not None, started_ns, ended_ns)
        except Exception:
            self._count("internal")
            return False

    def outcome(self, conversation_id: str, kind: str, status: str, detail: dict[str, Any] | None = None) -> bool:
        """Record a business outcome event (e.g. payment posted). Reported evidence, not a verdict."""
        try:
            if not self.config.active or not self._track(conversation_id):
                return False
            with self._lock:
                self._conversations[conversation_id] = True
                self._stats["outcomes"] += 1
            attrs = {"susanoo.event": "outcome", "susanoo.outcome.kind": kind, "susanoo.outcome.status": status}
            if detail:
                attrs["susanoo.outcome.detail"] = detail
            return self._enqueue(conversation_id, f"outcome {kind}", attrs, status in {"failed", "error"}, None, None)
        except Exception:
            self._count("internal")
            return False

    def instrument(self, name: str | None = None, conversation_id: Callable[..., str] | None = None):
        """Decorator for sync or async agent entry points. The wrapped call's behavior and errors are its own."""

        def pick(args: tuple, kwargs: dict) -> str:
            try:
                if conversation_id:
                    return str(conversation_id(*args, **kwargs))
                return str(kwargs.get("conversation_id") or kwargs.get("session_id") or uuid.uuid4().hex)
            except Exception:
                return uuid.uuid4().hex

        def decorate(fn):
            label = name or fn.__qualname__

            if inspect.iscoroutinefunction(fn):

                @functools.wraps(fn)
                async def awrapper(*args, **kwargs):
                    if not self.config.active:
                        return await fn(*args, **kwargs)
                    started = time.time_ns()
                    try:
                        result = await fn(*args, **kwargs)
                    except Exception as exc:
                        self.trace(
                            pick(args, kwargs),
                            label,
                            input=_call_repr(args, kwargs),
                            error=type(exc).__name__,
                            started_ns=started,
                        )
                        raise
                    self.trace(
                        pick(args, kwargs), label, input=_call_repr(args, kwargs), output=result, started_ns=started
                    )
                    return result

                return awrapper

            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                if not self.config.active:
                    return fn(*args, **kwargs)
                started = time.time_ns()
                try:
                    result = fn(*args, **kwargs)
                except Exception as exc:
                    self.trace(
                        pick(args, kwargs),
                        label,
                        input=_call_repr(args, kwargs),
                        error=type(exc).__name__,
                        started_ns=started,
                    )
                    raise
                self.trace(pick(args, kwargs), label, input=_call_repr(args, kwargs), output=result, started_ns=started)
                return result

            return wrapper

        return decorate

    def coverage(self) -> CoverageReport:
        with self._lock:
            with_outcome = sum(self._conversations.values()) + self._evicted_with
            sampled = self._stats["sampled"]
            return CoverageReport(
                enabled=self.config.active,
                sample_rate=self.config.sample_rate if self.config.enabled else 0.0,
                conversations_seen=self._stats["seen"],
                conversations_sampled=sampled,
                sampled_with_outcome=with_outcome,
                sampled_missing_outcome=sampled - with_outcome,
                outcome_events=self._stats["outcomes"],
                spans_enqueued=self._stats["enqueued"],
                dropped_queue_full=self._stats["dropped"],
                exported_spans=self._stats["exported"],
                export_failures=self._stats["failures"],
                internal_errors=self._stats["internal"],
                redactions=self._stats["redactions"],
            )

    def flush(self, timeout: float = 5.0) -> bool:
        """Best effort: wait until the queue drains or the timeout passes. Never raises."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.01)
        return False

    def shutdown(self, timeout: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    # -- internals --

    def _count(self, key: str, amount: int = 1) -> None:
        with self._lock:
            self._stats[key] += amount

    def _enqueue(self, conversation_id, name, attrs, is_error, started_ns, ended_ns) -> bool:
        redacted, count = self.redactor.value(attrs)  # before the queue: raw values never leave this call
        conv, n = self.redactor.text(conversation_id)
        now = time.time_ns()
        trace_id = hashlib.sha256(conversation_id.encode()).hexdigest()[:32]
        span = {
            "traceId": trace_id,
            "spanId": uuid.uuid4().hex[:16],
            "name": name[:200],
            "kind": 1,
            "startTimeUnixNano": str(started_ns or now),
            "endTimeUnixNano": str(ended_ns or now),
            "attributes": [_attr("gen_ai.conversation.id", conv)] + [_attr(k, v) for k, v in redacted.items()],
            "status": {"code": 2 if is_error else 1},
        }
        self._count("redactions", count + n)
        try:
            self._queue.put_nowait(span)
        except queue.Full:
            self._count("dropped")
            return False
        self._count("enqueued")
        self._ensure_thread()
        return True

    def _ensure_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            with self._lock:
                if self._thread is None or not self._thread.is_alive():
                    self._thread = threading.Thread(target=self._run, name="susanoo-passive-export", daemon=True)
                    self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            batch: list[dict[str, Any]] = []
            try:
                batch.append(self._queue.get(timeout=self.config.flush_interval_seconds))
            except queue.Empty:
                continue
            linger_until = time.monotonic() + self.config.linger_seconds
            while len(batch) < self.config.batch_size:
                remaining = linger_until - time.monotonic()
                try:
                    batch.append(self._queue.get(timeout=remaining) if remaining > 0 else self._queue.get_nowait())
                except queue.Empty:
                    break
            try:
                self._export(batch)
                self._count("exported", len(batch))
            except Exception:  # fail open: count, drop the batch, keep going
                self._count("failures", len(batch))
            finally:
                for _ in batch:
                    self._queue.task_done()

    def _export(self, spans: list[dict[str, Any]]) -> None:
        if self._transport is None:
            raise OSError("no collector configured")
        body = {
            "resourceSpans": [
                {
                    "resource": {"attributes": [_attr("service.name", self.config.service_name)]},
                    "scopeSpans": [{"scope": {"name": "susanoo-passive", "version": "1.0"}, "spans": spans}],
                }
            ]
        }
        headers = {"Content-Type": "application/json"}
        if self.config.token is not None:
            headers["Authorization"] = f"Bearer {self.config.token.get_secret_value()}"
        if self.config.project_id:
            headers["X-Susanoo-Project"] = self.config.project_id
        self._transport(json.dumps(body).encode(), headers, self.config.export_timeout_seconds)


def _call_repr(args: tuple, kwargs: dict) -> Any:
    return {"args": [a for a in args if isinstance(a, str | int | float | bool | dict | list)], "kwargs": kwargs}
