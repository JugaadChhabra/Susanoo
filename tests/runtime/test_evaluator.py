from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError
from susanoo_evaluator import (
    AttemptSummary,
    Control,
    EvalContext,
    ObservedState,
    calibrate,
    case_verdict,
    evaluate,
    evaluator_digest,
    evidence_supports,
    parse_check,
    run_gate,
)
from susanoo_evaluator.engine import CheckOutcome, ReasonCode

T0 = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
CTX = EvalContext(aliases={"fixture.primary_account": "ACC-1"}, started_at=T0)
INR = {"amount_minor": 500000, "currency": "INR"}
DUE = {"date": "2026-11-05", "timezone": "Asia/Kolkata"}


def obs(records, *, completeness="complete_for_scope", elapsed=True, baseline=None, name="promises", **extra):
    return ObservedState(
        name=name,
        source="collections_backend_read",
        availability="available",
        completeness=completeness,
        window_seconds=10,
        window_elapsed=elapsed,
        records=records,
        baseline=baseline,
        **extra,
    )


def promise(account="ACC-1", amount=INR, due=DUE, pid="P1"):
    return {"id": pid, "account_id": account, "amount": amount, "due_date": due}


def check(operator, **fields):
    base = {"id": f"c-{operator}", "requirement_id": "PTP-001", "observation": "promises", "operator": operator}
    return parse_check({**base, **fields})


ONCE = check("count", filter={"account_id": {"alias": "fixture.primary_account"}}, expected=1)


def run(c, *observations):
    return evaluate(c, {o.name: o for o in observations}, CTX)


# --- DSL validation -------------------------------------------------------------------------


def test_float_money_and_floats_are_rejected_at_approval():
    with pytest.raises(ValidationError):
        check("equals", path="amount", expected={"amount_minor": 5000.5, "currency": "INR"})
    with pytest.raises(ValidationError):
        check("equals", path="amount", expected=1999.99)


def test_paths_and_aliases_are_a_closed_grammar():
    with pytest.raises(ValidationError):
        check("equals", path="amount; drop table", expected=1)
    with pytest.raises(ValidationError):
        check("exists", filter={"account_id": {"alias": "os.environ"}})
    with pytest.raises(ValidationError):
        check("count", expected=1, min=0)
    with pytest.raises(ValidationError):
        check("range", path="amount", min=1, max=INR)


def test_business_dates_need_a_real_timezone():
    with pytest.raises(ValidationError):
        check("equals", path="due_date", expected={"date": "2026-11-05", "timezone": "Mars/Base"})


# --- Verdicts -------------------------------------------------------------------------------


def test_correct_write_passes():
    assert run(ONCE, obs([promise()])).verdict == "PASS"
    assert run(check("equals", path="amount", expected=INR), obs([promise()])).verdict == "PASS"
    assert run(check("equals", path="due_date", expected=DUE), obs([promise()])).verdict == "PASS"


def test_false_completion_fails_on_complete_absence():
    result = run(ONCE, obs([]))
    assert (result.verdict, result.reason_code) == ("FAIL", ReasonCode.VIOLATED)


def test_absence_before_the_window_is_unknown_not_fail():
    result = run(ONCE, obs([], elapsed=False))
    assert (result.verdict, result.reason_code) == ("UNKNOWN", ReasonCode.WINDOW_NOT_ELAPSED)


def test_observer_outage_is_unknown():
    down = ObservedState(
        name="promises",
        source="collections_backend_read",
        availability="unavailable",
        completeness="unknown",
        window_seconds=10,
        window_elapsed=True,
        reason="connection refused",
    )
    result = run(ONCE, down)
    assert (result.verdict, result.reason_code) == ("UNKNOWN", ReasonCode.OBSERVATION_UNAVAILABLE)
    assert run(ONCE).reason_code == ReasonCode.OBSERVATION_MISSING


def test_duplicate_is_proven_even_from_a_partial_view():
    two = [promise(pid="P1"), promise(pid="P2")]
    assert run(ONCE, obs(two, completeness="partial")).verdict == "FAIL"
    assert run(ONCE, obs([promise()], completeness="partial")).verdict == "UNKNOWN"


def test_wrong_amount_and_wrong_account():
    wrong = promise(amount={"amount_minor": 50000, "currency": "INR"})
    assert run(check("equals", path="amount", expected=INR), obs([wrong])).verdict == "FAIL"
    other = promise(account="ACC-2")
    assert run(ONCE, obs([other])).verdict == "FAIL"


