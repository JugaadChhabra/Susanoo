import copy
import io
import json
import zipfile
from uuid import uuid4

from susanoo_api.platform import artifacts, jobs
from susanoo_api.platform.database import Context, transaction
from susanoo_api.platform.automation import dispatch_automatic
from susanoo_contracts import RunCreate

from conftest import ProtocolRunner, approve, version


def comparison_job(suite, *, hidden=False):
    reference = version(suite, suite["environment"]["data"])
    approve(suite, reference)
    fidelity = version(suite, {
        "kind": "fidelity", "simulator_version_id": suite["environment"]["id"],
        "reference_version_id": reference["id"], "operations": ["read_account"],
        "identity_profile": "synthetic",
    }, hidden=hidden)
    approve(suite, fidelity)
    runner = ProtocolRunner(suite, capabilities=["action_comparison"])
    run = suite["owner"].ok("POST", f"/projects/{suite['project']['id']}/jobs", {
        "kind": "action_comparison", "version_ids": [fidelity["id"]],
    }, headers={"Idempotency-Key": "comparison"})
    return run, runner, runner.claim("action_comparison")


def comparison_payload(claim):
    return {"lease_token": claim["lease_token"], "report_type": "comparison", "audience": "project", "payload": {
        "contract_id": "synthetic", "contract_digest": "a"*64, "criteria_digest": "b"*64,
        "plan_id": "synthetic-plan", "plan_digest": "c"*64, "simulator_digest": "d"*64, "reference_digest": "e"*64,
        "status": "compared", "preflight": {"ok": True}, "candidates": [{
            "id": "duplicate", "category": "duplicate_write", "summary": "Synthetic duplicate state",
        }],
    }}


def test_typed_report_persistence_visibility_classification_and_snapshot_export(suite):
    run, runner, claim = comparison_job(suite)
    body = comparison_payload(claim)
    created = runner.ok(f"/attempts/{claim['attempt_id']}/reports", body)
    assert runner.ok(f"/attempts/{claim['attempt_id']}/reports", body)["duplicate"]
    changed = copy.deepcopy(body)
    changed["payload"]["candidates"][0]["summary"] = "changed"
    assert runner.send(f"/attempts/{claim['attempt_id']}/reports", changed).status_code == 409
    malformed = copy.deepcopy(body)
    malformed["payload"]["status"] = "invented"
    assert runner.send(f"/attempts/{claim['attempt_id']}/reports", malformed).status_code == 422
    candidate = suite["browsers"]["engineer"].ok("GET", f"/reports/{created['id']}")
    assert candidate["content_hash"] == created["content_hash"]
    assert suite["browsers"]["outsider"].send("GET", f"/reports/{created['id']}").status_code == 404
    classification = {
        "candidate_id": "duplicate", "classification": "intentional_simplification",
        "rationale": "Approved synthetic scope", "evidence_ids": [created["id"]],
    }
    assert suite["browsers"]["engineer"].send(
        "POST", f"/reports/{created['id']}/classifications", classification
    ).status_code == 403
    suite["reviewer"].ok("POST", f"/reports/{created['id']}/classifications", classification)
    original = suite["owner"].ok("GET", f"/reports/{created['id']}")
    assert original["payload"]["candidates"][0]["classification"] == "unresolved"
    assert original["reviews"][0]["classification"] == "intentional_simplification"
    runner.event(claim, 1, "job_result", {
        "job_kind": "action_comparison", "status": "succeeded", "report_ids": [created["id"]],
    })
    runner.finalize(claim, "FAIL")
    with transaction() as db:
        ctx = Context(db, {"user_id": suite["users"]["owner"]}, str(uuid4()))
        project = ctx.project(suite["project"]["id"])
        stored = db.execute("select * from platform.runs where id=%s", (run["id"],)).fetchone()
        snapshot = artifacts.export_snapshot(ctx, stored, project, "project")
    assert snapshot["advanced_reports"][0]["id"] == created["id"]
    assert "Synthetic duplicate state" in json.dumps(snapshot)
    pdf = artifacts.render_export(snapshot, "pdf")
    assert pdf.startswith(b"%PDF")
    bundle = zipfile.ZipFile(io.BytesIO(artifacts.render_export(snapshot, "bundle")))
    assert f"reports/{created['id']}.json" in bundle.namelist()


def test_hidden_report_cannot_be_made_public(suite):
    # Only the explicitly granted reviewer may read the hidden input graph.
    for role, roles in (("owner", ["owner", "engineer"]), ("reviewer", ["reviewer"])):
        suite["owner"].ok("POST", f"/workspaces/{suite['workspace']['id']}/members", {
            "user_id": str(suite["users"][role]), "roles": roles,
            "project_id": suite["project"]["id"], "hidden_access": True,
        })
    run, runner, claim = comparison_job(suite, hidden=True)
    created = runner.ok(f"/attempts/{claim['attempt_id']}/reports", comparison_payload(claim))
    assert suite["browsers"]["engineer"].send("GET", f"/reports/{created['id']}").status_code == 404
    assert suite["browsers"]["engineer"].ok("GET", f"/runs/{run['id']}/reports")["items"] == []


def test_automatic_run_budget_and_semantic_dedupe_are_persistent(suite):
    body = RunCreate.model_validate(suite["body"])
    def dispatch(key, **budget):
        with transaction() as db:
            ctx = Context(db, {"user_id": suite["users"]["owner"]}, str(uuid4()))
            return dispatch_automatic(
                ctx, suite["project"]["id"], body, trigger_kind="webhook", trigger_key="source-1",
                version_key=key, budget={"allowed_suites": [str(body.suite_version_id)], **budget},
            )
    first = dispatch("revision-1", max_runs_per_window=1)
    assert first["allowed"] and first["run_id"]
    assert dispatch("revision-1", max_runs_per_window=1)["run_id"] == first["run_id"]
    blocked = dispatch("revision-2", max_runs_per_window=1)
    assert not blocked["allowed"] and "rate limit" in blocked["reasons"][0]
    assert blocked["delayed_until"] is not None
    capped = dispatch("revision-3", max_agent_invocations=1)
    assert not capped["allowed"] and any("invocations" in value for value in capped["reasons"])
    unknown_cost = dispatch("revision-4", max_cost_minor=100)
    assert not unknown_cost["allowed"] and any("no cost estimate" in value for value in unknown_cost["reasons"])
    decisions = suite["owner"].ok("GET", f"/projects/{suite['project']['id']}/automatic-decisions")["items"]
    assert len(decisions) == 4
    assert sum(value["allowed"] for value in decisions) == 1
    with transaction() as db:
        assert db.execute("select count(*) n from platform.runs where kind='outcome_suite'").fetchone()["n"] == 1


def test_schedule_uses_automatic_policy_and_keeps_blocked_reason(suite):
    created = suite["owner"].ok("POST", f"/projects/{suite['project']['id']}/schedules", {
        "name": "Blocked synthetic schedule", "interval_seconds": 60, "enabled": True, "run": suite["body"],
        "automatic_budget": {"allowed_suites": [], "max_runs_per_window": 0},
    })
    with transaction() as db:
        db.execute("update platform.schedules set next_due_at=now()-interval '1 second' where id=%s", (created["id"],))
    jobs.tick()
    rows = suite["owner"].ok("GET", f"/projects/{suite['project']['id']}/automatic-decisions")["items"]
    assert len(rows) == 1 and not rows[0]["allowed"]
    assert any("outside" in reason for reason in rows[0]["reasons"])
