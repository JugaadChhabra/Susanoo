import hashlib
import json
import pytest
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from psycopg import sql
from susanoo_contracts import content_hash

from conftest import approve, launch, result, version

from susanoo_api.platform import jobs
from susanoo_api.platform import workflows
from susanoo_api.platform.cli import migrate
from susanoo_api.platform.database import PlatformError, transaction


def failed_run(suite):
    run = launch(suite)
    claim = suite["runner"].claim("outcome_suite")
    assert result(suite, claim, "FAIL")[0].is_success
    suite["runner"].event(claim, 1, "progress", {"code": "synthetic_failure"})
    suite["runner"].finalize(claim, "FAIL")
    return run, suite["owner"].ok("GET", f"/projects/{suite['project']['id']}/findings")["items"][0]


def test_fresh_reproduction_and_environment_compatibility(suite):
    original, finding = failed_run(suite)
    fresh = suite["owner"].ok("POST", f"/findings/{finding['id']}/reproductions", headers={"Idempotency-Key": "fresh"})
    assert fresh["original_run_id"] == original["id"] and fresh["reproduction_outcome"] == "pending"
    claim = suite["runner"].claim("outcome_suite")
    assert result(suite, claim)[0].is_success
    suite["runner"].event(claim, 1, "progress", {"code": "synthetic_reproduction_complete"})
    suite["runner"].finalize(claim)
    assert suite["owner"].ok("GET", f"/runs/{fresh['id']}")["reproduction_outcome"] == "not_reproduced"
    comparison = suite["owner"].ok("POST", "/comparisons", {"baseline_id": original["id"], "candidate_id": fresh["id"]})
    assert comparison["rows"][0]["classification"] == "improvement"
    environment = version(suite, {**suite["environment"]["data"], "fault_profile": {"synthetic_latency": 123}})
    approve(suite, environment)
    suite["body"]["environment_version_id"] = environment["id"]
    changed = launch(suite, "changed-environment")
    comparison = suite["owner"].ok(
        "POST", "/comparisons", {"baseline_id": original["id"], "candidate_id": changed["id"]}
    )
    assert comparison["rows"][0]["classification"] == "not_comparable"


