import io
import json
import os
from pathlib import Path
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from xml.etree.ElementTree import fromstring
from zipfile import ZipFile

import psycopg
import pytest
from conftest import Browser, approve, launch, result, version
from pypdf import PdfReader

from susanoo_api.platform import jobs
from susanoo_api.platform.database import transaction


def test_auth_csrf_and_roles(world):
    anonymous = Browser(world["app"])
    assert anonymous.send("GET", "/me").status_code == 401
    response = anonymous.client.post("/api/v1/auth/login", json={"email": "owner@test.local", "password": "wrong"})
    assert response.status_code == 403
    project_id = world["project"]["id"]
    assert world["browsers"]["outsider"].send("GET", f"/projects/{project_id}").status_code == 404
    response = world["browsers"]["viewer"].send(
        "POST",
        f"/projects/{project_id}/versions",
        {
            "logical_id": "no",
            "title": "No",
            "data": {"kind": "requirement", "source_clause": "s", "expected": "x", "owner": "x"},
        },
    )
    assert response.status_code == 403


def test_immutable_versions_approval_and_separate_reviewer(world):
    row = version(world, {"kind": "requirement", "source_clause": "s", "expected": "one", "owner": "policy"}, "R1")
    next_row = version(world, {"kind": "requirement", "source_clause": "s", "expected": "two", "owner": "policy"}, "R1")
    assert next_row["revision"] == 2
    assert next_row["content_hash"] != row["content_hash"]
    assert (
        world["owner"]
        .send(
            "POST",
            f"/versions/{row['id']}/approve",
            {"content_hash": row["content_hash"], "rationale": "self approval"},
        )
        .status_code
        == 403
    )
    assert (
        world["reviewer"]
        .send("POST", f"/versions/{row['id']}/approve", {"content_hash": "a" * 64, "rationale": "wrong version"})
        .status_code
        == 409
    )
    approve(world, row)
    assert world["owner"].ok("GET", f"/versions/{row['id']}")["approved"]
    with pytest.raises(psycopg.errors.CheckViolation), transaction() as db:
        db.execute("update platform.versions set title='rewrite' where id=%s", (row["id"],))


def test_hidden_grant_is_independent_of_engineer_role(world):
    response = world["owner"].send(
        "POST",
        f"/projects/{world['project']['id']}/versions",
        {
            "logical_id": "hidden",
            "title": "Secret",
            "hidden": True,
            "data": {
                "kind": "requirement",
                "source_clause": "s",
                "expected": "hidden-answer-canary",
                "owner": "policy",
            },
        },
    )
    assert response.status_code == 403
    world["owner"].ok(
        "POST",
        f"/workspaces/{world['workspace']['id']}/members",
        {
            "user_id": str(world["users"]["owner"]),
            "project_id": world["project"]["id"],
            "roles": ["engineer"],
            "hidden_access": True,
        },
    )
    row = version(
        world,
        {"kind": "requirement", "source_clause": "s", "expected": "hidden-answer-canary", "owner": "policy"},
        hidden=True,
    )
    assert world["browsers"]["engineer"].send("GET", f"/versions/{row['id']}").status_code == 404
    listing = world["browsers"]["engineer"].ok("GET", f"/projects/{world['project']['id']}/versions")
    assert "hidden-answer-canary" not in json.dumps(listing)


def test_launch_idempotency_and_conflict(suite):
    first = launch(suite)
    assert launch(suite)["id"] == first["id"]
    changed = {**suite["body"], "repetitions": 2}
    assert (
        suite["owner"]
        .send("POST", f"/projects/{suite['project']['id']}/runs", changed, headers={"Idempotency-Key": "launch"})
        .status_code
        == 409
    )


@pytest.mark.parametrize(
    ("verdict", "gate"), [("PASS", "READY_FOR_ACCEPTANCE"), ("FAIL", "BLOCKED"), ("UNKNOWN", "NEEDS_REVIEW")]
)
def test_runner_evidence_finalization_projects_reported_verdicts(suite, verdict, gate):
    run = launch(suite)
    claim = suite["runner"].claim("outcome_suite")
    assert "checks" not in claim["agent_context"]
    assert "expected" not in json.dumps(claim["agent_context"])
    assert claim["evaluator_context"]["checks"]
    response, body = result(suite, claim, verdict)
    assert response.status_code == 200, response.text
    suite["runner"].event(claim, 1, "progress", {"code": "evidence_ready"})
    assert suite["runner"].finalize(claim, verdict)["gate"] == gate
    assert suite["runner"].finalize(claim, verdict)["gate"] == gate
    stored = suite["owner"].ok("GET", f"/runs/{run['id']}")
    assert stored["gate"] == gate


