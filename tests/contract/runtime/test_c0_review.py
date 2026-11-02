"""Runtime-side review of the C0 contracts (docs/parallel/muaz/contract-requests/C0-review.md).

Passing tests pin what the runtime accepts. ``xfail(strict=True)`` tests encode open change
requests: when the canonical package changes they XPASS and fail, which is the signal to drop
the marker and record agreement.
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError
from susanoo_contracts import content_hash, models

ROOT = Path(__file__).resolve().parents[3]
SCHEMA = ROOT / "packages/contracts/schema"
T0 = datetime(2026, 10, 30, tzinfo=UTC)


def open_request(cr: str):
    # These assertions are now regression gates, not expected failures.
    # Agreement still requires peer review of the published canonical digest.
    return lambda test: test


def event(**overrides):
    values = {"event_id": uuid4(), "sequence": 1, "type": "progress", "occurred_at": T0, "payload": {"code": "x"}}
    return models.RunnerEvent(**{**values, **overrides})


# --- A. Accepted ---------------------------------------------------------------------------


def test_version_digest_matches_checked_in_schema():
    schema = json.loads((SCHEMA / "contracts.json").read_text(encoding="utf-8"))
    version = json.loads((SCHEMA / "VERSION.json").read_text(encoding="utf-8"))
    assert version["protocol_version"] == models.PROTOCOL_VERSION == "1.0"
    assert content_hash(schema) == version["digest"]


def test_integer_vectors_match_an_independent_canonicalizer():
    # For integer/ASCII-only values, JCS equals sorted compact JSON; the runner can verify without rfc8785.
    for vector in json.loads((SCHEMA / "examples.json").read_text(encoding="utf-8"))["hash_vectors"]:
        if all(isinstance(v, int) or (isinstance(v, str) and v.isascii()) for v in vector["value"].values()):
            plain = json.dumps(vector["value"], sort_keys=True, separators=(",", ":"))
            assert plain == vector["canonical_json"]
            assert hashlib.sha256(plain.encode()).hexdigest() == vector["sha256"]


def test_runner_timestamps_must_be_aware():
    with pytest.raises(ValidationError):
        event(occurred_at=datetime(2026, 10, 30))


def test_event_batches_are_bounded():
    lease = "x" * 40
    models.EventBatch(lease_token=lease, events=[event(sequence=i + 1) for i in range(100)])
    with pytest.raises(ValidationError):
        models.EventBatch(lease_token=lease, events=[event(sequence=i + 1) for i in range(101)])
    with pytest.raises(ValidationError):
        models.EventBatch(lease_token=lease, events=[])


def test_execution_verdict_cleanup_and_reconciliation_stay_separate():
    final = models.Finalize(
        lease_token="x" * 40,
        execution_state="interrupted",
        business_verdict="UNKNOWN",
        final_sequence=3,
        cleanup_state="failed",
        reconciliation_state="pending",
    )
    assert {final.execution_state, final.business_verdict, final.cleanup_state, final.reconciliation_state} == {
        "interrupted",
        "UNKNOWN",
        "failed",
        "pending",
    }


def test_capabilities_default_closed_and_concurrency_capped():
    caps = models.Capabilities()
    assert not (caps.invoke or caps.observe or caps.reset or caps.tool_routing or caps.idempotent_submission)
    with pytest.raises(ValidationError):
        models.Capabilities(max_concurrency=4)


# --- B. Blocking requests ------------------------------------------------------------------


@open_request("CR-01")
def test_cr01_step_response_payload_is_typed():
    with pytest.raises(ValidationError):
        event(type="step_response_recorded", payload={"dispatch_state": "whatever", "side_effect_state": 42})


@open_request("CR-02")
def test_cr02_claim_carries_verifiable_component_hashes():
    assert "component_hashes" in models.RunnerClaimResponse.model_fields
    assert "connection" in models.ExecutionManifest.model_fields


@open_request("CR-03")
def test_cr03_reconciliation_carries_late_journal_and_cleanup():
    fields = models.ReconciliationIn.model_fields
    assert "late_events" in fields and "cleanup_state" in fields


@open_request("CR-04")
def test_cr04_cleanup_budget_kind_exists():
    models.BudgetReservation(lease_token="x" * 40, operation_id="cleanup-1", budget_kind="cleanup")


@open_request("CR-05")
def test_cr05_check_spec_has_typed_filter():
    assert "filter" in models.CheckSpec.model_fields
    with pytest.raises(ValidationError):
        models.CheckSpec(
            requirement_version_id=uuid4(), operator="equals", observation="promise", path="amount", expected=1999.99
        )


@open_request("CR-05")
def test_cr05_observation_names_its_declared_query():
    assert "name" in models.Observation.model_fields


@open_request("CR-07")
def test_cr07_same_instant_hashes_equal():
    ist = timezone(timedelta(hours=5, minutes=30))
    first = event()
    assert content_hash(first) == content_hash(first.model_copy(update={"occurred_at": T0.astimezone(ist)}))


@open_request("CR-08")
def test_cr08_error_codes_are_enumerated():
    codes = {item.value for item in models.ErrorCode}
    assert {"LEASE_EXPIRED", "IDEMPOTENCY_CONFLICT", "BUDGET_EXCEEDED", "MISSING_CAPABILITY"} <= codes


@open_request("CR-09")
def test_cr09_adapter_boundary_models_exist():
    required = [
        "CapabilityReport",
        "SessionHandle",
        "Message",
        "AgentReply",
        "CancelResult",
        "FixtureContext",
        "FixtureHandle",
        "ObservationQuery",
        "StepIntent",
        "Reconciliation",
        "CleanupResult",
        "ApprovedAction",
        "ActionContext",
        "ActionResult",
        "SnapshotScope",
        "StateSnapshot",
        "JsonlFrame",
    ]
    assert [name for name in required if not hasattr(models, name)] == []


# --- C. Non-blocking requests --------------------------------------------------------------


@open_request("CR-10")
def test_cr10_observation_scope_has_entity():
    assert "entity" in models.Observation.model_fields


@open_request("CR-11")
def test_cr11_agent_context_has_tool_route():
    assert "tool_route" in models.AgentContext.model_fields


@open_request("CR-12")
def test_cr12_capabilities_record_consistency_and_retry():
    fields = models.Capabilities.model_fields
    assert "observer_consistency" in fields and "retry_semantics" in fields


@open_request("CR-13")
def test_cr13_fixture_digest_is_bound_to_recipe():
    with pytest.raises(ValidationError):
        models.FixtureSpec(adapter="collections", recipe={"accounts": 1}, digest="0" * 64, compatibility_key="k")
