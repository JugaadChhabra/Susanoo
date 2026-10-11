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
from collections.abc import Callable, Mapping
from typing import Any, TypeVar

import neatlogs
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

_MAX_DEPTH = 8


def scrub_text(text: str) -> str:
    """Replace anything in `text` that looks like a credential with [REDACTED]."""
    for pattern in _SECRET_VALUE_PATTERNS:
        text = pattern.sub(REDACTED, text)
    text = _URL_PASSWORD.sub(rf"\g<1>{REDACTED}\g<3>", text)
    return _ASSIGNED_SECRET.sub(rf"\g<1>{REDACTED}", text)


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
