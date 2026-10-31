"""Susanoo business evaluator: typed checks in, PASS/FAIL/UNKNOWN out. Pure; never mutates the system under test."""

from functools import cache

from pydantic import TypeAdapter
from susanoo_contracts import content_hash

from .calibration import CalibrationResult, Control, calibrate, control_set_hash
from .dsl import DSL_VERSION, Alias, BusinessDate, Check, Money, ObservedState
from .engine import CheckOutcome, EvalContext, ReasonCode, evaluate
from .verdict import AttemptSummary, CaseResult, case_verdict, evidence_supports, gate_blockers, run_gate

__version__ = "0.1.0"
CHECK_ADAPTER: TypeAdapter[Check] = TypeAdapter(Check)


@cache
def evaluator_digest() -> str:
    """Pins the evaluator implementation and DSL schema; recorded on every check result."""
    return content_hash({"package": "susanoo-evaluator", "version": __version__, "dsl": CHECK_ADAPTER.json_schema()})


def parse_check(data: dict) -> Check:
    return CHECK_ADAPTER.validate_python(data)


__all__ = [
    "DSL_VERSION",
    "Alias",
    "AttemptSummary",
    "BusinessDate",
    "CalibrationResult",
    "CaseResult",
    "Check",
    "CheckOutcome",
    "Control",
    "EvalContext",
    "Money",
    "ObservedState",
    "ReasonCode",
    "calibrate",
    "case_verdict",
    "control_set_hash",
    "evaluate",
    "evaluator_digest",
    "evidence_supports",
    "gate_blockers",
    "parse_check",
    "run_gate",
]
