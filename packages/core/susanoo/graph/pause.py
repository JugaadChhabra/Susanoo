"""Pausing for a human with exactly-once events. [PRD › LangGraph flow and shared state › Human gate]

LangGraph re-runs a node from the top every time it resumes, and re-raises the interrupt when the resume
value is missing or ignored (for example `Command(resume={})`). Events emitted before `interrupt()` would
therefore be duplicated. `pause()` emits its events only on the first pass:

- `interrupt()` raises GraphInterrupt only when it has no answer yet, so events go in the except branch.
- `build_graph` wraps each pausing node and, before the node runs, asks the checkpointer (public API:
  `aget_state`) whether that node already has a pending interrupt on this thread. If it does, the pause
  was announced before, and `ALREADY_ANNOUNCED` tells `pause()` to stay quiet.

This is durable (it reads the checkpoint, not process memory), so it also holds if a different worker
picks the run up after a restart.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from langgraph.errors import GraphInterrupt
from langgraph.types import interrupt

from susanoo.emit import emit

# True while a pausing node runs again after it already announced its pause (set by build.py).
ALREADY_ANNOUNCED: ContextVar[bool] = ContextVar("susanoo_pause_already_announced", default=False)


async def pause(state: Any, head: str, started: str, type_: str, message: str, payload: dict) -> Any:
    """`interrupt(payload)`, emitting `started` and `type_` (question_needed / approval_needed) once."""
    try:
        return interrupt(payload)
    except GraphInterrupt:
        if not ALREADY_ANNOUNCED.get():
            await emit(state, head, "started", started)
            await emit(state, head, type_, message, payload)
        raise