def test_waiver_expiry_changes_acceptance_not_machine_verdict(suite, monkeypatch):
    run, finding = failed_run(suite)
    detail = suite["owner"].ok("GET", f"/findings/{finding['id']}")
    suite["reviewer"].ok(
        "POST",
        f"/findings/{finding['id']}/waivers",
        {
            "rationale": "Synthetic scoped risk acceptance",
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    suite["reviewer"].ok(
        "POST",
        f"/findings/{finding['id']}/reviews",
        {
            "decision": "accept",
            "rationale": "Synthetic review with explicit evidence",
            "evidence_ids": [value["id"] for value in detail["evidence"]],
        },
    )
    assert suite["owner"].ok("GET", f"/findings/{finding['id']}")["acceptance"]["state"] == "accepted_with_waiver"

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(hours=2)

    monkeypatch.setattr(workflows, "datetime", Later)
    assert suite["owner"].ok("GET", f"/findings/{finding['id']}")["acceptance"]["state"] == "pending"
    assert suite["owner"].ok("GET", f"/runs/{run['id']}")["gate"] == "BLOCKED"


def test_default_deny_rls_and_repeat_migrations(world):
    row = version(world, {"kind": "requirement", "source_clause": "u", "expected": "preserved", "owner": "synthetic"})
    migrate()
    migrate()
    assert world["owner"].ok("GET", f"/versions/{row['id']}")["content_hash"] == row["content_hash"]
    role = "susanoo_test_rls_" + uuid4().hex
    with transaction() as db:
        db.execute(sql.SQL("create role {} nologin nosuperuser nobypassrls").format(sql.Identifier(role)))
        db.execute(sql.SQL("grant {} to current_user").format(sql.Identifier(role)))
        db.execute(sql.SQL("grant usage on schema platform to {}").format(sql.Identifier(role)))
        db.execute(sql.SQL("grant select on platform.projects to {}").format(sql.Identifier(role)))
        db.execute(sql.SQL("set local role {}").format(sql.Identifier(role)))
        assert db.execute("select count(*) n from platform.projects").fetchone()["n"] == 0
        db.execute("reset role")
        db.execute(sql.SQL("drop owned by {}").format(sql.Identifier(role)))
        db.execute(sql.SQL("drop role {}").format(sql.Identifier(role)))


@pytest.mark.parametrize("publication", ["recovered", "stale_race", "corrupt_artifact"])
def test_repair_validation_pins_artifact_and_separate_approval(suite, monkeypatch, publication):
    _, finding = failed_run(suite)
    suite["owner"].ok(
        "POST",
        f"/projects/{suite['project']['id']}/policy",
        {
            "upload_fields": ["count"],
            "source_upload_allowed": True,
        },
    )
    source = version(
        suite,
        {
            "kind": "source",
            "repository": "synthetic/agent",
            "commit": "a" * 40,
            "recipe_hash": "b" * 64,
            "lockfile_hash": "c" * 64,
            "trusted": True,
            "allowed_patch_paths": ["agent.py"],
        },
    )
    approve(suite, source)
    patch = json.dumps(
        {"base_commit": "a" * 40, "files": [{"path": "agent.py", "before": "one", "after": "two"}]}
    ).encode()
    artifact = suite["owner"].ok(
        "POST",
        f"/projects/{suite['project']['id']}/artifacts",
        {
            "content_type": "application/json",
            "size": len(patch),
            "digest": hashlib.sha256(patch).hexdigest(),
            "purpose": "patch",
        },
    )
    client = suite["owner"].client
    csrf = client.get("/api/v1/auth/csrf").json()["csrf_token"]
    response = client.put(
        f"/api/v1/artifacts/{artifact['id']}/content",
        content=patch,
        headers={"Origin": "http://localhost:3000", "X-CSRF-Token": csrf},
    )
    assert response.is_success, response.text
    repair = suite["owner"].ok(
        "POST",
        f"/projects/{suite['project']['id']}/repairs",
        {
            "finding_id": finding["id"],
            "source_version_id": source["id"],
            "patch_artifact_id": artifact["id"],
            "layer": "agent",
        },
    )
    approval = {"content_hash": repair["proposal_hash"], "rationale": "Synthetic validation review"}
    assert suite["reviewer"].send("POST", f"/repairs/{repair['id']}/approve", approval).status_code == 409
    suite["owner"].ok("POST", f"/repairs/{repair['id']}/validate", headers={"Idempotency-Key": "validate-patch"})
    runner = suite["runner"]
    claim = runner.claim("repair_retest")
    assert claim["execution_manifest"]["repair"]["patch_artifact_id"] == artifact["id"]
    response = runner.send(
        f"/attempts/{claim['attempt_id']}/artifacts/{artifact['id']}/download", {"lease_token": claim["lease_token"]}
    )
    assert response.content == patch
    typed = {
        "finding_id": finding["id"], "layer": "agent", "origin": "manual_upload", "base_commit": "a" * 40,
        "patch_digest": repair["validation_patch_digest"], "outcome": "accepted", "flags": [],
        "checks": [{"name": name, "passed": True, "detail": "Synthetic protocol assertion"} for name in (
            "reproduction_before_repair", "criteria_not_weakened", "unit_tests", "reduced_reproduction_fixed",
            "valid_controls_preserved", "heldout_matches",
        )],
        "pins": {"agent_commit": "a"*40, "agent_after": "d"*40, "simulator_before": "e"*64,
                 "simulator_after": "e"*64, "evaluator_before": "f"*64, "evaluator_after": "f"*64,
                 "criteria_before": "b"*64, "criteria_after": "b"*64},
        "heldout": {"split_digest": "c"*64, "passed": 1, "failed": 0, "unknown": 0}, "heldout_exposures": 1,
    }
    report = runner.ok(f"/attempts/{claim['attempt_id']}/reports", {
        "lease_token": claim["lease_token"], "report_type": "agent_repair", "payload": typed,
    })
    for index in range(1, 5):
        body = {"lease_token": claim["lease_token"], "report_type": "agent_repair", "payload": typed,
                "report_key": f"feedback-{index}"}
        assert not runner.ok(f"/attempts/{claim['attempt_id']}/reports", body)["duplicate"]
        assert runner.ok(f"/attempts/{claim['attempt_id']}/reports", body)["duplicate"]
    assert runner.send(f"/attempts/{claim['attempt_id']}/reports", {
        "lease_token": claim["lease_token"], "report_type": "agent_repair", "payload": typed,
        "report_key": "sixth-feedback",
    }).status_code == 409
    with transaction() as db:
        assert db.execute("select exposures from platform.heldout_exposure_counts").fetchone()["exposures"] == 5
    runner.event(
        claim,
        1,
        "job_result",
        {
            "patch_artifact_id": artifact["id"],
            "base_commit": "a" * 40,
            "validation_passed": True,
            "heldout_passed": True,
            "controls_passed": True,
            "criteria_unchanged": True,
            "report_id": report["id"],
        },
    )
    runner.finalize(claim)
    detail = suite["reviewer"].ok("GET", f"/repairs/{repair['id']}")
    assert detail["available_validation_report_id"] == report["id"]
    approval["evidence_digest"] = "0"*64
    assert suite["reviewer"].send("POST", f"/repairs/{repair['id']}/approve", approval).status_code == 409
    approval["evidence_digest"] = report["content_hash"]
    assert suite["owner"].send("POST", f"/repairs/{repair['id']}/approve", approval).status_code == 403
    assert suite["reviewer"].ok("POST", f"/repairs/{repair['id']}/approve", approval)["state"] == "approved"
    integration = suite["owner"].ok("POST", f"/projects/{suite['project']['id']}/integrations", {
        "provider": "github", "repository": "synthetic/agent", "installation_id": 101,
        "secret_ref": "TEST_APP_SECRET", "publish_enabled": True,
    })
    class ProviderDouble:
        def installation_token(self, *args, **kwargs):
            assert kwargs == {"write": True}
            return "synthetic-not-a-provider-token"
        def find_publication(self, *args):
            return "https://github.com/synthetic/agent/pull/7" if publication == "recovered" else None
        def request(self, method, path, token):
            assert publication != "recovered", "Marker recovery must precede base reads"
            assert method == "GET"
            return {"default_branch": "main"} if path == "/repos/synthetic/agent" else {"object": {"sha": "a"*40}}
        def draft_pr(self, *args):
            assert publication == "stale_race"
            raise PlatformError("STALE_BASE", "Synthetic moved-base race", 409)
        def close(self):
            pass
    monkeypatch.setattr(jobs, "GitHubApp", ProviderDouble)
    suite["reviewer"].ok("POST", f"/repairs/{repair['id']}/publish", {
        "integration_id": integration["id"], "proposal_hash": repair["proposal_hash"],
    })
    with transaction() as db:
        job = db.execute("select * from platform.jobs where kind='publish'").fetchone()
    if publication == "corrupt_artifact":
        from susanoo_api.platform.artifacts import ObjectStore
        monkeypatch.setattr(ObjectStore, "get", lambda self, key: b"corrupt")
        with pytest.raises(PlatformError, match="digest"):
            jobs.publish_job(job)
    elif publication == "stale_race":
        with pytest.raises(PlatformError, match="moved-base"):
            jobs.publish_job(job)
        with transaction() as db:
            row = db.execute("select * from platform.repairs where id=%s", (repair["id"],)).fetchone()
            assert row["state"] == "stale" and row["approved_by"] is None and row["pr_url"] is None
    else:
        jobs.publish_job(job)
        jobs.publish_job(job)
        assert suite["reviewer"].ok("GET", f"/repairs/{repair['id']}")["pr_url"].endswith("/7")


def test_exclusive_fixture_remains_quarantined_after_expired_lease(suite):
    environment = version(
        suite, {**suite["environment"]["data"], "isolation": "exclusive", "resource_key": "synthetic-shared-fixture"}
    )
    approve(suite, environment)
    suite["body"].update(environment_version_id=environment["id"], repetitions=2)
    launch(suite)
    runner = suite["runner"]
    claim = runner.claim("outcome_suite")
    with transaction() as db:
        db.execute(
            "update platform.attempts set lease_expires_at=now()-interval '1 second' where id=%s",
            (claim["attempt_id"],),
        )
    jobs.tick()
    assert runner.send("/claims", {"supported_job_kinds": ["outcome_suite"]}).status_code == 204
    resource = suite["reviewer"].ok("GET", f"/projects/{suite['project']['id']}/resource-leases")["items"][0]
    assert resource["state"] == "quarantined"
    reconciliation = runner.ok(
        f"/attempts/{claim['attempt_id']}/reconciliations",
        {
            "lease_token": claim["lease_token"],
            "state": "resolved",
            "rationale": "Synthetic recovery read-back without evidence",
        },
    )
    assert (
        suite["reviewer"]
        .send(
            "POST",
            f"/resource-leases/{resource['id']}/release",
            {
                "reconciliation_id": reconciliation["id"],
                "rationale": "Cannot release without read-back evidence",
            },
        )
        .status_code
        == 409
    )

    observation = {
        "observation_id": str(uuid4()),
        "name": "promises",
        "source": "synthetic_post_expiry_readback_not_business_backend",
        "captured_at": datetime.now(UTC).isoformat(),
        "availability": "available",
        "completeness": "complete_for_scope",
        "namespace": claim["namespace"],
        "window_seconds": 10,
        "window_elapsed": True,
        "storage_mode": "redacted_hosted",
        "values": {"count": 0},
        "redaction_version": "synthetic-test",
        "digest": content_hash({"count": 0}),
    }
    resolved = runner.ok(
        f"/attempts/{claim['attempt_id']}/reconciliations",
        {
            "lease_token": claim["lease_token"],
            "state": "resolved",
            "rationale": "Synthetic complete read-back, no dispatch authority",
            "observations": [observation],
        },
    )
    released = suite["reviewer"].ok(
        "POST",
        f"/resource-leases/{resource['id']}/release",
        {"reconciliation_id": resolved["id"], "rationale": "Synthetic read-back reviewed independently"},
    )
    assert released["released"] and released["original_verdict_unchanged"]
    next_attempt = runner.claim("outcome_suite")
    assert next_attempt["fencing_generation"] > claim["fencing_generation"]
    assert (
        runner.send(f"/attempts/{claim['attempt_id']}/heartbeat", {"lease_token": claim["lease_token"]}).status_code
        == 409
    )


def test_media_and_source_upload_fail_closed_without_policy(world):
    for content_type, purpose in [("audio/wav", "media"), ("application/json", "patch")]:
        response = world["owner"].send(
            "POST",
            f"/projects/{world['project']['id']}/artifacts",
            {
                "content_type": content_type,
                "purpose": purpose,
                "size": 1,
                "digest": "a" * 64,
            },
        )
        assert response.status_code == 403


def test_worker_exhausted_lease_becomes_dead_letter(world):
    with transaction() as db:
        db.execute(
            """insert into platform.jobs(workspace_id,project_id,kind,payload,dedupe_key,status,
            attempts,lease_expires_at)
            values(%s,%s,'export','{}','synthetic-crashed-export','running',5,now()-interval '1 second')""",
            (world["workspace"]["id"], world["project"]["id"]),
        )
    jobs.tick()
    records = world["owner"].ok("GET", f"/projects/{world['project']['id']}/internal-jobs")["items"]
    assert records[0]["status"] == "dead_letter"
