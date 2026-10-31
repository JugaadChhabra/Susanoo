"""Adapter-boundary models [Spec §10]. DRAFT for CR-09.

These are the proposal for ``susanoo_contracts`` protocol 1.1 (see
docs/parallel/muaz/contract-requests/CR-09-adapter-models.md). Once Aarav lands them canonically,
this module re-exports the canonical classes instead of defining them.

Rules carried by the shapes: the agent context never contains expected answers or hidden checks;
usage and tool events are explicitly ``Unavailable`` rather than fabricated; every possible
mutation is preceded by a ``StepIntent``; ``ActionContext`` carries the attempt's fencing generation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, SecretStr
from susanoo_contracts import Capabilities

PROTOCOL_VERSION = "1.0"


class Wire(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Unavailable(Wire):
    unavailable: Literal[True] = True
    reason: str = Field(min_length=1, max_length=500)


class Usage(Wire):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cost_minor: int | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")


class ToolRoute(Wire):
    """Approved route to the environment; the token binds namespace and operations server-side."""

    base_url: str = Field(min_length=1, max_length=500)
    namespace: str = Field(min_length=1, max_length=100)
    token: SecretStr
    operations: list[str] = Field(default_factory=list, max_length=100)


class InvocationContext(Wire):
    """Everything the agent may know about an attempt. No expected outcomes, checks or fixture truth."""

    attempt_id: str
    run_id: str | None = None
    case_id: str | None = None
    namespace: str
    deadline: AwareDatetime
    turn: int = Field(default=1, ge=1)
    tool_route: ToolRoute | None = None


class Message(Wire):
    role: Literal["user"] = "user"
    text: str = Field(min_length=1, max_length=65536)
    structured: dict[str, Any] | None = None


class SessionHandle(Wire):
    session_id: str = Field(min_length=1, max_length=200)
    attempt_id: str


class ToolEvent(Wire):
    operation: str
    status: str
    seq: int | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class AgentReply(Wire):
    invocation_id: str
    session_id: str
    text: str | None = Field(default=None, max_length=262144)
    structured: dict[str, Any] | None = None
    usage: Usage | Unavailable = Field(default_factory=lambda: Unavailable(reason="not reported by adapter"))
    tool_events: list[ToolEvent] | Unavailable = Field(
        default_factory=lambda: Unavailable(reason="not reported by adapter")
    )
    truncated: bool = False


class CancelResult(Wire):
    invocation_id: str
    acknowledged: bool
    remote_cancel_attempted: bool = False
    note: str | None = None


class CapabilityReport(Wire):
    adapter: str
    adapter_version: str
    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    capabilities: Capabilities
    checks: dict[str, bool] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class FixtureContext(Wire):
    attempt_id: str
    namespace: str
    fencing_generation: int = Field(ge=1)
    deadline: AwareDatetime


class FixtureHandle(Wire):
    namespace: str
    seed_digest: str
    aliases: dict[str, bool | int | str] = Field(default_factory=dict)
    prepared_at: AwareDatetime


class ObservationQuery(Wire):
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    kind: str
    window_seconds: int = Field(default=10, ge=0, le=3600)
    baseline: bool = False


ActionKind = Literal["agent_invoke", "environment_action", "fixture_prepare", "observe", "cleanup", "reference_call"]
DispatchState = Literal[
    "prepared", "dispatching", "acknowledged", "rejected_before_effect", "uncertain", "not_dispatched"
]


class StepIntent(Wire):
    attempt_id: str
    step_id: str
    operation_id: str
    action_kind: ActionKind
    request_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    prepared_at: AwareDatetime


class Reconciliation(Wire):
    operation_id: str
    state: Literal["resolved", "unresolved"]
    finding: Literal["effect_present", "effect_absent", "indeterminate"]
    note: str = Field(min_length=1, max_length=4000)


class CleanupResult(Wire):
    state: Literal["succeeded", "failed", "not_required"]
    reason_code: str | None = None
    detail: str | None = Field(default=None, max_length=4000)


class ApprovedAction(Wire):
    operation: str
    request: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, max_length=200)


class ActionContext(Wire):
    attempt_id: str
    namespace: str
    operation_id: str
    fencing_generation: int = Field(ge=1)
    deadline: AwareDatetime


class ActionResult(Wire):
    status: Literal["ok", "rejected", "error", "replayed", "uncertain"]
    code: str | None = None
    response: dict[str, Any] = Field(default_factory=dict)


class SnapshotScope(Wire):
    kinds: list[str] = Field(min_length=1)


class StateSnapshot(Wire):
    captured_at: AwareDatetime
    digest: str
    state: dict[str, list[dict[str, Any]]]


FrameType = Literal[
    "preflight", "start_session", "invoke", "cancel", "close_session", "shutdown", "result", "error", "event"
]


class JsonlFrame(Wire):
    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    request_id: str = Field(min_length=1, max_length=100)
    type: FrameType
    payload: dict[str, Any] = Field(default_factory=dict)


def utcnow() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)
