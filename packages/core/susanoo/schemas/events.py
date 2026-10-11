"""Event shape, streamed over SSE and exported to TypeScript. [PRD › Contracts (locked) › Event shape]

Frozen: change only with both owners' agreement (see the contract-change skill).
"""

from typing import Literal

from pydantic import BaseModel


class RunEvent(BaseModel):
    run_id: str
    seq: int
    head: Literal[
        "planner", "builder", "agent_smith", "simulator", "runner", "diagnostician", "fixer", "verifier", "system"
    ]
    type: Literal["started", "progress", "finished", "error", "question_needed", "approval_needed", "metrics"]
    message: str  # plain English, shown in the event log
    payload: dict
    trace_url: str | None = None
    at: str  # ISO timestamp
