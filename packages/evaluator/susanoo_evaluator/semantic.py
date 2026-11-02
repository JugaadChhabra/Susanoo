"""Optional semantic grading [Spec §13 › Challenge the grader; T30, T38].

A semantic check is graded through a ``SemanticProvider`` (a Protocol; no model SDK is imported here).
Every grader is pinned by grader id, model id, prompt digest and rubric digest, and may be used only
after calibration against labelled positive/negative/ambiguous controls (calibration and held-out
splits) meets owner-set agreement and false-pass thresholds. Content goes to an external provider
only with explicit permission for that observation, after redaction; otherwise the result is
``UNKNOWN`` and nothing is sent. Provider errors, timeouts, invalid or ambiguous output are
``UNKNOWN``, never ``PASS``. A semantic result never overrides a deterministic ``FAIL``. Labels
written by the grading model itself are excluded, so a model cannot approve its own answer key.

``evaluate_with_semantic`` wraps ``engine.evaluate``: deterministic checks are untouched; only the
``semantic`` operator is routed to a calibrated grader.
"""

from __future__ import annotations

import multiprocessing
import json
from collections.abc import Iterable, Mapping
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from susanoo_contracts import content_hash

from .dsl import Alias, Check, DslModel, ObservedState, SemanticCheck
from .engine import CheckOutcome, EvalContext, ReasonCode, Verdict, equal, evaluate, get_path

SemanticStatus = Literal[
    "graded",
    "ambiguous",
    "not_calibrated",
    "calibration_mismatch",
    "version_mismatch",
    "external_data_denied",
    "provider_error",
    "provider_timeout",
    "invalid_output",
    "no_grader",
    "deterministic_fail_precedence",
    "observation_unusable",
]
WORKER_STARTUP_SECONDS = 5
WORKER_STOP_SECONDS = 1


class Rubric(DslModel):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
    version: str = Field(min_length=1, max_length=40)
    criteria: str = Field(min_length=1, max_length=8000)
    pass_definition: str = Field(min_length=1, max_length=2000)
    fail_definition: str = Field(min_length=1, max_length=2000)

    @property
    def digest(self) -> str:
        return content_hash(self.model_dump(mode="json"))


class GraderConfig(DslModel):
    grader_id: str = Field(min_length=1, max_length=100)
    provider: str = Field(min_length=1, max_length=100)
    model_id: str = Field(min_length=1, max_length=200)
    prompt_template: str = Field(min_length=1, max_length=8000)
    rubric: Rubric
    timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    redact_paths: list[str] = Field(default_factory=list, max_length=50)
    max_records: int = Field(default=20, ge=1, le=200)

    @property
    def prompt_digest(self) -> str:
        return content_hash({"template": self.prompt_template})

    @property
    def version_id(self) -> str:
        """Pins grader, provider, model, prompt, rubric and redaction; any change needs recalibration."""
        return content_hash(
            {
                "grader_id": self.grader_id,
                "provider": self.provider,
                "model_id": self.model_id,
                "prompt": self.prompt_digest,
                "rubric": self.rubric.digest,
                "redact": sorted(self.redact_paths),
                "max_records": self.max_records,
            }
        )


class ExternalDataPermission(DslModel):
    """Off by default. ``observations`` lists what may leave the trust boundary (after redaction)."""

    allowed: bool = False
    observations: list[str] = Field(default_factory=list)
    granted_by: str | None = None

    def permits(self, name: str) -> bool:
        return self.allowed and name in self.observations


class GraderRequest(DslModel):
    grader_version_id: str
    model_id: str
    prompt: str
    rubric: Rubric
    content: dict[str, Any]