def test_empty_redacted_object_never_satisfies_a_check():
    result = run(check("equals", path="amount", expected=INR), obs([promise()], redacted_paths=["amount"]))
    assert (result.verdict, result.reason_code) == ("UNKNOWN", ReasonCode.REDACTED_VALUE)


def test_float_money_in_observed_state_is_unknown_not_guessed():
    floaty = promise(amount={"amount_minor": 5000.0, "currency": "INR"})
    result = run(check("equals", path="amount", expected=INR), obs([floaty]))
    assert (result.verdict, result.reason_code) == ("UNKNOWN", ReasonCode.INVALID_VALUE)


def test_range_with_money_and_currency_mismatch():
    bounded = check("range", path="amount", min={"amount_minor": 100, "currency": "INR"}, max=INR)
    assert run(bounded, obs([promise()])).verdict == "PASS"
    usd = promise(amount={"amount_minor": 100, "currency": "USD"})
    assert run(bounded, obs([usd])).reason_code == ReasonCode.INVALID_VALUE


def test_unresolved_alias_is_unknown():
    c = check("exists", filter={"account_id": {"alias": "fixture.missing"}})
    assert run(c, obs([promise()])).reason_code == ReasonCode.ALIAS_UNRESOLVED


def test_forbidden_change_on_balance():
    before = [{"id": "ACC-1", "balance": INR, "status": "active"}]
    after = [{"id": "ACC-1", "balance": {"amount_minor": 0, "currency": "INR"}, "status": "active"}]
    c = check("forbidden_change", observation="accounts", paths=["balance"])
    assert run(c, obs(after, baseline=before, name="accounts")).verdict == "FAIL"
    assert run(c, obs(before, baseline=before, name="accounts")).verdict == "PASS"
    assert run(c, obs(before, name="accounts")).reason_code == ReasonCode.BASELINE_MISSING


def test_transition_rules():
    c = check("transition", observation="accounts", path="status", allowed=[["active", "promise_to_pay"]])
    before = [{"id": "ACC-1", "status": "active"}]
    ok = run(c, obs([{"id": "ACC-1", "status": "promise_to_pay"}], baseline=before, name="accounts"))
    bad = run(c, obs([{"id": "ACC-1", "status": "closed"}], baseline=before, name="accounts"))
    assert (ok.verdict, bad.verdict) == ("PASS", "FAIL")


def test_relationship_needs_the_target_to_be_complete():
    c = check("relationship", path="account_id", target_observation="accounts")
    accounts = obs([{"id": "ACC-1"}], name="accounts")
    assert run(c, obs([promise()]), accounts).verdict == "PASS"
    assert run(c, obs([promise(account="ACC-9")]), accounts).verdict == "FAIL"
    partial = obs([{"id": "ACC-1"}], name="accounts", completeness="partial")
    assert run(c, obs([promise(account="ACC-9")]), partial).verdict == "UNKNOWN"


def test_deadline_uses_aware_timestamps():
    c = check("deadline", observation="handoffs", path="created_at", within_seconds=60)
    on_time = obs([{"id": "H1", "created_at": "2026-11-01T10:00:30Z"}], name="handoffs")
    late = obs([{"id": "H1", "created_at": "2026-11-01T10:05:00Z"}], name="handoffs")
    naive = obs([{"id": "H1", "created_at": "2026-11-01T10:00:30"}], name="handoffs")
    assert run(c, on_time).verdict == "PASS"
    assert run(c, late).verdict == "FAIL"
    assert run(c, naive).reason_code == ReasonCode.INVALID_VALUE


def test_manual_and_semantic_are_pending_unknown():
    assert run(check("manual", instructions="Reviewer reads transcript")).reason_code == ReasonCode.MANUAL_PENDING
    assert run(check("semantic", rubric_id="tone")).reason_code == ReasonCode.SEMANTIC_NOT_CONFIGURED


def test_evaluator_digest_is_stable_hex():
    assert evaluator_digest() == evaluator_digest()
    assert len(evaluator_digest()) == 64


# --- Aggregation ----------------------------------------------------------------------------


def outcome(check_id, verdict, required=True):
    return CheckOutcome(
        check_id=check_id, requirement_id="R", required=required, verdict=verdict, reason_code="satisfied", reason="r"
    )


