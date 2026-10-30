import hmac
import json
from datetime import UTC, datetime, timedelta

from conftest import ProtocolRunner, approve, version

from susanoo_api.platform import source_access
from susanoo_api.platform.database import transaction


def test_global_app_webhook_revokes_installation_without_repository(world, monkeypatch):
    monkeypatch.setenv("PLATFORM_GITHUB_WEBHOOK_SECRET", "synthetic-app-webhook-secret")
    integration = world["owner"].ok(
        "POST",
        f"/projects/{world['project']['id']}/integrations",
        {
            "provider": "github",
            "repository": "synthetic/agent",
            "installation_id": 101,
            "secret_ref": "PLATFORM_GITHUB_WEBHOOK_SECRET",
        },
    )
    raw = json.dumps({"installation": {"id": 101}, "action": "deleted"}).encode()
    response = world["owner"].client.post(
        "/api/v1/callbacks/github/app",
        content=raw,
        headers={
            "X-GitHub-Event": "installation",
            "X-GitHub-Delivery": "synthetic-uninstall",
            "X-Hub-Signature-256": "sha256=" + hmac.new(b"synthetic-app-webhook-secret", raw, "sha256").hexdigest(),
        },
    )
    assert response.status_code == 202, response.text
    assert response.json()["routes_processed"] == 1
    with transaction() as db:
        assert db.execute("select revoked_at from platform.integrations where id=%s", (integration["id"],)).fetchone()[
            "revoked_at"
        ]


def test_source_read_grant_is_pinned_encrypted_and_revoked_after_lease(world, monkeypatch):
    calls = []

    class FakeApp:
        def installation_token_info(self, installation_id, repository, write=False):
            calls.append((installation_id, repository, write))
            return {
                "token": "synthetic-read-only-token-not-a-real-credential",
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            }

        def revoke_token(self, token):
            calls.append("revoked")

        def close(self):
            pass

    monkeypatch.setattr(source_access, "GitHubApp", FakeApp)
    source = version(
        world,
        {
            "kind": "source",
            "repository": "synthetic/agent",
            "installation_id": 101,
            "commit": "a" * 40,
            "recipe_hash": "b" * 64,
            "lockfile_hash": "c" * 64,
            "trusted": True,
        },
    )
    approve(world, source)
    connection = version(world, {"kind": "connection", "family": "I01", "adapter": "protocol_test_only"})
    world["owner"].ok(
        "POST",
        f"/projects/{world['project']['id']}/integrations",
        {
            "provider": "github",
            "repository": "synthetic/agent",
            "installation_id": 101,
            "secret_ref": "TEST_APP_SECRET",
        },
    )
    world["owner"].ok(
        "POST",
        f"/projects/{world['project']['id']}/jobs",
        {
            "kind": "build",
            "version_ids": [source["id"], connection["id"]],
        },
        headers={"Idempotency-Key": "synthetic-build"},
    )
    runner = ProtocolRunner(world)
    claim = runner.claim("build")
    body = {"lease_token": claim["lease_token"], "source_version_id": source["id"]}
    reply = runner.ok(f"/attempts/{claim['attempt_id']}/source-credential", body)
    assert reply["read_only"] and reply["source_commit"] == "a" * 40
    assert calls == [(101, "synthetic/agent", False)]
    with transaction() as db:
        stored = db.execute("select token_cipher from platform.source_grants").fetchone()["token_cipher"]
        assert reply["token"] not in stored
        db.execute(
            "update platform.attempts set lease_expires_at=now()-interval '1 second' where id=%s",
            (claim["attempt_id"],),
        )
    source_access.revoke_settled_grants()
    assert calls[-1] == "revoked"
    with transaction() as db:
        grant = db.execute("select state,token_cipher from platform.source_grants").fetchone()
        assert grant == {"state": "revoked", "token_cipher": ""}
