"""CLI run/preflight/wait/reproduce/compare/export on local runs [Spec §16, §18, §27; T15, T16, T17].

Exit codes are checked with real subprocesses of ``python -m susanoo_runner.cli`` against the
collections demo, on small case subsets with the in-process demo agent to keep runtime modest.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from susanoo_runner.ci import (
    RunDir,
    RunStatus,
    WaiverPolicy,
    evaluate_acceptance,
    machine_exit_code,
)
from susanoo_runner.compare import compare_runs, comparison_exit_code

ROOT = Path(__file__).resolve().parents[2]
SUITE = ROOT / "examples/collections/suite.json"
PATHS = [
    "packages/evaluator",
    "packages/simulator",
    "packages/sdk-python",
    "packages/runner",
    "packages/connectors",
    "examples/collections",
]


def cli(*args: str, cwd: Path, timeout: float = 120) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(str(ROOT / p) for p in PATHS)}
    return subprocess.run(
        [sys.executable, "-m", "susanoo_runner.cli", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def run(tmp: Path, *args: str) -> tuple[subprocess.CompletedProcess, RunDir]:
    before = set((tmp / "runs").glob("*")) if (tmp / "runs").exists() else set()
    result = cli("run", "--suite", str(SUITE), "--runs-dir", str(tmp / "runs"), *args, cwd=tmp)
    created = sorted(set((tmp / "runs").glob("*")) - before)
    return result, RunDir(created[0]) if created else None


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def waivers(tmp: Path, *items: dict) -> Path:
    path = tmp / "acceptance-waivers.json"
    path.write_text(json.dumps({"kind": "susanoo.acceptance_waivers", "waivers": list(items)}), encoding="utf-8")
    return path


def waiver(case_id: str, *, days: int = 7, check_ids: list[str] | None = None) -> dict:
    return {
        "id": f"W-{case_id}-{days}",
        "approver": "owner@example.invalid",
        "reason": "Known issue, remediation scheduled",
        "scope": {"suite_id": "collections-ptp-v1", "case_id": case_id, "check_ids": check_ids or []},
        "expires_at": (datetime.now(UTC) + timedelta(days=days)).isoformat(),
    }


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    """Shared runs: correct, a required failure and an observer outage."""
    tmp = tmp_path_factory.mktemp("cli")
    ready, ready_dir = run(tmp, "--agent", "inprocess:correct", "--case", "C01", "--case", "C02", "--case", "C07")
    failing, failing_dir = run(
        tmp, "--agent", "inprocess:wrong_amount", "--case", "C01", "--case", "C02", "--case", "C07"
    )
    unknown, unknown_dir = run(tmp, "--agent", "inprocess:correct", "--case", "C01", "--observer-outage")
    return {
        "tmp": tmp,
        "ready": (ready, ready_dir),
        "failing": (failing, failing_dir),
        "unknown": (unknown, unknown_dir),
    }


def test_exit_0_when_ready(runs):
    result, run_dir = runs["ready"]
    assert result.returncode == 0, result.stderr
    assert run_dir.status().state == "finished" and run_dir.status().exit_code == 0
    assert run_dir.report().gate == "READY_FOR_ACCEPTANCE"
    assert {p.name for p in run_dir.path.iterdir()} >= {
        "suite.json",
        "manifest.json",
        "status.json",
        "report.json",
        "journal.sqlite",
    }
    manifest = run_dir.manifest()
    assert manifest.run_id == run_dir.report().run_id
    assert [c.case_id for c in manifest.cases] == ["C01", "C02", "C07"]


def test_exit_1_on_observed_required_failure(runs):
    result, run_dir = runs["failing"]
    assert result.returncode == 1, result.stdout + result.stderr
    report = run_dir.report()
    assert any(a.required and a.business_verdict == "FAIL" for a in report.attempts)


def test_exit_2_on_unknown(runs):
    result, run_dir = runs["unknown"]
    assert result.returncode == 2
    assert run_dir.report().gate == "NEEDS_REVIEW"


def test_exit_2_on_cleanup_failure_even_when_every_check_passes(tmp_path):
    result, run_dir = run(tmp_path, "--agent", "inprocess:correct", "--case", "C01", "--cleanup-failure")
    assert result.returncode == 2
    assert run_dir.report().attempts[0].business_verdict == "PASS"


@pytest.mark.parametrize(
    "extra",
    [
        ["--agent", "nonsense:x", "--case", "C01"],
        ["--agent", "inprocess:correct", "--case", "NOPE"],
        ["--agent", "inprocess:correct", "--no-such-flag"],
    ],
)
def test_exit_3_on_invalid_configuration(tmp_path, extra):
    result, _ = run(tmp_path, *extra)
    assert result.returncode == 3, result.stdout + result.stderr


def test_exit_3_on_invalid_suite_profile_or_waiver_policy(tmp_path):
    bad_suite = tmp_path / "suite.json"
    bad_suite.write_text('{"id": "x", "timezone": "Asia/Kolkata", "cases": []}', encoding="utf-8")
    assert cli("run", "--suite", str(bad_suite), "--runs-dir", "runs", cwd=tmp_path).returncode == 3
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"allowed_operations": ["read_account"]}), encoding="utf-8")
    refused, _ = run(tmp_path, "--agent", "inprocess:correct", "--case", "C01", "--profile", str(profile))
    assert refused.returncode == 3 and "refuses" in refused.stderr
    policy = tmp_path / "waivers.json"
    policy.write_text(json.dumps({"waivers": []}), encoding="utf-8")  # not a named waiver policy
    invalid, _ = run(tmp_path, "--agent", "inprocess:correct", "--case", "C01", "--waiver-policy", str(policy))
    assert invalid.returncode == 3


# --- T16: waivers change acceptance only ---------------------------------------------------------


def test_waiver_changes_acceptance_never_the_machine_verdict(runs):
    tmp = runs["tmp"]
    _, run_dir = runs["failing"]
    report_hash = sha(run_dir.report_path)
    failed = [a.case_id for a in run_dir.report().attempts if a.required and a.business_verdict == "FAIL"]
    default = cli("wait", "--run", str(run_dir.path), "--timeout", "5", cwd=tmp)
    assert default.returncode == 1  # default CI applies no waivers
    policy = waivers(tmp, *(waiver(case_id) for case_id in failed))
    waived = cli("wait", "--run", str(run_dir.path), "--timeout", "5", "--waiver-policy", str(policy), cwd=tmp)
    assert waived.returncode == 0, waived.stdout
    assert "accepted_with_waivers" in waived.stdout and "remains in the record" in waived.stdout
    report = run_dir.report()
    assert sha(run_dir.report_path) == report_hash
    assert report.gate == "BLOCKED" and machine_exit_code(report) == 1
    assert [a.case_id for a in report.attempts if a.business_verdict == "FAIL"] == failed


def test_expired_or_narrow_waivers_do_not_authorize_acceptance(runs):
    _, run_dir = runs["failing"]
    report = run_dir.report()
    failed = [a.case_id for a in report.attempts if a.required and a.business_verdict == "FAIL"]
    expired = WaiverPolicy.model_validate(
        {"kind": "susanoo.acceptance_waivers", "waivers": [waiver(c, days=-1) for c in failed]}
    )
    decision = evaluate_acceptance(report, expired)
    assert decision.exit_code == 1 and decision.decision == "not_accepted" and decision.expired
    narrow = WaiverPolicy.model_validate(
        {"kind": "susanoo.acceptance_waivers", "waivers": [waiver(c, check_ids=["some_other_check"]) for c in failed]}
    )
    assert evaluate_acceptance(report, narrow).exit_code == 1


def test_waivers_cannot_cover_operational_blockers(tmp_path):
    result, run_dir = run(tmp_path, "--agent", "inprocess:correct", "--case", "C01", "--cleanup-failure")
    policy = waivers(tmp_path, waiver("C01"))
    waited = cli("wait", "--run", str(run_dir.path), "--waiver-policy", str(policy), cwd=tmp_path)
    assert result.returncode == 2 and waited.returncode == 2


# --- wait and detach -----------------------------------------------------------------------------


def test_detached_run_then_wait(tmp_path):
    started = time.monotonic()
    result, run_dir = run(tmp_path, "--detach", "--agent", "inprocess:false_completion", "--case", "C01")
    assert result.returncode == 0 and run_dir.status().state in {"queued", "running", "finished"}
    waited = cli("wait", "--run", run_dir.handle, "--runs-dir", str(tmp_path / "runs"), "--timeout", "90", cwd=tmp_path)
    assert waited.returncode == 1, waited.stdout + waited.stderr + run_dir.log_path.read_text()
    assert run_dir.status().state == "finished"
    assert time.monotonic() - started < 90


def test_wait_times_out_or_detects_interruption_as_incomplete(tmp_path):
    queued = RunDir.create(tmp_path / "runs", "queued-run")
    queued.write_status(RunStatus(state="queued"))
    assert cli("wait", "--run", str(queued.path), "--timeout", "0.3", cwd=tmp_path).returncode == 2
    dead = RunDir.create(tmp_path / "runs", "dead-run")
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    dead.write_status(RunStatus(state="running", pid=proc.pid))
    result = cli("wait", "--run", str(dead.path), "--timeout", "5", cwd=tmp_path)
    assert result.returncode == 2 and "interrupted" in result.stderr
    assert cli("wait", "--run", "missing", "--runs-dir", str(tmp_path / "runs"), cwd=tmp_path).returncode == 3


# --- reproduce -----------------------------------------------------------------------------------


def test_reproduce_reruns_one_case_in_a_fresh_namespace(runs):
    tmp = runs["tmp"]
    _, run_dir = runs["failing"]
    original_hash = sha(run_dir.report_path)
    original = next(a for a in run_dir.report().attempts if a.case_id == "C01")
    assert original.business_verdict == "FAIL"
    result = cli("reproduce", "--run", str(run_dir.path), "--case", "C01", "--runs-dir", str(tmp / "runs"), cwd=tmp)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "reproduced" in result.stdout and "not_reproduced" not in result.stdout
    fresh_dir = RunDir(tmp / "runs" / result.stdout.split("Fresh run ")[1].split(";")[0])
    fresh = fresh_dir.report().attempts
    assert [a.case_id for a in fresh] == ["C01"] and fresh[0].namespace != original.namespace
    assert fresh_dir.manifest().reproduces == {"handle": run_dir.handle, "case_id": "C01"}
    assert sha(run_dir.report_path) == original_hash  # original evidence untouched


# --- T15: compare --------------------------------------------------------------------------------


def test_compare_exact_transitions(runs):
    tmp = runs["tmp"]
    (_, base), (_, cand) = runs["ready"], runs["failing"]
    comparison = compare_runs(base.report(), base.manifest(), cand.report(), cand.manifest())
    rows = {row.case_id: row for row in comparison.rows}
    assert rows["C01"].compatibility == "comparable"
    assert rows["C01"].transition == "PASS->FAIL" and rows["C01"].category == "pass_to_fail"
    assert {t.check_id: t.transition for t in rows["C01"].check_transitions}["promise_amount_matches"] == "PASS->FAIL"
    assert rows["C07"].check_transitions == []  # held-out: no check detail in the engineer view
    assert comparison_exit_code(comparison) == 1
    result = cli("compare", "--baseline", str(base.path), "--candidate", str(cand.path), "--output", "c.json", cwd=tmp)
    assert result.returncode == 1 and "PASS->FAIL" in result.stdout
    assert json.loads((tmp / "c.json").read_text())["summary"]["pass_to_fail"] >= 1
    same = cli("compare", "--baseline", str(base.path), "--candidate", str(base.path), cwd=tmp)
    assert same.returncode == 0 and "unchanged" in same.stdout
    reverse = compare_runs(cand.report(), cand.manifest(), base.report(), base.manifest())
    assert {r.case_id: r.category for r in reverse.rows}["C01"] == "fail_to_pass"


def test_compare_changed_criteria_are_not_comparable(runs, tmp_path):
    (_, base) = runs["ready"]
    suite = json.loads(SUITE.read_text(encoding="utf-8"))
    c01 = next(c for c in suite["cases"] if c["id"] == "C01")
    c01["checks"][0]["severity"] = "low"  # a check version change, not an improvement
    c07 = next(c for c in suite["cases"] if c["id"] == "C07")
    c07["checks"][0]["severity"] = "low"
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps(suite), encoding="utf-8")
    result = cli(
        "run",
        "--suite",
        str(changed),
        "--runs-dir",
        str(tmp_path / "runs"),
        "--agent",
        "inprocess:correct",
        "--case",
        "C01",
        "--case",
        "C02",
        "--case",
        "C03",
        "--case",
        "C07",
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    cand = RunDir(next((tmp_path / "runs").iterdir()))
    comparison = compare_runs(base.report(), base.manifest(), cand.report(), cand.manifest())
    rows = {row.case_id: row for row in comparison.rows}
    assert rows["C01"].compatibility == "not_comparable" and rows["C01"].transition is None
    assert f"check_changed:{c01['checks'][0]['id']}" in rows["C01"].reasons
    assert rows["C07"].reasons == ["check_versions_changed"]  # hidden ids withheld from engineers
    assert rows["C02"].category == "unchanged"
    assert rows["C03"].reasons == ["case_absent_in_baseline"]
    assert comparison_exit_code(comparison) == 2
    operator = compare_runs(base.report(), base.manifest(), cand.report(), cand.manifest(), audience="operator")
    assert any(r.startswith("check_changed:") for r in {x.case_id: x for x in operator.rows}["C07"].reasons)


# --- T17: exports from one snapshot --------------------------------------------------------------


def test_exports_share_one_snapshot_and_hide_held_out_content(runs):
    tmp = runs["tmp"]
    _, run_dir = runs["failing"]
    out = tmp / "exports"
    result = cli("export", "--run", str(run_dir.path), "--format", "all", "--output", str(out), cwd=tmp)
    assert result.returncode == 0, result.stderr
    data = json.loads((out / "results.json").read_text())
    bundle = json.loads((out / "reproduction.json").read_text())
    junit_text = (out / "results.junit.xml").read_text()
    junit = ET.fromstring(junit_text)
    digest = {p.get("name"): p.get("value") for p in junit.iter("property")}["susanoo.snapshot_digest"]
    assert data["snapshot_digest"] == bundle["snapshot_digest"] == digest
    report = run_dir.report()
    required_fail = [a for a in report.attempts if a.required and a.business_verdict == "FAIL"]
    assert int(junit.get("failures")) == len(required_fail) == len(list(junit.iter("failure")))
    assert junit.get("skipped") == "0"
    assert data["machine"]["exit_code"] == 1 and data["snapshot"]["report"]["gate"] == report.gate
    # held-out content never appears in the engineer view, nested payloads included
    suite = json.loads(SUITE.read_text(encoding="utf-8"))
    hidden = next(c for c in suite["cases"] if c["id"] == "C07")
    secrets = [hidden["script"][0]["message"], hidden["title"]] + [
        json.dumps(check, sort_keys=True) for check in hidden["checks"]
    ]
    for blob in (json.dumps(data), json.dumps(bundle), junit_text):
        for secret in secrets:
            assert secret not in blob
    c07 = next(c for c in bundle["cases"] if c["case_id"] == "C07")
    assert (
        c07["content_withheld"]
        and "checks" not in c07
        and set(c07["check_digests"]) == {c["id"] for c in hidden["checks"]}
    )
    c01 = next(c for c in bundle["cases"] if c["case_id"] == "C01")
    assert c01["checks"] and c01["fixture"]["recipe"]["seed"] and "reproduce" in c01["command"]
    assert bundle["evaluator_digest"] == report.evaluator_digest and bundle["agent"]["spec"] == "inprocess:wrong_amount"
    operator = cli(
        "export",
        "--run",
        str(run_dir.path),
        "--format",
        "bundle",
        "--audience",
        "operator",
        "--output",
        str(tmp / "op.json"),
        cwd=tmp,
    )
    assert operator.returncode == 0
    assert hidden["script"][0]["message"] in (tmp / "op.json").read_text(encoding="utf-8")


def test_junit_marks_unknown_as_non_green_error(runs):
    tmp = runs["tmp"]
    _, run_dir = runs["unknown"]
    cli("export", "--run", str(run_dir.path), "--format", "junit", "--output", str(tmp / "u.xml"), cwd=tmp)
    junit = ET.parse(tmp / "u.xml").getroot()
    assert junit.get("errors") == "1" and junit.get("failures") == "0"
    assert [e.get("type") for e in junit.iter("error")] == ["susanoo.unresolved"]


# --- preflight and the existing subcommands ------------------------------------------------------


def test_preflight(tmp_path):
    assert cli("preflight", "--suite", str(SUITE), "--agent", "jsonl:correct", cwd=tmp_path).returncode == 0
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"allowed_fixture_adapters": ["other"]}), encoding="utf-8")
    assert cli("preflight", "--suite", str(SUITE), "--profile", str(profile), cwd=tmp_path).returncode == 3
    missing = cli("preflight", "--suite", str(SUITE), "--agent-cmd", "/nonexistent/agent", cwd=tmp_path)
    assert missing.returncode == 2


def test_existing_local_and_recover_commands_still_work(tmp_path):
    local = cli(
        "local",
        "--suite",
        str(SUITE),
        "--agent",
        "inprocess:correct",
        "--case",
        "C01",
        "--journal",
        "j.sqlite",
        cwd=tmp_path,
    )
    assert local.returncode == 0 and "READY_FOR_ACCEPTANCE" in local.stdout
    recover = cli("recover", "--journal", "j.sqlite", cwd=tmp_path)
    assert recover.returncode == 0 and "0 step(s)" in recover.stdout


def test_agent_that_never_starts_is_operational_not_an_observed_failure(tmp_path):
    result, run_dir = run(tmp_path, "--case", "C01", "--agent-cmd", sys.executable, "-c", "pass")
    attempt = run_dir.report().attempts[0]
    assert attempt.execution_state == "error" and attempt.business_verdict == "FAIL"  # nothing was written
    assert result.returncode == 2