def test_case_verdict_rules():
    a = check("exists", id="a")
    b = check("exists", id="b")
    opt = check("exists", id="opt", required=False)
    assert case_verdict([a, b], [outcome("a", "PASS"), outcome("b", "PASS")]).verdict == "PASS"
    assert case_verdict([a, b], [outcome("a", "PASS")]).verdict == "UNKNOWN"
    assert case_verdict([a, b], [outcome("a", "UNKNOWN"), outcome("b", "FAIL")]).verdict == "FAIL"
    result = case_verdict([a, opt], [outcome("a", "PASS"), outcome("opt", "FAIL", required=False)])
    assert result.verdict == "PASS" and result.optional_failures == ["opt"]


VERDICTS = st.sampled_from(["PASS", "FAIL", "UNKNOWN"])


@given(st.lists(st.tuples(VERDICTS, st.booleans()), min_size=1, max_size=8))
def test_adding_a_required_fail_never_improves_a_case(rows):
    checks = [check("exists", id=f"c{i}", required=req) for i, (_, req) in enumerate(rows)]
    outcomes = [outcome(f"c{i}", v, req) for i, (v, req) in enumerate(rows)]
    extra = check("exists", id="extra")
    worse = case_verdict([*checks, extra], [*outcomes, outcome("extra", "FAIL")])
    assert worse.verdict == "FAIL"


ATTEMPTS = st.builds(
    AttemptSummary,
    required=st.booleans(),
    execution_state=st.sampled_from(["completed", "error", "timed_out", "cancelled", "interrupted"]),
    business_verdict=st.one_of(st.none(), VERDICTS),
    cleanup_state=st.sampled_from(["succeeded", "failed", "not_required"]),
    reconciliation_state=st.sampled_from(["not_needed", "pending", "resolved", "unresolved"]),
)


@given(st.lists(ATTEMPTS, max_size=10))
def test_gate_is_never_ready_with_a_blocker(attempts):
    gate = run_gate(attempts)
    if gate == "READY_FOR_ACCEPTANCE":
        assert attempts
        for item in attempts:
            assert item.cleanup_state != "failed"
            assert item.reconciliation_state in {"not_needed", "resolved"}
            if item.required:
                assert item.execution_state == "completed" and item.business_verdict == "PASS"


def test_gate_states():
    done = AttemptSummary(
        required=True, execution_state="completed", business_verdict="PASS", cleanup_state="succeeded"
    )
    assert run_gate([]) == "BLOCKED"
    assert run_gate([done]) == "READY_FOR_ACCEPTANCE"
    assert run_gate([done, done.model_copy(update={"execution_state": "running"})]) == "IN_PROGRESS"
    assert run_gate([done.model_copy(update={"business_verdict": "UNKNOWN"})]) == "NEEDS_REVIEW"
    assert run_gate([done.model_copy(update={"reconciliation_state": "pending"})]) == "BLOCKED"
    optional_fail = done.model_copy(update={"required": False, "business_verdict": "FAIL"})
    assert run_gate([done, optional_fail]) == "READY_FOR_ACCEPTANCE"


def test_evidence_supports_partial_fail_but_not_partial_pass_for_counts():
    meta = dict(
        requires_complete_observation=True,
        check_window_seconds=10,
        availability="available",
        completeness="partial",
        window_seconds=10,
        window_elapsed=True,
    )
    assert evidence_supports("count", "FAIL", **meta)
    assert not evidence_supports("count", "PASS", **meta)
    assert evidence_supports("exists", "PASS", **meta)
    assert not evidence_supports("exists", "FAIL", **meta)
    assert not evidence_supports("count", "PASS", **{**meta, "availability": "unavailable"})


# --- Calibration ----------------------------------------------------------------------------


def test_calibration_catches_missed_defects_and_invalid_controls():
    good = Control(id="ok", label="positive", observations=[obs([promise()])], aliases=CTX.aliases, started_at=T0)
    dup = Control(
        id="dup",
        label="negative",
        observations=[obs([promise(pid="P1"), promise(pid="P2")])],
        aliases=CTX.aliases,
        started_at=T0,
        violation_verified=True,
    )
    unverified = dup.model_copy(update={"id": "unverified", "violation_verified": False})
    result = calibrate(ONCE, [good, dup, unverified])
    assert not result.accepted and result.invalid_negative_controls == ["unverified"]
    lax = check("count", filter={"account_id": {"alias": "fixture.primary_account"}}, min=1)
    weak = calibrate(lax, [good, dup])
    assert not weak.accepted and weak.missed_defects == ["dup"]
    assert weak.control_set_hash != result.control_set_hash