class GraderReply(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: Literal["pass", "fail", "ambiguous"]
    rationale: str = Field(default="", max_length=4000)


@runtime_checkable
class SemanticProvider(Protocol):
    """``external`` providers leave the trust boundary and need ``ExternalDataPermission``."""

    name: str
    external: bool

    def grade(self, request: GraderRequest, *, timeout: float) -> Any: ...


class LabeledExample(DslModel):
    id: str = Field(min_length=1, max_length=100)
    label: Literal["positive", "negative", "ambiguous"]
    observation: str = Field(min_length=1, max_length=64)
    content: list[dict[str, Any]]
    split: Literal["calibration", "heldout"] = "calibration"
    labeled_by: str = Field(min_length=1, max_length=200, description="Reviewer or model that wrote the label")


class SemanticThresholds(DslModel):
    """Reviewer-defined error thresholds, applied to the calibration and held-out splits separately."""

    min_agreement: float = Field(default=0.9, ge=0, le=1, allow_inf_nan=False)
    max_false_pass_rate: float = Field(default=0.0, ge=0, le=1, allow_inf_nan=False)
    max_false_fail_rate: float = Field(default=0.1, ge=0, le=1, allow_inf_nan=False)
    min_per_label: int = Field(default=1, ge=1, le=1000)
    min_heldout: int = Field(default=1, ge=1, le=1000)


class SplitMetrics(DslModel):
    examples: int
    agreement: float
    false_pass_rate: float
    false_fail_rate: float
    errors: int


class SemanticCalibration(DslModel):
    grader_version_id: str
    model_id: str
    prompt_digest: str
    rubric_digest: str
    control_set_hash: str
    thresholds: SemanticThresholds
    calibration: SplitMetrics | None = None
    heldout: SplitMetrics | None = None
    excluded_self_labeled: list[str] = Field(default_factory=list)
    false_passes: list[str] = Field(default_factory=list)
    false_fails: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    accepted: bool
    reasons: list[str] = Field(default_factory=list)


def _redact(record: Any, paths: Iterable[str]) -> Any:
    data = dict(record) if isinstance(record, Mapping) else record
    for path in paths:
        parts = path.split(".")
        target = data
        for part in parts[:-1]:
            target = target.get(part) if isinstance(target, Mapping) else None
            if target is None:
                break
        if isinstance(target, dict):
            target.pop(parts[-1], None)
    return data


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value))


def build_request(config: GraderConfig, observation: str, records: list[dict[str, Any]]) -> GraderRequest:
    redacted = [_redact(_copy(record), config.redact_paths) for record in records[: config.max_records]]
    return GraderRequest(
        grader_version_id=config.version_id,
        model_id=config.model_id,
        prompt=config.prompt_template,
        rubric=config.rubric,
        content={"observation": observation, "records": redacted, "truncated": len(records) > config.max_records},
    )


def _provider_worker(connection, provider, request, timeout):
    try:
        connection.send(("ready", None))
        connection.recv()
        connection.send(("graded", provider.grade(request, timeout=timeout)))
    except Exception as error:
        connection.send(("provider_error", f"Grader failed: {type(error).__name__}"))
    finally:
        connection.close()


def _call(
    provider: SemanticProvider, request: GraderRequest, timeout: float
) -> tuple[SemanticStatus, GraderReply | str]:
    """A timed-out provider cannot leave an unbounded grading thread behind.

    Providers must be spawn-serializable. Create network clients inside grade(), never
    inherit connection pools or fork a process containing active application threads.
    """
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_provider_worker, args=(child, provider, request, timeout), daemon=True)
    try:
        process.start()
        child.close()
        if not parent.poll(WORKER_STARTUP_SECONDS):
            return "provider_timeout", "Grader worker did not start within the bounded startup allowance"
        status, raw = parent.recv()
        if status != "ready":
            return "provider_error", "Grader worker could not initialize"
        parent.send(True)
        if not parent.poll(timeout):
            return "provider_timeout", f"Grader gave no answer within {timeout}s"
        status, raw = parent.recv()
        if status != "graded":
            return "provider_error", raw
    except Exception as error:
        return "provider_error", f"Grader failed: {type(error).__name__}"
    finally:
        if process.pid:
            if process.is_alive():
                process.terminate()
            process.join(timeout=WORKER_STOP_SECONDS)
            if process.is_alive():
                process.kill()
                process.join(timeout=WORKER_STOP_SECONDS)
            process.close()
        parent.close()
        child.close()
    try:
        return "graded", GraderReply.model_validate(raw)
    except ValidationError:
        return "invalid_output", "Grader output did not match the reply schema"


