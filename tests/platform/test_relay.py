import gzip
import json
import time
from urllib.parse import urlencode

import pytest
from susanoo_connectors.webhook import sign
from susanoo_connectors.media.twilio import compute_signature
from susanoo_api.platform.database import transaction

from conftest import ProtocolRunner, version


def relay(world, monkeypatch, provider="webhook"):
    monkeypatch.setenv("TEST_CALLBACK_KEY", "synthetic-callback-key")
    project_id = world["project"]["id"]
    integration = world["owner"].ok("POST", f"/projects/{project_id}/integrations", {
        "provider": provider, "secret_ref": "TEST_CALLBACK_KEY",
        "relay": {"public_base_url": "https://callbacks.example.test"},
    })
    connection = version(world, {
        "kind": "connection", "family": "I12" if provider == "media" else "I04", "adapter": "callback-test",
        "mapping": {"relay_integration_id": integration["id"]},
    })
    runner = ProtocolRunner(world)
    world["owner"].ok("POST", f"/projects/{project_id}/jobs", {
        "kind": "preflight", "version_ids": [connection["id"]],
    }, headers={"Idempotency-Key": "callback-preflight"})
    claim = runner.claim("preflight")
    runner.event(claim, 1, "step_prepared", {
        "operation_id": "op-1", "request_digest": "a" * 64, "action_kind": "agent_invoke",
    }, step_id="step-1")
    runner.ok(f"/attempts/{claim['attempt_id']}/reservations", {
        "lease_token": claim["lease_token"], "operation_id": "op-1", "budget_kind": "agent",
    })
    runner.event(claim, 2, "step_dispatching", {"operation_id": "op-1"}, step_id="step-1")
    task = runner.ok(f"/attempts/{claim['attempt_id']}/tasks", {
        "lease_token": claim["lease_token"], "integration_id": integration["id"], "task_id": "task-1",
        "invocation_id": "inv-1", "operation_id": "op-1",
    })
    return runner, claim, integration, task


def callback(world, integration, payload, *, bad=False):
    raw = json.dumps(payload).encode()
    stamp = int(time.time())
    return world["owner"].send("POST", f"/callbacks/runtime/{integration['id']}", content=raw, headers={
        "Content-Type": "application/json", "X-Susanoo-Timestamp": str(stamp),
        "X-Susanoo-Signature": "v1=" + sign("wrong" if bad else "synthetic-callback-key", stamp, raw),
    })


def test_persistent_relay_is_correlated_ordered_idempotent_and_evidence_only(world, monkeypatch):
    runner, claim, integration, task = relay(world, monkeypatch)
    def send(event, sequence, status="running", **extra):
        return callback(world, integration, {
            "event_id": event, "task_id": "task-1", "sequence": sequence, "status": status, **extra,
        })
    assert send("e1", 2, result={"count": 1, "password": "must-not-store"}).json()["disposition"] == "accepted"
    assert send("e1", 2, result={"count": 1, "password": "must-not-store"}).json()["disposition"] == "duplicate"
    assert send("old", 1).json()["disposition"] == "out_of_order"
    assert send("done", 3, "completed").json()["disposition"] == "accepted"
    assert send("late", 4).json()["disposition"] == "after_terminal"
    assert send("e1", 7).status_code == 409
    with transaction() as db:
        db.execute("update platform.attempts set lease_expires_at=now()-interval '1 second' where id=%s",
                   (claim["attempt_id"],))
    received = runner.ok(f"/attempts/{claim['attempt_id']}/tasks/{task['id']}/poll", {
        "lease_token": claim["lease_token"], "after": 0,
    })
    assert [item["event_id"] for item in received["events"]] == ["e1", "done"]
    assert received["events"][0]["result"] == {"count": 1}
    assert not received["dispatch_authority"]
    assert "must-not-store" not in str(received)
    assert runner.ok(f"/attempts/{claim['attempt_id']}/tasks/{task['id']}/poll", {
        "lease_token": claim["lease_token"], "after": received["cursor"],
    })["events"] == []
    with transaction() as db:
        assert db.execute("select count(*) n from platform.runs").fetchone()["n"] == 1


