from datetime import UTC, datetime, timedelta
from multiprocessing import active_children
import time
from types import SimpleNamespace

import httpx
import pytest
from susanoo_evaluator.semantic import GraderRequest, Rubric, _call
from susanoo_runner.journal import Journal, JournalError
from susanoo_runner.ci import EXIT_OPERATIONAL
from susanoo_sdk import FixtureHandle, StepIntent
from susanoo_simulator.adapter import SimulatorFixtureAdapter
from susanoo_simulator.domains import get_domain


def test_probe_settlement_is_idempotent_and_cannot_refund_in_flight_twice(tmp_path):
    journal = Journal(tmp_path / "settlement.sqlite")
    try:
        journal.set_budget("probe", "reference", 10)
        assert journal.reserve("probe", "reference", "operation", 8)
        journal.settle_reservation("probe", "operation", 3)
        journal.settle_reservation("probe", "operation", 3)
        assert journal.budget("probe", "reference") == (10, 3)
        with pytest.raises(JournalError, match="Completed allowance"):
            journal.reserve("probe", "reference", "operation", 8)
        with pytest.raises(JournalError):
            journal.settle_reservation("probe", "operation", 0)
        assert journal.budget("probe", "reference") == (10, 3)
    finally:
        journal.close()


async def test_reconciliation_does_not_attribute_an_unrelated_namespace_write():
    def log(_request):
        return httpx.Response(
            200,
            json={
                "actions": [
                    {
                        "operation_id": "other-operation",
                        "mutating": True,
                        "status": "ok",
                        "operation": "create_promise",
                        "faults": [],
                    }
                ]
            },
        )

    adapter = SimulatorFixtureAdapter("http://synthetic.test", "synthetic", transport=httpx.MockTransport(log))
    adapter._tokens["namespace"] = {"observer_token": "synthetic"}
    try:
        answer = await adapter.reconcile(
            StepIntent(
                attempt_id="a",
                step_id="s",
                operation_id="uncertain-operation",
                action_kind="agent_invoke",
                request_digest="a" * 64,
                prepared_at=datetime.now(UTC),
            ),
            FixtureHandle(namespace="namespace", seed_digest="b" * 64, prepared_at=datetime.now(UTC)),
        )
        assert answer.state == "unresolved" and answer.finding == "indeterminate"
    finally:
        await adapter.aclose()


def test_collision_domain_is_registered():
    assert get_domain("shared_ledger").name == "shared_ledger"


class HungProvider:
    def grade(self, request, *, timeout):
        time.sleep(30)
        return {"label": "pass"}


def test_timed_out_semantic_worker_is_terminated():
    existing = {child.pid for child in active_children()}
    status, _ = _call(
        HungProvider(),
        GraderRequest(
            grader_version_id="synthetic",
            model_id="no-provider-call",
            prompt="Synthetic",
            rubric=Rubric(
                id="synthetic", version="1", criteria="Synthetic", pass_definition="Yes", fail_definition="No"
            ),
            content={},
        ),
        0.05,
    )
    assert status == "provider_timeout"
    assert {child.pid for child in active_children()} <= existing


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({}, 0),
        ({"cleanup_state": "failed"}, EXIT_OPERATIONAL),
        ({"reconciliation_state": "pending"}, EXIT_OPERATIONAL),
        ({"execution_state": "interrupted"}, EXIT_OPERATIONAL),
        ({"business_verdict": "UNKNOWN"}, EXIT_OPERATIONAL),
        ({"business_verdict": "FAIL"}, 1),
        ({"business_verdict": "FAIL", "execution_state": "interrupted"}, EXIT_OPERATIONAL),
    ],
)
def test_platform_worker_exit_cannot_hide_operational_failure(changes, expected):
    from susanoo_runner.cli import _platform_exit_code

    values = {
        "execution_state": "completed",
        "business_verdict": "PASS",
        "cleanup_state": "succeeded",
        "reconciliation_state": "not_needed",
    }
    assert _platform_exit_code(SimpleNamespace(**(values | changes))) == expected


@pytest.mark.parametrize("failed_suffix", ["/events", "/reservations"])
def test_dispatch_acknowledgement_loss_permanently_fences_attempt(tmp_path, failed_suffix):
    from susanoo_runner.executor import CancelToken
    from susanoo_runner.platform import PlatformJournal

    posts = []
    now = datetime.now(UTC)

    def post(path, body):
        posts.append(path)
        if path.endswith(failed_suffix):
            raise httpx.ReadTimeout("synthetic lost acknowledgement")
        if path.endswith("/control"):
            return {
                "server_time": now.isoformat(),
                "lease_expires_at": (now + timedelta(seconds=30)).isoformat(),
                "deadline": (now + timedelta(seconds=60)).isoformat(),
                "cancel_requested": False,
            }
        return {}

    token = CancelToken()
    journal = PlatformJournal(
        tmp_path / "partition.sqlite", SimpleNamespace(post=post),
        SimpleNamespace(attempt_id="attempt", lease_token="synthetic"), token,
    )
    try:
        journal.begin_attempt(
            attempt_id="attempt", run_id="run", case_id="case", namespace="namespace",
            manifest={}, manifest_hash="a" * 64, fencing_generation=1, deadline=now + timedelta(seconds=60),
        )
        journal.prepare_step("attempt", "turn", "agent_invoke", "b" * 64)
        with pytest.raises(httpx.ReadTimeout):
            journal.mark_dispatching("attempt", "turn")
        assert journal.lost_authority and token.requested
        assert journal.step("attempt", "turn").dispatch_state == "prepared"
        before = list(posts)
        with pytest.raises(JournalError, match="cannot resume"):
            journal.mark_dispatching("attempt", "turn")
        assert posts == before
    finally:
        journal.close()