def calibrate_grader(
    config: GraderConfig,
    provider: SemanticProvider,
    examples: Iterable[LabeledExample],
    *,
    thresholds: SemanticThresholds,
    permission: ExternalDataPermission,
) -> SemanticCalibration:
    """Grade labelled controls; accept only when both splits meet the thresholds with no blocked sends."""
    examples = sorted(examples, key=lambda e: e.id)
    base = {
        "grader_version_id": config.version_id,
        "model_id": config.model_id,
        "prompt_digest": config.prompt_digest,
        "rubric_digest": config.rubric.digest,
        "control_set_hash": content_hash(
            {"grader": config.version_id, "examples": [e.model_dump(mode="json") for e in examples]}
        ),
        "thresholds": thresholds,
    }
    self_labeled = [e.id for e in examples if e.labeled_by in {config.model_id, config.grader_id}]
    usable = [e for e in examples if e.id not in self_labeled]
    if provider.external and any(not permission.permits(e.observation) for e in usable):
        return SemanticCalibration(
            **base,
            excluded_self_labeled=self_labeled,
            accepted=False,
            reasons=["external_data_denied: calibration content would leave the trust boundary without permission"],
        )
    reasons: list[str] = []
    false_passes, false_fails, errors = [], [], []
    metrics: dict[str, SplitMetrics] = {}
    for split in ("calibration", "heldout"):
        rows = [e for e in usable if e.split == split]
        agree = fp = ff = err = 0
        negatives = sum(1 for e in rows if e.label == "negative")
        positives = sum(1 for e in rows if e.label == "positive")
        for example in rows:
            status, reply = _call(
                provider, build_request(config, example.observation, example.content), config.timeout_seconds
            )
            if status != "graded":
                err += 1
                errors.append(example.id)
                continue
            label = reply.label
            expected = {"positive": "pass", "negative": "fail", "ambiguous": "ambiguous"}[example.label]
            if label == expected:
                agree += 1
            elif label == "pass" and example.label != "positive":
                fp += 1
                false_passes.append(example.id)
            elif label == "fail" and example.label == "positive":
                ff += 1
                false_fails.append(example.id)
        metrics[split] = SplitMetrics(
            examples=len(rows),
            agreement=round(agree / len(rows), 6) if rows else 0.0,
            false_pass_rate=round(fp / max(negatives + sum(1 for e in rows if e.label == "ambiguous"), 1), 6),
            false_fail_rate=round(ff / max(positives, 1), 6),
            errors=err,
        )
        if split == "calibration":
            for label in ("positive", "negative", "ambiguous"):
                if sum(1 for e in rows if e.label == label) < thresholds.min_per_label:
                    reasons.append(f"calibration split needs at least {thresholds.min_per_label} {label} example(s)")
        elif len(rows) < thresholds.min_heldout:
            reasons.append(f"held-out split needs at least {thresholds.min_heldout} example(s)")
        m = metrics[split]
        if rows and m.agreement < thresholds.min_agreement:
            reasons.append(f"{split} agreement {m.agreement} is below {thresholds.min_agreement}")
        if m.false_pass_rate > thresholds.max_false_pass_rate:
            reasons.append(f"{split} false-pass rate {m.false_pass_rate} exceeds {thresholds.max_false_pass_rate}")
        if m.false_fail_rate > thresholds.max_false_fail_rate:
            reasons.append(f"{split} false-fail rate {m.false_fail_rate} exceeds {thresholds.max_false_fail_rate}")
    return SemanticCalibration(
        **base,
        calibration=metrics["calibration"],
        heldout=metrics["heldout"],
        excluded_self_labeled=self_labeled,
        false_passes=false_passes,
        false_fails=false_fails,
        errors=errors,
        accepted=not reasons,
        reasons=reasons,
    )