@pytest.mark.parametrize(("availability", "completeness"), [("unavailable", "unknown"), ("available", "partial")])
def test_incomplete_observation_cannot_be_reported_pass(suite, availability, completeness):
    launch(suite)
    claim = suite["runner"].claim("outcome_suite")
    response, _ = result(suite, claim, "PASS", availability, completeness)
    assert response.status_code == 422


def test_event_dedupe_conflicts_and_sequence_gaps(suite):
    launch(suite)
    claim = suite["runner"].claim("outcome_suite")
    runner = suite["runner"]
    _, event = runner.event(claim, 2, "progress", {"code": "prepared"})
    payload = {"lease_token": claim["lease_token"], "events": [event]}
    assert runner.ok(f"/attempts/{claim['attempt_id']}/events", payload)["duplicates"] == [event["event_id"]]
    event["payload"]["code"] = "conflicting"
    assert runner.send(f"/attempts/{claim['attempt_id']}/events", payload).status_code == 409
    response, _ = result(suite, claim)
    assert response.is_success
    assert (
        runner.send(
            f"/attempts/{claim['attempt_id']}/finalize",
            {
                "lease_token": claim["lease_token"],
                "execution_state": "completed",
                "business_verdict": "PASS",
                "final_sequence": 2,
                "cleanup_state": "succeeded",
                "reconciliation_state": "not_needed",
            },
        ).status_code
        == 409
    )


def test_cancel_stops_dispatch_after_acknowledgement(suite):
    run = launch(suite)
    claim = suite["runner"].claim("outcome_suite")
    suite["owner"].ok("POST", f"/runs/{run['id']}/cancel")
    control = suite["runner"].ok(f"/attempts/{claim['attempt_id']}/control", {"lease_token": claim["lease_token"]})
    assert control["cancel_requested"]
    suite["runner"].event(claim, 1, "cancel_acknowledged")
    response = suite["runner"].send(
        f"/attempts/{claim['attempt_id']}/events",
        {
            "lease_token": claim["lease_token"],
            "events": [
                {
                    "event_id": str(uuid4()),
                    "sequence": 2,
                    "type": "step_dispatching",
                    "step_id": "write",
                    "occurred_at": datetime.now(UTC).isoformat(),
                    "payload": {},
                }
            ],
        },
    )
    assert response.status_code == 409


def test_expired_attempt_never_reclaimed(suite):
    run = launch(suite)
    claim = suite["runner"].claim("outcome_suite")
    with transaction() as db:
        db.execute(
            "update platform.attempts set lease_expires_at=now()-interval '1 second' where id=%s",
            (claim["attempt_id"],),
        )
    jobs.tick()
    assert suite["runner"].send("/claims", {"supported_job_kinds": ["outcome_suite"]}).status_code == 204
    stored = suite["owner"].ok("GET", f"/runs/{run['id']}")
    assert stored["gate"] == "BLOCKED"
    assert stored["attempts"][0]["reconciliation_state"] == "pending"


def test_budget_reservation_atomic_and_idempotent(suite):
    launch(suite)
    claim = suite["runner"].claim("outcome_suite")
    url = f"/attempts/{claim['attempt_id']}/reservations"
    body = {"lease_token": claim["lease_token"], "operation_id": "one", "budget_kind": "agent", "amount": 160}
    assert suite["runner"].ok(url, body)["remaining"] == 0
    assert suite["runner"].ok(url, body)["duplicate"]
    body["operation_id"] = "two"
    body["amount"] = 1
    assert suite["runner"].send(url, body).status_code == 409


