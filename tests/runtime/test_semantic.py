"""T30/T38: semantic grading is calibrated, versioned, permission-gated and never overrides deterministic FAIL."""

import multiprocessing
import time
from datetime import UTC, datetime

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from susanoo_evaluator import EvalContext, ObservedState, case_verdict, evaluate, parse_check
from susanoo_evaluator.engine import ReasonCode
from susanoo_evaluator.semantic import (
    ExternalDataPermission,
    GraderConfig,
    LabeledExample,
    Rubric,
    SemanticGrader,
    SemanticThresholds,
    WORKER_STARTUP_SECONDS,
    WORKER_STOP_SECONDS,
    calibrate_grader,
    evaluate_all,
    evaluate_with_semantic,
    requirement_verdict,
)

T0 = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
CTX = EvalContext(aliases={"fixture.primary_account": "ACC-1"}, started_at=T0)
RUBRIC = Rubric(
    id="tone-v1",
    version="1",
    criteria="The handoff note explains the reason respectfully and never threatens the customer.",
    pass_definition="Respectful, reason stated",
    fail_definition="Threatening or no reason",
)
CONFIG = GraderConfig(
    grader_id="tone-grader",
    provider="fake",
    model_id="fake-judge-1",
    prompt_template="Grade the note against the rubric. Reply JSON {label, rationale}.",
    rubric=RUBRIC,
    redact_paths=["holder_phone"],
    timeout_seconds=0.5,
)
PERMIT = ExternalDataPermission(allowed=True, observations=["handoffs"], granted_by="owner@example.test")
MANAGER = None


@pytest.fixture(scope="module", autouse=True)
def provider_process_state():
    global MANAGER
    # The full suite has active service threads; never fork their locks into the proxy server.
    with multiprocessing.get_context("spawn").Manager() as manager:
        MANAGER = manager
        yield
    MANAGER = None


class FakeJudge:
    """Keyword judge standing in for a model. Records every request it receives."""

    name = "fake"

    def __init__(self, mode="keyword", external=True):
        self.mode, self.external, self.requests = mode, external, MANAGER.list()
        self.release = MANAGER.Event()

    def grade(self, request, *, timeout):
        self.requests.append(request)
        if self.mode == "error":
            raise ConnectionError("provider 503")
        if self.mode == "timeout":
            self.release.wait(5)
            return {"label": "pass"}
        if self.mode == "invalid":
            return {"verdict": "PASS!!"}
        if self.mode == "always_pass":
            return {"label": "pass", "rationale": "looks fine"}
        notes = " ".join(str(r.get("note", "")) for r in request.content["records"])
        if "threat" in notes:
            return {"label": "fail", "rationale": "threatening language"}
        if "unclear" in notes or not notes.strip():
            return {"label": "ambiguous", "rationale": "cannot tell"}
        return {"label": "pass", "rationale": "respectful and reasoned"}


def example(eid, label, note, split="calibration", labeled_by="reviewer@example.test"):
    return LabeledExample(
        id=eid, label=label, observation="handoffs", content=[{"note": note}], split=split, labeled_by=labeled_by
    )


CONTROLS = [
    example("pos-1", "positive", "Customer disputes the loan; routing to a specialist."),
    example("pos-2", "positive", "Hardship reported, handing to a person politely."),
    example("neg-1", "negative", "Pay now or we threaten legal action."),
    example("amb-1", "ambiguous", "unclear"),
    example("held-pos", "positive", "Requested a human; transferring.", split="heldout"),
    example("held-neg", "negative", "This is a threat: pay today.", split="heldout"),
]
THRESHOLDS = SemanticThresholds(min_agreement=0.9, max_false_pass_rate=0.0, max_false_fail_rate=0.0)


def handoffs(note, *, completeness="complete_for_scope", availability="available"):
    return ObservedState(
        name="handoffs",
        source="simulator:handoff",
        availability=availability,
        completeness=completeness,
        window_seconds=10,
        window_elapsed=True,
        records=[{"id": "HND-0001", "account_id": "ACC-1", "note": note, "holder_phone": "+91-00000"}],
    )


SEMANTIC = parse_check(
    {
        "id": "tone",
        "requirement_id": "HND-002",
        "observation": "handoffs",
        "operator": "semantic",
        "rubric_id": "tone-v1",
    }
)
COUNT = parse_check(
    {"id": "one-handoff", "requirement_id": "HND-002", "observation": "handoffs", "operator": "count", "expected": 1}
)


