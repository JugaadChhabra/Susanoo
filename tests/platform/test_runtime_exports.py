"""Actual runtime projection helpers -> authorized API persistence -> fresh public ZIP materials."""

import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from susanoo_contracts import content_hash
from susanoo_runner.ci import RunManifest, case_manifest
from susanoo_runner.export import ExportSnapshot, export_json, reproduction_ingredient
from susanoo_runner.local import RunReport
from susanoo_runner.suite import LocalSuite
from susanoo_api.platform import artifacts
from susanoo_api.platform.database import Context, transaction

from conftest import launch, result


def test_runtime_helpers_produce_validated_public_reproduction_bundle(suite):
    run = launch(suite)
    runner = suite["runner"]
    claim = runner.claim("outcome_suite")
    root = Path(__file__).resolve().parents[2]
    local = LocalSuite.model_validate_json((root / "examples/collections/suite.json").read_text(encoding="utf-8"))
    local = local.model_copy(update={"cases": [case for case in local.cases if case.id in {"C01", "C07"}]})
    now = datetime.now(UTC)
    manifest = RunManifest(handle="synthetic-projection", run_id=run["id"], suite_id=local.id,
                           suite_hash=local.content_hash, synthetic=True, agent_label="projection fixture",
                           agent_adapter="protocol_test_only", evaluator_digest="a"*64, created_at=now,
                           profile={}, cases=[case_manifest(case) for case in local.cases])
    report = RunReport(run_id=run["id"], suite_id=local.id, suite_hash=local.content_hash, synthetic=True,
                       approval_kind="synthetic", agent="projection fixture", evaluator_digest="a"*64,
                       started_at=now, finished_at=now, gate="BLOCKED", blockers=["Projection fixture only"],
                       counts=dict.fromkeys(("scheduled", "attempted", "completed", "passed", "failed", "unknown",
                                             "execution_errors"), 0), attempts=[])
    data = {"report": report.view("engineer"), "manifest": manifest.model_dump(mode="json")}
    for case in data["manifest"]["cases"]:
        if case["visibility"] == "hidden":
            case["title"] = "Held-out case"
    snapshot = ExportSnapshot("engineer", report, manifest, local, data, content_hash(data))
    values = {"run_export": export_json(snapshot), "reproduction": reproduction_ingredient(snapshot)}
    for kind, payload in values.items():
        runner.ok(f"/attempts/{claim['attempt_id']}/reports", {
            "lease_token": claim["lease_token"], "report_type": kind, "audience": "project", "payload": payload,
        })
    assert result(suite, claim)[0].is_success
    runner.event(claim, 1, "progress", {"code": "projection_fixture_only"})
    runner.finalize(claim)
    with transaction() as db:
        ctx = Context(db, {"user_id": suite["users"]["owner"]}, str(uuid4()))
        project = ctx.project(suite["project"]["id"])
        stored = db.execute("select * from platform.runs where id=%s", (run["id"],)).fetchone()
        exported = artifacts.export_snapshot(ctx, stored, project, "project")
    archive = zipfile.ZipFile(io.BytesIO(artifacts.render_export(exported, "bundle")))
    fresh = LocalSuite.model_validate_json(archive.read("reproduction-0/suite.json"))
    assert [case.id for case in fresh.cases] == ["C01"]
    assert "Held-out" not in archive.read("reproduction-0/suite.json").decode()
    assert json.loads(archive.read("reproduction-0/profile.json")) == {}
    assert values["run_export"]["snapshot_digest"] == values["reproduction"]["snapshot_digest"]
