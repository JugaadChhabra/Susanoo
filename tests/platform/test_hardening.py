import hashlib
import hmac
import json
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import psycopg
import pytest
from conftest import PASSWORD, Browser, ProtocolRunner, launch, result, version
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from susanoo_contracts import PatchBundle

from susanoo_api.platform import jobs, recovery
from susanoo_api.platform.database import PlatformError, transaction
from susanoo_api.platform.github_app import GitHubApp, patch_policy
from susanoo_api.platform.mail import mail_cipher


def test_invitation_single_use_and_membership_revocation(world, monkeypatch):
    monkeypatch.setenv("PLATFORM_SMTP_HOST", "synthetic.invalid")
    world["owner"].ok(
        "POST",
        f"/workspaces/{world['workspace']['id']}/invitations",
        {
            "email": "outsider@test.local",
            "roles": ["engineer"],
            "project_id": world["project"]["id"],
        },
    )
    with transaction() as db:
        body = (
            mail_cipher()
            .decrypt(db.execute("select body from platform.mail_outbox").fetchone()["body"].encode())
            .decode()
        )
        token = body.split("#token=", 1)[1].strip()
    anonymous = Browser(world["app"])
    assert (
        anonymous.send(
            "POST", "/invitations/accept", {"token": token, "password": "incorrect-existing-password"}
        ).status_code
        == 401
    )
    assert anonymous.ok("POST", "/invitations/accept", {"token": token, "password": PASSWORD})["accepted"]
    assert anonymous.send("POST", "/invitations/accept", {"token": token, "password": PASSWORD}).status_code == 401
    outsider = world["browsers"]["outsider"]
    cli = outsider.ok("POST", f"/projects/{world['project']['id']}/cli-credentials")["token"]
    world["owner"].ok("DELETE", f"/workspaces/{world['workspace']['id']}/members/{world['users']['outsider']}")
    assert outsider.send("GET", f"/projects/{world['project']['id']}").status_code == 404
    assert (
        outsider.send(
            "GET", f"/projects/{world['project']['id']}", headers={"Authorization": "Bearer " + cli}
        ).status_code
        == 401
    )
    assert (
        world["owner"]
        .send("DELETE", f"/workspaces/{world['workspace']['id']}/members/{world['users']['owner']}")
        .status_code
        == 409
    )


def test_upload_rechecks_deletion_after_object_io(world, monkeypatch):
    from susanoo_api.platform.artifacts import ObjectStore

    content = b"synthetic-upload-race"
    row = world["owner"].ok(
        "POST",
        f"/projects/{world['project']['id']}/artifacts",
        {
            "content_type": "text/plain",
            "digest": hashlib.sha256(content).hexdigest(),
            "size": len(content),
        },
    )
    original = ObjectStore.put
    keys = []

    def put(store, key, data, content_type):
        original(store, key, data, content_type)
        keys.append(key)
        world["owner"].ok("DELETE", f"/projects/{world['project']['id']}")

    monkeypatch.setattr(ObjectStore, "put", put)
    client = world["owner"].client
    csrf = client.get("/api/v1/auth/csrf").json()["csrf_token"]
    response = client.put(
        f"/api/v1/artifacts/{row['id']}/content",
        content=content,
        headers={"Origin": "http://localhost:3000", "X-CSRF-Token": csrf},
    )
    assert response.status_code == 404
    assert keys and not ObjectStore().path(keys[0]).exists()


def test_registration_single_use_and_revoke(world):
    runner = ProtocolRunner(world)
    assert world["owner"].send("POST", "/runner/register", runner.register_body).status_code == 401
    with transaction() as db:
        runner_id = db.execute("select id from platform.runners").fetchone()["id"]
    world["owner"].ok("POST", f"/runners/{runner_id}/revoke")
    assert runner.send("/claims", {"supported_job_kinds": ["preflight"]}).status_code == 401


