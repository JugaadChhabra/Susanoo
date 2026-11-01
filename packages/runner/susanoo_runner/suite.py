"""Local suite and profile models.

``LocalSuite`` is the runner's input in local mode (no control plane). In platform mode the same
pieces arrive pinned in a claim. ``LocalProfile`` is the maximum authority approved on this
machine: a suite or a remote claim can narrow it, never widen it [Spec §10].
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from susanoo_contracts import FixtureSpec, content_hash
from susanoo_evaluator import Check, parse_check
from susanoo_sdk import ObservationQuery


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScriptTurn(_Model):
    message: str = Field(min_length=1, max_length=65536)


class Requirement(_Model):
    id: str
    severity: Literal["critical", "high", "medium", "low"]
    text: str


class Limits(_Model):
    max_turns: int = Field(default=8, ge=1, le=100)
    invocation_timeout_seconds: float = Field(default=60, gt=0, le=300)
    case_timeout_seconds: float = Field(default=300, gt=0, le=3600)
    max_concurrency: int = Field(default=1, ge=1, le=3)
    max_agent_invocations: int = Field(default=160, ge=1)
    cleanup_reserve: int = Field(default=3, ge=1)


class LocalCase(_Model):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
    title: str
    requirement_ids: list[str]
    required: bool = True
    visibility: Literal["public", "hidden"] = "public"
    modality: Literal["text"] = "text"
    script: list[ScriptTurn] = Field(min_length=1, max_length=100)
    fixture: FixtureSpec
    observations: list[ObservationQuery] = Field(min_length=1)
    checks: list[dict] = Field(min_length=1)
    injected_faults: list[str] = Field(default_factory=list)

    @field_validator("checks")
    @classmethod
    def valid_checks(cls, value: list[dict]) -> list[dict]:
        for item in value:
            parse_check(item)
        return value

    def parsed_checks(self) -> list[Check]:
        return [parse_check(item) for item in self.checks]


class LocalSuite(_Model):
    schema_version: Literal["1.0"] = "1.0"
    id: str
    synthetic: bool = True
    approval: dict = Field(default_factory=dict)
    timezone: str
    requirements: list[Requirement] = Field(default_factory=list)
    limits: Limits = Field(default_factory=Limits)
    cases: list[LocalCase] = Field(min_length=1, max_length=500)

    @field_validator("cases")
    @classmethod
    def required_present(cls, value: list[LocalCase]) -> list[LocalCase]:
        if not any(case.required for case in value):
            raise ValueError("An approved suite needs at least one required case")
        if len({case.id for case in value}) != len(value):
            raise ValueError("Case IDs must be unique")
        return value

    @property
    def content_hash(self) -> str:
        return content_hash(self.model_dump(mode="json"))


class LocalProfile(_Model):
    """Approved on this machine; the ceiling for every run it executes."""

    allowed_operations: list[str] = Field(default_factory=list)
    allowed_fixture_adapters: list[str] = Field(default_factory=lambda: ["susanoo_simulator"])
    max_turns: int = Field(default=8, ge=1)
    invocation_timeout_seconds: float = Field(default=60, gt=0)
    case_timeout_seconds: float = Field(default=300, gt=0)
    max_concurrency: int = Field(default=1, ge=1, le=3)
    max_agent_invocations: int = Field(default=160, ge=1)
    data_mode: Literal["synthetic", "local_only", "redacted_hosted"] = "synthetic"

    def narrow(self, requested: Limits) -> Limits:
        return Limits(
            max_turns=min(self.max_turns, requested.max_turns),
            invocation_timeout_seconds=min(self.invocation_timeout_seconds, requested.invocation_timeout_seconds),
            case_timeout_seconds=min(self.case_timeout_seconds, requested.case_timeout_seconds),
            max_concurrency=min(self.max_concurrency, requested.max_concurrency),
            max_agent_invocations=min(self.max_agent_invocations, requested.max_agent_invocations),
            cleanup_reserve=requested.cleanup_reserve,
        )

    def admit(self, case: LocalCase) -> list[str]:
        """Return reasons this profile refuses the case (empty list means admitted)."""
        problems = []
        if case.fixture.adapter not in self.allowed_fixture_adapters:
            problems.append(f"fixture adapter {case.fixture.adapter} is not approved on this runner")
        actors = case.fixture.recipe.get("seed", {}).get("actors", {})
        wanted = set(actors.get("agent", {}).get("operations", []))
        if self.allowed_operations and not wanted <= set(self.allowed_operations):
            problems.append(f"operations not approved locally: {sorted(wanted - set(self.allowed_operations))}")
        return problems
