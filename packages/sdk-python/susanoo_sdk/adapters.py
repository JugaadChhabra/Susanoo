"""Adapter protocols [Spec §10]. Agent, fixture and environment adapters get separate contexts.

The observer/fixture side holds different credentials from the agent: an ``AgentAdapter`` only ever
sees an ``InvocationContext`` (with its tool route), never fixture truth or checks.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from susanoo_contracts import FixtureSpec
from susanoo_evaluator import ObservedState

from .models import (
    ActionContext,
    ActionResult,
    AgentReply,
    ApprovedAction,
    CancelResult,
    CapabilityReport,
    CleanupResult,
    FixtureContext,
    FixtureHandle,
    InvocationContext,
    Message,
    ObservationQuery,
    Reconciliation,
    SessionHandle,
    SnapshotScope,
    StateSnapshot,
    StepIntent,
    ToolRoute,
)


class AdapterError(Exception):
    """The adapter broke its contract (malformed frame, crash, oversize output). Never a passing result."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@runtime_checkable
class AgentAdapter(Protocol):
    async def preflight(self) -> CapabilityReport: ...

    async def start_session(self, context: InvocationContext) -> SessionHandle: ...

    async def invoke(self, session: SessionHandle, message: Message, context: InvocationContext) -> AgentReply: ...

    async def cancel(self, invocation_id: str) -> CancelResult: ...

    async def close_session(self, session: SessionHandle) -> None: ...


@runtime_checkable
class FixtureAdapter(Protocol):
    async def prepare(self, spec: FixtureSpec, context: FixtureContext) -> FixtureHandle: ...

    def tool_route(self, fixture: FixtureHandle) -> ToolRoute | None: ...

    async def observe(self, fixture: FixtureHandle, queries: list[ObservationQuery]) -> list[ObservedState]: ...

    async def reconcile(self, intent: StepIntent, fixture: FixtureHandle) -> Reconciliation: ...

    async def cleanup(self, fixture: FixtureHandle) -> CleanupResult: ...


@runtime_checkable
class EnvironmentAdapter(Protocol):
    async def execute(self, action: ApprovedAction, context: ActionContext) -> ActionResult: ...

    async def snapshot(self, scope: SnapshotScope) -> StateSnapshot: ...