def test_project_role_override_is_effective(world):
    world["owner"].ok(
        "POST",
        f"/workspaces/{world['workspace']['id']}/members",
        {
            "user_id": str(world["users"]["engineer"]),
            "project_id": world["project"]["id"],
            "roles": ["viewer"],
        },
    )
    assert world["browsers"]["engineer"].ok("GET", f"/projects/{world['project']['id']}")["roles"] == ["viewer"]
    assert (
        world["browsers"]["engineer"]
        .send("POST", f"/projects/{world['project']['id']}/runner-registration")
        .status_code
        == 403
    )


def test_reset_encrypted_single_use_and_session_revocation(world, monkeypatch):
    monkeypatch.setenv("PLATFORM_SMTP_HOST", "synthetic.invalid")
    world["owner"].ok("POST", "/auth/password-reset", {"email": "owner@test.local"})
    with transaction() as db:
        stored = db.execute("select body from platform.mail_outbox").fetchone()["body"]
        assert "#token=" not in stored and "http" not in stored
        body = mail_cipher().decrypt(stored.encode()).decode()
        token = body.split("#token=", 1)[1].strip()
    anonymous = Browser(world["app"])
    assert anonymous.ok("POST", "/auth/password-reset/confirm", {"token": token, "password": "new-synthetic-password"})[
        "reset"
    ]
    assert world["owner"].send("GET", "/me").status_code == 401
    assert (
        anonymous.send(
            "POST", "/auth/password-reset/confirm", {"token": token, "password": "new-synthetic-password"}
        ).status_code
        == 401
    )
    with transaction() as db:
        assert db.execute("select body from platform.mail_outbox").fetchone()["body"] == ""


def test_signed_callback_dedupe_and_repository_boundary(world, monkeypatch):
    monkeypatch.setenv("TEST_CALLBACK_SECRET", "synthetic-signature-secret")
    row = world["owner"].ok(
        "POST",
        f"/projects/{world['project']['id']}/integrations",
        {
            "provider": "github",
            "installation_id": 101,
            "repository": "synthetic/agent",
            "secret_ref": "TEST_CALLBACK_SECRET",
        },
    )
    payload = {
        "installation": {"id": 101},
        "repository": {"full_name": "synthetic/agent"},
        "ref": "refs/heads/main",
        "after": "a" * 40,
    }

    def send(data, signature=True):
        raw = json.dumps(data).encode()
        return world["owner"].client.post(
            f"/api/v1/callbacks/{row['id']}",
            content=raw,
            headers={
                "X-GitHub-Delivery": "synthetic-delivery",
                "X-GitHub-Event": "push",
                "X-Hub-Signature-256": "sha256="
                + (hmac.new(b"synthetic-signature-secret", raw, "sha256").hexdigest() if signature else "bad"),
            },
        )

    assert send(payload, False).status_code == 403
    assert send(payload).status_code == 202
    assert send(payload).json()["duplicate"]
    assert send({**payload, "after": "b" * 40}).status_code == 409
    assert send({**payload, "installation": {"id": 102}}).status_code == 403
    assert (
        world["browsers"]["outsider"].send("GET", f"/projects/{world['project']['id']}/integrations").status_code == 404
    )


def test_concurrency_and_run_deadline_are_server_enforced(suite):
    suite["body"]["repetitions"] = 2
    run = launch(suite)
    first = suite["runner"].claim("outcome_suite")
    assert first
    assert suite["runner"].send("/claims", {"supported_job_kinds": ["outcome_suite"]}).status_code == 204
    with transaction() as db:
        db.execute("update platform.runs set created_at=now()-interval '3 hours' where id=%s", (run["id"],))
    jobs.tick()
    assert suite["runner"].send("/claims", {"supported_job_kinds": ["outcome_suite"]}).status_code == 204
    with transaction() as db:
        assert (
            db.execute(
                "select count(*) n from platform.attempts where run_id=%s and execution_state='timed_out'", (run["id"],)
            ).fetchone()["n"]
            == 1
        )


