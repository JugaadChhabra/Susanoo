"""Runner restart and capacity: the runtime half of T39/T40 [Spec §23, §25].

The restart test kills a real ``susanoo-runner local`` process mid-suite, then recovers the journal it left.
Clean install, migrations and backup/restore are platform gates (Aarav) and are not claimed here.
"""

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from collections_demo import CollectionsAgent, build_suite
from susanoo_runner import Journal, LocalSuite, SimulatorServer
from susanoo_runner.capacity import benchmark, percentile
from susanoo_sdk import InProcessAgentAdapter

ROOT = Path(__file__).resolve().parents[2]
SUITE = LocalSuite.model_validate(build_suite())
PATHS = ["packages/evaluator", "packages/simulator", "packages/sdk-python", "packages/runner", "examples/collections"]


def runner_env() -> dict[str, str]:
    return {**os.environ, "PYTHONPATH": os.pathsep.join(str(ROOT / p) for p in PATHS)}


def attempts_started(journal: Path) -> int:
    if not journal.exists():
        return 0
    try:
        with sqlite3.connect(f"file:{journal}?mode=ro", uri=True, timeout=1) as db:
            return db.execute("select count(*) from attempts").fetchone()[0]
    except sqlite3.OperationalError:
        return 0


def test_killed_runner_restarts_into_a_consistent_journal(tmp_path):
    journal_path = tmp_path / "journal.sqlite"
    process = subprocess.Popen(
        [sys.executable, "-m", "susanoo_runner.cli", "local", "--suite", str(ROOT / "examples/collections/suite.json")]
        + ["--agent", "jsonl:correct", "--journal", str(journal_path)],
        env=runner_env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 60
        while attempts_started(journal_path) < 4:
            assert process.poll() is None, "runner finished before it could be interrupted"
            assert time.monotonic() < deadline, "runner never started enough attempts"
            time.sleep(0.05)
    finally:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)

    journal = Journal(journal_path)
    try:
        assert journal._db.execute("pragma integrity_check").fetchone()[0] == "ok"
        before = {
            row["attempt_id"]: (row["business_verdict"], row["finalized_at"])
            for row in journal._db.execute("select * from attempts where finalized_at is not null")
        }
        uncertain = journal.recover()
        rows = [dict(r) for r in journal._db.execute("select * from attempts")]
        steps = [dict(r) for r in journal._db.execute("select * from steps")]
    finally:
        journal.close()

    # Finalized attempts keep their verdicts; every other attempt is interrupted with reconciliation pending.
    assert before, "expected at least one attempt to finish before the kill"
    for row in rows:
        if row["attempt_id"] in before:
            assert (row["business_verdict"], row["finalized_at"]) == before[row["attempt_id"]]
        else:
            assert row["execution_state"] == "interrupted" and row["reconciliation_state"] == "pending"
    # Nothing is left mid-dispatch, and every step the runner may have sent is marked for read-back, not resent.
    assert not [s for s in steps if s["dispatch_state"] in ("dispatching", "prepared")]
    assert {s.step_id for s in uncertain} <= {s["step_id"] for s in steps if s["dispatch_state"] == "uncertain"}

    # Recovering again on restart is idempotent.
    again = Journal(journal_path)
    try:
        assert again.recover() == []
    finally:
        again.close()


def test_percentile_is_nearest_rank():
    values = [float(v) for v in range(1, 21)]
    assert percentile(values, 0.95) == 19.0
    assert percentile(values, 0.5) == 10.0
    assert percentile([3.0], 0.95) == 3.0
    assert percentile([], 0.95) == 0.0


@pytest.mark.parametrize("concurrency", [1, 3])
async def test_capacity_benchmark_records_measured_numbers(tmp_path, concurrency):
    with SimulatorServer() as simulator:
        report = await benchmark(
            SUITE,
            agent_factory=lambda: InProcessAgentAdapter(CollectionsAgent("correct")),
            simulator_url=simulator.base_url,
            simulator_admin_key=simulator.admin_key,
            journal_path=tmp_path / "capacity.sqlite",
            concurrency=concurrency,
            case_ids=["C01", "C02", "C03", "C04", "C05"],
        )
    assert report.attempts == 5 * concurrency and report.passed == report.attempts
    assert report.execution_errors == 0 and set(report.gates) == {"READY_FOR_ACCEPTANCE"}
    assert 0 < report.attempt_p50_seconds <= report.attempt_p95_seconds <= report.attempt_max_seconds
    assert report.peak_rss_mb > 0 and report.journal_bytes > 0
    json.loads(report.model_dump_json())