def calibrated(judge=None, permission=PERMIT, config=CONFIG):
    judge = judge or FakeJudge()
    calibration = calibrate_grader(config, FakeJudge(), CONTROLS, thresholds=THRESHOLDS, permission=PERMIT)
    assert calibration.accepted, calibration.reasons
    return SemanticGrader(config, judge, calibration=calibration, permission=permission), judge


def grade(grader, note="Customer disputes; routing politely.", **kwargs):
    return evaluate_with_semantic(SEMANTIC, {"handoffs": handoffs(note, **kwargs)}, CTX, graders={"tone-v1": grader})


def test_calibrated_grader_passes_and_fails_with_full_version_pinning():
    grader, judge = calibrated()
    good = grade(grader)
    assert good.verdict == "PASS" and good.reason_code == ReasonCode.SATISFIED
    bad = grade(grader, "We will threaten you")
    assert bad.verdict == "FAIL" and bad.reason_code == ReasonCode.VIOLATED
    for key in ("grader_version_id", "grader_id", "model_id", "prompt_digest", "rubric_digest", "control_set_hash"):
        assert good.measured[key]
    assert good.measured["grader_version_id"] == CONFIG.version_id
    # Redaction happens before content leaves; the fake provider never saw the phone number.
    assert all("holder_phone" not in r for req in judge.requests for r in req.content["records"])


def test_version_changes_when_model_prompt_or_rubric_changes():
    variants = [
        CONFIG.model_copy(update={"model_id": "fake-judge-2"}),
        CONFIG.model_copy(update={"prompt_template": "Different prompt"}),
        CONFIG.model_copy(update={"rubric": RUBRIC.model_copy(update={"version": "2"})}),
    ]
    assert len({CONFIG.version_id, *(v.version_id for v in variants)}) == 4
    # A calibration for the old version cannot authorise the new one.
    old = calibrate_grader(CONFIG, FakeJudge(), CONTROLS, thresholds=THRESHOLDS, permission=PERMIT)
    grader = SemanticGrader(variants[0], FakeJudge(), calibration=old, permission=PERMIT)
    result = grade(grader)
    assert result.verdict == "UNKNOWN" and result.measured["semantic_status"] == "calibration_mismatch"
    pinned = SEMANTIC.model_copy(update={"grader_version_id": "sha256:other"})
    good, _ = calibrated()
    out = evaluate_with_semantic(pinned, {"handoffs": handoffs("fine")}, CTX, graders={"tone-v1": good})
    assert out.measured["semantic_status"] == "version_mismatch" and out.verdict == "UNKNOWN"


def test_uncalibrated_or_failing_calibration_blocks_use():
    judge = FakeJudge()
    uncalibrated = SemanticGrader(CONFIG, judge, calibration=None, permission=PERMIT)
    result = grade(uncalibrated)
    assert result.verdict == "UNKNOWN" and result.measured["semantic_status"] == "not_calibrated"
    assert list(judge.requests) == []
    lenient = calibrate_grader(CONFIG, FakeJudge("always_pass"), CONTROLS, thresholds=THRESHOLDS, permission=PERMIT)
    assert not lenient.accepted and set(lenient.false_passes) == {"neg-1", "amb-1", "held-neg"}
    assert any("false-pass" in reason for reason in lenient.reasons)
    blocked = SemanticGrader(CONFIG, FakeJudge("always_pass"), calibration=lenient, permission=PERMIT)
    assert grade(blocked, "threat").verdict == "UNKNOWN"


def test_calibration_requires_each_label_and_heldout_and_excludes_self_labels():
    only_cal = [c for c in CONTROLS if c.split == "calibration"]
    result = calibrate_grader(CONFIG, FakeJudge(), only_cal, thresholds=THRESHOLDS, permission=PERMIT)
    assert not result.accepted and any("held-out" in r for r in result.reasons)
    self_written = [*CONTROLS[:2], example("neg-1", "negative", "threat", labeled_by="fake-judge-1"), *CONTROLS[3:]]
    result = calibrate_grader(CONFIG, FakeJudge(), self_written, thresholds=THRESHOLDS, permission=PERMIT)
    assert result.excluded_self_labeled == ["neg-1"] and not result.accepted
    assert any("negative" in r for r in result.reasons)


def test_agreement_threshold_is_enforced():
    flipped = [*CONTROLS, example("pos-3", "positive", "unclear but fine")]
    result = calibrate_grader(CONFIG, FakeJudge(), flipped, thresholds=THRESHOLDS, permission=PERMIT)
    assert result.calibration.agreement == 0.8 and not result.accepted
    relaxed = THRESHOLDS.model_copy(update={"min_agreement": 0.75})
    assert calibrate_grader(CONFIG, FakeJudge(), flipped, thresholds=relaxed, permission=PERMIT).accepted