def test_dispatch_requires_budget_and_cannot_repeat(suite):
    launch(suite)
    claim = suite["runner"].claim("outcome_suite")
    runner = suite["runner"]
    runner.event(
        claim,
        1,
        "step_prepared",
        {"request_digest": "a" * 64, "operation_id": "operation-one", "action_kind": "agent"},
        step_id="one",
    )
    event = {
        "event_id": str(uuid4()),
        "sequence": 2,
        "type": "step_dispatching",
        "step_id": "one",
        "occurred_at": datetime.now(UTC).isoformat(),
        "payload": {"operation_id": "operation-one"},
    }
    body = {"lease_token": claim["lease_token"], "events": [event]}
    assert runner.send(f"/attempts/{claim['attempt_id']}/events", body).status_code == 409
    runner.ok(
        f"/attempts/{claim['attempt_id']}/reservations",
        {"lease_token": claim["lease_token"], "operation_id": "operation-one", "budget_kind": "agent"},
    )
    assert runner.send(f"/attempts/{claim['attempt_id']}/events", body).status_code == 200
    event.update(event_id=str(uuid4()), sequence=3)
    assert runner.send(f"/attempts/{claim['attempt_id']}/events", body).status_code == 409


def test_runner_artifact_scope_and_late_reconciliation(suite):
    launch(suite)
    claim = suite["runner"].claim("outcome_suite")
    runner = suite["runner"]
    content = b'{"synthetic":true}'
    record = runner.ok(
        f"/attempts/{claim['attempt_id']}/artifacts",
        {
            "lease_token": claim["lease_token"],
            "artifact": {
                "content_type": "application/json",
                "size": len(content),
                "digest": hashlib.sha256(content).hexdigest(),
            },
        },
    )
    response = suite["owner"].client.put(
        f"/api/v1/runner/attempts/{claim['attempt_id']}/artifacts/{record['id']}/content",
        content=content,
        headers={"Authorization": "Bearer " + runner.token, "X-Lease-Token": claim["lease_token"]},
    )
    assert response.status_code == 200, response.text
    with transaction() as db:
        db.execute(
            "update platform.attempts set lease_expires_at=now()-interval '1 second' where id=%s",
            (claim["attempt_id"],),
        )
    jobs.tick()
    reconciled = runner.ok(
        f"/attempts/{claim['attempt_id']}/reconciliations",
        {
            "lease_token": claim["lease_token"],
            "state": "resolved",
            "rationale": "Synthetic read-back found no uncertain side effect",
        },
    )
    assert reconciled["dispatch_authority"] is False
    assert (
        runner.send(f"/attempts/{claim['attempt_id']}/heartbeat", {"lease_token": claim["lease_token"]}).status_code
        == 409
    )


def test_policy_change_revokes_old_exports_and_retention_is_explicit(suite):
    run = launch(suite)
    claim = suite["runner"].claim("outcome_suite")
    assert result(suite, claim)[0].is_success
    suite["runner"].event(claim, 1, "progress", {"code": "done"})
    suite["runner"].finalize(claim)
    artifact = suite["owner"].ok("POST", f"/runs/{run['id']}/exports", {"format": "json"})
    jobs.process_one()
    suite["owner"].ok("POST", f"/projects/{suite['project']['id']}/policy", {"upload_fields": []})
    assert suite["owner"].send("GET", f"/artifacts/{artifact['id']}/download").status_code == 409
    with transaction() as db:
        db.execute("update platform.runs set created_at=now()-interval '31 days' where id=%s", (run["id"],))
    jobs.expire_evidence()
    exported = suite["owner"].ok("POST", f"/runs/{run['id']}/exports", {"format": "json"})
    jobs.process_one()
    body = suite["owner"].send("GET", f"/artifacts/{exported['id']}/download").json()
    assert body["retention"] and body["evidence"] == []
    assert body["run"]["gate"] == "READY_FOR_ACCEPTANCE"


