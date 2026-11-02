"""Real advanced worker -> TCP API/Postgres -> durable journal -> HTTP simulator -> typed report."""

import asyncio

import pytest
from collections_demo import CollectionsAgent
from susanoo_sdk import InProcessAgentAdapter
from susanoo_runner.local import AppServer, SimulatorServer
from susanoo_runner.platform import PlatformClient, PlatformProfile
from susanoo_runner.platform_jobs import approval_key, execute_advanced
from susanoo_contracts import content_hash

from conftest import approve, version
from test_runtime_integration import setup_runtime


@pytest.mark.parametrize(
    "kind",
    [
        "simulator_inspect",
        "simulator_reset",
        "action_comparison",
        "reduce_reproduction",
        "trace_import",
        "incident_draft",
        "fresh_agent_pair",
        "two_by_two",
    ],
)
def test_actual_advanced_runtime_reports(world, tmp_path, kind):
    matrix = kind == "two_by_two"
    kind = "fresh_agent_pair" if matrix else kind
    cleanup_reserve = 60 if matrix else 30
    _, connection_hash, initial = setup_runtime(world)
    env = initial["environment_version_id"]
    inputs = {"job_kind": kind, "entity_kinds": ["account"]}
    if matrix:
        inputs.update(mode="two_by_two", repetitions=1)
    versions = [env]
    if kind in {"action_comparison", "reduce_reproduction", "fresh_agent_pair"}:
        with_env = world["owner"].ok("GET", f"/versions/{env}")
        reference = version(world, {**with_env["data"], "identity_profile": "reference"})
        approve(world, reference)
        fidelity = version(
            world,
            {
                "kind": "fidelity",
                "simulator_version_id": env,
                "reference_version_id": reference["id"],
                "operations": ["read_account"],
                "identity_profile": "synthetic",
                "consistency_window_seconds": 0,
            },
        )
        approve(world, fidelity)
        versions = [fidelity["id"]]
        inputs.update(
            plan={
                "id": "read-plan",
                "actions": [{"step": "read", "operation": "read_account", "request": {"account_id": "ACC-001"}}],
            },
            violation_category="duplicate_write",
            reduction_budget=1,
        )
        if kind == "fresh_agent_pair":
            old = world["owner"].ok("GET", f"/versions/{initial['connection_version_id']}")
            connection = version(
                world,
                {
                    **old["data"],
                    "capabilities": {
                        **old["data"]["capabilities"],
                        "tool_routing": True,
                    },
                },
            )
            connection_hash = content_hash(connection["data"])
            suite = world["owner"].ok("GET", f"/versions/{initial['suite_version_id']}")
            expanded = version(
                world,
                {
                    **suite["data"],
                    "limits": {
                        **suite["data"]["limits"],
                        "cleanup_reserve": cleanup_reserve,
                        "max_reference_calls": 10,
                    },
                },
            )
            approve(world, expanded)
            versions.extend([connection["id"], expanded["id"]])
    if kind in {"trace_import", "incident_draft"}:
        connection = version(world, {"kind": "connection", "family": "I13", "adapter": "jsonl-evidence"})
        versions = [connection["id"]]
        inputs.update(
            data='{"conversation_id":"synthetic-1","messages":[{"role":"user","text":"help"}],'
            '"outcome":[{"kind":"task","status":"failed"}]}',
            domain="collections",
        )
    registration = world["owner"].ok("POST", f"/projects/{world['project']['id']}/runner-registration")
    token = world["owner"].ok(
        "POST",
        "/runner/register",
        {
            "registration_token": registration["token"],
            "name": "Actual advanced runtime",
            "version": "test",
            "capabilities": [kind],
        },
    )["token"]
    run = world["owner"].ok(
        "POST",
        f"/projects/{world['project']['id']}/jobs",
        {
            "kind": kind,
            "version_ids": versions,
            "limits": {"cleanup_reserve": cleanup_reserve, "max_reference_calls": 10},
        },
        headers={"Idempotency-Key": "actual-advanced"},
    )
    with AppServer(world["app"]) as api, SimulatorServer() as simulator:
        client = PlatformClient(api.base_url + "/api/v1", token)
        try:
            claim = client.claim([kind])
            profile = PlatformProfile(
                allowed_connection_adapters=["locally-approved-collections"],
                allowed_connection_hashes=[connection_hash],
                cleanup_reserve=cleanup_reserve,
                observation_kinds={"promises": "promise"},
                advanced_inputs={approval_key(claim): inputs},
            )
            result = asyncio.run(
                execute_advanced(
                    client,
                    claim,
                    profile=profile,
                    journal_path=tmp_path / "advanced.sqlite",
                    simulator_url=simulator.base_url,
                    simulator_key=simulator.admin_key,
                    agent_factory=lambda: InProcessAgentAdapter(CollectionsAgent("correct")),
                    agent_factories={
                        label: lambda: InProcessAgentAdapter(CollectionsAgent("correct")) for label in ("old", "new")
                    },
                )
            )
            assert result.execution_state == "completed", result
        finally:
            client.close()
    stored = world["owner"].ok("GET", f"/runs/{run['id']}/reports")["items"]
    assert stored and all(item["content_hash"] == content_hash(item["payload"]) for item in stored)
    if matrix:
        assert stored[0]["report_type"] == "two_by_two"
        cells = stored[0]["payload"]["cells"]
        assert {(cell["agent"], cell["environment"]) for cell in cells} == {
            (agent, environment) for agent in ("old", "new") for environment in ("old", "new")
        }
        assert all(
            result["business_verdict"] == "PASS" and result["execution_state"] == "completed"
            for cell in cells
            for result in cell["results"]
        )
    elif kind == "fresh_agent_pair":
        assert all(stored[0]["payload"][side]["execution_state"] == "completed" for side in ("simulator", "reference"))
        assert all(stored[0]["payload"][side]["business_verdict"] == "PASS" for side in ("simulator", "reference"))


