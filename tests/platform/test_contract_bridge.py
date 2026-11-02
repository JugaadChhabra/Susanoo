import copy
from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

from susanoo_contracts import RunnerClaimResponse, claim_component_hashes, content_hash
from susanoo_runner.platform import verify_claim
from susanoo_runner.journal import JournalError
import pytest
from susanoo_api.platform.database import transaction

from conftest import launch, result


def test_claim_components_bind_exact_wire_payload(suite):
    launch(suite)
    wire = suite["runner"].claim("outcome_suite")
    claim = RunnerClaimResponse.model_validate(wire)
    assert claim.component_hashes == claim_component_hashes(claim.execution_manifest, claim.evaluator_context)
    assert claim.manifest_hash == content_hash(claim.component_hashes)
    altered = copy.deepcopy(wire)
    altered["execution_manifest"]["limits"]["max_turns"] += 1
    assert claim_component_hashes(altered["execution_manifest"], altered["evaluator_context"]) != claim.component_hashes
    assert "connection" in wire["execution_manifest"]
    verify_claim(claim)
    altered = copy.deepcopy(wire)
    altered["agent_context"]["connection"]["adapter"] = "different-local-agent"
    with pytest.raises(JournalError, match="Agent connection"):
        verify_claim(RunnerClaimResponse.model_validate(altered))


def test_same_instant_event_retry_is_idempotent(suite):
    launch(suite)
    runner = suite["runner"]
    claim = runner.claim("outcome_suite")
    _, event = runner.event(claim, 1, "progress", {"code": "captured"})
    moment = datetime.fromisoformat(event["occurred_at"])
    event["occurred_at"] = moment.astimezone(timezone(timedelta(hours=5, minutes=30))).isoformat()
    reply = runner.ok(
        f"/attempts/{claim['attempt_id']}/events",
        {
            "lease_token": claim["lease_token"],
            "events": [event],
        },
    )
    assert reply["duplicates"] == [event["event_id"]]


def test_cleanup_can_dispatch_after_cancel_but_not_agent_work(suite):
    run = launch(suite)
    runner = suite["runner"]
    claim = runner.claim("outcome_suite")
    suite["owner"].ok("POST", f"/runs/{run['id']}/cancel")
    runner.event(claim, 1, "cancel_acknowledged")
    runner.event(
        claim,
        2,
        "step_prepared",
        {
            "operation_id": "cleanup",
            "request_digest": "a" * 64,
            "action_kind": "cleanup",
        },
        step_id="cleanup",
    )
    runner.ok(
        f"/attempts/{claim['attempt_id']}/reservations",
        {
            "lease_token": claim["lease_token"],
            "operation_id": "cleanup",
            "budget_kind": "cleanup",
        },
    )
    runner.event(claim, 3, "step_dispatching", {"operation_id": "cleanup"}, step_id="cleanup")
    assert (
        runner.send(
            f"/attempts/{claim['attempt_id']}/reservations",
            {
                "lease_token": claim["lease_token"],
                "operation_id": "new-business",
                "budget_kind": "agent",
            },
        ).status_code
        == 409
    )


def test_late_journal_is_append_only_and_cannot_restore_authority(suite):
    run = launch(suite)
    runner = suite["runner"]
    claim = runner.claim("outcome_suite")
    with transaction() as db:
        db.execute(
            "update platform.attempts set lease_expires_at=now()-interval '1 second' where id=%s",
            (claim["attempt_id"],),
        )
    event = {
        "event_id": str(uuid4()),
        "sequence": 1,
        "type": "step_prepared",
        "step_id": "lost-ack",
        "occurred_at": datetime.now(UTC).isoformat(),
        "payload": {"operation_id": "op1", "request_digest": "b" * 64, "action_kind": "agent_invoke"},
    }
    body = {
        "lease_token": claim["lease_token"],
        "state": "unresolved",
        "rationale": "Partition during mutation",
        "late_events": [event],
        "cleanup_state": "failed",
    }
    first = runner.ok(f"/attempts/{claim['attempt_id']}/reconciliations", body)
    second = runner.ok(f"/attempts/{claim['attempt_id']}/reconciliations", body)
    assert first["id"] == second["id"] and not first["dispatch_authority"]
    with transaction() as db:
        assert (
            db.execute(
                "select count(*) n from platform.runner_events where attempt_id=%s", (claim["attempt_id"],)
            ).fetchone()["n"]
            == 0
        )
        assert (
            db.execute(
                "select count(*) n from platform.late_runner_events where attempt_id=%s", (claim["attempt_id"],)
            ).fetchone()["n"]
            == 1
        )
    assert (
        runner.send(f"/attempts/{claim['attempt_id']}/heartbeat", {"lease_token": claim["lease_token"]}).status_code
        == 409
    )
    body["late_events"][0]["payload"]["request_digest"] = "c" * 64
    assert runner.send(f"/attempts/{claim['attempt_id']}/reconciliations", body).status_code == 409
    persisted = suite["owner"].ok("GET", f"/runs/{run['id']}")
    assert persisted["gate"] == "BLOCKED" and persisted["counts"]["unknown"] == 1


def test_observation_name_binding_and_partial_violation(suite):
    launch(suite)
    claim = suite["runner"].claim("outcome_suite")
    response, body = result(suite, claim, "UNKNOWN")
    assert response.is_success
    body["observations"][0]["observation_id"] = str(uuid4())
    body["observations"][0]["name"] = "wrong_query"
    body["check_results"][0]["observation_ids"] = [body["observations"][0]["observation_id"]]
    assert suite["runner"].send(f"/attempts/{claim['attempt_id']}/evidence", body).status_code == 422
