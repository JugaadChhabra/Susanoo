from datetime import UTC, datetime
from multiprocessing import active_children
import time

import httpx
import pytest
from susanoo_evaluator.semantic import GraderRequest, Rubric, _call
from susanoo_runner.journal import Journal, JournalError
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
