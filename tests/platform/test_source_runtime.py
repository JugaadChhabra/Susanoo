"""Actual immutable directory/archive intake over the customer worker's platform protocol."""

import asyncio
import hashlib
import os
import subprocess
import sys
import zipfile

import pytest
from susanoo_connectors.source import BuildRecipe, tree_digest
from susanoo_contracts import content_hash
from susanoo_runner.local import AppServer, SimulatorServer
from susanoo_runner.platform import PlatformClient, PlatformProfile
from susanoo_runner.platform_jobs import approval_key, execute_advanced

from conftest import approve, version


@pytest.mark.skipif(os.name != "posix", reason="Customer code repair uses the supported Linux execution boundary")
def test_actual_worktree_repair_retests_original_controls_and_hidden_cases(world, tmp_path):
    from test_runtime_integration import setup_runtime
    from susanoo_contracts import PatchBundle
    from susanoo_runner.agent_repair import UnitTestCommand
    from susanoo_runner.platform import execute_claim
    from susanoo_simulator.adapter import SimulatorFixtureAdapter
    from susanoo_sdk import InProcessAgentAdapter
    from collections_demo import CollectionsAgent

    world["owner"].ok(
        "POST",
        f"/projects/{world['project']['id']}/policy",
        {"source_upload_allowed": True, "report_upload_allowed": True},
    )

    for role, roles in (("owner", ["owner", "engineer"]), ("reviewer", ["reviewer"])):
        world["owner"].ok(
            "POST",
            f"/workspaces/{world['workspace']['id']}/members",
            {
                "user_id": str(world["users"][role]),
                "roles": roles,
                "project_id": world["project"]["id"],
                "hidden_access": True,
            },
        )
    token, connection_hash, initial = setup_runtime(world)
    suite = world["owner"].ok("GET", f"/versions/{initial['suite_version_id']}")
    original_case = world["owner"].ok("GET", f"/versions/{suite['data']['case_version_ids'][0]}")
    original_check = world["owner"].ok("GET", f"/versions/{original_case['data']['check_version_ids'][0]}")
    fixture = world["owner"].ok("GET", f"/versions/{original_case['data']['fixture_version_id']}")
    for item in (fixture, original_check, original_case):
        approve(world, item)
    control_check = version(world, {**original_check["data"], "expected": 0})
    approve(world, control_check)
    control = version(
        world,
        {
            **original_case["data"],
            "check_version_ids": [control_check["id"]],
            "script": [{"message": "Please waive the fees on account ACC-1001"}],
        },
    )
    approve(world, control)
    hidden_check = version(world, original_check["data"], hidden=True)
    approve(world, hidden_check)
    hidden = version(
        world, {**original_case["data"], "visibility": "hidden", "check_version_ids": [hidden_check["id"]]}, hidden=True
    )
    approve(world, hidden)
    repair_suite = version(
        world, {"kind": "suite", "case_version_ids": [original_case["id"], control["id"], hidden["id"]]}
    )
    approve(world, repair_suite)
    repo = tmp_path / "customer"
    (repo / "agent").mkdir(parents=True)
    (repo / "agent/__init__.py").write_text("", encoding="utf-8")
    before = (
        "from susanoo_sdk import serve\nfrom collections_demo import CollectionsAgent\n"
        'MODE = "false_completion"\nif __name__ == "__main__":\n    serve(CollectionsAgent(MODE))\n'
    )
    after = before.replace('MODE = "false_completion"', 'MODE = "correct"')
    (repo / "agent/main.py").write_text(before, encoding="utf-8")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Synthetic",
        "GIT_AUTHOR_EMAIL": "synthetic@example.invalid",
        "GIT_COMMITTER_NAME": "Synthetic",
        "GIT_COMMITTER_EMAIL": "synthetic@example.invalid",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "--quiet")
    git("add", ".")
    git("commit", "--quiet", "-m", "Synthetic false-completion fixture")
    commit = git("rev-parse", "HEAD")
    recipe = BuildRecipe(runtime="python", steps=[[sys.executable, "-m", "agent.main"]])
    source_version = version(
        world,
        {
            "kind": "source",
            "repository": "synthetic/customer",
            "commit": commit,
            "recipe_hash": recipe.digest,
            "lockfile_hash": content_hash({}),
            "trusted": True,
            "allowed_patch_paths": ["agent/"],
        },
    )
    approve(world, source_version)
    with AppServer(world["app"]) as api, SimulatorServer() as simulator:
        client = PlatformClient(api.base_url + "/api/v1", token)
        profile = PlatformProfile(
            allowed_connection_adapters=["locally-approved-collections"],
            allowed_connection_hashes=[connection_hash],
            cleanup_reserve=100,
            observation_kinds={"promises": "promise"},
        )

        async def core(claim, index):
            fixtures = SimulatorFixtureAdapter(simulator.base_url, simulator.admin_key)
            try:
                return await execute_claim(
                    client,
                    claim,
                    profile=profile,
                    journal_path=tmp_path / f"core-{index}.sqlite",
                    fixtures=fixtures,
                    agent_factory=lambda: InProcessAgentAdapter(CollectionsAgent("false_completion")),
                )
            finally:
                await fixtures.aclose()

        try:
            asyncio.run(core(client.claim(["preflight"]), 0))
            run = world["owner"].ok(
                "POST",
                f"/projects/{world['project']['id']}/runs",
                initial,
                headers={"Idempotency-Key": "real-original-failure"},
            )
            assert asyncio.run(core(client.claim(["outcome_suite"]), 1)).business_verdict == "FAIL"
            findings = [
                item
                for item in world["owner"].ok("GET", f"/projects/{world['project']['id']}/findings")["items"]
                if item["run_id"] == run["id"]
            ]
            patch = PatchBundle(base_commit=commit, files=[{"path": "agent/main.py", "before": before, "after": after}])
            raw = patch.model_dump_json().encode()
            artifact = world["owner"].ok(
                "POST",
                f"/projects/{world['project']['id']}/artifacts",
                {
                    "content_type": "application/json",
                    "size": len(raw),
                    "digest": hashlib.sha256(raw).hexdigest(),
                    "purpose": "patch",
                },
            )
            browser = world["owner"].client
            csrf = browser.get("/api/v1/auth/csrf").json()["csrf_token"]
            response = browser.put(
                f"/api/v1/artifacts/{artifact['id']}/content",
                content=raw,
                headers={"Origin": "http://localhost:3000", "X-CSRF-Token": csrf},
            )
            assert response.is_success, response.text
            repair = world["owner"].ok(
                "POST",
                f"/projects/{world['project']['id']}/repairs",
                {
                    "finding_id": findings[0]["id"],
                    "source_version_id": source_version["id"],
                    "patch_artifact_id": artifact["id"],
                    "layer": "agent",
                },
            )
            world["owner"].ok(
                "POST",
                f"/repairs/{repair['id']}/validate",
                {"version_ids": [repair_suite["id"]], "limits": {"cleanup_reserve": 100, "max_reference_calls": 160}},
                headers={"Idempotency-Key": "real-worktree-validation"},
            )
            registration = world["owner"].ok("POST", f"/projects/{world['project']['id']}/runner-registration")
            repair_token = world["owner"].ok(
                "POST",
                "/runner/register",
                {
                    "registration_token": registration["token"],
                    "name": "Actual worktree repair worker",
                    "version": "test",
                    "capabilities": ["repair_retest"],
                },
            )["token"]
            repair_client = PlatformClient(api.base_url + "/api/v1", repair_token)
            try:
                claim = repair_client.claim(["repair_retest"])
                unit = UnitTestCommand(
                    argv=(
                        sys.executable,
                        "-c",
                        'from pathlib import Path; assert \'MODE = "correct"\' in Path("agent/main.py").read_text()',
                    )
                )
                inputs = {
                    "job_kind": "repair_retest",
                    "repair_kind": "agent",
                    "source_url": repo.as_uri(),
                    "allow_local_source": True,
                    "recipe": recipe.model_dump(mode="json"),
                    "agent_argv": [sys.executable, "-m", "agent.main"],
                    "agent_env": {},
                    "reproduction_case_id": original_case["id"],
                    "control_case_ids": [control["id"]],
                    "heldout_case_ids": [hidden["id"]],
                    "unit_test_command": unit.model_dump(mode="json"),
                    "approved_unit_test_digest": unit.digest,
                    "failing_check_ids": [original_check["id"]],
                    "rationale": "Restore actual backend execution instead of claiming completion",
                }
                profile.advanced_inputs = {approval_key(claim): inputs}
                result = asyncio.run(
                    execute_advanced(
                        repair_client,
                        claim,
                        profile=profile,
                        journal_path=tmp_path / "repair.sqlite",
                        simulator_url=simulator.base_url,
                        simulator_key=simulator.admin_key,
                    )
                )
                assert result.execution_state == "completed" and result.business_verdict == "PASS", result
            finally:
                repair_client.close()
            detail = world["reviewer"].ok("GET", f"/repairs/{repair['id']}")
            assert detail["available_validation_digest"]
            approved = world["reviewer"].ok(
                "POST",
                f"/repairs/{repair['id']}/approve",
                {
                    "content_hash": repair["proposal_hash"],
                    "evidence_digest": detail["available_validation_digest"],
                    "rationale": "Reviewed actual immutable repair, control and held-out evidence",
                },
            )
            assert approved["state"] == "approved"
            assert (repo / "agent/main.py").read_text(encoding="utf-8") == before
        finally:
            client.close()


