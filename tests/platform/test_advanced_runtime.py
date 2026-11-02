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


@pytest.mark.parametrize("kind", ["simulator_inspect", "simulator_reset", "action_comparison",
                                  "reduce_reproduction", "trace_import", "incident_draft", "fresh_agent_pair"])
def test_actual_advanced_runtime_reports(world, tmp_path, kind):
    _, connection_hash, initial = setup_runtime(world)
    env = initial["environment_version_id"]
    inputs = {"job_kind": kind, "entity_kinds": ["account"]}
    versions = [env]
    if kind in {"action_comparison", "reduce_reproduction", "fresh_agent_pair"}:
        with_env = world["owner"].ok("GET", f"/versions/{env}")
        reference = version(world, {**with_env["data"], "identity_profile": "reference"})
        approve(world, reference)
        fidelity = version(world, {"kind": "fidelity", "simulator_version_id": env,
                                  "reference_version_id": reference["id"], "operations": ["read_account"],
                                  "identity_profile": "synthetic", "consistency_window_seconds": 0})
        approve(world, fidelity)
        versions = [fidelity["id"]]
        inputs.update(plan={"id": "read-plan", "actions": [{"step": "read", "operation": "read_account",
                                                            "request": {"account_id": "ACC-001"}}]},
                      violation_category="duplicate_write", reduction_budget=1)
        if kind == "fresh_agent_pair":
            old = world["owner"].ok("GET", f"/versions/{initial['connection_version_id']}")
            connection = version(world, {**old["data"], "capabilities": {
                **old["data"]["capabilities"], "tool_routing": True,
            }})
            connection_hash = content_hash(connection["data"])
            suite = world["owner"].ok("GET", f"/versions/{initial['suite_version_id']}")
            expanded = version(world, {**suite["data"], "limits": {
                **suite["data"]["limits"], "cleanup_reserve": 30, "max_reference_calls": 10,
            }})
            approve(world, expanded)
            versions.extend([connection["id"], expanded["id"]])
    if kind in {"trace_import", "incident_draft"}:
        connection = version(world, {"kind": "connection", "family": "I13", "adapter": "jsonl-evidence"})
        versions = [connection["id"]]
        inputs.update(data='{"conversation_id":"synthetic-1","messages":[{"role":"user","text":"help"}],'
                           '"outcome":[{"kind":"task","status":"failed"}]}', domain="collections")
    registration = world["owner"].ok("POST", f"/projects/{world['project']['id']}/runner-registration")
    token = world["owner"].ok("POST", "/runner/register", {"registration_token": registration["token"],
                                "name": "Actual advanced runtime", "version": "test", "capabilities": [kind]})["token"]
    run = world["owner"].ok("POST", f"/projects/{world['project']['id']}/jobs", {
        "kind": kind, "version_ids": versions, "limits": {"cleanup_reserve": 30, "max_reference_calls": 10},
    }, headers={"Idempotency-Key": "actual-advanced"})
    with AppServer(world["app"]) as api, SimulatorServer() as simulator:
        client = PlatformClient(api.base_url + "/api/v1", token)
        try:
            claim = client.claim([kind])
            profile = PlatformProfile(allowed_connection_adapters=["locally-approved-collections"],
                                      allowed_connection_hashes=[connection_hash], cleanup_reserve=30,
                                      observation_kinds={"promises": "promise"},
                                      advanced_inputs={approval_key(claim): inputs})
            result = asyncio.run(execute_advanced(client, claim, profile=profile,
                                                  journal_path=tmp_path / "advanced.sqlite",
                                                  simulator_url=simulator.base_url,
                                                  simulator_key=simulator.admin_key,
                                                  agent_factory=lambda: InProcessAgentAdapter(
                                                      CollectionsAgent("correct"))))
            assert result.execution_state == "completed", result
        finally:
            client.close()
    stored = world["owner"].ok("GET", f"/runs/{run['id']}/reports")["items"]
    assert stored and all(item["content_hash"] == content_hash(item["payload"]) for item in stored)
    if kind == "fresh_agent_pair":
        assert all(stored[0]["payload"][side]["execution_state"] == "completed" for side in ("simulator", "reference"))
        assert all(stored[0]["payload"][side]["business_verdict"] == "PASS" for side in ("simulator", "reference"))
