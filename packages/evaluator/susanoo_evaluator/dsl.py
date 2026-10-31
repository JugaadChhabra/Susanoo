"""Typed check DSL [Spec §13]. No eval, no user SQL, no unrestricted JSONPath, no float money.

A check names one declared observation, an optional record filter and one operator. Operands are
typed: integers, strings, booleans, ``Money`` (integer minor units plus currency), ``BusinessDate``
(calendar date plus explicit IANA timezone) or ``Alias`` (a fixture alias resolved at evaluation
time, never interpolated).
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, StringConstraints
from pydantic import field_validator, model_validator

DSL_VERSION = "1.0"
PATH_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*|\[[0-9]+\])*$"

Path = Annotated[str, StringConstraints(pattern=PATH_PATTERN, max_length=200)]
FieldName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", max_length=64)]
CheckId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")]
Severity = Literal["critical", "high", "medium", "low"]


class DslModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


def known_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("Use a known IANA timezone") from None
    return value


class Money(DslModel):
    amount_minor: StrictInt
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]


class BusinessDate(DslModel):
    date: date
    timezone: str

    @field_validator("timezone")
    @classmethod
    def _known(cls, value: str) -> str:
        return known_timezone(value)


class Alias(DslModel):
    alias: Annotated[str, StringConstraints(pattern=r"^fixture\.[A-Za-z_][A-Za-z0-9_]*$")]


# Order matters for pydantic's smart union only between overlapping types; the strict scalar types
# and the extra="forbid" models never overlap, so a float or a stray key is always rejected.
Operand = Money | BusinessDate | Alias | StrictBool | StrictInt | StrictStr


class CheckBase(DslModel):
    schema_version: Literal["1.0"] = DSL_VERSION
    id: CheckId
    requirement_id: CheckId
    required: bool = True
    severity: Severity = "high"
    observation: FieldName
    filter: dict[Path, Operand] = Field(default_factory=dict, max_length=20)
    requires_complete_observation: bool = True
    window_seconds: int = Field(default=10, ge=0, le=3600)


class ExistsCheck(CheckBase):
    operator: Literal["exists"]


class CountCheck(CheckBase):
    operator: Literal["count"]
    expected: StrictInt | None = Field(default=None, ge=0)
    min: StrictInt | None = Field(default=None, ge=0)
    max: StrictInt | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def one_bound_form(self) -> CountCheck:
        if self.expected is not None and (self.min is not None or self.max is not None):
            raise ValueError("Use either expected or min/max, not both")
        if self.expected is None and self.min is None and self.max is None:
            raise ValueError("A count check needs expected, min or max")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("min cannot exceed max")
        return self

    @property
    def bounds(self) -> tuple[int, int | None]:
        if self.expected is not None:
            return self.expected, self.expected
        return self.min or 0, self.max


class EqualsCheck(CheckBase):
    """Every record matching the filter must have ``path`` equal to ``expected``; at least one must match."""

    operator: Literal["equals"]
    path: Path
    expected: Operand


class RangeCheck(CheckBase):
    operator: Literal["range"]
    path: Path
    min: Operand | None = None
    max: Operand | None = None

    @model_validator(mode="after")
    def bounded(self) -> RangeCheck:
        if self.min is None and self.max is None:
            raise ValueError("A range check needs min or max")
        for bound in (self.min, self.max):
            if isinstance(bound, (bool, str, Alias)):
                raise ValueError("Range bounds must be integers, Money or BusinessDate")
        if self.min is not None and self.max is not None and type(self.min) is not type(self.max):
            raise ValueError("Range bounds must share one type")
        return self


class RelationshipCheck(CheckBase):
    """Each matching record's ``path`` must reference a record in ``target_observation`` by ``target_path``."""

    operator: Literal["relationship"]
    path: Path
    target_observation: FieldName
    target_path: Path = "id"
    target_filter: dict[Path, Operand] = Field(default_factory=dict, max_length=20)


class TransitionCheck(CheckBase):
    operator: Literal["transition"]
    path: Path
    key: Path = "id"
    allowed: list[tuple[StrictStr, StrictStr]] = Field(min_length=1, max_length=100)
    allow_unchanged: bool = True


class ForbiddenChangeCheck(CheckBase):
    """No record in scope may be created, deleted or (on ``paths``, or any field) modified."""

    operator: Literal["forbidden_change"]
    key: Path = "id"
    paths: list[Path] | None = Field(default=None, max_length=50)
    forbid_create: bool = True
    forbid_delete: bool = True


class UnchangedCheck(CheckBase):
    operator: Literal["unchanged"]
    key: Path = "id"
    paths: list[Path] = Field(min_length=1, max_length=50)


class DeadlineCheck(CheckBase):
    """A matching record exists whose UTC timestamp at ``path`` is within ``within_seconds`` of case start."""

    operator: Literal["deadline"]
    path: Path
    within_seconds: int = Field(ge=0, le=86400)


class ManualCheck(CheckBase):
    operator: Literal["manual"]
    instructions: str = Field(min_length=1, max_length=4000)


class SemanticCheck(CheckBase):
    operator: Literal["semantic"]
    rubric_id: CheckId
    grader_version_id: str | None = None


Check = Annotated[
    ExistsCheck
    | CountCheck
    | EqualsCheck
    | RangeCheck
    | RelationshipCheck
    | TransitionCheck
    | ForbiddenChangeCheck
    | UnchangedCheck
    | DeadlineCheck
    | ManualCheck
    | SemanticCheck,
    Field(discriminator="operator"),
]

# Operators whose violation can be proven from a partial view (a seen violation stays a violation).
FAIL_PROVABLE_FROM_PARTIAL = frozenset(
    {"count", "equals", "range", "relationship", "transition", "forbidden_change", "unchanged", "deadline"}
)
# Operators whose success can be proven from a partial view (a seen record stays seen).
PASS_PROVABLE_FROM_PARTIAL = frozenset({"exists", "deadline"})


class ObservedState(DslModel):
    """Raw local observation handed to the evaluator by an independent observer, never by the agent."""

    name: FieldName
    source: str = Field(min_length=1, max_length=200)
    availability: Literal["available", "unavailable"]
    completeness: Literal["complete_for_scope", "partial", "unknown"]
    window_seconds: int = Field(ge=0, le=3600)
    window_elapsed: bool
    records: list[dict[str, Any]] = Field(default_factory=list)
    baseline: list[dict[str, Any]] | None = None
    redacted_paths: list[Path] = Field(default_factory=list)
    reason: str | None = Field(default=None, max_length=4000)