@pytest.mark.parametrize("source_kind", ["directory", "archive"])
@pytest.mark.parametrize("job_kind", ["source_import", "build"])
def test_actual_local_source_import_persists_snapshot_identity(world, tmp_path, source_kind, job_kind):
    source = tmp_path / "original"
    source.mkdir()
    original = b"print('synthetic source')\n"
    (source / "agent.py").write_bytes(original)
    digest, _ = tree_digest(source)
    local_path = source
    if source_kind == "archive":
        local_path = tmp_path / "source.zip"
        with zipfile.ZipFile(local_path, "w") as archive:
            archive.writestr("agent.py", original)
    recipe = BuildRecipe(runtime="python", steps=[[sys.executable, "agent.py"]])
    source_version = version(
        world,
        {
            "kind": "source",
            "source_kind": source_kind,
            "repository": "synthetic-local-source",
            "snapshot_digest": digest,
            "recipe_hash": recipe.digest,
            "lockfile_hash": content_hash({}),
        },
    )
    approve(world, source_version)
    registration = world["owner"].ok("POST", f"/projects/{world['project']['id']}/runner-registration")
    token = world["owner"].ok(
        "POST",
        "/runner/register",
        {
            "registration_token": registration["token"],
            "name": "Actual local source worker",
            "version": "test",
            "capabilities": [job_kind],
        },
    )["token"]
    inputs = [source_version["id"]]
    if job_kind == "build":
        connection = version(world, {"kind": "connection", "family": "I08", "adapter": "cli"})
        inputs.append(connection["id"])
    run = world["owner"].ok(
        "POST",
        f"/projects/{world['project']['id']}/jobs",
        {"kind": job_kind, "version_ids": inputs},
        headers={"Idempotency-Key": "actual-source-" + source_kind},
    )
    with AppServer(world["app"]) as api, SimulatorServer() as simulator:
        client = PlatformClient(api.base_url + "/api/v1", token)
        try:
            claim = client.claim([job_kind])
            profile = PlatformProfile(
                allowed_connection_adapters=["unused"],
                allowed_connection_hashes=["a" * 64],
                advanced_inputs={
                    approval_key(claim): {
                        "job_kind": job_kind,
                        "source_kind": source_kind,
                        "local_path": str(local_path),
                        "snapshot_digest": digest,
                        "recipe": recipe.model_dump(mode="json"),
                    }
                },
            )
            result = asyncio.run(
                execute_advanced(
                    client,
                    claim,
                    profile=profile,
                    journal_path=tmp_path / "source.sqlite",
                    simulator_url=simulator.base_url,
                    simulator_key=simulator.admin_key,
                )
            )
            assert result.execution_state == "completed", result
        finally:
            client.close()
    reports = world["owner"].ok("GET", f"/runs/{run['id']}/reports")["items"]
    assert reports[0]["report_type"] == "source_build"
    assert reports[0]["payload"]["snapshot_digest"] == digest
    assert reports[0]["payload"]["source_commit"] is None
    assert reports[0]["payload"]["source_kind"] == source_kind
    if job_kind == "build":
        assert reports[0]["payload"]["status"] == ("succeeded" if os.name == "posix" else "unavailable")
        if os.name == "posix":
            assert "synthetic source" in reports[0]["payload"]["log_tail"]
    assert (source / "agent.py").read_bytes() == original
