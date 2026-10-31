"""Evaluate one typed check against independent observations [Spec §13].

The evaluator is pure: it reads ``ObservedState`` values and returns a ``CheckOutcome``. It never
calls the system under test, never reads agent replies and never treats ``success=true`` from an
agent as evidence. Missing, unavailable, redacted or out-of-window evidence yields ``UNKNOWN``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field

from .dsl import (
    FAIL_PROVABLE_FROM_PARTIAL,
    PASS_PROVABLE_FROM_PARTIAL,
    Alias,
    BusinessDate,
    Check,
    CountCheck,
    DeadlineCheck,
    DslModel,
    EqualsCheck,
    ExistsCheck,
    ForbiddenChangeCheck,
    ManualCheck,
    Money,
    ObservedState,
    Operand,
    RangeCheck,
    RelationshipCheck,
    SemanticCheck,
    TransitionCheck,
    UnchangedCheck,
)

Verdict = Literal["PASS", "FAIL", "UNKNOWN"]


class ReasonCode(StrEnum):
    SATISFIED = "satisfied"
    VIOLATED = "violated"
    OBSERVATION_MISSING = "observation_missing"
    OBSERVATION_UNAVAILABLE = "observation_unavailable"
    OBSERVATION_INCOMPLETE = "observation_incomplete"
    WINDOW_NOT_ELAPSED = "window_not_elapsed"
    REDACTED_VALUE = "redacted_value"
    INVALID_VALUE = "invalid_value"
    ALIAS_UNRESOLVED = "alias_unresolved"
    BASELINE_MISSING = "baseline_missing"
    MANUAL_PENDING = "manual_pending"
    SEMANTIC_NOT_CONFIGURED = "semantic_not_configured"


class EvalContext(DslModel):
    aliases: dict[str, bool | int | str] = Field(default_factory=dict)
    started_at: datetime

    def resolve(self, operand: Operand) -> Operand:
        if isinstance(operand, Alias):
            if operand.alias not in self.aliases:
                raise _Unknown(ReasonCode.ALIAS_UNRESOLVED, f"Fixture alias {operand.alias} is not defined")
            return self.aliases[operand.alias]
        return operand


class CheckOutcome(DslModel):
    check_id: str
    requirement_id: str
    required: bool
    verdict: Verdict
    reason_code: ReasonCode
    reason: str
    measured: dict[str, Any] = Field(default_factory=dict)
    observation_names: list[str] = Field(default_factory=list)


class _Unknown(Exception):
    def __init__(self, code: ReasonCode, reason: str):
        super().__init__(reason)
        self.code, self.reason = code, reason


class _Invalid(Exception):
    """A value cannot be compared without guessing (float money, naive time, mismatched currency)."""


_MISSING = object()
_TOKEN = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)|\[([0-9]+)\]")


def get_path(record: Any, path: str) -> Any:
    value = record
    for name, index in _TOKEN.findall(path):
        if name:
            if not isinstance(value, Mapping) or name not in value:
                return _MISSING
            value = value[name]
        else:
            position = int(index)
            if not isinstance(value, list) or position >= len(value):
                return _MISSING
            value = value[position]
    return value


def _money(value: Any) -> Money:
    if not isinstance(value, Mapping) or set(value) != {"amount_minor", "currency"}:
        raise _Invalid("money must be {amount_minor, currency}")
    amount = value["amount_minor"]
    if type(amount) is not int:
        raise _Invalid("money amount must be integer minor units")
    return Money(amount_minor=amount, currency=value["currency"])


def _business_date(value: Any) -> BusinessDate:
    if not isinstance(value, Mapping) or set(value) != {"date", "timezone"}:
        raise _Invalid("business date must be {date, timezone}")
    try:
        return BusinessDate(date=date.fromisoformat(value["date"]), timezone=value["timezone"])
    except (TypeError, ValueError) as error:
        raise _Invalid(f"invalid business date: {error}") from None


def equal(value: Any, operand: Any) -> bool:
    """Type-strict equality. Missing values are unequal; uncomparable values raise ``_Invalid``."""
    if value is _MISSING:
        return False
    if isinstance(operand, Money):
        return _money(value) == operand
    if isinstance(operand, BusinessDate):
        return _business_date(value) == operand
    if isinstance(operand, bool):
        return type(value) is bool and value is operand
    if isinstance(operand, int):
        if isinstance(value, float):
            raise _Invalid("float compared with an integer operand")
        return type(value) is int and value == operand
    return type(value) is str and value == operand


def _ordered(value: Any, bound: Any) -> tuple[Any, Any]:
    """Return comparable keys for ``value`` and ``bound`` or raise ``_Invalid``."""
    if isinstance(bound, Money):
        money = _money(value)
        if money.currency != bound.currency:
            raise _Invalid("currency differs from the bound")
        return money.amount_minor, bound.amount_minor
    if isinstance(bound, BusinessDate):
        day = _business_date(value)
        if day.timezone != bound.timezone:
            raise _Invalid("timezone differs from the bound")
        return day.date, bound.date
    if type(value) is not int:
        raise _Invalid("range value must be an integer")
    return value, bound


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise _Invalid("timestamp must be an ISO 8601 string")
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _Invalid("timestamp is not ISO 8601") from None
    if moment.tzinfo is None:
        raise _Invalid("timestamp must carry a timezone")
    return moment.astimezone(UTC)


def _filtered(records: list[dict[str, Any]], filters: dict[str, Operand], ctx: EvalContext) -> list[dict[str, Any]]:
    resolved = {path: ctx.resolve(operand) for path, operand in filters.items()}
    return [record for record in records if all(equal(get_path(record, p), v) for p, v in resolved.items())]


def _check_redaction(observation: ObservedState, paths: list[str]) -> None:
    redacted = set(observation.redacted_paths)
    for path in paths:
        root = _TOKEN.findall(path)[0][0]
        if path in redacted or root in redacted:
            raise _Unknown(ReasonCode.REDACTED_VALUE, f"{path} was redacted from {observation.name}")


def _keyed(records: list[dict[str, Any]], key: str) -> dict[Any, dict[str, Any]]:
    keyed: dict[Any, dict[str, Any]] = {}
    for record in records:
        identity = get_path(record, key)
        if identity is _MISSING or not isinstance(identity, (str, int)) or identity in keyed:
            raise _Unknown(ReasonCode.INVALID_VALUE, f"records need a unique {key} to compare state")
        keyed[identity] = record
    return keyed


# Each operator returns (holds, proven_from_partial, explanation, measured).
Finding = tuple[bool, bool, str, dict[str, Any]]


def _exists(check: ExistsCheck, rows: list[dict[str, Any]]) -> Finding:
    return bool(rows), bool(rows), f"{len(rows)} matching record(s)", {"count": len(rows)}


def _count(check: CountCheck, rows: list[dict[str, Any]]) -> Finding:
    low, high = check.bounds
    count = len(rows)
    if high is not None and count > high:
        return False, True, f"{count} matching records, at most {high} allowed", {"count": count}
    if count < low:
        return False, False, f"{count} matching records, at least {low} required", {"count": count}
    # Seeing enough records proves a lower bound only; an upper bound still needs the full scope.
    return True, high is None, f"{count} matching record(s)", {"count": count}


def _equals(check: EqualsCheck, rows: list[dict[str, Any]], ctx: EvalContext) -> Finding:
    expected = ctx.resolve(check.expected)
    if not rows:
        return False, False, "no record matched the filter", {"count": 0}
    for row in rows:
        value = get_path(row, check.path)
        if not equal(value, expected):
            shown = None if value is _MISSING else value
            return False, True, f"{check.path} is {shown!r}, expected {_show(expected)}", {"value": shown}
    return True, False, f"{check.path} equals {_show(expected)} on {len(rows)} record(s)", {"count": len(rows)}


def _range(check: RangeCheck, rows: list[dict[str, Any]]) -> Finding:
    if not rows:
        return False, False, "no record matched the filter", {"count": 0}
    for row in rows:
        value = get_path(row, check.path)
        if value is _MISSING:
            return False, True, f"{check.path} is missing", {}
        for bound, outside in ((check.min, lambda v, b: v < b), (check.max, lambda v, b: v > b)):
            if bound is None:
                continue
            key, limit = _ordered(value, bound)
            if outside(key, limit):
                return False, True, f"{check.path} {value!r} is outside the allowed range", {"value": value}
    return True, False, f"{check.path} within range on {len(rows)} record(s)", {"count": len(rows)}


def _relationship(
    check: RelationshipCheck, rows: list[dict[str, Any]], observations: Mapping[str, ObservedState], ctx: EvalContext
) -> tuple[Finding, ObservedState]:
    target = _usable(observations, check.target_observation)
    _check_redaction(target, [check.target_path, *check.target_filter])
    targets = _filtered(target.records, check.target_filter, ctx)
    known = [get_path(item, check.target_path) for item in targets]
    for row in rows:
        reference = get_path(row, check.path)
        if not any(equal(reference, candidate) for candidate in known if candidate is not _MISSING):
            # A dangling reference is proven only if the target view is complete.
            proven = _complete(target, check)
            return (False, proven, f"{check.path}={reference!r} has no {check.target_observation} match", {}), target
    return (True, False, f"{len(rows)} record(s) reference {check.target_observation}", {"count": len(rows)}), target


def _baseline(observation: ObservedState) -> list[dict[str, Any]]:
    if observation.baseline is None:
        raise _Unknown(ReasonCode.BASELINE_MISSING, f"{observation.name} has no before-state to compare")
    return observation.baseline


def _transition(check: TransitionCheck, rows: list[dict[str, Any]], before: list[dict[str, Any]]) -> Finding:
    previous = _keyed(before, check.key)
    allowed = set(check.allowed)
    for identity, row in _keyed(rows, check.key).items():
        if identity not in previous:
            continue
        old, new = get_path(previous[identity], check.path), get_path(row, check.path)
        if old == new and check.allow_unchanged:
            continue
        if (old, new) not in allowed:
            return False, True, f"{identity}: {check.path} moved {old!r} -> {new!r}", {"from": old, "to": new}
    return True, False, "all observed transitions are allowed", {"count": len(rows)}


def _changes(check: ForbiddenChangeCheck | UnchangedCheck, rows, before) -> Finding:
    after_keyed, before_keyed = _keyed(rows, check.key), _keyed(before, check.key)
    created = sorted(set(after_keyed) - set(before_keyed), key=str)
    deleted = sorted(set(before_keyed) - set(after_keyed), key=str)
    forbid_create = getattr(check, "forbid_create", False)
    forbid_delete = getattr(check, "forbid_delete", False)
    if forbid_create and created:
        return False, True, f"record(s) created: {created}", {"created": len(created)}
    # A record missing from a partial view may simply be unseen, so deletion needs the full scope.
    if forbid_delete and deleted:
        return False, False, f"record(s) deleted: {deleted}", {"deleted": len(deleted)}
    for identity in sorted(set(after_keyed) & set(before_keyed), key=str):
        old, new = before_keyed[identity], after_keyed[identity]
        paths = check.paths or sorted(set(old) | set(new))
        for path in paths:
            if get_path(old, path) != get_path(new, path):
                return False, True, f"{identity}: {path} changed", {"changed": path}
    return True, False, "no forbidden change observed", {"count": len(after_keyed)}


def _deadline(check: DeadlineCheck, rows: list[dict[str, Any]], ctx: EvalContext) -> Finding:
    if not rows:
        return False, False, "no matching record", {"count": 0}
    limit = ctx.started_at.astimezone(UTC) + timedelta(seconds=check.within_seconds)
    on_time = [row for row in rows if _timestamp(get_path(row, check.path)) <= limit]
    if on_time:
        return True, True, f"{len(on_time)} record(s) completed within {check.within_seconds}s", {"count": len(on_time)}
    return False, False, f"no record completed within {check.within_seconds}s", {"count": 0}


def _usable(observations: Mapping[str, ObservedState], name: str) -> ObservedState:
    observation = observations.get(name)
    if observation is None:
        raise _Unknown(ReasonCode.OBSERVATION_MISSING, f"No independent observation named {name}")
    if observation.availability != "available":
        detail = f": {observation.reason}" if observation.reason else ""
        raise _Unknown(ReasonCode.OBSERVATION_UNAVAILABLE, f"Observer for {name} was unavailable{detail}")
    return observation


def _complete(observation: ObservedState, check: Check) -> bool:
    if not check.requires_complete_observation:
        return True
    return (
        observation.completeness == "complete_for_scope"
        and observation.window_elapsed
        and observation.window_seconds >= check.window_seconds
    )


def _incomplete_reason(observation: ObservedState, check: Check) -> tuple[ReasonCode, str]:
    if not observation.window_elapsed or observation.window_seconds < check.window_seconds:
        return ReasonCode.WINDOW_NOT_ELAPSED, f"Consistency window for {observation.name} has not elapsed"
    return ReasonCode.OBSERVATION_INCOMPLETE, f"{observation.name} is {observation.completeness}, not complete"


def _show(value: Any) -> str:
    if isinstance(value, Money):
        return f"{value.amount_minor} {value.currency} (minor units)"
    if isinstance(value, BusinessDate):
        return f"{value.date.isoformat()} {value.timezone}"
    return repr(value)


def _paths(check: Check) -> list[str]:
    paths = list(check.filter)
    for name in ("path", "key"):
        if hasattr(check, name):
            paths.append(getattr(check, name))
    paths.extend(getattr(check, "paths", None) or [])
    return paths


def evaluate(check: Check, observations: Mapping[str, ObservedState], ctx: EvalContext) -> CheckOutcome:
    def outcome(verdict: Verdict, code: ReasonCode, reason: str, measured=None, names=None) -> CheckOutcome:
        return CheckOutcome(
            check_id=check.id,
            requirement_id=check.requirement_id,
            required=check.required,
            verdict=verdict,
            reason_code=code,
            reason=reason,
            measured=measured or {},
            observation_names=names if names is not None else [check.observation],
        )

    if isinstance(check, ManualCheck):
        return outcome("UNKNOWN", ReasonCode.MANUAL_PENDING, "Awaiting a reviewer decision", names=[])
    if isinstance(check, SemanticCheck):
        return outcome("UNKNOWN", ReasonCode.SEMANTIC_NOT_CONFIGURED, "No calibrated semantic grader", names=[])
    try:
        observation = _usable(observations, check.observation)
        _check_redaction(observation, _paths(check))
        rows = _filtered(observation.records, check.filter, ctx)
        names = [check.observation]
        complete = _complete(observation, check)
        if isinstance(check, ExistsCheck):
            finding = _exists(check, rows)
        elif isinstance(check, CountCheck):
            finding = _count(check, rows)
        elif isinstance(check, EqualsCheck):
            finding = _equals(check, rows, ctx)
        elif isinstance(check, RangeCheck):
            finding = _range(check, rows)
        elif isinstance(check, RelationshipCheck):
            finding, target = _relationship(check, rows, observations, ctx)
            names.append(target.name)
            complete = complete and _complete(target, check)
        elif isinstance(check, TransitionCheck):
            finding = _transition(check, rows, _filtered(_baseline(observation), check.filter, ctx))
        elif isinstance(check, (ForbiddenChangeCheck, UnchangedCheck)):
            finding = _changes(check, rows, _filtered(_baseline(observation), check.filter, ctx))
        elif isinstance(check, DeadlineCheck):
            finding = _deadline(check, rows, ctx)
        else:  # pragma: no cover - the discriminated union is exhaustive
            raise TypeError(f"Unsupported operator {check.operator}")
    except _Unknown as unknown:
        return outcome("UNKNOWN", unknown.code, unknown.reason)
    except _Invalid as invalid:
        return outcome("UNKNOWN", ReasonCode.INVALID_VALUE, f"Cannot compare without guessing: {invalid}")

    holds, proven_partial, explanation, measured = finding
    if holds and (complete or (proven_partial and check.operator in PASS_PROVABLE_FROM_PARTIAL)):
        return outcome("PASS", ReasonCode.SATISFIED, explanation, measured, names)
    if not holds and (complete or (proven_partial and check.operator in FAIL_PROVABLE_FROM_PARTIAL)):
        return outcome("FAIL", ReasonCode.VIOLATED, explanation, measured, names)
    code, reason = _incomplete_reason(observation, check)
    return outcome("UNKNOWN", code, f"{reason}; observed so far: {explanation}", measured, names)