class SemanticGrader:
    """A pinned grader plus its calibration and data permission. Uncalibrated graders never grade."""

    def __init__(
        self,
        config: GraderConfig,
        provider: SemanticProvider,
        *,
        calibration: SemanticCalibration | None,
        permission: ExternalDataPermission | None = None,
    ):
        self.config, self.provider, self.calibration = config, provider, calibration
        self.permission = permission or ExternalDataPermission()
        self.sent: list[GraderRequest] = []

    def _blocked(self, check: SemanticCheck) -> tuple[SemanticStatus, str] | None:
        if check.rubric_id != self.config.rubric.id:
            return "no_grader", f"Grader rubric {self.config.rubric.id} does not match {check.rubric_id}"
        if check.grader_version_id is not None and check.grader_version_id != self.config.version_id:
            return "version_mismatch", "Check pins a different grader version"
        if self.calibration is None:
            return "not_calibrated", "Semantic grader has not been calibrated"
        if self.calibration.grader_version_id != self.config.version_id:
            return "calibration_mismatch", "Calibration was run for a different grader version"
        if not self.calibration.accepted:
            return "not_calibrated", "Calibration did not meet thresholds: " + "; ".join(self.calibration.reasons)
        if self.provider.external and not self.permission.permits(check.observation):
            return "external_data_denied", f"No permission to send {check.observation} to an external grader"
        return None

    def grade(
        self,
        check: SemanticCheck,
        observations: Mapping[str, ObservedState],
        ctx: EvalContext,
        *,
        deterministic: Iterable[CheckOutcome] = (),
    ) -> CheckOutcome:
        versions = {
            "grader_version_id": self.config.version_id,
            "grader_id": self.config.grader_id,
            "model_id": self.config.model_id,
            "prompt_digest": self.config.prompt_digest,
            "rubric_digest": self.config.rubric.digest,
            "control_set_hash": self.calibration.control_set_hash if self.calibration else None,
        }

        def outcome(verdict: Verdict, code: ReasonCode, status: SemanticStatus, reason: str) -> CheckOutcome:
            return CheckOutcome(
                check_id=check.id,
                requirement_id=check.requirement_id,
                required=check.required,
                verdict=verdict,
                reason_code=code,
                reason=reason,
                measured={**versions, "semantic_status": status},
                observation_names=[check.observation],
            )

        if any(o.requirement_id == check.requirement_id and o.verdict == "FAIL" for o in deterministic):
            return outcome(
                "UNKNOWN",
                ReasonCode.SEMANTIC_BLOCKED,
                "deterministic_fail_precedence",
                "A deterministic check already failed this requirement; the semantic grader was not consulted",
            )
        blocked = self._blocked(check)
        if blocked:
            code = ReasonCode.SEMANTIC_NOT_CONFIGURED if blocked[0] == "no_grader" else ReasonCode.SEMANTIC_BLOCKED
            return outcome("UNKNOWN", code, *blocked)
        observation = observations.get(check.observation)
        if observation is None:
            return outcome(
                "UNKNOWN",
                ReasonCode.OBSERVATION_MISSING,
                "observation_unusable",
                f"No independent observation named {check.observation}",
            )
        if observation.availability != "available":
            return outcome(
                "UNKNOWN",
                ReasonCode.OBSERVATION_UNAVAILABLE,
                "observation_unusable",
                f"Observer for {check.observation} was unavailable",
            )
        complete = (
            observation.completeness == "complete_for_scope"
            and observation.window_elapsed
            and observation.window_seconds >= check.window_seconds
        )
        if check.requires_complete_observation and not complete:
            return outcome(
                "UNKNOWN",
                ReasonCode.OBSERVATION_INCOMPLETE,
                "observation_unusable",
                f"{check.observation} is not complete for scope",
            )
        for operand in check.filter.values():
            if isinstance(operand, Alias) and operand.alias not in ctx.aliases:
                return outcome(
                    "UNKNOWN",
                    ReasonCode.ALIAS_UNRESOLVED,
                    "observation_unusable",
                    f"Fixture alias {operand.alias} is not defined",
                )
        resolved = {path: ctx.resolve(operand) for path, operand in check.filter.items()}
        try:
            rows = [r for r in observation.records if all(equal(get_path(r, p), v) for p, v in resolved.items())]
        except Exception:
            return outcome(
                "UNKNOWN",
                ReasonCode.INVALID_VALUE,
                "observation_unusable",
                "Filter values cannot be compared without guessing",
            )
        request = build_request(self.config, check.observation, rows)
        self.sent.append(request)
        status, reply = _call(self.provider, request, self.config.timeout_seconds)
        if status == "invalid_output":
            return outcome("UNKNOWN", ReasonCode.SEMANTIC_PROVIDER_ERROR, status, str(reply))
        if status != "graded":
            return outcome("UNKNOWN", ReasonCode.SEMANTIC_PROVIDER_ERROR, status, str(reply))
        if reply.label == "ambiguous":
            return outcome(
                "UNKNOWN",
                ReasonCode.SEMANTIC_AMBIGUOUS,
                "ambiguous",
                f"Grader judged the evidence ambiguous: {reply.rationale}",
            )
        if reply.label == "pass":
            return outcome("PASS", ReasonCode.SATISFIED, "graded", reply.rationale or "Rubric satisfied")
        return outcome("FAIL", ReasonCode.VIOLATED, "graded", reply.rationale or "Rubric violated")


