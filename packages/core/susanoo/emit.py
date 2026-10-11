"""Event emission for heads. [PRD › Contracts (locked) › Event shape]

Heads call `emit(state, head, type, message, ...)`. By default the event is persisted through
Jugaad's `susanoo.db.emit_event` (needs DATABASE_URL). Tests and the offline demo swap the sink
with `use_sink(...)` so no database is needed; the sink travels in a contextvar, so it also
reaches LangGraph node tasks started inside the `with` block.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, Protocol

from susanoo.schemas import RunEvent


class EventSink(Protocol):
    async def __call__(
        self,
        run_id: str,
        head: str,
        type: str,
        message: str,
        payload: dict | None = None,
        trace_url: str | None = None,
    ) -> RunEvent: ...


async def _db_sink(
    run_id: str,
    head: str,
    type: str,
    message: str,
    payload: dict | None = None,
    trace_url: str | None = None,
) -> RunEvent:
    from susanoo.db import emit_event  # imported lazily so offline use never needs a database

    return await emit_event(run_id, head, type, message, payload, trace_url)


_sink: ContextVar[EventSink] = ContextVar("susanoo_event_sink", default=_db_sink)


class InMemorySink:
    """Builds validated RunEvent objects with seq 1, 2, 3... per run, without a database."""

    def __init__(self) -> None:
        self.events: list[RunEvent] = []
        self._seq: dict[str, int] = {}

    async def __call__(
        self,
        run_id: str,
        head: str,
        type: str,
        message: str,
        payload: dict | None = None,
        trace_url: str | None = None,
    ) -> RunEvent:
        seq = self._seq.get(run_id, 0) + 1
        self._seq[run_id] = seq
        event = RunEvent(
            run_id=run_id,
            seq=seq,
            head=head,  # type: ignore[arg-type]  # validated by the model
            type=type,  # type: ignore[arg-type]
            message=message,
            payload=payload or {},
            trace_url=trace_url,
            at=datetime.now(UTC).isoformat(),
        )
        self.events.append(event)
        return event


@contextmanager
def use_sink(sink: EventSink) -> Iterator[EventSink]:
    """Route every `emit` call inside the block to `sink`."""
    token = _sink.set(sink)
    try:
        yield sink
    finally:
        _sink.reset(token)


async def emit(
    state: Mapping[str, Any],
    head: str,
    type: str,
    message: str,
    payload: dict | None = None,
    trace_url: str | None = None,
) -> RunEvent:
    """Emit one RunEvent for the run in `state`. `message` is plain English shown in the UI."""
    return await _sink.get()(state["run_id"], head, type, message, payload, trace_url)
