"""C0 proposal: immutable configuration, runner evidence and platform requests."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, field_validator, model_validator
from pydantic import create_model, model_serializer
import rfc8785

from .checks import Alias as Alias, BusinessDate as BusinessDate, Check, Money as Money, Operand

PROTOCOL_VERSION = "1.0"
CONTRACT_REVISION = "1.1-proposed"
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Digest = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
SecretRef = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,127}$")]


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    @model_serializer(mode="wrap")
    def normalized_wire(self, handler, info):
        result = handler(self)
        if info.mode == "json":
            for name, value in self.__dict__.items():
                if isinstance(value, datetime):
                    if value.tzinfo is None:
                        raise ValueError("Wire timestamps must be timezone-aware")
                    key = type(self).model_fields[name].alias or name
                    result[key] = value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
        return result

    @model_validator(mode="after")
    def finite_json(self):
        canonical_json(self.model_dump(mode="json", by_alias=True))
        return self


def canonical_json(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", by_alias=True)
    return rfc8785.dumps(value).decode("utf-8")


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


class Role(StrEnum):
    OWNER = "owner"
    ENGINEER = "engineer"
    REVIEWER = "reviewer"
    VIEWER = "viewer"


class Verdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class ExecutionState(StrEnum):
    QUEUED = "queued"
    PREPARING = "preparing"
    RUNNING = "running"
    OBSERVING = "observing"
    EVALUATING = "evaluating"
    COMPLETED = "completed"
    ERROR = "error"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class Gate(StrEnum):
    IN_PROGRESS = "IN_PROGRESS"
    BLOCKED = "BLOCKED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    READY_FOR_ACCEPTANCE = "READY_FOR_ACCEPTANCE"


class Capabilities(WireModel):
    invoke: bool = False
    sessions: bool = False
    multi_turn: bool = False
    stream: bool = False
    async_jobs: bool = False
    cancel_remote: bool = False
    observe: bool = False
    reset: bool = False
    tool_routing: bool = False
    tool_events: bool = False
    audio: bool = False
    reference_replay: bool = False
    patch_source: bool = False
    local_evidence: bool = True
    idempotent_submission: bool = False
    isolation_tested: bool = False
    observer_consistency: Literal["bounded", "immediate", "unbounded", "unknown"] = "unknown"
    retry_semantics: Literal["idempotent", "never", "unknown"] = "unknown"
    max_concurrency: int = Field(default=1, ge=1, le=3)
    modalities: list[Literal["text", "audio", "browser", "tool"]] = Field(default_factory=lambda: ["text"])


class RunLimits(WireModel):
    max_concurrency: int = Field(default=1, ge=1, le=3)
    max_turns: int = Field(default=8, ge=1, le=100)
    invocation_timeout_seconds: int = Field(default=60, ge=1, le=300)
    case_timeout_seconds: int = Field(default=300, ge=1, le=3600)
    run_timeout_seconds: int = Field(default=7200, ge=1, le=86400)
    max_agent_invocations: int = Field(default=160, ge=1, le=100000)
    max_reference_calls: int = Field(default=0, ge=0, le=100000)
    max_output_bytes: int = Field(default=262144, ge=1, le=1048576)
    max_storage_bytes: int = Field(default=10485760, ge=1, le=1073741824)
    cleanup_reserve: int = Field(default=3, ge=0, le=1000)


class DataPolicy(WireModel):
    mode: Literal["synthetic", "local_only", "redacted_hosted"] = "synthetic"
    hosted_retention_days: int = Field(default=30, ge=1, le=30)
    upload_fields: list[Name] = Field(default_factory=list, max_length=100)
    external_models_allowed: bool = False
    source_upload_allowed: bool = False
    media_upload_allowed: bool = False
    report_upload_allowed: bool = False
    max_heldout_exposures: int = Field(default=5, ge=1, le=100)
    allowed_hosts: list[Name] = Field(default_factory=list, max_length=100)
    allowed_operations: list[Name] = Field(default_factory=list, max_length=100)


class WorkspaceCreate(WireModel):
    name: Name


class ProjectCreate(WireModel):
    workspace_id: UUID
    name: Name
    workflow: Name
    timezone: str = "Asia/Kolkata"
    policy: DataPolicy = Field(default_factory=DataPolicy)

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            raise ValueError("Use a known IANA timezone") from None
        return value


class MembershipChange(WireModel):
    user_id: UUID
    roles: list[Role] = Field(min_length=1, max_length=4)
    hidden_access: bool = False
    project_id: UUID | None = None


class ProjectPatch(WireModel):
    name: Name | None = None
    workflow: Name | None = None
    timezone: str | None = None

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value):
        if value is not None:
            try:
                ZoneInfo(value)
            except ZoneInfoNotFoundError:
                raise ValueError("Use a known IANA timezone") from None
        return value

    @model_validator(mode="after")
    def nonempty(self):
        if not any(value is not None for value in (self.name, self.workflow, self.timezone)):
            raise ValueError("Supply at least one project field")
        return self


class ConnectionSpec(WireModel):
    kind: Literal["connection"] = "connection"
    family: Literal["I01", "I02", "I03", "I04", "I05", "I06", "I07", "I08", "I09", "I10", "I11", "I12", "I13", "I14"]
    adapter: Name
    protocol_version: str = PROTOCOL_VERSION
    source_commit: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{40}$")] | None = None
    image_digest: Annotated[str, StringConstraints(pattern=r"^sha256:[a-f0-9]{64}$")] | None = None
    repository: Name | None = None
    entrypoint: list[Name] = Field(default_factory=list, max_length=30)
    secret_refs: list[SecretRef] = Field(default_factory=list, max_length=30)
    capabilities: Capabilities = Field(default_factory=Capabilities)
    mapping: dict[str, str] = Field(default_factory=dict)


class PolicySpec(WireModel):
    kind: Literal["policy"] = "policy"
    policy: DataPolicy


class RequirementSpec(WireModel):
    kind: Literal["requirement"] = "requirement"
    source_clause: Name
    expected: str = Field(min_length=1, max_length=10000)
    forbidden: list[str] = Field(default_factory=list, max_length=100)
    severity: Literal["critical", "high", "medium", "low"] = "high"
    owner: Name
    exceptions: list[str] = Field(default_factory=list, max_length=100)


class CheckSpec(WireModel):
    kind: Literal["check"] = "check"
    requirement_version_id: UUID
    operator: Literal[
        "exists",
        "equals",
        "range",
        "count",
        "relationship",
        "transition",
        "forbidden_change",
        "unchanged",
        "deadline",
        "manual",
        "semantic",
    ]
    observation: Name
    entity: Name | None = None
    path: (
        Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*|\[[0-9]+\])*$")]
        | None
    ) = None
    expected: Operand | None = None
    filter: dict[str, Operand] = Field(default_factory=dict, max_length=20)
    min: Operand | None = None
    max: Operand | None = None
    target_observation: Name | None = None
    target_path: str | None = None
    target_filter: dict[str, Operand] = Field(default_factory=dict, max_length=20)
    key: str | None = None
    allowed: list[tuple[str, str]] | None = None
    allow_unchanged: bool | None = None
    paths: list[str] | None = None
    forbid_create: bool | None = None
    forbid_delete: bool | None = None
    within_seconds: int | None = Field(default=None, ge=0, le=86400)
    instructions: str | None = Field(default=None, min_length=1, max_length=4000)
    rubric_id: str | None = None
    required: bool = True
    severity: Literal["critical", "high", "medium", "low"] = "high"
    requires_complete_observation: bool = True
    window_seconds: int = Field(default=10, ge=0, le=3600)
    grader_version_id: UUID | None = None
    control_set_hash: Digest | None = None

    @model_validator(mode="after")
    def typed_definition(self):
        TypeAdapter(Check).validate_python(self.evaluator_definition())
        return self

    def evaluator_definition(self, check_id=None):
        excluded = {"kind", "requirement_version_id", "grader_version_id", "control_set_hash", "entity"}
        data = {
            key: value
            for key, value in self.model_dump(mode="json").items()
            if key not in excluded and value is not None
        }
        if not data["filter"]:
            data.pop("filter")
        if not data["target_filter"]:
            data.pop("target_filter")
        data["id"] = str(check_id or self.requirement_version_id)
        data["requirement_id"] = str(self.requirement_version_id)
        if self.grader_version_id and self.operator == "semantic":
            data["grader_version_id"] = str(self.grader_version_id)
        return data


class FixtureSpec(WireModel):
    kind: Literal["fixture"] = "fixture"
    adapter: Name
    recipe: dict[str, Any] = Field(default_factory=dict)
    digest: Digest
    synthetic: bool = True
    compatibility_key: Name

    @model_validator(mode="after")
    def recipe_digest(self):
        if self.digest != content_hash(self.recipe):
            raise ValueError("Fixture digest must equal the canonical recipe hash")
        return self


class ScriptStep(WireModel):
    message: str = Field(min_length=1, max_length=65536)
    when: Literal["always", "reply_contains", "reply_equals"] = "always"
    value: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def condition_has_value(self) -> ScriptStep:
        if self.when != "always" and self.value is None:
            raise ValueError("Conditional steps need an explicit value")
        return self


class CaseSpec(WireModel):
    kind: Literal["case"] = "case"
    fixture_version_id: UUID
    check_version_ids: list[UUID] = Field(min_length=1, max_length=100)
    script: list[ScriptStep] = Field(min_length=1, max_length=100)
    required: bool = True
    visibility: Literal["public", "hidden"] = "public"
    modality: Literal["text", "audio", "browser", "tool"] = "text"
    dependencies: list[UUID] = Field(default_factory=list)


class SuiteSpec(WireModel):
    kind: Literal["suite"] = "suite"
    case_version_ids: list[UUID] = Field(min_length=1, max_length=500)
    limits: RunLimits = Field(default_factory=RunLimits)

    @field_validator("case_version_ids")
    @classmethod
    def no_duplicates(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("Suite cases must be unique")
        return value


class EnvironmentSpec(WireModel):
    kind: Literal["environment"] = "environment"
    adapter: Name
    version: Name
    allowed_operations: list[Name] = Field(min_length=1)
    identity_profile: Name
    capabilities: Capabilities = Field(default_factory=Capabilities)
    fixture_version_id: UUID
    fault_profile: dict[str, Any] = Field(default_factory=dict)
    clock: str | None = None
    isolation: Literal["namespace", "exclusive"] = "namespace"
    resource_key: Name | None = None

    @model_validator(mode="after")
    def exclusive_resource(self):
        if self.isolation == "exclusive" and not self.resource_key:
            raise ValueError("Exclusive environments need an approved resource key")
        return self


class ResourceReleaseIn(WireModel):
    rationale: str = Field(min_length=1, max_length=4000)
    reconciliation_id: UUID


class DeclaredSimplification(WireModel):
    path: str = Field(min_length=1, max_length=500)
    rationale: str = Field(min_length=1, max_length=4000)


class FidelitySpec(WireModel):
    kind: Literal["fidelity"] = "fidelity"
    simulator_version_id: UUID
    reference_version_id: UUID
    operations: list[Name] = Field(min_length=1)
    identity_profile: Name
    normalizers: list[dict[str, Any]] = Field(default_factory=list)
    intentional_omissions: list[str] = Field(default_factory=list)
    intentional_simplifications: list[DeclaredSimplification] = Field(default_factory=list, max_length=100)
    reference_limitations: list[str] = Field(default_factory=list)
    consistency_window_seconds: int = Field(default=10, ge=0, le=3600)


class GraderSpec(WireModel):
    kind: Literal["grader"] = "grader"
    mode: Literal["deterministic", "semantic"] = "deterministic"
    evaluator_digest: Digest
    control_set_hash: Digest
    positive_control_ids: list[UUID] = Field(min_length=1)
    negative_control_ids: list[UUID] = Field(min_length=1)
    ambiguous_control_ids: list[UUID] = Field(default_factory=list)
    heldout_control_ids: list[UUID] = Field(default_factory=list)
    rubric: str | None = Field(default=None, max_length=10000)
    provider: Name | None = None
    model: Name | None = None
    max_missed_defects: int = Field(default=0, ge=0)
    max_false_alarms: int = Field(default=0, ge=0)


class SourceSpec(WireModel):
    kind: Literal["source"] = "source"
    repository: Name
    source_kind: Literal["git", "directory", "archive"] = "git"
    commit: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{40}$")] | None = None
    snapshot_digest: Digest | None = None
    recipe_hash: Digest
    lockfile_hash: Digest
    installation_id: int | None = Field(default=None, ge=1)
    trusted: bool = False
    allowed_patch_paths: list[Name] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def pinned_source_identity(self):
        if self.source_kind == "git" and self.commit is None:
            raise ValueError("Git sources require a pinned full commit")
        if self.source_kind != "git" and (self.snapshot_digest is None or self.commit is not None):
            raise ValueError("Local/archive sources require a snapshot digest, not a fabricated Git commit")
        if self.source_kind != "git" and self.installation_id is not None:
            raise ValueError("Only Git sources may request GitHub installation access")
        return self


class VoiceSpec(WireModel):
    kind: Literal["voice"] = "voice"
    provider: Literal["synthetic", "twilio"] = "synthetic"
    allowed_destinations: list[Name] = Field(default_factory=list)
    authorization_reference: Name
    recording_consent: bool = False
    media_location: Literal["local_only", "approved_hosted"] = "local_only"
    max_duration_seconds: int = Field(default=120, ge=1, le=600)
    max_concurrent_calls: int = Field(default=1, ge=1, le=3)
    cost_ceiling_minor: int = Field(default=0, ge=0, strict=True)
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")] = "INR"
    timezone: str = "Asia/Kolkata"
    permitted_hours: list[int] = Field(default_factory=lambda: list(range(9, 18)))


class ProbeSpec(WireModel):
    kind: Literal["probe"] = "probe"
    fidelity_version_id: UUID
    strategy: Literal["uniform", "priority"]
    seed: int
    max_reference_calls: int = Field(ge=1, le=100000)
    cleanup_reserve: int = Field(default=1, ge=0)
    candidate_ids: list[UUID] = Field(min_length=1)


class CollisionSpec(WireModel):
    kind: Literal["collision"] = "collision"
    environment_version_id: UUID
    actors: int = Field(default=2, ge=2, le=2)
    barriers: list[Name] = Field(min_length=1, max_length=30)
    repetitions: int = Field(default=1, ge=1, le=10)
    limits: RunLimits = Field(default_factory=RunLimits)


VersionData = Annotated[
    PolicySpec
    | ConnectionSpec
    | RequirementSpec
    | CheckSpec
    | FixtureSpec
    | CaseSpec
    | SuiteSpec
    | EnvironmentSpec
    | FidelitySpec
    | GraderSpec
    | SourceSpec
    | VoiceSpec
    | ProbeSpec
    | CollisionSpec,
    Field(discriminator="kind"),
]


class VersionCreate(WireModel):
    logical_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")]
    title: Name
    data: VersionData
    provenance: dict[str, str] = Field(default_factory=dict)
    hidden: bool = False


class ApprovalIn(WireModel):
    content_hash: Digest
    rationale: str = Field(min_length=1, max_length=4000)
    evidence_digest: Digest | None = None


class RunCreate(WireModel):
    schema_version: Literal["1.0"] = PROTOCOL_VERSION
    kind: Literal["outcome_suite"] = "outcome_suite"
    suite_version_id: UUID
    connection_version_id: UUID
    environment_version_id: UUID
    repetitions: int = Field(default=1, ge=1, le=10)
    limits: RunLimits = Field(default_factory=RunLimits)


class RunnerRegistration(WireModel):
    registration_token: str = Field(min_length=32, max_length=200)
    name: Name
    version: Name
    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    capabilities: list[Name] = Field(min_length=1)


class RunnerClaim(WireModel):
    available_slots: int = Field(default=1, ge=1, le=3)
    supported_job_kinds: list[Name] = Field(default_factory=lambda: ["outcome_suite"], max_length=50)


class LeaseRequest(WireModel):
    lease_token: str = Field(min_length=32, max_length=200)


ActionKind = Literal["agent_invoke", "environment_action", "fixture_prepare", "observe", "cleanup", "reference_call"]
DispatchState = Literal["acknowledged", "rejected_before_effect", "uncertain", "not_dispatched"]
SideEffectState = Literal["none_expected", "requires_observation", "confirmed_by_readback", "unresolved"]
CleanupState = Literal["succeeded", "failed", "not_required"]


class PreparedPayload(WireModel):
    operation_id: Name
    request_digest: Digest
    action_kind: ActionKind


class DispatchPayload(WireModel):
    operation_id: Name


class ResponsePayload(DispatchPayload):
    dispatch_state: DispatchState
    side_effect_state: SideEffectState
    response_artifact_id: UUID | None = None


class ObservationPayload(WireModel):
    observation_id: UUID


class CheckPayload(WireModel):
    check_version_id: UUID


class CleanupPayload(WireModel):
    state: CleanupState
    reason_code: str | None = Field(default=None, max_length=200)


class ReconciliationPayload(WireModel):
    state: Literal["resolved", "unresolved"]
    evidence_id: UUID


class CancelPayload(WireModel):
    pass


class ProgressPayload(WireModel):
    code: Name


class PreflightResult(WireModel):
    job_kind: Literal["preflight"] = "preflight"
    invocation_ok: bool = Field(strict=True)
    observer_ok: bool = Field(strict=True)
    cleanup_ok: bool = Field(strict=True)
    isolation_verified: bool = False
    max_concurrency: int = Field(default=1, ge=1, le=3, strict=True)


class CalibrationResult(WireModel):
    job_kind: Literal["calibration"] = "calibration"
    control_set_hash: Digest
    controls_verified: bool = Field(strict=True)
    missed_defects: int = Field(ge=0, strict=True)
    false_alarms: int = Field(ge=0, strict=True)
    report_ids: list[UUID] = Field(default_factory=list, max_length=100)


class BuildResult(WireModel):
    job_kind: Literal["build", "source_import"] = "build"
    status: Literal["succeeded", "failed", "unavailable"]
    source_commit: str | None = None
    image_digest: str | None = None
    snapshot_digest: Digest | None = None
    artifact_ids: list[UUID] = Field(default_factory=list, max_length=100)
    report_ids: list[UUID] = Field(default_factory=list, max_length=100)


class SourceBuildReport(WireModel):
    source_kind: Literal["git", "directory", "archive"]
    status: Literal["succeeded", "failed", "unavailable"]
    source_commit: str | None = None
    snapshot_digest: Digest
    recipe_digest: Digest
    files: int = Field(ge=0)
    runtime: Literal["python", "node", "container"]
    log_tail: str = Field(default="", max_length=4096)
    exit_code: int | None = None


class RepairResult(WireModel):
    job_kind: Literal["repair_retest", "patch_proposal"] = "repair_retest"
    tested_version: UUID | None = None
    source_commit: str | None = None
    patch_artifact_id: UUID | None = None
    base_commit: str | None = None
    validation_passed: bool = Field(strict=True)
    heldout_passed: bool = Field(strict=True)
    controls_passed: bool = Field(strict=True)
    criteria_unchanged: bool = Field(strict=True)
    report_id: UUID | None = None


class ReportResult(WireModel):
    job_kind: Literal[
        "action_comparison",
        "fresh_agent_pair",
        "reduce_reproduction",
        "simulator_inspect",
        "simulator_reset",
        "trace_import",
        "incident_draft",
        "collision",
        "probe",
        "voice",
    ]
    status: Literal["succeeded", "failed", "unknown", "unavailable"]
    artifact_ids: list[UUID] = Field(default_factory=list, max_length=100)
    report_ids: list[UUID] = Field(default_factory=list, max_length=100)


JobResult = Annotated[
    PreflightResult | CalibrationResult | BuildResult | RepairResult | ReportResult, Field(discriminator="job_kind")
]


def typed_job_result(value):
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    value = dict(value)
    if "job_kind" not in value:
        # Preserve existing 1.0 producers only where the result shape is unambiguous.
        if "invocation_ok" in value:
            value["job_kind"] = "preflight"
        elif "control_set_hash" in value:
            value["job_kind"] = "calibration"
        elif "validation_passed" in value:
            value["job_kind"] = "repair_retest"
        elif "source_commit" in value:
            value["job_kind"] = "build"
    return TypeAdapter(JobResult).validate_python(value)


class SourceCredentialIn(LeaseRequest):
    source_version_id: UUID


class SourceCredentialResponse(WireModel):
    token: str
    expires_at: datetime
    repository: str
    source_commit: str
    read_only: Literal[True] = True


class RunnerEvent(WireModel):
    event_id: UUID
    sequence: int = Field(ge=1)
    type: Literal[
        "step_prepared",
        "step_dispatching",
        "step_response_recorded",
        "observation_recorded",
        "check_recorded",
        "cleanup_recorded",
        "reconciliation_recorded",
        "cancel_acknowledged",
        "job_result",
        "progress",
    ]
    step_id: Name | None = None
    occurred_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def typed_payload(cls, value):
        if not isinstance(value, dict):
            return value
        models = {
            "step_prepared": PreparedPayload,
            "step_dispatching": DispatchPayload,
            "step_response_recorded": ResponsePayload,
            "observation_recorded": ObservationPayload,
            "check_recorded": CheckPayload,
            "cleanup_recorded": CleanupPayload,
            "reconciliation_recorded": ReconciliationPayload,
            "cancel_acknowledged": CancelPayload,
            "progress": ProgressPayload,
        }
        event_type = value.get("type")
        payload = value.get("payload", {})
        if event_type == "job_result":
            payload = typed_job_result(payload).model_dump(mode="json")
        elif event_type in models:
            payload = models[event_type].model_validate(payload).model_dump(mode="json")
        return {**value, "payload": payload}

    @field_validator("occurred_at")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("UTC-aware timestamps are required")
        return value


# Concrete variants keep both Python construction and generated wire schemas typed.
StepPreparedEvent = create_model(
    "StepPreparedEvent",
    __base__=RunnerEvent,
    type=(Literal["step_prepared"], "step_prepared"),
    payload=(PreparedPayload, ...),
)
StepDispatchingEvent = create_model(
    "StepDispatchingEvent",
    __base__=RunnerEvent,
    type=(Literal["step_dispatching"], "step_dispatching"),
    payload=(DispatchPayload, ...),
)
StepResponseEvent = create_model(
    "StepResponseEvent",
    __base__=RunnerEvent,
    type=(Literal["step_response_recorded"], "step_response_recorded"),
    payload=(ResponsePayload, ...),
)
ObservationEvent = create_model(
    "ObservationEvent",
    __base__=RunnerEvent,
    type=(Literal["observation_recorded"], "observation_recorded"),
    payload=(ObservationPayload, ...),
)
CheckEvent = create_model(
    "CheckEvent", __base__=RunnerEvent, type=(Literal["check_recorded"], "check_recorded"), payload=(CheckPayload, ...)
)
CleanupEvent = create_model(
    "CleanupEvent",
    __base__=RunnerEvent,
    type=(Literal["cleanup_recorded"], "cleanup_recorded"),
    payload=(CleanupPayload, ...),
)
ReconciliationEvent = create_model(
    "ReconciliationEvent",
    __base__=RunnerEvent,
    type=(Literal["reconciliation_recorded"], "reconciliation_recorded"),
    payload=(ReconciliationPayload, ...),
)
CancelEvent = create_model(
    "CancelEvent",
    __base__=RunnerEvent,
    type=(Literal["cancel_acknowledged"], "cancel_acknowledged"),
    payload=(CancelPayload, Field(default_factory=CancelPayload)),
)
ProgressEvent = create_model(
    "ProgressEvent", __base__=RunnerEvent, type=(Literal["progress"], "progress"), payload=(ProgressPayload, ...)
)
JobResultEvent = create_model(
    "JobResultEvent", __base__=RunnerEvent, type=(Literal["job_result"], "job_result"), payload=(JobResult, ...)
)
TypedRunnerEvent = Annotated[
    StepPreparedEvent
    | StepDispatchingEvent
    | StepResponseEvent
    | ObservationEvent
    | CheckEvent
    | CleanupEvent
    | ReconciliationEvent
    | CancelEvent
    | ProgressEvent
    | JobResultEvent,
    Field(discriminator="type"),
]


class EventBatch(LeaseRequest):
    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    events: list[TypedRunnerEvent] = Field(min_length=1, max_length=100)

    @field_validator("events", mode="before")
    @classmethod
    def event_instances(cls, value):
        return [item.model_dump(mode="json") if isinstance(item, BaseModel) else item for item in value]

    @model_validator(mode="after")
    def bounded_bytes(self):
        if len(canonical_json(self).encode()) > 1048576:
            raise ValueError("Event batches cannot exceed 1 MiB")
        return self


class Observation(WireModel):
    observation_id: UUID
    name: Name
    entity: Name | None = None
    source: Name
    captured_at: datetime
    availability: Literal["available", "unavailable"]
    completeness: Literal["complete_for_scope", "partial", "unknown"]
    namespace: Name
    window_seconds: int = Field(ge=0, le=3600)
    window_elapsed: bool
    storage_mode: Literal["local_only", "redacted_hosted"]
    values: dict[str, Any] = Field(default_factory=dict)
    artifact_ids: list[UUID] = Field(default_factory=list)
    redaction_version: Name
    digest: Digest

    @field_validator("captured_at")
    @classmethod
    def aware_time(cls, value):
        if value.tzinfo is None:
            raise ValueError("UTC-aware timestamps are required")
        return value


class CheckResult(WireModel):
    check_version_id: UUID
    evaluator_digest: Digest
    verdict: Verdict
    reason: str = Field(min_length=1, max_length=4000)
    reason_code: str | None = Field(default=None, max_length=200)
    observation_ids: list[UUID] = Field(default_factory=list)
    measured: dict[str, Any] = Field(default_factory=dict)


class EvidenceBatch(LeaseRequest):
    observations: list[Observation] = Field(default_factory=list, max_length=100)
    check_results: list[CheckResult] = Field(default_factory=list, max_length=100)


class ReconciliationIn(LeaseRequest):
    state: Literal["resolved", "unresolved"]
    rationale: str = Field(min_length=1, max_length=4000)
    observations: list[Observation] = Field(default_factory=list, max_length=100)
    late_events: list[TypedRunnerEvent] = Field(default_factory=list, max_length=100)
    cleanup_state: Literal["succeeded", "failed", "not_required"] | None = None


class Finalize(LeaseRequest):
    execution_state: Literal["completed", "error", "timed_out", "cancelled", "interrupted"]
    business_verdict: Verdict
    final_sequence: int = Field(ge=0)
    cleanup_state: Literal["succeeded", "failed", "not_required"]
    reconciliation_state: Literal["not_needed", "pending", "resolved", "unresolved"]
    reason: str = Field(default="", max_length=4000)


class ReviewIn(WireModel):
    decision: Literal[
        "confirm",
        "dispute",
        "request_evidence",
        "intentional_simplification",
        "accept",
        "revoke_waiver",
        "classify_defect",
        "reference_limitation",
    ]
    rationale: str = Field(min_length=1, max_length=4000)
    evidence_ids: list[UUID] = Field(default_factory=list, max_length=100)


class WaiverIn(WireModel):
    rationale: str = Field(min_length=1, max_length=4000)
    expires_at: datetime
    remediation_owner: Name | None = None

    @field_validator("expires_at")
    @classmethod
    def aware_expiry(cls, value):
        if value.tzinfo is None:
            raise ValueError("UTC-aware expiry is required")
        return value


class ExportIn(WireModel):
    format: Literal["pdf", "json", "junit", "bundle"]
    audience: Literal["project", "reviewers"] = "project"
    snapshot_artifact_id: UUID | None = None


class JobCreate(WireModel):
    kind: Literal[
        "preflight",
        "build",
        "source_import",
        "action_comparison",
        "fresh_agent_pair",
        "reduce_reproduction",
        "repair_retest",
        "simulator_inspect",
        "simulator_reset",
        "calibration",
        "trace_import",
        "incident_draft",
        "collision",
        "probe",
        "voice",
        "patch_proposal",
    ]
    version_ids: list[UUID] = Field(min_length=1, max_length=100)
    finding_id: UUID | None = None
    input_artifact_ids: list[UUID] = Field(default_factory=list, max_length=20)
    limits: RunLimits = Field(default_factory=RunLimits)


class AutomaticRunBudget(WireModel):
    max_runs_per_window: int = Field(default=10, ge=0, le=1000)
    window_seconds: int = Field(default=3600, ge=60, le=2592000)
    max_cases_per_run: int = Field(default=50, ge=1, le=5000)
    max_agent_invocations: int = Field(default=400, ge=1, le=100000)
    max_cost_minor: int | None = Field(default=None, ge=0)
    allowed_suites: list[str] = Field(default_factory=list, max_length=100)


class ScheduleIn(WireModel):
    name: Name
    interval_seconds: int = Field(ge=60, le=2592000)
    run: RunCreate
    enabled: bool = False
    automatic_budget: AutomaticRunBudget | None = None


class DependencyIn(WireModel):
    source_version_id: UUID
    target_version_id: UUID
    relationship: Literal["declared_dependency", "observed_dependency"]


class ChangeIn(WireModel):
    version_id: UUID
    reason: str = Field(min_length=1, max_length=4000)
    classification: Literal["retest_recommended", "scope_changed", "reference_unverified"]


class ArtifactCreate(WireModel):
    purpose: Literal["evidence", "source", "patch", "media"] = "evidence"
    authorization_version_id: UUID | None = None
    content_type: Literal[
        "application/json",
        "text/plain",
        "application/pdf",
        "application/xml",
        "application/zip",
        "audio/wav",
        "text/csv",
        "application/x-ndjson",
        "application/x-tar",
        "application/gzip",
        "audio/mpeg",
        "audio/ogg",
    ]
    digest: Digest
    size: int = Field(ge=1, le=10485760)
    audience: Literal["project", "reviewers"] = "project"


class RunnerArtifactCreate(LeaseRequest):
    artifact: ArtifactCreate


class ErrorCode(StrEnum):
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    BUDGET_REQUIRED = "BUDGET_REQUIRED"
    CALIBRATION_REQUIRED = "CALIBRATION_REQUIRED"
    CAPACITY_EXCEEDED = "CAPACITY_EXCEEDED"
    CSRF_FAILED = "CSRF_FAILED"
    EXPIRED_BACKUP = "EXPIRED_BACKUP"
    EXPORT_TOO_LARGE = "EXPORT_TOO_LARGE"
    FORBIDDEN = "FORBIDDEN"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    INCOMPATIBLE_VERSION = "INCOMPATIBLE_VERSION"
    INCOMPLETE_EVIDENCE = "INCOMPLETE_EVIDENCE"
    INTEGRITY_ERROR = "INTEGRITY_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    INVALID_INPUT = "INVALID_INPUT"
    INVALID_STATE = "INVALID_STATE"
    LEASE_EXPIRED = "LEASE_EXPIRED"
    MISSING_CAPABILITY = "MISSING_CAPABILITY"
    NOT_FOUND = "NOT_FOUND"
    POLICY_CHANGED = "POLICY_CHANGED"
    PREFLIGHT_REQUIRED = "PREFLIGHT_REQUIRED"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    PUBLICATION_CONFLICT = "PUBLICATION_CONFLICT"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    STALE_BASE = "STALE_BASE"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    UNAVAILABLE = "UNAVAILABLE"
    UNSUPPORTED_JOB = "UNSUPPORTED_JOB"
    VALIDATION_REQUIRED = "VALIDATION_REQUIRED"


class ErrorDetail(WireModel):
    code: ErrorCode
    message: str
    details: dict[str, str] = Field(default_factory=dict)
    correlation_id: UUID


class Page(WireModel):
    items: list[dict[str, Any]]
    next_cursor: str | None = None


class InvitationIn(WireModel):
    email: Annotated[str, StringConstraints(pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$", max_length=254)]
    roles: list[Role] = Field(min_length=1, max_length=4)
    project_id: UUID | None = None
    hidden_access: bool = False


class RepairIn(WireModel):
    finding_id: UUID
    source_version_id: UUID
    patch_artifact_id: UUID
    layer: Literal["agent", "simulator", "evaluator"]


class RepairValidationIn(WireModel):
    version_ids: list[UUID] = Field(default_factory=list, max_length=20)
    limits: RunLimits | None = None


class RelayConfiguration(WireModel):
    public_base_url: str = Field(pattern=r"^https://[^/?#]+$", max_length=500)
    event_id_path: str = "event_id"
    task_id_path: str = "task_id"
    status_path: str = "status"
    sequence_path: str | None = "sequence"
    result_path: str | None = "result"
    terminal_states: list[Name] = Field(default_factory=lambda: ["completed", "failed", "cancelled"], min_length=1)
    previous_secret_ref: SecretRef | None = None

    @field_validator("public_base_url")
    @classmethod
    def public_origin(cls, value):
        parsed = urlsplit(value)
        if not parsed.hostname or parsed.username or parsed.password or any(ord(c) < 33 for c in value):
            raise ValueError("Use an HTTPS public origin without credentials or whitespace")
        return value

    @field_validator("event_id_path", "task_id_path", "status_path", "sequence_path", "result_path")
    @classmethod
    def mapping_path(cls, value):
        import re
        from .checks import PATH_PATTERN

        if value is not None and not re.fullmatch(PATH_PATTERN, value):
            raise ValueError("Use a bounded typed field path")
        return value


class IntegrationIn(WireModel):
    provider: Literal["github", "ci", "media", "webhook"]
    secret_ref: SecretRef
    installation_id: int | None = Field(default=None, ge=1)
    repository: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")] | None = None
    publish_enabled: bool = False
    trigger_run: RunCreate | None = None
    approved_branch: Name | None = None
    relay: RelayConfiguration | None = None
    automatic_budget: AutomaticRunBudget | None = None


class RelayTaskRegistration(LeaseRequest):
    integration_id: UUID
    task_id: Name
    invocation_id: Name
    operation_id: Name


class RelayPoll(LeaseRequest):
    after: int = Field(default=0, ge=0)


class CollectorCredentialIn(WireModel):
    hours: int = Field(default=24, ge=1, le=720)


class ReportClassificationIn(WireModel):
    candidate_id: Name
    classification: Literal["confirmed_defect", "intentional_simplification", "reference_limitation", "unresolved"]
    rationale: str = Field(min_length=1, max_length=4000)
    evidence_ids: list[UUID] = Field(min_length=1, max_length=30)


class AdvancedReportRecord(WireModel):
    id: UUID
    run_id: UUID
    attempt_id: UUID
    report_type: Name
    report_key: Name
    content_hash: Digest
    payload: dict[str, Any]
    hidden: bool
    hosted_heldout_exposures: int | None = Field(default=None, ge=1)
    created_at: datetime


class PatchFile(WireModel):
    path: str = Field(min_length=1, max_length=500)
    before: str | None = Field(default=None, max_length=262144)
    after: str | None = Field(default=None, max_length=262144)


class PatchBundle(WireModel):
    base_commit: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{40}$")]
    files: list[PatchFile] = Field(min_length=1, max_length=20)

    def validation_diff(self) -> str:
        """Canonical data-only unified diff used by the runtime validation digest."""
        import difflib

        parts = []
        for file in self.files:
            parts.append(f"diff --git a/{file.path} b/{file.path}\n")
            lines = difflib.unified_diff(
                (file.before or "").splitlines(keepends=True), (file.after or "").splitlines(keepends=True),
                fromfile="a/" + file.path if file.before is not None else "/dev/null",
                tofile="b/" + file.path if file.after is not None else "/dev/null", lineterm="\n",
            )
            for line in lines:
                parts.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
        return "".join(parts)


class PublishIn(WireModel):
    proposal_hash: Digest
    integration_id: UUID


class BudgetReservation(LeaseRequest):
    operation_id: Name
    budget_kind: Literal["agent", "reference", "cleanup"]
    amount: int = Field(default=1, ge=1, le=1000)


class ReadModel(WireModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)


class IdentityRecord(ReadModel):
    id: UUID
    email: str


class WorkspaceRecord(ReadModel):
    id: UUID
    name: str
    roles: list[Role]


class MeResponse(WireModel):
    user: IdentityRecord
    workspaces: list[WorkspaceRecord]


class ProjectRecord(ReadModel):
    id: UUID
    workspace_id: UUID
    name: str
    workflow: str
    timezone: str
    policy: DataPolicy
    roles: list[Role]
    hidden_access: bool = False


class VersionRecord(ReadModel):
    id: UUID
    workspace_id: UUID
    project_id: UUID
    kind: str
    logical_id: str
    revision: int
    title: str
    data: VersionData
    provenance: dict[str, str]
    content_hash: Digest
    hidden: bool
    created_by: UUID
    created_at: datetime
    approved: bool = False


class RunCounts(WireModel):
    scheduled: int
    attempted: int
    completed: int
    passed: int = Field(alias="pass")
    failed: int = Field(alias="fail")
    unknown: int
    errors: int


class AttemptRecord(ReadModel):
    id: UUID
    case_version_id: UUID | None
    repetition: int
    arm: str
    required: bool
    execution_state: ExecutionState
    business_verdict: Verdict | None
    cleanup_state: str
    reconciliation_state: str
    hidden: bool


class RunRecord(ReadModel):
    id: UUID
    project_id: UUID
    kind: str
    manifest_hash: Digest
    status: str
    gate: Gate
    created_at: datetime
    cancel_requested_at: datetime | None
    original_run_id: UUID | None = None
    reproduction_outcome: Literal["pending", "reproduced", "not_reproduced", "inconclusive"] | None = None
    counts: RunCounts
    attempts: list[AttemptRecord]
    attempts_next_cursor: UUID | None = None
    hidden_attempt_count: int
    request: RunCreate | None


class FindingRecord(ReadModel):
    id: UUID
    run_id: UUID
    attempt_id: UUID
    check_version_id: UUID
    verdict: Verdict
    hidden: bool


class ArtifactRecord(ReadModel):
    id: UUID
    status: str
    digest: str
    size: int
    content_type: str
    export_format: str | None
    expires_at: datetime


class RepairRecord(ReadModel):
    id: UUID
    finding_id: UUID
    source_version_id: UUID
    patch_artifact_id: UUID
    layer: str
    base_commit: str
    proposal_hash: Digest
    validation_run_id: UUID | None
    validation_report_id: UUID | None = None
    validation_evidence_digest: Digest | None = None
    publication_record: dict[str, Any] | None = None
    available_validation_digest: Digest | None = None
    available_validation_report_id: UUID | None = None
    approved_by: UUID | None
    approved_at: datetime | None
    state: str
    pr_url: str | None
    created_at: datetime


class ScheduleRecord(ReadModel):
    id: UUID
    name: str
    interval_seconds: int
    configuration: RunCreate
    enabled: bool
    next_due_at: datetime
    automatic_budget: AutomaticRunBudget | None = None


class RunnerRecord(ReadModel):
    id: UUID
    name: str
    version: str
    capabilities: list[str]
    last_heartbeat: datetime | None
    revoked_at: datetime | None


class PinnedCase(WireModel):
    case: VersionRecord
    checks: list[VersionRecord]
    fixture: VersionRecord
    requirements: list[VersionRecord] = Field(default_factory=list)
    graders: list[VersionRecord] = Field(default_factory=list)


class AgentContext(WireModel):
    attempt_id: UUID
    run_id: UUID
    namespace: Name
    deadline: datetime
    case_id: UUID | None
    connection: ConnectionSpec | None
    script: list[ScriptStep]
    tool_route: ToolRoute | None = None


class RepairContext(WireModel):
    id: UUID
    proposal_hash: Digest
    patch_artifact_id: UUID
    base_commit: str
    layer: Literal["agent", "simulator", "evaluator"]
    finding_id: UUID
    validation_patch_digest: Digest | None = None


class ExecutionManifest(WireModel):
    schema_version: Literal["1.0"] = PROTOCOL_VERSION
    kind: Name | None = None
    environment: VersionRecord | None = None
    connection: VersionRecord | None = None
    suite: VersionRecord | None = None
    request: RunCreate | None = None
    versions: list[VersionRecord] = Field(default_factory=list)
    version_ids: list[UUID] = Field(default_factory=list)
    input_artifacts: list[ArtifactRecord] = Field(default_factory=list)
    finding_id: UUID | None = None
    repair: RepairContext | None = None
    policy: DataPolicy
    limits: RunLimits
    timezone: str


class RunnerClaimResponse(WireModel):
    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    attempt_id: UUID
    run_id: UUID
    kind: Name
    lease_token: str = Field(min_length=32, max_length=200)
    manifest_hash: Digest
    component_hashes: dict[str, Digest] = Field(default_factory=dict)
    run_manifest_hash: Digest
    lease_expires_at: datetime
    fencing_generation: int = Field(ge=1)
    deadline: datetime
    namespace: Name
    agent_context: AgentContext
    evaluator_context: PinnedCase | None
    execution_manifest: ExecutionManifest


def claim_component_hashes(execution_manifest, evaluator_context):
    execution = ExecutionManifest.model_validate(execution_manifest).model_dump(mode="json")
    components = {
        name: execution[name]
        for name in (
            "environment",
            "connection",
            "suite",
            "request",
            "policy",
            "limits",
            "timezone",
            "versions",
            "version_ids",
            "input_artifacts",
            "finding_id",
            "repair",
            "kind",
        )
    }
    if evaluator_context:
        case = PinnedCase.model_validate(evaluator_context).model_dump(mode="json")
        components.update(case)
    return {name: content_hash(value) for name, value in components.items()}


class ControlResponse(WireModel):
    attempt_id: UUID
    cancel_requested: bool
    lease_expires_at: datetime
    server_time: datetime
    deadline: datetime


class EventAcknowledgement(WireModel):
    accepted: list[UUID]
    duplicates: list[UUID]
    highest_contiguous_sequence: int = Field(ge=0)


class FinalizeResponse(WireModel):
    attempt_id: UUID
    gate: Gate


from .adapters import (  # noqa: E402,F401
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
    JsonlFrame,
    Message,
    ObservationQuery,
    Reconciliation,
    SessionHandle,
    SnapshotScope,
    StateSnapshot,
    StepIntent,
    ToolEvent,
    ToolRoute,
    Unavailable,
    Usage,
)

AgentContext.model_rebuild()
