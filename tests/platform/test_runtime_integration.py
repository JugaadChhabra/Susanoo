"""Actual TCP API + PostgreSQL + journal + JSONL agent + independent HTTP simulator.

All business data is synthetic. This is not a live vendor or customer-system test.
"""

import asyncio
import sys
from pathlib import Path

import pytest
from collections_demo.suite import build_suite
from susanoo_runner.local import AppServer, SimulatorServer
from susanoo_runner.platform import PlatformClient, PlatformProfile, execute_claim
from susanoo_runner.platform import reconcile_stored
from susanoo_runner.journal import Journal
from susanoo_api.platform.database import transaction
from susanoo_runner.suite import LocalSuite
from susanoo_sdk import JsonlAgentProcess
from susanoo_contracts import content_hash
from susanoo_simulator.adapter import SimulatorFixtureAdapter

from conftest import approve, version


def setup_runtime(world):
    sample = LocalSuite.model_validate(build_suite()).cases[0]
    requirement = version(
        world,
        {
            "kind": "requirement",
            "source_clause": "INTEGRATED-PTP",
            "expected": "Exactly one promise",
            "owner": "Synthetic reviewer",
        },
    )
    approve(world, requirement)
    fixture = version(world, sample.fixture.model_dump(mode="json"))
    check = version(
        world,
        {
            "kind": "check",
            "requirement_version_id": requirement["id"],
            "operator": "count",
            "observation": "promises",
            "entity": "promise",
            "expected": 1,
        },
    )
    case = version(
        world,
        {
            "kind": "case",
            "fixture_version_id": fixture["id"],
            "check_version_ids": [check["id"]],
            "script": [turn.model_dump(mode="json") for turn in sample.script],
        },
    )
    suite = version(world, {"kind": "suite", "case_version_ids": [case["id"]]})
    approve(world, suite)
    environment = version(
        world,
        {
            "kind": "environment",
            "adapter": "susanoo_simulator",
            "version": "1.0",
            "allowed_operations": sample.fixture.recipe["seed"]["actors"]["agent"]["operations"],
            "identity_profile": "synthetic-agent",
            "fixture_version_id": fixture["id"],
        },
    )
    approve(world, environment)
    connection = version(
        world,
        {
            "kind": "connection",
            "family": "I06",
            "adapter": "locally-approved-collections",
            "capabilities": {"invoke": True, "observe": True},
        },
    )
    token = world["owner"].ok("POST", f"/projects/{world['project']['id']}/runner-registration")["token"]
    credential = world["owner"].ok(
        "POST",
        "/runner/register",
        {
            "registration_token": token,
            "name": "Real customer-side JSONL runner",
            "version": "integration-test",
            "capabilities": ["outcome_suite", "preflight"],
        },
    )["token"]
    world["owner"].ok(
        "POST",
        f"/projects/{world['project']['id']}/jobs",
        {
            "kind": "preflight",
            "version_ids": [connection["id"], environment["id"], suite["id"]],
        },
        headers={"Idempotency-Key": "real-preflight"},
    )
    return (
        credential,
        content_hash(connection["data"]),
        {
            "suite_version_id": suite["id"],
            "connection_version_id": connection["id"],
            "environment_version_id": environment["id"],
        },
    )


@pytest.mark.parametrize(
    "mode,outage,expected,gate,expire",
    [
        ("correct", False, "PASS", "READY_FOR_ACCEPTANCE", False),
        ("false_completion", False, "FAIL", "BLOCKED", False),
        ("correct", True, "UNKNOWN", "NEEDS_REVIEW", False),
        ("correct", False, "UNKNOWN", "BLOCKED", True),
    ],
)
def test_real_runtime_round_trip(world, tmp_path, mode, outage, expected, gate, expire, monkeypatch):
    token, connection_hash, launch = setup_runtime(world)
    profile = PlatformProfile(
        allowed_connection_adapters=["locally-approved-collections"], allowed_connection_hashes=[connection_hash]
    )

    async def execute(client, claim, simulator, name, observer_outage=False):
        fixtures = SimulatorFixtureAdapter(simulator.base_url, simulator.admin_key, observer_outage=observer_outage)
        try:
            return await execute_claim(
                client,
                claim,
                profile=profile,
                journal_path=Path(tmp_path) / f"{name}.sqlite",
                agent_factory=lambda: JsonlAgentProcess(
                    [
                        sys.executable,
                        "-m",
                        "collections_demo.serve",
                        "--mode",
                        name,
                    ]
                ),
                fixtures=fixtures,
            )
        finally:
            await fixtures.aclose()

    with AppServer(world["app"]) as api, SimulatorServer() as simulator:
        client = PlatformClient(api.base_url + "/api/v1", token)
        try:
            preflight = client.claim()
            assert preflight.kind == "preflight"
            preflight_result = asyncio.run(execute(client, preflight, simulator, "correct"))
            assert preflight_result.execution_state == "completed", preflight_result.model_dump()
            assert preflight_result.cleanup_state == "succeeded", preflight_result.model_dump()
            run = world["owner"].ok(
                "POST", f"/projects/{world['project']['id']}/runs", launch, headers={"Idempotency-Key": "real-outcome"}
            )
            claim = client.claim()
            assert claim.kind == "outcome_suite"
            observations = []
            if expire:
                invoke = JsonlAgentProcess.invoke

                async def expire_after_write(agent, *args, **kwargs):
                    reply = await invoke(agent, *args, **kwargs)
                    with transaction() as db:
                        db.execute(
                            "update platform.attempts set lease_expires_at=now()-interval '1 second' where id=%s",
                            (claim.attempt_id,),
                        )
                    return reply

                async def forbidden_observation(*args, **kwargs):
                    observations.append(True)
                    raise AssertionError("Observer must not mutate the simulated clock after authority is lost")

                monkeypatch.setattr(JsonlAgentProcess, "invoke", expire_after_write)
                monkeypatch.setattr(SimulatorFixtureAdapter, "observe", forbidden_observation)
            result = asyncio.run(execute(client, claim, simulator, mode, outage))
            assert result.business_verdict == expected
            if expire:
                assert result.execution_state == "interrupted" and result.reconciliation_state == "pending"
                assert observations == []
                path = Path(tmp_path) / f"{mode}.sqlite"
                journal = Journal(path)
                try:
                    assert journal.step(str(claim.attempt_id), "fixture-observe").dispatch_state == "prepared"
                    assert journal.step(str(claim.attempt_id), "fixture-cleanup").dispatch_state == "prepared"
                finally:
                    journal.close()
                late = reconcile_stored(client, path, str(claim.attempt_id))
                assert not late["dispatch_authority"]
                assert reconcile_stored(client, path, str(claim.attempt_id))["id"] == late["id"]
            persisted = world["owner"].ok("GET", f"/runs/{run['id']}")
            assert persisted["gate"] == gate
            assert persisted["counts"][expected.lower() if expected != "UNKNOWN" else "unknown"] == 1
        finally:
            client.close()
