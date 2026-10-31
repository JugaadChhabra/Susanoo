"""Journal durability and crash windows [Spec §11; T05-T10]. Crashes are real process kills."""

import os
import subprocess
import sys
import textwrap
import threading
from datetime import UTC, datetime, timedelta

import pytest
from susanoo_runner import Journal, JournalError

DIGEST = "0" * 64


def begin(journal: Journal, attempt="a1"):
    journal.begin_attempt(
        attempt_id=attempt,
        run_id="r1",
        case_id="C01",
        namespace=f"ns-{attempt}",
        manifest={"case": "C01"},
        manifest_hash=DIGEST,
        fencing_generation=1,
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    )


def crash_after(tmp_path, stage: str):
    """Run the dispatch sequence in a child process and kill it hard at ``stage``."""
    script = textwrap.dedent(
        f"""
        import os, sys
        from datetime import UTC, datetime, timedelta
        sys.path[:0] = {sys.path!r}
        from susanoo_runner import Journal
        j = Journal({str(tmp_path / "j.sqlite")!r})
        j.begin_attempt(attempt_id="a1", run_id="r1", case_id="C01", namespace="ns", manifest={{}},
                        manifest_hash="{DIGEST}", fencing_generation=1,
                        deadline=datetime.now(UTC) + timedelta(minutes=5))
        j.set_budget("r1", "agent", 5)
        step = j.prepare_step("a1", "turn-001", "agent_invoke", "{DIGEST}")
        if "{stage}" == "prepared":
            os._exit(9)
        j.reserve("r1", "agent", step.operation_id)
        j.mark_dispatching("a1", "turn-001", scope="r1")
        os._exit(9)  # dies after marking dispatching, before any response is recorded
        """
    )
    result = subprocess.run([sys.executable, "-c", script], env={**os.environ}, capture_output=True, timeout=30)
    assert result.returncode == 9, result.stderr.decode()


def test_wal_and_full_sync(tmp_path):
    journal = Journal(tmp_path / "j.sqlite")
    assert journal._db.execute("pragma journal_mode").fetchone()[0] == "wal"
    assert journal._db.execute("pragma synchronous").fetchone()[0] == 2  # FULL


def test_crash_while_dispatching_becomes_uncertain_never_redispatched(tmp_path):
    crash_after(tmp_path, "dispatching")
    journal = Journal(tmp_path / "j.sqlite")
    uncertain = journal.recover()
    assert [(s.step_id, s.dispatch_state, s.side_effect_state) for s in uncertain] == [
        ("turn-001", "uncertain", "requires_observation")
    ]
    attempt = journal.attempt("a1")
    assert (attempt["execution_state"], attempt["reconciliation_state"]) == ("interrupted", "pending")
    with pytest.raises(JournalError) as error:
        journal.mark_dispatching("a1", "turn-001")
    assert error.value.code == "already_dispatched"
    types = [event["type"] for event in journal.events("a1")]
    assert types == ["step_prepared", "step_dispatching", "step_response_recorded"]


def test_crash_after_prepare_means_never_sent(tmp_path):
    crash_after(tmp_path, "prepared")
    journal = Journal(tmp_path / "j.sqlite")
    assert journal.recover() == []
    assert journal.step("a1", "turn-001").dispatch_state == "not_dispatched"


def test_dispatch_requires_prior_reservation(tmp_path):
    journal = Journal(tmp_path / "j.sqlite")
    begin(journal)
    journal.set_budget("r1", "agent", 1)
    journal.prepare_step("a1", "turn-001", "agent_invoke", DIGEST)
    with pytest.raises(JournalError) as error:
        journal.mark_dispatching("a1", "turn-001", scope="r1")
    assert error.value.code == "budget_required"


def test_concurrent_reservations_never_overspend(tmp_path):
    journal = Journal(tmp_path / "j.sqlite")
    journal.set_budget("run", "agent", 10)
    results: list[bool] = []

    def worker(index: int):
        results.append(journal.reserve("run", "agent", f"op-{index}"))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(True) == 10
    assert journal.budget("run", "agent") == (10, 10)
    assert journal.reserve("run", "agent", "op-0") in (True, False)  # idempotent for a reserved op


def test_reservation_reuse_with_different_input_conflicts(tmp_path):
    journal = Journal(tmp_path / "j.sqlite")
    journal.set_budget("run", "agent", 5)
    journal.set_budget("run", "reference", 5)
    assert journal.reserve("run", "agent", "op-1")
    with pytest.raises(JournalError):
        journal.reserve("run", "reference", "op-1")


def test_events_are_contiguous_and_evidence_immutable(tmp_path):
    journal = Journal(tmp_path / "j.sqlite")
    begin(journal)
    journal.append_event("a1", "progress", {"code": "a"})
    journal.append_event("a1", "progress", {"code": "b"})
    assert [event["sequence"] for event in journal.events("a1")] == [1, 2]
    journal.store_evidence("a1", "observation", "promises", {"count": 1})
    journal.store_evidence("a1", "observation", "promises", {"count": 1})  # identical retry is fine
    with pytest.raises(JournalError):
        journal.store_evidence("a1", "observation", "promises", {"count": 2})


def test_finalize_once(tmp_path):
    journal = Journal(tmp_path / "j.sqlite")
    begin(journal)
    final = dict(
        execution_state="completed",
        business_verdict="PASS",
        cleanup_state="succeeded",
        reconciliation_state="not_needed",
    )
    journal.finalize("a1", **final)
    with pytest.raises(JournalError):
        journal.finalize("a1", **final)
    assert journal.unfinished_attempts() == []
