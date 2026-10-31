"""Deterministic fault schedules [Spec §14 › Stateful simulator requirements].

Faults fire by per-operation call number, never by randomness, and every injected fault is written
to the action log so an injected defect is never mistaken for a discovered one.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

FaultKind = Literal[
    "missing_write",
    "duplicate_write",
    "permission_bypass",
    "wrong_field",
    "delayed_visibility",
    "stale_read",
    "failure_before_commit",
    "failure_after_commit",
]


class FaultRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: FaultKind
    operation: str = Field(min_length=1, max_length=100)
    on_calls: list[int] | None = Field(default=None, description="1-based call numbers; None means every call")
    delay_seconds: int = Field(default=0, ge=0, le=86400)
    field: str | None = Field(default=None, max_length=100)
    value: Any = None

    @model_validator(mode="after")
    def complete(self) -> FaultRule:
        if self.kind == "wrong_field" and not self.field:
            raise ValueError("wrong_field needs the field to corrupt")
        if self.kind == "delayed_visibility" and self.delay_seconds <= 0:
            raise ValueError("delayed_visibility needs a positive delay")
        if self.on_calls is not None and any(n < 1 for n in self.on_calls):
            raise ValueError("call numbers start at 1")
        return self

    def fires(self, operation: str, call: int) -> bool:
        return self.operation == operation and (self.on_calls is None or call in self.on_calls)


class FaultProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(default="none", min_length=1, max_length=100)
    rules: list[FaultRule] = Field(default_factory=list, max_length=100)

    def active(self, operation: str, call: int) -> list[FaultRule]:
        return [rule for rule in self.rules if rule.fires(operation, call)]


NO_FAULTS = FaultProfile()
