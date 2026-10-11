"""neatlogs tracing for every head and every model call [PRD › Tech stack].

Findings (neatlogs 1.4.26, checked against its source; the PRD's open question in
[PRD › Timeline, owners, risks]):
- Anthropic: neatlogs patches the Anthropic clients natively (`instrumentations=["anthropic"]`), so
  every `llm.call` is traced with prompt, response, tokens and latency. `setup()` must run before the
  client is constructed; `llm._get_default_client()` is lazy, so calling `setup()` at startup is enough.
- LangGraph: no native instrumentation (`["langgraph"]` is a no-op). Heads use `span(HEAD, run_id=...)`,
  which nests the auto-captured model calls under the head span.

Usage in a head: `with tracing.span(HEAD, run_id=state["run_id"]):` (see the new-head skill).

Credentials never reach a trace [CLAUDE.md §5]. Two layers: `span()` redacts attributes as they are set,
and `_mask_snapshot` (installed as neatlogs' `mask=`) scrubs every span at export, including the
auto-captured prompt and response text. Redaction is best effort pattern matching, so keep secrets out of
prompts in the first place.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import json
import logging
import os
import re
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypeVar

import neatlogs
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
from opentelemetry.trace import Span, Status, StatusCode

log = logging.getLogger(__name__)

REDACTED = "[REDACTED]"
_F = TypeVar("_F", bound=Callable[..., Any])

# --- redaction -------------------------------------------------------------------------------------------

_SENSITIVE_KEY = re.compile(
    r"api[_-]?key|token|secret|password|passwd|authorization|credential|cookie|private[_-]?key",
    re.IGNORECASE,
)

# Each pattern replaces the whole match with [REDACTED] unless noted below.
_SECRET_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # PEM private key block; an unterminated block (truncated text) is redacted to the end.
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?:.*?-----END [A-Z ]*PRIVATE KEY-----|.*\Z)", re.DOTALL),
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{8,}"),  # Anthropic
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),  # other sk- keys (OpenAI and similar)
    re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{8,}"),  # Stripe style
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=\-]{8,}", re.IGNORECASE),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}"),  # JWT (incl. Supabase keys)
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),  # GitHub tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
)
# scheme://user:password@host -> keep everything but the password.
_URL_PASSWORD = re.compile(r"(\b[a-z][a-z0-9+.\-]*://[^:/\s@]+:)([^@\s]+)(@)", re.IGNORECASE)
# password=hunter2 style assignments in free text.
_ASSIGNED_SECRET = re.compile(
    r"\b((?:api[_-]?key|secret|password|passwd|access[_-]?token|auth[_-]?token)\s*=\s*)([^\s&\"']{4,})",
    re.IGNORECASE,
)

# name = "value", name: "value", name="value" (keyword/default arg), "name": "value" (dict entry) and
# annotated forms, for any name that contains a credential word (X-Api-Key, access_token, db_password...).
# Quoted values need their own pattern because the one above deliberately skips quotes.
_QUOTED_ASSIGNED_SECRET = re.compile(
    r"""(?<![\w\-])"""
    r"""([\w\-]*?(?:api[_-]?key|access[_-]?key|secret|token|passw(?:or)?d|authorization|credential|private[_-]?key)"""
    r"""[\w\-]*["']?\s*(?::\s*[\w\[\], .|]+?)?\s*[:=]\s*)"""
    r"""(["'])((?:(?!\2)[^\n\\]|\\.){4,}?)\2""",
    re.IGNORECASE,
)

_MAX_DEPTH = 8


def scrub_text(text: str) -> str:
    """Replace anything in `text` that looks like a credential with [REDACTED]."""
    for pattern in _SECRET_VALUE_PATTERNS:
        text = pattern.sub(REDACTED, text)
    text = _URL_PASSWORD.sub(rf"\g<1>{REDACTED}\g<3>", text)
    text = _QUOTED_ASSIGNED_SECRET.sub(rf"\g<1>\g<2>{REDACTED}\g<2>", text)
    return _ASSIGNED_SECRET.sub(rf"\g<1>{REDACTED}", text)


def looks_secret_shaped(text: str) -> bool:
    """True if `text` contains a credential by its shape alone (API key prefixes, JWT, PEM, URL password).

    Unlike `scrub_text` this ignores `name = "value"` forms, so prose or code that merely mentions an
    example assignment (`API_KEY = "pay_live_..."`) does not count.
    """
    return any(p.search(text) for p in _SECRET_VALUE_PATTERNS) or _URL_PASSWORD.search(text) is not None


def is_sensitive_name(name: str) -> bool:
    """True if a variable, argument, header or dict key name suggests it holds a credential."""
    return _is_sensitive_key(name)


def _is_sensitive_key(key: Any) -> bool:
    return isinstance(key, str) and _SENSITIVE_KEY.search(key) is not None


def _is_secret_shaped(value: Any) -> bool:
    """A non-empty string or container: the kinds of value a secret key can hold (counts cannot)."""
    return isinstance(value, (str, bytes, Mapping, list, tuple)) and bool(value)


def _scrub_obj(obj: Any, depth: int = 0) -> Any:
    """Scrub every string in a nested structure, redacting whole values under sensitive keys.

    Types are preserved (numbers, bools, None, key names) so it is safe to hand back to neatlogs.
    """
    if isinstance(obj, str):
        return scrub_text(obj)
    if depth >= _MAX_DEPTH:
        return REDACTED if isinstance(obj, (Mapping, list, tuple, set)) else obj
    if isinstance(obj, Mapping):
        return {
            k: REDACTED if _is_sensitive_key(k) and _is_secret_shaped(v) else _scrub_obj(v, depth + 1)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_scrub_obj(v, depth + 1) for v in obj]
    if isinstance(obj, tuple):
        return tuple(_scrub_obj(v, depth + 1) for v in obj)
    return obj


def redact(key: str, value: Any) -> Any:
    """Return `value` with credentials removed; the whole value goes if `key` names a secret.

    Numbers and booleans pass through even under keys like `input_tokens` (counts, not secrets).
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if _is_sensitive_key(key):
        return REDACTED
    return _scrub_obj(value)


def _mask_snapshot(snapshot: Any) -> Any:
    """neatlogs `mask=` hook: scrub all text in a span snapshot at export. Never raises.

    If scrubbing itself fails, return the snapshot with attributes, events and status text cleared
    rather than leaking.
    """
    try:
        return _scrub_obj(snapshot)
    except Exception:
        log.warning("Could not scrub a trace span for credentials, so its details were dropped.")
        try:
            safe = dict(snapshot)
            safe["attributes"] = {}
            safe["events"] = []
            safe["links"] = []
            safe["status"] = {"code": "UNSET", "description": None}
            safe["resource"] = {"attributes": {}}
            safe["name"] = REDACTED if not isinstance(safe.get("name"), str) else scrub_text(safe["name"])
            return safe
        except Exception:
            return {"attributes": {}, "events": []}


# --- setup -----------------------------------------------------------------------------------------------

_initialised = False


def setup(*, workflow_name: str = "susanoo") -> None:
    """Start neatlogs once per process. Call at startup, before any Anthropic client exists.

    Reads NEATLOGS_API_KEY (and NEATLOGS_ENDPOINT) from the environment. Without a key the spans still
    work for local runs and tests; they are just not exported.
    """
    global _initialised
    if _initialised:
        return
    if not os.environ.get("NEATLOGS_API_KEY"):
        log.warning("NEATLOGS_API_KEY is not set, so traces will not be sent to neatlogs.")
    neatlogs.init(workflow_name=workflow_name, instrumentations=["anthropic"], mask=_mask_snapshot)
    _initialised = True
    _install_recorder()


# --- spans -----------------------------------------------------------------------------------------------

_current: contextvars.ContextVar[Span | None] = contextvars.ContextVar("susanoo_current_span", default=None)

_PRIMITIVES = (str, bool, int, float)


def _sanitize_attr(key: str, value: Any) -> Any:
    """Make `value` a safe OTel attribute: redacted, and a primitive."""
    value = redact(key, value)
    if value is None:
        return None
    if isinstance(value, _PRIMITIVES):
        return value
    try:
        return scrub_text(json.dumps(value, default=str))
    except Exception:
        return scrub_text(str(value))


class _SpanContext:
    """`with span(...) as s:` and `@span(...)` on sync or async functions."""

    def __init__(self, name: str, kind: str, attrs: dict[str, Any]) -> None:
        self._name = name
        self._kind = kind
        self._attrs = attrs
        self._stack: list[tuple[Any, Span, contextvars.Token[Span | None]]] = []

    def __enter__(self) -> Span:
        setup()  # no-op once started; keeps spans real if a caller forgot to call setup() at startup
        cm = neatlogs.trace(self._name, kind=self._kind)
        s = cm.__enter__()
        try:
            for key, value in self._attrs.items():
                clean = _sanitize_attr(key, value)
                if clean is not None:
                    s.set_attribute(key, clean)
        except BaseException as exc:
            cm.__exit__(type(exc), exc, exc.__traceback__)
            raise
        self._stack.append((cm, s, _current.set(s)))
        return s

    def __exit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> bool:
        cm, s, token = self._stack.pop()
        _current.reset(token)
        if exc is not None:
            _record_error(s, exc)
        cm.__exit__(exc_type, exc, tb)
        return False  # never swallow

    def __call__(self, fn: _F) -> _F:
        name, kind, attrs = self._name, self._kind, self._attrs
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                with _SpanContext(name, kind, attrs):
                    return await fn(*args, **kwargs)

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with _SpanContext(name, kind, attrs):
                return fn(*args, **kwargs)

        return wrapper  # type: ignore[return-value]


def _record_error(s: Span, exc: BaseException) -> None:
    """Mark the span failed. The message is scrubbed; the traceback is not attached (it can hold secrets)."""
    try:
        message = scrub_text(str(exc))
        s.add_event(
            "exception",
            {"exception.type": type(exc).__qualname__, "exception.message": message},
        )
        s.set_status(Status(StatusCode.ERROR, f"{type(exc).__qualname__}: {message}"))
    except Exception:
        log.warning("Could not record an error on a trace span.", exc_info=False)


def span(name: str, *, kind: str = "CHAIN", **attrs: Any) -> _SpanContext:
    """Open a neatlogs span, as a context manager or a decorator. Attributes are redacted first.

    On an exception the span is marked ERROR and the exception propagates.
    """
    return _SpanContext(name, kind, attrs)


def current_ids() -> tuple[str, str] | None:
    """(trace_id, span_id) of the innermost open `span(...)`, or None outside one."""
    s = _current.get()
    if s is None:
        return None
    ctx = s.get_span_context()
    if not ctx.is_valid:
        return None
    return f"{ctx.trace_id:032x}", f"{ctx.span_id:016x}"


def trace_url(trace_id: str) -> str | None:
    """Link to a trace in the neatlogs dashboard, or None if the link format is not configured.

    TODO(Muaaz): the dashboard URL format is unknown until the first real trace is seen. Then set
    NEATLOGS_TRACE_URL_TEMPLATE, e.g. "https://<dashboard>/traces/{trace_id}".
    """
    template = os.environ.get("NEATLOGS_TRACE_URL_TEMPLATE")
    if not template or "{trace_id}" not in template:
        return None
    return template.replace("{trace_id}", trace_id)


# --- local span recorder ---------------------------------------------------------------------------------
#
# The Diagnostician needs the spans of a scenario run right after the run, without waiting for neatlogs to
# ingest them and without a read token. `_Recorder` is an OTel SpanProcessor attached to neatlogs' own
# provider (see `_install_recorder`); it keeps finished spans per trace id in a bounded LRU. Attribute
# values are scrubbed and trimmed when recorded, so credentials never sit in this cache either.
# `recorded_spans(trace_id)` returns them in the shape prompts/diagnostician.md calls "the trace excerpt".

MAX_RECORDED_TRACES = 256
MAX_SPANS_PER_TRACE = 300
MAX_RECORDED_CHARS = 20_000_000  # attribute text across all traces; oldest traces go first
_MAX_ATTR_CHARS = 6000
LLM_SPAN_NAME = "anthropic.messages.create"

_KEPT_NEATLOGS_ATTR = re.compile(
    r"^neatlogs\.(?:span\.kind|llm\.finish_reason"
    r"|llm\.input_messages\.\d+\.(?:role|content)"
    r"|llm\.output_messages\.0\.content"
    r"|llm\.tool_calls\.\d+\.(?:id|name|arguments))$"
)
_INPUT_MESSAGE = re.compile(r"^neatlogs\.llm\.input_messages\.(\d+)\.(role|content)$")
_TOOL_CALL = re.compile(r"^neatlogs\.llm\.tool_calls\.(\d+)\.(id|name|arguments)$")
_TOOL_RESULT_CHARS = 1500
_TEXT_OUT_CHARS = 3000
_TOOL_INPUT_CHARS = 1500
_SPAN_ATTR_CHARS = 500


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def _record_attrs(span: ReadableSpan) -> dict[str, Any]:
    """The attributes worth keeping from a finished span, scrubbed and trimmed. Never raises."""
    raw = dict(span.attributes or {})
    system_first = raw.get("neatlogs.llm.input_messages.0.role") == "system"
    kept: dict[str, Any] = {}
    for key, value in raw.items():
        if key.startswith("neatlogs."):
            if not _KEPT_NEATLOGS_ATTR.match(key):
                continue
            if system_first and key == "neatlogs.llm.input_messages.0.content":
                continue  # the agent's system prompt is repeated every round; the Diagnostician gets it separately
        elif key.startswith("openinference."):
            continue
        value = redact(key, value)
        if isinstance(value, str):
            value = _clip(value, _MAX_ATTR_CHARS)
        elif not isinstance(value, (bool, int, float)):
            value = _clip(scrub_text(str(value)), _MAX_ATTR_CHARS)
        kept[key] = value
    return kept


def _record_chars(record: Mapping[str, Any]) -> int:
    return len(record["name"]) + sum(len(k) + (len(v) if isinstance(v, str) else 8) for k, v in record["attrs"].items())


class _Recorder(SpanProcessor):
    """Keeps finished spans per trace id, newest traces last, oldest evicted first.

    Bounded three ways: number of traces, spans per trace, and total characters of attribute text.
    """

    def __init__(
        self,
        max_traces: int = MAX_RECORDED_TRACES,
        max_spans: int = MAX_SPANS_PER_TRACE,
        max_chars: int = MAX_RECORDED_CHARS,
    ) -> None:
        self._max_traces = max_traces
        self._max_spans = max_spans
        self._max_chars = max_chars
        self._lock = threading.Lock()
        self._traces: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
        self._sizes: dict[str, int] = {}
        self._total = 0

    def on_start(self, span: Any, parent_context: Any = None) -> None:  # noqa: D102
        return None

    def _evict_oldest(self) -> None:
        old, _ = self._traces.popitem(last=False)
        self._total -= self._sizes.pop(old, 0)

    def on_end(self, span: ReadableSpan) -> None:
        try:
            ctx = span.context
            if ctx is None or not ctx.is_valid:
                return
            status = span.status
            is_error = status is not None and status.status_code == StatusCode.ERROR
            record = {
                "span_id": f"{ctx.span_id:016x}",
                "parent_id": f"{span.parent.span_id:016x}" if span.parent is not None else None,
                "name": scrub_text(span.name or ""),
                "status": "error" if is_error else "ok",
                "status_desc": scrub_text(status.description or "") if is_error and status is not None else "",
                "start_ns": span.start_time or 0,
                "attrs": _record_attrs(span),
            }
            trace_id = f"{ctx.trace_id:032x}"
            size = _record_chars(record)
        except Exception:  # a recorder must never break tracing
            log.warning("Could not record a span for the local trace cache.", exc_info=False)
            return
        with self._lock:
            spans = self._traces.get(trace_id)
            if spans is None:
                spans = self._traces[trace_id] = []
                self._sizes[trace_id] = 0
                while len(self._traces) > self._max_traces:
                    self._evict_oldest()
            else:
                self._traces.move_to_end(trace_id)
            if len(spans) >= self._max_spans or size > self._max_chars:
                return
            spans.append(record)
            self._sizes[trace_id] += size
            self._total += size
            while self._total > self._max_chars and len(self._traces) > 1:
                self._evict_oldest()  # the trace just written is the newest, so it stays

    def get(self, trace_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(s) for s in self._traces.get(trace_id, [])]

    def clear(self) -> None:
        with self._lock:
            self._traces.clear()
            self._sizes.clear()
            self._total = 0

    def shutdown(self) -> None:  # noqa: D102
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:  # noqa: D102
        return True


_recorder = _Recorder()
_recorder_providers: set[int] = set()


def _install_recorder() -> None:
    """Attach the recorder to neatlogs' own TracerProvider, once per provider. Never raises."""
    try:
        from neatlogs._wrap_utils import get_neatlogs_provider  # private, but it is the one provider all spans use

        provider = get_neatlogs_provider()
        if provider is None or id(provider) in _recorder_providers:
            return
        provider.add_span_processor(_recorder)
        _recorder_providers.add(id(provider))
    except Exception:
        log.warning("Could not attach the local trace recorder, so traces are only read back from neatlogs.")


def _tool_results(attrs: Mapping[str, Any], tool_names: Mapping[str, str]) -> list[dict[str, Any]]:
    """The tool answers the model received at the start of an LLM span: the last input message, if it has any."""
    messages: dict[int, dict[str, Any]] = {}
    for key, value in attrs.items():
        m = _INPUT_MESSAGE.match(key)
        if m:
            messages.setdefault(int(m.group(1)), {})[m.group(2)] = value
    if not messages:
        return []
    last = messages[max(messages)]
    content = last.get("content")
    if last.get("role") != "user" or not isinstance(content, str):
        return []
    try:
        blocks = json.loads(content)
    except ValueError:
        return []
    if not isinstance(blocks, list):
        return []
    out: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        body = block.get("content")
        if not isinstance(body, str):
            body = json.dumps(body, default=str)
        out.append(
            {
                "name": tool_names.get(str(block.get("tool_use_id")), "unknown"),
                "is_error": bool(block.get("is_error", False)),
                "content": _clip(scrub_text(body), _TOOL_RESULT_CHARS),
            }
        )
    return out


def _tool_calls(attrs: Mapping[str, Any]) -> list[dict[str, Any]]:
    calls: dict[int, dict[str, Any]] = {}
    for key, value in attrs.items():
        m = _TOOL_CALL.match(key)
        if m:
            calls.setdefault(int(m.group(1)), {})[m.group(2)] = value
    return [calls[i] for i in sorted(calls)]


def _tool_input(arguments: Any) -> Any:
    if not isinstance(arguments, str):
        return {}
    try:
        parsed = json.loads(arguments)
    except ValueError:
        return {"_unparsed": _clip(scrub_text(arguments), _TOOL_INPUT_CHARS)}
    text = json.dumps(parsed, default=str)
    if len(text) <= _TOOL_INPUT_CHARS:
        return _scrub_obj(parsed)
    return {"_shortened": _clip(scrub_text(text), _TOOL_INPUT_CHARS)}


def trace_excerpt(raw_spans: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Turn raw recorded spans into the trace excerpt that prompts/diagnostician.md describes.

    Model-call spans (`anthropic.messages.create`) become {span_id, name, parent_id, status, round, stop_reason,
    tool_results_in, tool_calls_out, text_out}; every other span becomes {span_id, name, parent_id, status, attrs}.
    Spans are ordered by start time, and rounds count from 1 in that order.
    """
    ordered = sorted(enumerate(raw_spans), key=lambda pair: (pair[1].get("start_ns") or 0, pair[0]))
    tool_names: dict[str, str] = {}
    excerpt: list[dict[str, Any]] = []
    round_no = 0
    for _, raw in ordered:
        attrs: Mapping[str, Any] = raw.get("attrs") or {}
        base = {
            "span_id": str(raw.get("span_id", "")),
            "name": str(raw.get("name", "")),
            "parent_id": raw.get("parent_id"),
            "status": str(raw.get("status", "ok")),
        }
        if base["name"] == LLM_SPAN_NAME or attrs.get("neatlogs.span.kind") == "llm":
            round_no += 1
            calls = _tool_calls(attrs)
            results_in = _tool_results(attrs, tool_names)  # answers to calls made by EARLIER spans
            for call in calls:
                if call.get("id"):
                    tool_names[str(call["id"])] = str(call.get("name", ""))
            text_out = attrs.get("neatlogs.llm.output_messages.0.content", "")
            excerpt.append(
                {
                    **base,
                    "round": round_no,
                    "stop_reason": attrs.get("neatlogs.llm.finish_reason"),
                    "tool_results_in": results_in,
                    "tool_calls_out": [
                        {"name": str(c.get("name", "")), "input": _tool_input(c.get("arguments"))} for c in calls
                    ],
                    "text_out": _clip(scrub_text(str(text_out)), _TEXT_OUT_CHARS) if text_out else "",
                }
            )
            continue
        kept = {
            k: (_clip(v, _SPAN_ATTR_CHARS) if isinstance(v, str) else v)
            for k, v in attrs.items()
            if not k.startswith(("neatlogs.", "openinference."))
        }
        if raw.get("status") == "error" and raw.get("status_desc") and "error" not in kept:
            kept["error"] = _clip(str(raw["status_desc"]), _SPAN_ATTR_CHARS)
        excerpt.append({**base, "attrs": kept})
    return excerpt


def recorded_spans(trace_id: str) -> list[dict[str, Any]]:
    """The spans recorded in this process for `trace_id`, as the trace excerpt; [] if none (or evicted)."""
    return trace_excerpt(_recorder.get(trace_id))


def clear_recorded_spans() -> None:
    """Forget every recorded span (tests)."""
    _recorder.clear()
