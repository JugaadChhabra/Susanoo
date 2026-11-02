"""Real PostgreSQL fixtures; fake runners are protocol test clients, not a replacement runtime."""

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from susanoo_contracts import content_hash

from susanoo_api.platform.cli import migrate
from susanoo_api.platform.database import transaction
from susanoo_api.platform.router import create_platform_app
from susanoo_api.platform.security import create_identity

PASSWORD = "synthetic-test-password-1234"


@pytest.fixture(scope="session", autouse=True)
def database():
    url = os.environ.get("PLATFORM_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Run scripts/test_platform.py to provision an isolated real PostgreSQL database")
    if not conninfo_to_dict(url)["dbname"].startswith("susanoo_test_"):
        raise RuntimeError("Refusing non-disposable test database")
    os.environ["PLATFORM_DATABASE_URL"] = url
    migrate()


@pytest.fixture(autouse=True)
def isolated(database, monkeypatch, tmp_path):
    monkeypatch.setenv("PLATFORM_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("PLATFORM_ENV", "development")
    monkeypatch.setenv("PLATFORM_ORIGIN", "http://localhost:3000")
    monkeypatch.setenv("PLATFORM_DELETION_JOURNAL", str(tmp_path / "deletion-journal.enc"))
    monkeypatch.delenv("PLATFORM_S3_BUCKET", raising=False)
    with transaction() as db:
        tables = [
            row["tablename"]
            for row in db.execute(
                "select tablename from pg_tables where schemaname='platform' and tablename<>'schema_versions'"
            ).fetchall()
        ]
        db.execute(
            sql.SQL("truncate {} restart identity cascade").format(
                sql.SQL(",").join(sql.Identifier("platform", name) for name in tables)
            )
        )


class Browser:
    def __init__(self, app, email=None):
        self.client = TestClient(app)
        self.email = email
        if email:
            response = self.send("POST", "/auth/login", {"email": email, "password": PASSWORD})
            assert response.status_code == 200, response.text

    def send(self, method, path, body=None, **kwargs):
        headers = {"Origin": "http://localhost:3000", **kwargs.pop("headers", {})}
        if method not in {"GET", "HEAD"} and "Authorization" not in headers:
            csrf = self.client.get("/api/v1/auth/csrf").json()["csrf_token"]
            headers["X-CSRF-Token"] = csrf
        return self.client.request(method, "/api/v1" + path, json=body, headers=headers, **kwargs)

    def ok(self, method, path, body=None, **kwargs):
        response = self.send(method, path, body, **kwargs)
        assert response.is_success, (method, path, response.status_code, response.text)
        return response.json()


@pytest.fixture
def world():
    app = FastAPI()
    app.mount("/api/v1", create_platform_app())
    users = {}
    with transaction() as db:
        for name in ("owner", "reviewer", "viewer", "outsider", "engineer"):
            users[name] = create_identity(db, f"{name}@test.local", PASSWORD)["id"]
    browsers = {name: Browser(app, f"{name}@test.local") for name in users}
    owner = browsers["owner"]
    workspace = owner.ok("POST", "/workspaces", {"name": "Protocol test workspace"})
    for role in ("reviewer", "viewer", "engineer"):
        owner.ok("POST", f"/workspaces/{workspace['id']}/members", {"user_id": str(users[role]), "roles": [role]})
    project = owner.ok(
        "POST",
        "/projects",
        {
            "workspace_id": workspace["id"],
            "name": "Synthetic protocol fixture",
            "workflow": "collections",
            "policy": {"upload_fields": ["count", "amount_minor", "currency"]},
        },
    )
    return {
        "app": app,
        "users": users,
        "browsers": browsers,
        "owner": owner,
        "reviewer": browsers["reviewer"],
        "workspace": workspace,
        "project": project,
    }


def version(world, data, logical_id=None, hidden=False):
    return world["owner"].ok(
        "POST",
        f"/projects/{world['project']['id']}/versions",
        {
            "logical_id": logical_id or data["kind"] + "-" + uuid4().hex[:8],
            "title": "Synthetic " + data["kind"],
            "data": data,
            "hidden": hidden,
        },
    )


def approve(world, row):
    return world["reviewer"].ok(
        "POST",
        f"/versions/{row['id']}/approve",
        {"content_hash": row["content_hash"], "rationale": "Synthetic protocol fixture review"},
    )


class ProtocolRunner:
    def __init__(self, world, capabilities=None):
        self.world = world
        token = world["owner"].ok("POST", f"/projects/{world['project']['id']}/runner-registration")["token"]
        self.register_body = {
            "registration_token": token,
            "name": "Explicit protocol test double",
            "version": "test-only",
            "capabilities": capabilities or ["preflight", "outcome_suite", "calibration", "repair_retest", "build"],
        }
        self.token = world["owner"].ok("POST", "/runner/register", self.register_body)["token"]

    def send(self, path, body):
        return self.world["owner"].send(
            "POST", "/runner" + path, body, headers={"Authorization": "Bearer " + self.token}
        )

    def ok(self, path, body):
        response = self.send(path, body)
        assert response.is_success, (path, response.status_code, response.text)
        return response.json()

    def claim(self, kind):
        return self.ok("/claims", {"supported_job_kinds": [kind]})

    def event(self, claim, sequence, type, payload=None, **kwargs):
        event = {
            "event_id": str(uuid4()),
            "sequence": sequence,
            "type": type,
            "occurred_at": datetime.now(UTC).isoformat(),
            "payload": payload or {},
            **kwargs,
        }
        return self.ok(
            f"/attempts/{claim['attempt_id']}/events", {"lease_token": claim["lease_token"], "events": [event]}
        ), event

    def finalize(self, claim, verdict="PASS", seq=1, **kwargs):
        return self.ok(
            f"/attempts/{claim['attempt_id']}/finalize",
            {
                "lease_token": claim["lease_token"],
                "execution_state": "completed",
                "business_verdict": verdict,
                "final_sequence": seq,
                "cleanup_state": "succeeded",
                "reconciliation_state": "not_needed",
                **kwargs,
            },
        )


@pytest.fixture
def suite(world):
    requirement = version(
        world,
        {
            "kind": "requirement",
            "source_clause": "PTP-001",
            "expected": "Exactly one record",
            "owner": "Synthetic reviewer",
        },
    )
    approve(world, requirement)
    check = version(
        world,
        {
            "kind": "check",
            "requirement_version_id": requirement["id"],
            "operator": "count",
            "observation": "promises",
            "expected": 1,
        },
    )
    fixture = version(
        world,
        {
            "kind": "fixture",
            "adapter": "protocol_test_only",
            "digest": content_hash({}),
            "compatibility_key": "synthetic-v1",
        },
    )
    case = version(
        world,
        {
            "kind": "case",
            "fixture_version_id": fixture["id"],
            "check_version_ids": [check["id"]],
            "script": [{"message": "Synthetic protocol action"}],
        },
    )
    approved_suite = version(world, {"kind": "suite", "case_version_ids": [case["id"]]})
    approve(world, approved_suite)
    environment = version(
        world,
        {
            "kind": "environment",
            "adapter": "protocol_test_only",
            "version": "test-v1",
            "allowed_operations": ["synthetic_write"],
            "identity_profile": "test",
            "fixture_version_id": fixture["id"],
        },
    )
    approve(world, environment)
    connection = version(
        world,
        {
            "kind": "connection",
            "family": "I03",
            "adapter": "protocol_test_only",
            "capabilities": {"invoke": True, "observe": True},
        },
    )
    runner = ProtocolRunner(world)
    world["owner"].ok(
        "POST",
        f"/projects/{world['project']['id']}/jobs",
        {"kind": "preflight", "version_ids": [connection["id"]]},
        headers={"Idempotency-Key": "preflight"},
    )
    preflight = runner.claim("preflight")
    runner.event(preflight, 1, "job_result", {"invocation_ok": True, "observer_ok": True, "cleanup_ok": True})
    runner.finalize(preflight)
    body = {
        "suite_version_id": approved_suite["id"],
        "connection_version_id": connection["id"],
        "environment_version_id": environment["id"],
    }
    return {
        **world,
        "requirement": requirement,
        "check": check,
        "fixture": fixture,
        "case": case,
        "suite": approved_suite,
        "environment": environment,
        "connection": connection,
        "runner": runner,
        "body": body,
    }


def launch(suite, key="launch"):
    return suite["owner"].ok(
        "POST", f"/projects/{suite['project']['id']}/runs", suite["body"], headers={"Idempotency-Key": key}
    )


def result(suite, claim, verdict="PASS", availability="available", completeness="complete_for_scope"):
    observation_id = str(uuid4())
    values = {"count": 1 if verdict == "PASS" else 0}
    body = {
        "lease_token": claim["lease_token"],
        "observations": [
            {
                "observation_id": observation_id,
                "name": suite["check"]["data"]["observation"],
                "source": "protocol_test_double_not_business_backend",
                "captured_at": datetime.now(UTC).isoformat(),
                "availability": availability,
                "completeness": completeness,
                "namespace": claim["namespace"],
                "window_seconds": 10,
                "window_elapsed": True,
                "storage_mode": "redacted_hosted",
                "values": values,
                "redaction_version": "test",
                "digest": content_hash(values),
            }
        ],
        "check_results": [
            {
                "check_version_id": suite["check"]["id"],
                "evaluator_digest": "a" * 64,
                "verdict": verdict,
                "reason": "Synthetic protocol test; no runtime correctness claimed",
                "observation_ids": [observation_id],
            }
        ],
    }
    return suite["runner"].send(f"/attempts/{claim['attempt_id']}/evidence", body), body
