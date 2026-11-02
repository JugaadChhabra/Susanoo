"""Case verdict and run gate aggregation [Spec §13 › Verdict rules].

This is the single business-verdict implementation (two-agent plan). The platform persists and
projects these results; it should call these functions rather than keep its own copy (CR-06).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from pydantic import Field

from .dsl import FAIL_PROVABLE_FROM_PARTIAL, PASS_PROVABLE_FROM_PARTIAL, Check, DslModel
from .engine import CheckOutcome, Verdict

ExecutionState = Literal[
    "queued",
    "preparing",
    "running",
    "observing",
    "evaluating",
    "completed",
    "error",
    "timed_out",
    "cancelled",
    "interrupted",
]
CleanupState = Literal["pending", "succeeded", "failed", "not_required"]
ReconciliationState = Literal["not_needed", "pending", "resolved", "unresolved"]
Gate = Literal["IN_PROGRESS", "BLOCKED", "NEEDS_REVIEW", "READY_FOR_ACCEPTANCE"]

ACTIVE_STATES = frozenset({"queued", "preparing", "running", "observing", "evaluating"})


class CaseResult(DslModel):
    verdict: Verdict
    failed_required: list[str] = Field(default_factory=list)
    unknown_required: list[str] = Field(default_factory=list)
    optional_failures: list[str] = Field(default_factory=list)


def case_verdict(checks: Iterable[Check], outcomes: Iterable[CheckOutcome]) -> CaseResult:
    """Any required FAIL -> FAIL; else any required UNKNOWN or missing -> UNKNOWN; else PASS.

    Optional failures stay visible but never become required.
    """
    by_id = {outcome.check_id: outcome for outcome in outcomes}
    failed, unknown, optional = [], [], []
    for check in checks:
        result = by_id.get(check.id)
        verdict = result.verdict if result else "UNKNOWN"
        if check.required:
            if verdict == "FAIL":
                failed.append(check.id)
            elif verdict != "PASS":
                unknown.append(check.id)
        elif verdict == "FAIL":
            optional.append(check.id)
    verdict: Verdict = "FAIL" if failed else "UNKNOWN" if unknown else "PASS"
    return CaseResult(verdict=verdict, failed_required=failed, unknown_required=unknown, optional_failures=optional)


class AttemptSummary(DslModel):
    required: bool
    execution_state: ExecutionState
    business_verdict: Verdict | None = None
    cleanup_state: CleanupState = "pending"
    reconciliation_state: ReconciliationState = "not_needed"


def gate_blockers(attempt: AttemptSummary) -> list[str]:
    blockers = []
    if attempt.cleanup_state == "failed":
        blockers.append("cleanup_failed")
    if attempt.reconciliation_state in {"pending", "unresolved"}:
        blockers.append("unresolved_side_effect")
    if attempt.required and attempt.business_verdict == "FAIL":
        blockers.append("required_failure")
    if attempt.required and attempt.execution_state != "completed":
        blockers.append(f"required_{attempt.execution_state}")
    return blockers


def run_gate(attempts: Iterable[AttemptSummary]) -> Gate:
    """IN_PROGRESS while active; then BLOCKED, NEEDS_REVIEW or READY_FOR_ACCEPTANCE.

    An empty run is BLOCKED: an approved suite cannot pass with nothing executed.
    """
    attempts = list(attempts)
    if any(item.execution_state in ACTIVE_STATES for item in attempts):
        return "IN_PROGRESS"
    if not attempts or any(gate_blockers(item) for item in attempts):
        return "BLOCKED"
    if any(item.required and item.business_verdict != "PASS" for item in attempts):
        return "NEEDS_REVIEW"
    return "READY_FOR_ACCEPTANCE"


def evidence_supports(
    operator: str,
    verdict: Verdict,
    *,
    requires_complete_observation: bool,
    check_window_seconds: int,
    availability: str,
    completeness: str,
    window_seconds: int,
    window_elapsed: bool,
) -> bool:
    """Whether observation metadata can carry a definitive verdict, for server-side validation.

    UNKNOWN is always supportable. A FAIL can rest on a partial view for operators whose violations
    stay violations as more records appear; a PASS can rest on a partial view only for presence checks.
    """
    if verdict == "UNKNOWN":
        return True
    if availability != "available" or operator in {"manual", "semantic"}:
        return False
    if not requires_complete_observation:
        return True
    if completeness == "complete_for_scope" and window_elapsed and window_seconds >= check_window_seconds:
        return True
    provable = FAIL_PROVABLE_FROM_PARTIAL if verdict == "FAIL" else PASS_PROVABLE_FROM_PARTIAL
    return operator in provable