@pytest.mark.parametrize("path", ["../agent.py", "agent.py?ref=other", "src//a.py", "/src/a.py", "src/.git/hooks/run"])
def test_patch_paths_reject_provider_url_and_traversal_ambiguity(path):
    with pytest.raises(PlatformError):
        patch_policy(
            PatchBundle(base_commit="a" * 40, files=[{"path": path, "before": "one", "after": "two"}]),
            ["src/", "agent.py"],
        )


def test_uncertain_pr_reconciles_before_checking_moved_base():
    marker = "<!-- susanoo-publication:stable -->"

    def handler(request):
        if request.url.path.endswith("/pulls"):
            return httpx.Response(200, json=[{"body": marker, "html_url": "https://github.com/synthetic/agent/pull/1"}])
        assert request.url.path == "/repos/synthetic/agent"
        return httpx.Response(200, json={"default_branch": "main"})

    client = GitHubApp(httpx.MockTransport(handler))
    try:
        url = client.draft_pr(
            "synthetic/agent",
            "synthetic-token",
            {"id": uuid4(), "publication_key": "stable", "base_commit": "a" * 40},
            PatchBundle(base_commit="a" * 40, files=[{"path": "agent.py", "before": "one", "after": "two"}]),
            ["agent.py"],
        )
        assert url.endswith("/pull/1")
    finally:
        client.close()


def test_backup_restore_reapplies_newer_deletion_and_revokes_credentials(world, tmp_path, monkeypatch):
    version(
        world,
        {"kind": "requirement", "source_clause": "r", "expected": "delete-on-restore-canary", "owner": "synthetic"},
    )
    destination = tmp_path / "snapshot.susanoobak"
    recovery.backup(destination)
    world["owner"].ok("DELETE", f"/projects/{world['project']['id']}")
    config = conninfo_to_dict(os.environ["PLATFORM_DATABASE_URL"])
    target_name = "susanoo_test_restore_" + uuid4().hex
    admin_url = make_conninfo(**{**config, "dbname": "postgres"})
    with psycopg.connect(admin_url, autocommit=True) as db:
        db.execute(sql.SQL("create database {}").format(sql.Identifier(target_name)))
    target_url = make_conninfo(**{**config, "dbname": target_name})
    try:
        retention = tmp_path / "backup-retention"
        retention.mkdir()
        expired_copy = retention / "expired.susanoobak"
        expired_copy.write_bytes(destination.read_bytes())
        invalid = retention / "unrecognized.susanoobak"
        invalid.write_bytes(b"not an authenticated Susanoo backup")
        assert recovery.expire_backups(retention)["expired_backups_removed"] == 0

        class Later(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(tz) + timedelta(days=31)

        with monkeypatch.context() as clock:
            clock.setattr(recovery, "datetime", Later)
            with pytest.raises(PlatformError) as error:
                recovery.restore(destination, target_url, target_name, recovery.journal_path())
            assert error.value.code == "EXPIRED_BACKUP"
            assert recovery.expire_backups(retention)["expired_backups_removed"] == 1
            assert not expired_copy.exists() and invalid.exists()
        assert destination.exists()
        monkeypatch.setenv("PLATFORM_ARTIFACT_DIR", str(tmp_path / "restored-objects"))
        report = recovery.restore(destination, target_url, target_name, recovery.journal_path())
        assert report["deletions_reapplied"] == 1
        with psycopg.connect(target_url) as db:
            assert db.execute("select count(*) from platform.versions").fetchone()[0] == 0
            assert db.execute("select count(*) from platform.credentials where revoked_at is null").fetchone()[0] == 0
            assert db.execute("select purged_at is not null from platform.deletion_tombstones").fetchone()[0]
    finally:
        with psycopg.connect(admin_url, autocommit=True) as db:
            db.execute(sql.SQL("drop database {} with (force)").format(sql.Identifier(target_name)))