def test_actual_seeded_discrepancy_then_budgeted_reduction(world, tmp_path):
    from collections_demo import fidelity as demo
    from susanoo_contracts import FixtureSpec

    contract = demo.contract()
    environments = []
    for side in (contract.simulator, contract.reference):
        recipe = {"seed": side.seed_for(contract.fixture).model_dump(mode="json")}
        fixture = version(
            world,
            FixtureSpec(
                adapter="susanoo_simulator",
                recipe=recipe,
                digest=content_hash(recipe),
                compatibility_key="synthetic-fidelity",
            ).model_dump(mode="json"),
        )
        env = version(
            world,
            {
                "kind": "environment",
                "adapter": "susanoo_simulator",
                "version": side.domain_version,
                "allowed_operations": contract.operations,
                "identity_profile": "same-synthetic-actor",
                "fixture_version_id": fixture["id"],
                "fault_profile": side.faults.model_dump(mode="json"),
            },
        )
        approve(world, env)
        environments.append(env)
    fidelity = version(
        world,
        {
            "kind": "fidelity",
            "simulator_version_id": environments[0]["id"],
            "reference_version_id": environments[1]["id"],
            "operations": contract.operations,
            "identity_profile": "same-synthetic-actor",
            "consistency_window_seconds": 0,
            "normalizers": [value.model_dump(mode="json") for value in contract.normalizers],
            "intentional_simplifications": [
                value.model_dump(mode="json") for value in contract.intentional_simplifications
            ],
        },
    )
    approve(world, fidelity)
    registration = world["owner"].ok("POST", f"/projects/{world['project']['id']}/runner-registration")
    token = world["owner"].ok(
        "POST",
        "/runner/register",
        {
            "registration_token": registration["token"],
            "name": "Actual discrepancy worker",
            "version": "test",
            "capabilities": ["action_comparison", "reduce_reproduction", "probe"],
        },
    )["token"]
    with AppServer(world["app"]) as api, SimulatorServer() as simulator:
        client = PlatformClient(api.base_url + "/api/v1", token)
        try:
            for kind in ("action_comparison", "reduce_reproduction"):
                run = world["owner"].ok(
                    "POST",
                    f"/projects/{world['project']['id']}/jobs",
                    {
                        "kind": kind,
                        "version_ids": [fidelity["id"]],
                        "limits": {"cleanup_reserve": 200, "max_reference_calls": 100},
                    },
                    headers={"Idempotency-Key": "seeded-" + kind},
                )
                claim = client.claim([kind])
                inputs = {
                    "job_kind": kind,
                    "entity_kinds": contract.entity_kinds,
                    "plan": demo.SESSION.model_dump(mode="json"),
                    "violation_category": "duplicate_write",
                    "reduction_budget": 3,
                }
                profile = PlatformProfile(
                    allowed_connection_adapters=["unused"],
                    allowed_connection_hashes=["a" * 64],
                    cleanup_reserve=200,
                    max_reference_calls=100,
                    advanced_inputs={approval_key(claim): inputs},
                )
                result = asyncio.run(
                    execute_advanced(
                        client,
                        claim,
                        profile=profile,
                        journal_path=tmp_path / (kind + ".sqlite"),
                        simulator_url=simulator.base_url,
                        simulator_key=simulator.admin_key,
                    )
                )
                assert result.execution_state == "completed", result
                reports = world["owner"].ok("GET", f"/runs/{run['id']}/reports")["items"]
                payload = reports[0]["payload"]
                if kind == "action_comparison":
                    assert result.business_verdict == "FAIL"
                    assert {value["category"] for value in payload["candidates"]} >= {
                        "duplicate_write",
                        "access_control",
                    }
                    assert all(value["classification"] == "unresolved" for value in payload["candidates"])
                else:
                    assert payload["runs_used"] <= 3
                    assert payload["reduced_plan"]["actions"]
                    assert payload["stopped_because"] != "original_not_reproduced"
        finally:
            client.close()


