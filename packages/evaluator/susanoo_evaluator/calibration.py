"""Challenge the grader [Spec §13 › Challenge the grader; U06].

A critical checker must catch owner-approved negative controls and accept valid alternatives before
suite approval. A negative control counts only if its violation was independently verified.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Literal

from pydantic import Field
from susanoo_contracts import content_hash

from .dsl import Check, DslModel, ObservedState
from .engine import EvalContext, evaluate


class Control(DslModel):
    id: str = Field(min_length=1, max_length=100)
    label: Literal["positive", "negative"]
    observations: list[ObservedState] = Field(min_length=1)
    aliases: dict[str, bool | int | str] = Field(default_factory=dict)
    started_at: datetime
    violation_verified: bool = False
    split: Literal["calibration", "heldout"] = "calibration"


class CalibrationResult(DslModel):
    check_id: str
    control_set_hash: str
    missed_defects: list[str] = Field(default_factory=list)
    false_alarms: list[str] = Field(default_factory=list)
    inconclusive: list[str] = Field(default_factory=list)
    invalid_negative_controls: list[str] = Field(default_factory=list)
    heldout_missed_defects: list[str] = Field(default_factory=list)
    heldout_false_alarms: list[str] = Field(default_factory=list)
    accepted: bool


def control_set_hash(check: Check, controls: Iterable[Control]) -> str:
    return content_hash(
        {
            "check": check.model_dump(mode="json"),
            "controls": sorted((c.model_dump(mode="json") for c in controls), key=lambda c: c["id"]),
        }
    )


def calibrate(
    check: Check, controls: Iterable[Control], *, max_missed_defects: int = 0, max_false_alarms: int = 0
) -> CalibrationResult:
    controls = list(controls)
    missed, alarms, inconclusive, invalid, held_missed, held_alarms = [], [], [], [], [], []
    for control in controls:
        if control.label == "negative" and not control.violation_verified:
            invalid.append(control.id)
            continue
        observations = {item.name: item for item in control.observations}
        result = evaluate(check, observations, EvalContext(aliases=control.aliases, started_at=control.started_at))
        if result.verdict == "UNKNOWN":
            inconclusive.append(control.id)
        elif control.label == "negative" and result.verdict == "PASS":
            (held_missed if control.split == "heldout" else missed).append(control.id)
        elif control.label == "positive" and result.verdict == "FAIL":
            (held_alarms if control.split == "heldout" else alarms).append(control.id)
    has_both = {c.label for c in controls if c.id not in invalid} >= {"positive", "negative"}
    accepted = (
        has_both
        and not inconclusive
        and len(missed) + len(held_missed) <= max_missed_defects
        and len(alarms) + len(held_alarms) <= max_false_alarms
    )
    return CalibrationResult(
        check_id=check.id,
        control_set_hash=control_set_hash(check, controls),
        missed_defects=missed,
        false_alarms=alarms,
        inconclusive=inconclusive,
        invalid_negative_controls=invalid,
        heldout_missed_defects=held_missed,
        heldout_false_alarms=held_alarms,
        accepted=accepted,
    )