def test_waiver_preserves_verdict_and_four_exports_agree(suite):
    run = launch(suite)
    claim = suite["runner"].claim("outcome_suite")
    assert result(suite, claim, "FAIL")[0].is_success
    suite["runner"].event(claim, 1, "progress", {"code": "done"})
    suite["runner"].finalize(claim, "FAIL")
    finding = suite["owner"].ok("GET", f"/projects/{suite['project']['id']}/findings")["items"][0]
    suite["reviewer"].ok(
        "POST",
        f"/findings/{finding['id']}/waivers",
        {"rationale": "Synthetic review exception", "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()},
    )
    assert suite["owner"].ok("GET", f"/runs/{run['id']}")["gate"] == "BLOCKED"
    exports = {}
    snapshot_id = None
    for format in ("json", "pdf", "junit", "bundle"):
        record = suite["owner"].ok(
            "POST",
            f"/runs/{run['id']}/exports",
            {
                "format": format,
                "snapshot_artifact_id": snapshot_id,
            },
        )
        snapshot_id = snapshot_id or record["id"]
        assert jobs.process_one()
        response = suite["owner"].send("GET", f"/artifacts/{record['id']}/download")
        assert response.status_code == 200, response.text
        exports[format] = response.content
    assert json.loads(exports["json"])["run"]["gate"] == "BLOCKED"
    assert fromstring(exports["junit"]).attrib["failures"] == "1"
    snapshot_hash = json.loads(exports["json"])["snapshot_hash"]
    assert fromstring(exports["junit"]).attrib["snapshot_hash"] == snapshot_hash
    assert "BLOCKED" in "".join(page.extract_text() for page in PdfReader(io.BytesIO(exports["pdf"])).pages)
    with ZipFile(io.BytesIO(exports["bundle"])) as bundle:
        assert json.loads(bundle.read("result.json"))["run"]["gate"] == "BLOCKED"
        assert json.loads(bundle.read("result.json"))["snapshot_hash"] == snapshot_hash
    if os.environ.get("PLATFORM_CAPTURE_EXPORT"):
        Path(os.environ["PLATFORM_CAPTURE_EXPORT"]).write_bytes(exports["pdf"])


def test_delete_revokes_access_and_purges_payloads(world):
    version(world, {"kind": "requirement", "source_clause": "s", "expected": "deletion-canary", "owner": "policy"})
    project_id = world["project"]["id"]
    world["owner"].ok("DELETE", f"/projects/{project_id}")
    assert world["owner"].send("GET", f"/projects/{project_id}").status_code == 404
    jobs.purge_deleted()
    with transaction() as db:
        assert (
            db.execute("select count(*) n from platform.versions where project_id=%s", (project_id,)).fetchone()["n"]
            == 0
        )
        assert db.execute(
            "select purged_at from platform.deletion_tombstones where project_id=%s", (project_id,)
        ).fetchone()["purged_at"]


def test_revoked_membership_blocks_existing_artifact_download(suite):
    run = launch(suite)
    claim = suite["runner"].claim("outcome_suite")
    result(suite, claim)
    suite["runner"].event(claim, 1, "progress", {"code": "done"})
    suite["runner"].finalize(claim)
    artifact = suite["browsers"]["viewer"].ok("POST", f"/runs/{run['id']}/exports", {"format": "json"})
    jobs.process_one()
    with transaction() as db:
        db.execute(
            "delete from platform.memberships where workspace_id=%s and user_id=%s",
            (suite["workspace"]["id"], suite["users"]["viewer"]),
        )
    assert suite["browsers"]["viewer"].send("GET", f"/artifacts/{artifact['id']}/download").status_code == 404


def test_cli_credential_cannot_escape_project_or_administer_workspace(world):
    token = world["owner"].ok("POST", f"/projects/{world['project']['id']}/cli-credentials")["token"]
    headers = {"Authorization": "Bearer " + token}
    assert world["owner"].send("POST", "/workspaces", {"name": "escape"}, headers=headers).status_code == 403
    assert world["owner"].send("GET", f"/projects/{uuid4()}", headers=headers).status_code == 404


def test_database_rejects_cross_workspace_version_links(world):
    a = version(world, {"kind": "requirement", "source_clause": "a", "expected": "a", "owner": "a"})
    outsider = world["browsers"]["outsider"]
    workspace = outsider.ok("POST", "/workspaces", {"name": "Other"})
    project = outsider.ok("POST", "/projects", {"workspace_id": workspace["id"], "name": "Other", "workflow": "other"})
    b = outsider.ok(
        "POST",
        f"/projects/{project['id']}/versions",
        {
            "logical_id": "B",
            "title": "B",
            "data": {"kind": "requirement", "source_clause": "b", "expected": "b", "owner": "b"},
        },
    )
    with pytest.raises(psycopg.errors.ForeignKeyViolation), transaction() as db:
        db.execute(
            "insert into platform.version_links values(%s,%s,%s,%s,'declared_dependency')",
            (world["workspace"]["id"], world["project"]["id"], a["id"], b["id"]),
        )


def test_change_invalidation_preserves_old_versions(world):
    a = version(world, {"kind": "requirement", "source_clause": "a", "expected": "a", "owner": "a"})
    b = version(world, {"kind": "requirement", "source_clause": "b", "expected": "b", "owner": "b"})
    world["owner"].ok(
        "POST",
        f"/projects/{world['project']['id']}/dependencies",
        {"source_version_id": b["id"], "target_version_id": a["id"], "relationship": "declared_dependency"},
    )
    changed = world["owner"].ok(
        "POST",
        f"/projects/{world['project']['id']}/changes",
        {"version_id": a["id"], "reason": "Policy changed", "classification": "retest_recommended"},
    )
    assert set(changed["affected_version_ids"]) == {a["id"], b["id"]}
    assert world["owner"].ok("GET", f"/versions/{a['id']}")["data"]["expected"] == "a"