def test_unknown_bad_signature_and_unpinned_tasks_are_rejected(world, monkeypatch):
    runner, claim, integration, _ = relay(world, monkeypatch)
    payload = {"event_id": "e", "task_id": "unknown", "status": "completed", "sequence": 1}
    assert callback(world, integration, payload).status_code == 404
    assert callback(world, integration, payload, bad=True).status_code == 401
    other = world["owner"].ok("POST", f"/projects/{world['project']['id']}/integrations", {
        "provider": "webhook", "secret_ref": "TEST_CALLBACK_KEY",
        "relay": {"public_base_url": "https://callbacks.example.test"},
    })
    assert runner.send(f"/attempts/{claim['attempt_id']}/tasks", {
        "lease_token": claim["lease_token"], "integration_id": other["id"], "task_id": "task-2",
        "invocation_id": "inv-2", "operation_id": "op-1",
    }).status_code == 404


def test_twilio_signature_uses_configured_public_url_and_never_grades_call_completion(world, monkeypatch):
    runner, claim, integration, task = relay(world, monkeypatch, "media")
    path = f"/api/v1/callbacks/runtime/{integration['id']}"
    params = {"CallSid": "task-1", "CallStatus": "completed", "SequenceNumber": "1", "From": "+15551234567"}
    raw = urlencode(params).encode()
    signature = compute_signature("synthetic-callback-key", "https://callbacks.example.test" + path, params)
    response = world["owner"].client.post(path, content=raw, headers={
        "Content-Type": "application/x-www-form-urlencoded", "X-Twilio-Signature": signature,
    })
    assert response.status_code == 200, response.text
    assert response.json()["task_outcome"] == "not_established"
    wrong = compute_signature("synthetic-callback-key", "https://attacker.example" + path, params)
    assert world["owner"].client.post(path, content=raw, headers={
        "Content-Type": "application/x-www-form-urlencoded", "X-Twilio-Signature": wrong,
    }).status_code == 403
    events = runner.ok(
        f"/attempts/{claim['attempt_id']}/tasks/{task['id']}/poll", {"lease_token": claim["lease_token"]}
    )
    assert "15551234567" not in str(events)


def otlp():
    return {"resourceSpans": [{"scopeSpans": [{"spans": [{
        "traceId": "1" * 32, "spanId": "2" * 16, "name": "agent.respond",
        "startTimeUnixNano": "1700000000000000000", "endTimeUnixNano": "1700000001000000000",
        "attributes": [{"key": "gen_ai.prompt", "value": {"stringValue": "private conversation"}}],
    }]}]}]}


@pytest.mark.parametrize("encoding", ["identity", "gzip"])
def test_collector_tokens_are_scoped_hashed_revocable_and_dedupe_across_requests(world, encoding):
    project = world["project"]["id"]
    issued = world["owner"].ok("POST", f"/projects/{project}/collector-credentials", {})
    raw = json.dumps(otlp()).encode()
    if encoding == "gzip":
        raw = gzip.compress(raw)
    headers = {"Authorization": "Bearer " + issued["token"], "Content-Type": "application/json",
               "Content-Encoding": encoding}
    for _ in range(2):
        response = world["owner"].send("POST", "/v1/traces", content=raw, headers=headers)
        assert response.status_code == 200, response.text
    with transaction() as db:
        rows = db.execute("select * from platform.passive_records").fetchall()
        assert len(rows) == 1
        assert "private conversation" not in str(rows)
        assert db.execute("select token_hash from platform.credentials where id=%s",
                          (issued["credential_id"],)).fetchone()["token_hash"] != issued["token"]
    headers["X-Susanoo-Project"] = "other-project"
    assert world["owner"].send("POST", "/v1/traces", content=raw, headers=headers).status_code == 401
    del headers["X-Susanoo-Project"]
    world["owner"].ok("POST", f"/credentials/{issued['credential_id']}/revoke")
    assert world["owner"].send("POST", "/v1/traces", content=raw, headers=headers).status_code == 401