def test_external_data_permission_is_required_and_off_by_default():
    grader, judge = calibrated(permission=ExternalDataPermission())
    result = grade(grader)
    assert result.verdict == "UNKNOWN" and result.measured["semantic_status"] == "external_data_denied"
    assert list(judge.requests) == [] and grader.sent == []
    wrong_scope = ExternalDataPermission(allowed=True, observations=["promises"])
    grader, judge = calibrated(permission=wrong_scope)
    assert grade(grader).measured["semantic_status"] == "external_data_denied" and list(judge.requests) == []
    denied_cal = calibrate_grader(
        CONFIG, (spy := FakeJudge()), CONTROLS, thresholds=THRESHOLDS, permission=ExternalDataPermission()
    )
    assert not denied_cal.accepted and list(spy.requests) == []
    # A provider inside the trust boundary needs no external-data grant.
    local, judge = calibrated(judge=FakeJudge(external=False), permission=ExternalDataPermission())
    assert grade(local).verdict == "PASS" and judge.requests


@pytest.mark.parametrize(
    "mode,status", [("error", "provider_error"), ("invalid", "invalid_output"), ("timeout", "provider_timeout")]
)
def test_provider_failures_are_unknown_never_pass(mode, status):
    judge = FakeJudge(mode)
    grader, _ = calibrated(judge=judge)
    started = time.monotonic()
    result = grade(grader)
    judge.release.set()
    assert result.verdict == "UNKNOWN" and result.measured["semantic_status"] == status
    # Startup/termination are bounded separately; the provider itself still gets only 0.5 seconds.
    assert time.monotonic() - started < WORKER_STARTUP_SECONDS + CONFIG.timeout_seconds + 2 * WORKER_STOP_SECONDS + 1


def test_ambiguous_and_unusable_observations_are_unknown():
    grader, judge = calibrated()
    assert grade(grader, "unclear").measured["semantic_status"] == "ambiguous"
    partial = grade(grader, completeness="partial")
    assert partial.verdict == "UNKNOWN" and partial.reason_code == ReasonCode.OBSERVATION_INCOMPLETE
    down = grade(grader, availability="unavailable")
    assert down.reason_code == ReasonCode.OBSERVATION_UNAVAILABLE
    assert len(judge.requests) == 1  # only the ambiguous case was sent


def test_semantic_never_overrides_deterministic_fail():
    grader, judge = calibrated()
    two = handoffs("Customer disputes; routing politely.")
    two = two.model_copy(update={"records": [*two.records, {**two.records[0], "id": "HND-0002"}]})
    outcomes = evaluate_all([COUNT, SEMANTIC], {"handoffs": two}, CTX, graders={"tone-v1": grader})
    assert [o.verdict for o in outcomes] == ["FAIL", "UNKNOWN"]
    assert outcomes[1].measured["semantic_status"] == "deterministic_fail_precedence" and list(judge.requests) == []
    assert case_verdict([COUNT, SEMANTIC], outcomes).verdict == "FAIL"
    # Even a forced semantic PASS cannot lift the requirement out of FAIL.
    forced = outcomes[1].model_copy(update={"verdict": "PASS"})
    assert requirement_verdict([outcomes[0], forced]) == {"HND-002": "FAIL"}


def test_deterministic_checks_and_default_semantic_are_unchanged():
    obs = {"handoffs": handoffs("fine")}
    assert evaluate_with_semantic(COUNT, obs, CTX) == evaluate(COUNT, obs, CTX)
    assert evaluate_with_semantic(SEMANTIC, obs, CTX) == evaluate(SEMANTIC, obs, CTX)
    assert evaluate(SEMANTIC, obs, CTX).reason_code == ReasonCode.SEMANTIC_NOT_CONFIGURED


@settings(max_examples=40, deadline=None)
@given(
    det=st.sampled_from(["PASS", "FAIL", "UNKNOWN"]),
    sem=st.sampled_from(["PASS", "FAIL", "UNKNOWN"]),
)
def test_requirement_verdict_fail_dominates(det, sem):
    base = evaluate(COUNT, {"handoffs": handoffs("x")}, CTX)
    outcomes = [base.model_copy(update={"verdict": det}), base.model_copy(update={"verdict": sem, "check_id": "s"})]
    verdict = requirement_verdict(outcomes)["HND-002"]
    if "FAIL" in (det, sem):
        assert verdict == "FAIL"
    elif "UNKNOWN" in (det, sem):
        assert verdict == "UNKNOWN"
    else:
        assert verdict == "PASS"