def evaluate_with_semantic(
    check: Check,
    observations: Mapping[str, ObservedState],
    ctx: EvalContext,
    *,
    graders: Mapping[str, SemanticGrader] | None = None,
    deterministic: Iterable[CheckOutcome] = (),
) -> CheckOutcome:
    """Same contract as ``engine.evaluate``; ``semantic`` checks go to the grader for their rubric."""
    if not isinstance(check, SemanticCheck):
        return evaluate(check, observations, ctx)
    grader = (graders or {}).get(check.rubric_id)
    if grader is None:
        return evaluate(check, observations, ctx)  # unchanged default: UNKNOWN, semantic_not_configured
    return grader.grade(check, observations, ctx, deterministic=deterministic)


def evaluate_all(
    checks: Iterable[Check],
    observations: Mapping[str, ObservedState],
    ctx: EvalContext,
    *,
    graders: Mapping[str, SemanticGrader] | None = None,
) -> list[CheckOutcome]:
    """Deterministic checks first, then semantic ones, so a deterministic FAIL is known before grading."""
    checks = list(checks)
    deterministic = [evaluate(c, observations, ctx) for c in checks if not isinstance(c, SemanticCheck)]
    semantic = [
        evaluate_with_semantic(c, observations, ctx, graders=graders, deterministic=deterministic)
        for c in checks
        if isinstance(c, SemanticCheck)
    ]
    by_id = {o.check_id: o for o in deterministic + semantic}
    return [by_id[c.id] for c in checks]


def requirement_verdict(outcomes: Iterable[CheckOutcome]) -> dict[str, Verdict]:
    """Per requirement: any FAIL wins, so a semantic PASS can never turn a deterministic FAIL into PASS."""
    result: dict[str, Verdict] = {}
    for item in outcomes:
        if not item.required:
            continue
        current = result.get(item.requirement_id, "PASS")
        if item.verdict == "FAIL" or current == "FAIL":
            result[item.requirement_id] = "FAIL"
        elif item.verdict == "UNKNOWN" or current == "UNKNOWN":
            result[item.requirement_id] = "UNKNOWN"
        else:
            result[item.requirement_id] = "PASS"
    return result
