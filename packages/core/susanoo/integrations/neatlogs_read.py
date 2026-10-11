"""Read traces back from neatlogs (public read API). [PRD › Tech stack] [PRD › The eight heads of Orochi]

Used by the Diagnostician when a trace is not in this process's local recorder (`tracing.recorded_spans`),
for example when the worker restarted between the Runner and the Diagnostician.

API (from the neatlogs docs):
    GET {base}/traces/{trace_id}                      -> trace with `finalizationStatus`
    GET {base}/traces/{trace_id}/spans?limit=50&cursor=...  -> a page of spans
    headers: Authorization: Bearer $NEATLOGS_TOKEN, x-project-id: $NEATLOGS_PROJECT_ID

Only `spanId`, `parentSpanId`, `spanName`, `spanType`, `status` and `costUsd` are verified field names. Every
other field is read defensively (several likely spellings, never a crash on a missing one). The token is never
logged and never put into an error message.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import quote

import httpx

from susanoo.integrations import tracing

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://app.neatlogs.com/api/v1/public"
TOKEN_ENV = "NEATLOGS_TOKEN"
PROJECT_ENV = "NEATLOGS_PROJECT_ID"
BASE_URL_ENV = "NEATLOGS_READ_BASE_URL"

PAGE_LIMIT = 50
MAX_PAGES = 20
FINALIZE_WAIT_S = 20.0
POLL_INTERVAL_S = 1.0
REQUEST_TIMEOUT_S = 15.0

_TRACE_ID = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_-]{5,127}$")
_ERROR_STATUSES = frozenset({"error", "failed", "failure", "exception"})


class NeatlogsReadError(Exception):
    """neatlogs could not be read. The message is safe to show (it never contains the token)."""


class NeatlogsAuthError(NeatlogsReadError):
    """neatlogs rejected the read token or project id (HTTP 401 or 403)."""


class NeatlogsNotReady(NeatlogsReadError):
    """The trace did not reach `finalizationStatus == "finalized"` within the wait."""


def _find(body: Any, *keys: str) -> Any:
    """First value found under any of `keys` at the top of `body` or one level down (e.g. under "data")."""
    if not isinstance(body, Mapping):
        return None
    for key in keys:
        if key in body:
            return body[key]
    for value in body.values():
        if isinstance(value, Mapping):
            for key in keys:
                if key in value:
                    return value[key]
    return None


def _span_list(body: Any) -> list[Any]:
    if isinstance(body, list):
        return body
    found = _find(body, "spans", "data", "items", "results")
    if isinstance(found, list):
        return found
    if isinstance(found, Mapping):
        return _span_list(found)
    return []


def _next_cursor(body: Any) -> str | None:
    cursor = _find(body, "nextCursor", "next_cursor", "cursor", "next")
    return cursor if isinstance(cursor, str) and cursor else None


def _flatten(prefix: str, value: Any, out: dict[str, Any]) -> None:
    """Flatten nested attribute dicts to dotted keys, so {"neatlogs": {"llm": {...}}} == {"neatlogs.llm...": ...}."""
    if isinstance(value, Mapping):
        for k, v in value.items():
            _flatten(f"{prefix}.{k}" if prefix else str(k), v, out)
    elif isinstance(value, list) and prefix:
        for i, v in enumerate(value):
            _flatten(f"{prefix}.{i}", v, out)
    elif prefix:
        out[prefix] = value


def _start_ns(record: Mapping[str, Any], index: int) -> int:
    for key in ("startTimeUnixNano", "startTimeNs"):
        value = record.get(key)
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return index  # API order


def to_raw_span(record: Mapping[str, Any], index: int) -> dict[str, Any]:
    """One neatlogs span record -> the raw span shape `tracing.trace_excerpt` takes."""
    status = str(record.get("status", "") or "").lower()
    attrs: dict[str, Any] = {}
    for key in ("attributes", "attrs", "metadata"):
        if isinstance(record.get(key), Mapping):
            _flatten("", record[key], attrs)
    span_type = str(record.get("spanType", "") or "")
    name = str(record.get("spanName", "") or "")
    if span_type.lower() in {"llm", "generation"} and "neatlogs.span.kind" not in attrs:
        attrs["neatlogs.span.kind"] = "llm"
    output = record.get("output")
    if "neatlogs.llm.output_messages.0.content" not in attrs and isinstance(output, str) and output:
        attrs["neatlogs.llm.output_messages.0.content"] = output
    scrubbed = {
        k: tracing.redact(k, v) if isinstance(v, (str, bool, int, float)) else tracing.scrub_text(str(v))
        for k, v in attrs.items()
    }
    return {
        "span_id": str(record.get("spanId", "") or ""),
        "parent_id": record.get("parentSpanId") or None,
        "name": tracing.scrub_text(name),
        "status": "error" if status in _ERROR_STATUSES else "ok",
        "status_desc": "",
        "start_ns": _start_ns(record, index),
        "attrs": scrubbed,
    }


class NeatlogsReadClient:
    """Async client for the neatlogs public read API. Inject `http` (an `httpx.AsyncClient`) in tests."""

    def __init__(
        self,
        token: str,
        project_id: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        http: httpx.AsyncClient | None = None,
        finalize_wait_s: float = FINALIZE_WAIT_S,
        poll_interval_s: float = POLL_INTERVAL_S,
        page_limit: int = PAGE_LIMIT,
        max_pages: int = MAX_PAGES,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not token or not project_id:
            raise ValueError("A neatlogs read token and project id are both required.")
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}", "x-project-id": project_id}
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S)
        self._finalize_wait_s = finalize_wait_s
        self._poll_interval_s = poll_interval_s
        self._page_limit = page_limit
        self._max_pages = max_pages
        self._sleep = sleep
        self._clock = clock

    @classmethod
    def from_env(cls, **kw: Any) -> NeatlogsReadClient | None:
        """A client from NEATLOGS_TOKEN and NEATLOGS_PROJECT_ID, or None when either is missing."""
        token = os.environ.get(TOKEN_ENV, "").strip()
        project = os.environ.get(PROJECT_ENV, "").strip()
        if not token or not project:
            return None
        return cls(token, project, base_url=os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL, **kw)

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def _get(self, path: str, params: Mapping[str, Any] | None = None) -> httpx.Response:
        try:
            response = await self._http.get(f"{self._base}{path}", params=params, headers=self._headers)
        except httpx.HTTPError as exc:
            raise NeatlogsReadError(f"neatlogs could not be reached ({type(exc).__name__}).") from None
        if response.status_code in (401, 403):
            raise NeatlogsAuthError("neatlogs rejected the read token or project id.")
        return response

    @staticmethod
    def _json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            raise NeatlogsReadError("neatlogs sent an answer that is not JSON.") from None

    @staticmethod
    def _check_trace_id(trace_id: str) -> str:
        if not isinstance(trace_id, str) or not _TRACE_ID.match(trace_id):
            raise NeatlogsReadError("That is not a valid trace id.")
        return quote(trace_id, safe="")

    async def wait_finalized(self, trace_id: str) -> dict[str, Any]:
        """Poll the trace until `finalizationStatus == "finalized"`; raise NeatlogsNotReady after the wait."""
        tid = self._check_trace_id(trace_id)
        deadline = self._clock() + self._finalize_wait_s
        while True:
            response = await self._get(f"/traces/{tid}")
            # 404 right after a run can simply mean "not ingested yet"; 429 and 5xx are transient.
            if response.status_code == 200:
                body = self._json(response)
                if _find(body, "finalizationStatus") == "finalized":
                    return body if isinstance(body, dict) else {}
            elif response.status_code not in (404, 429) and response.status_code < 500:
                raise NeatlogsReadError(f"neatlogs answered with HTTP {response.status_code} for the trace.")
            if self._clock() >= deadline:
                raise NeatlogsNotReady("The trace is not ready in neatlogs yet.")
            await self._sleep(self._poll_interval_s)

    async def fetch_spans(self, trace_id: str) -> list[dict[str, Any]]:
        """Every span record of the trace, following the pagination cursor. Records are returned as sent."""
        tid = self._check_trace_id(trace_id)
        records: list[dict[str, Any]] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(self._max_pages):
            params: dict[str, Any] = {"limit": self._page_limit}
            if cursor:
                params["cursor"] = cursor
            response = await self._get(f"/traces/{tid}/spans", params)
            if response.status_code != 200:
                raise NeatlogsReadError(f"neatlogs answered with HTTP {response.status_code} for the spans.")
            body = self._json(response)
            records.extend(r for r in _span_list(body) if isinstance(r, dict))
            cursor = _next_cursor(body)
            if not cursor or cursor in seen:
                break
            seen.add(cursor)
        else:
            log.warning("Stopped reading spans after %d pages.", self._max_pages)
        return records

    async def trace_spans(self, trace_id: str) -> list[dict[str, Any]]:
        """Wait for the trace to be finalized, read all its spans, and return them as the trace excerpt."""
        await self.wait_finalized(trace_id)
        records = await self.fetch_spans(trace_id)
        return tracing.trace_excerpt([to_raw_span(r, i) for i, r in enumerate(records)])


__all__ = [
    "NeatlogsAuthError",
    "NeatlogsNotReady",
    "NeatlogsReadClient",
    "NeatlogsReadError",
    "to_raw_span",
]
