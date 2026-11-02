"""Regression comparison between two local runs [Spec §16 › Regression comparison; T15].

Cases are compared only when the logical case, its inputs, every check version, the fixture semantics
and the evaluator all match. Anything else is an explicit ``not_comparable`` row with reasons; changed
criteria are never reported as an improvement. All repetitions are shown, not an aggregate.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .ci import EXIT_OPERATIONAL, EXIT_READY, EXIT_REQUIRED_FAILURE, CaseManifest, RunManifest
from .executor import AttemptResult
from .local import RunReport

Category = Literal["pass_to_fail", "fail_to_pass", "unchanged", "unresolved", "not_comparable"]
Audience = Literal["operator", "engineer"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CheckTransition(_Model):
    check_id: str
    baseline: str
    candidate: str
    transition: str


class RunRef(_Model):
    handle: str
    run_id: str | None
    suite_id: str
    suite_hash: str
    agent: str
    evaluator_digest: str


class CompareRow(_Model):
    case_id: str
    visibility: str
    required: bool
    compatibility: Literal["comparable", "not_comparable"]
    reasons: list[str] = Field(default_factory=list)
    baseline_verdicts: list[str] = Field(default_factory=list)
    candidate_verdicts: list[str] = Field(default_factory=list)
    baseline_execution: list[str] = Field(default_factory=list)
    candidate_execution: list[str] = Field(default_factory=list)
    transition: str | None = Field(default=None, description="exact, e.g. PASS->FAIL; None when not comparable")
    category: Category
    check_transitions: list[CheckTransition] = Field(default_factory=list)


class Comparison(_Model):
    schema_version: Literal["1.0"] = "1.0"
    audience: Audience
    baseline: RunRef
    candidate: RunRef
    rows: list[CompareRow]
    summary: dict[str, int]


def _ref(report: RunReport, manifest: RunManifest) -> RunRef:
    return RunRef(
        handle=manifest.handle,
        run_id=report.run_id,
        suite_id=report.suite_id,
        suite_hash=report.suite_hash,
        agent=report.agent,
        evaluator_digest=report.evaluator_digest,
    )


def _incompatibilities(
    base: CaseManifest | None,
    cand: CaseManifest | None,
    base_report: RunReport,
    cand_report: RunReport,
    reveal: bool,
) -> list[str]:
    if base is None:
        return ["case_absent_in_baseline"]
    if cand is None:
        return ["case_absent_in_candidate"]
    reasons = []
    if base_report.suite_id != cand_report.suite_id:
        reasons.append("suite_id_differs")
    if base.inputs_digest != cand.inputs_digest:
        reasons.append("case_inputs_changed")
    if base.check_digests != cand.check_digests:
        if not reveal:
            reasons.append("check_versions_changed")
        else:
            for check_id in sorted(set(base.check_digests) | set(cand.check_digests)):
                old, new = base.check_digests.get(check_id), cand.check_digests.get(check_id)
                if old is None:
                    reasons.append(f"check_added:{check_id}")
                elif new is None:
                    reasons.append(f"check_removed:{check_id}")
                elif old != new:
                    reasons.append(f"check_changed:{check_id}")
    if base.fixture_compatibility_key != cand.fixture_compatibility_key:
        reasons.append("fixture_semantics_changed")
    elif base.fixture_digest != cand.fixture_digest:
        reasons.append("fixture_changed")
    if base.fixture_adapter != cand.fixture_adapter:
        reasons.append("fixture_adapter_changed")
    if base_report.evaluator_digest != cand_report.evaluator_digest:
        reasons.append("evaluator_changed")
    return reasons


def _category(base: list[AttemptResult], cand: list[AttemptResult]) -> tuple[str, Category]:
    left = sorted({a.business_verdict for a in base})
    right = sorted({a.business_verdict for a in cand})
    transition = f"{','.join(left)}->{','.join(right)}"
    unresolved = any(a.execution_state != "completed" or a.business_verdict == "UNKNOWN" for a in base + cand)
    if unresolved or len(left) != 1 or len(right) != 1:
        return transition, "unresolved"
    if left == right:
        return transition, "unchanged"
    if left == ["PASS"] and right == ["FAIL"]:
        return transition, "pass_to_fail"
    return transition, "fail_to_pass"


def _check_transitions(base: list[AttemptResult], cand: list[AttemptResult]) -> list[CheckTransition]:
    def verdicts(attempts: list[AttemptResult]) -> dict[str, str]:
        seen: dict[str, set[str]] = {}
        for attempt in attempts:
            for outcome in attempt.outcomes:
                seen.setdefault(outcome.check_id, set()).add(outcome.verdict)
        return {key: ",".join(sorted(value)) for key, value in seen.items()}

    left, right = verdicts(base), verdicts(cand)
    out = []
    for check_id in sorted(set(left) & set(right)):
        if left[check_id] != right[check_id]:
            out.append(
                CheckTransition(
                    check_id=check_id,
                    baseline=left[check_id],
                    candidate=right[check_id],
                    transition=f"{left[check_id]}->{right[check_id]}",
                )
            )
    return out


def compare_runs(
    base_report: RunReport,
    base_manifest: RunManifest,
    cand_report: RunReport,
    cand_manifest: RunManifest,
    audience: Audience = "engineer",
) -> Comparison:
    order = [c.case_id for c in base_manifest.cases]
    order += [c.case_id for c in cand_manifest.cases if c.case_id not in order]
    rows = []
    for case_id in order:
        base, cand = base_manifest.case(case_id), cand_manifest.case(case_id)
        hidden = any(item is not None and item.visibility == "hidden" for item in (base, cand))
        reveal = audience == "operator" or not hidden
        base_attempts = [a for a in base_report.attempts if a.case_id == case_id]
        cand_attempts = [a for a in cand_report.attempts if a.case_id == case_id]
        reasons = _incompatibilities(base, cand, base_report, cand_report, reveal)
        if base is not None and not base_attempts:
            reasons.append("not_executed_in_baseline")
        if cand is not None and not cand_attempts:
            reasons.append("not_executed_in_candidate")
        manifest = cand or base
        assert manifest is not None
        row = CompareRow(
            case_id=case_id,
            visibility="hidden" if hidden else "public",
            required=manifest.required,
            compatibility="not_comparable" if reasons else "comparable",
            reasons=reasons,
            baseline_verdicts=[a.business_verdict for a in base_attempts],
            candidate_verdicts=[a.business_verdict for a in cand_attempts],
            baseline_execution=[a.execution_state for a in base_attempts],
            candidate_execution=[a.execution_state for a in cand_attempts],
            category="not_comparable",
        )
        if not reasons:
            transition, category = _category(base_attempts, cand_attempts)
            row.transition, row.category = transition, category
            if reveal:
                row.check_transitions = _check_transitions(base_attempts, cand_attempts)
        rows.append(row)
    summary = {name: 0 for name in ("pass_to_fail", "fail_to_pass", "unchanged", "unresolved", "not_comparable")}
    for row in rows:
        summary[row.category] += 1
    return Comparison(
        audience=audience,
        baseline=_ref(base_report, base_manifest),
        candidate=_ref(cand_report, cand_manifest),
        rows=rows,
        summary=summary,
    )


def comparison_exit_code(comparison: Comparison) -> int:
    """1 for an observed PASS->FAIL on a required case; 2 when anything is unresolved or not comparable; else 0."""
    if any(row.category == "pass_to_fail" and row.required for row in comparison.rows):
        return EXIT_REQUIRED_FAILURE
    if comparison.summary["unresolved"] or comparison.summary["not_comparable"]:
        return EXIT_OPERATIONAL
    return EXIT_READY