def test_actual_probe_compares_equal_budget_strategies(world, tmp_path):
    _, _, initial = setup_runtime(world)
    environment = initial["environment_version_id"]
    reference = version(world, world["owner"].ok("GET", f"/versions/{environment}")["data"])
    approve(world, reference)
    fidelity = version(
        world,
        {
            "kind": "fidelity",
            "simulator_version_id": environment,
            "reference_version_id": reference["id"],
            "operations": ["read_account"],
            "identity_profile": "synthetic",
            "consistency_window_seconds": 0,
        },
    )
    approve(world, fidelity)
    suite = world["owner"].ok("GET", f"/versions/{initial['suite_version_id']}")
    case_id = suite["data"]["case_version_ids"][0]
    probe = version(
        world,
        {
            "kind": "probe",
            "fidelity_version_id": fidelity["id"],
            "strategy": "priority",
            "seed": 7,
            "max_reference_calls": 4,
            "cleanup_reserve": 1,
            "candidate_ids": [case_id],
        },
    )
    approve(world, probe)
    run = world["owner"].ok(
        "POST",
        f"/projects/{world['project']['id']}/jobs",
        {"kind": "probe", "version_ids": [probe["id"]], "limits": {"cleanup_reserve": 30, "max_reference_calls": 4}},
        headers={"Idempotency-Key": "actual-equal-budget-probe"},
    )
    registration = world["owner"].ok("POST", f"/projects/{world['project']['id']}/runner-registration")
    token = world["owner"].ok(
        "POST",
        "/runner/register",
        {
            "registration_token": registration["token"],
            "name": "Actual probing worker",
            "version": "test",
            "capabilities": ["probe"],
        },
    )["token"]
    with AppServer(world["app"]) as api, SimulatorServer() as simulator:
        client = PlatformClient(api.base_url + "/api/v1", token)
        try:
            claim = client.claim(["probe"])
            profile = PlatformProfile(
                allowed_connection_adapters=["unused"],
                allowed_connection_hashes=["a" * 64],
                cleanup_reserve=30,
                advanced_inputs={
                    approval_key(claim): {
                        "job_kind": "probe",
                        "entity_kinds": ["account"],
                        "probe_candidates": [
                            {
                                "case_id": case_id,
                                "candidate": {"id": case_id, "transition": "read account", "severity": "high"},
                                "plan": {
                                    "id": "read",
                                    "actions": [
                                        {
                                            "step": "read",
                                            "operation": "read_account",
                                            "request": {"account_id": "ACC-1001"},
                                        }
                                    ],
                                },
                            }
                        ],
                    }
                },
            )
            result = asyncio.run(
                execute_advanced(
                    client,
                    claim,
                    profile=profile,
                    journal_path=tmp_path / "probe.sqlite",
                    simulator_url=simulator.base_url,
                    simulator_key=simulator.admin_key,
                )
            )
            assert result.execution_state == "completed", result
        finally:
            client.close()
    reports = world["owner"].ok("GET", f"/runs/{run['id']}/reports")["items"]
    runs = [report["payload"] for report in reports if report["report_type"] == "probe_run"]
    assert len(runs) == 2 and all(item["reference_calls"] == 1 for item in runs)
    assert len({item["budget_cap"] for item in runs}) == 1
    assert all(item["executions"][0]["status"] == "no_finding" for item in runs)
