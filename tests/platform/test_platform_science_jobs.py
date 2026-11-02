import asyncio
import base64
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from susanoo_contracts import GraderSpec, VoiceSpec, content_hash
from susanoo_runner.journal import Journal
from susanoo_runner.local import AppServer, SimulatorServer
from susanoo_runner.platform import PlatformClient, PlatformProfile
from susanoo_runner.platform_jobs import approval_key, execute_advanced
from susanoo_evaluator import evaluator_digest, parse_check
from susanoo_contracts import CheckSpec

from susanoo_evaluator import Control, ObservedState, calibrate, control_set_hash
from conftest import approve, version


def _check():
    return parse_check(
        {
            "id": "count-promises",
            "requirement_id": "promises-exist",
            "observation": "promises",
            "operator": "count",
            "expected": 1,
        }
    )


def _control(identifier, label, *, verified=False):
    return Control(
        id=identifier,
        label=label,
        observations=[
            ObservedState(
                name="promises",
                source="collections_backend_read",
                availability="available",
                completeness="complete_for_scope",
                window_seconds=10,
                window_elapsed=True,
                records=[{"id": "p-1"}] if label == "positive" else [],
            )
        ],
        started_at=datetime(2026, 11, 3, tzinfo=UTC),
        violation_verified=verified,
    )


def test_deterministic_calibration_rejects_unverified_negative_control():
    result = calibrate(
        _check(),
        [
            _control("positive-1", "positive"),
            _control("negative-1", "negative"),
        ],
    )

    assert not result.accepted
    assert result.invalid_negative_controls == ["negative-1"]


def test_calibration_rejects_duplicate_controls_and_negative_thresholds():
    control = _control("positive-1", "positive")
    with pytest.raises(ValueError, match="unique"):
        calibrate(_check(), [control, control])
    with pytest.raises(ValueError, match="cannot be negative"):
        calibrate(_check(), [control], max_missed_defects=-1)


def test_invalid_negative_control_never_counts_as_a_verified_calibration_sample():
    result = calibrate(
        _check(),
        [
            _control("positive-1", "positive"),
            _control("negative-1", "negative", verified=False),
        ],
    )

    assert result.invalid_negative_controls == ["negative-1"]
    assert result.missed_defects == []
    assert result.inconclusive == []
    assert result.accepted is False


def _job_runner(world, kind):
    registration = world["owner"].ok("POST", f"/projects/{world['project']['id']}/runner-registration")
    return world["owner"].ok(
        "POST",
        "/runner/register",
        {
            "registration_token": registration["token"],
            "name": f"Actual {kind} science worker",
            "version": "integration-test",
            "capabilities": [kind],
        },
    )["token"]


def test_actual_claim_calibrates_pinned_deterministic_controls(world, tmp_path):
    _, _, initial = __import__("test_runtime_integration").setup_runtime(world)
    suite = world["owner"].ok("GET", f"/versions/{initial['suite_version_id']}")
    source_case = world["owner"].ok("GET", f"/versions/{suite['data']['case_version_ids'][0]}")
    check_row = world["owner"].ok("GET", f"/versions/{source_case['data']['check_version_ids'][0]}")
    check = parse_check(CheckSpec.model_validate(check_row["data"]).evaluator_definition(check_row["id"]))
    ids = []
    for _title in ("Positive control", "Negative control", "Held-out negative control"):
        row = version(world, {**source_case["data"], "modality": "text"})
        ids.append(row["id"])
    controls = [
        Control(
            id=ids[0],
            label="positive",
            observations=[
                ObservedState(
                    name="promises",
                    source="collections_backend_read",
                    availability="available",
                    completeness="complete_for_scope",
                    window_seconds=10,
                    window_elapsed=True,
                    records=[{"id": "p-1"}],
                )
            ],
            started_at=datetime(2026, 11, 3, tzinfo=UTC),
        ),
        Control(
            id=ids[1],
            label="negative",
            violation_verified=True,
            observations=[
                ObservedState(
                    name="promises",
                    source="collections_backend_read",
                    availability="available",
                    completeness="complete_for_scope",
                    window_seconds=10,
                    window_elapsed=True,
                    records=[],
                )
            ],
            started_at=datetime(2026, 11, 3, tzinfo=UTC),
        ),
        Control(
            id=ids[2],
            label="negative",
            violation_verified=True,
            split="heldout",
            observations=[
                ObservedState(
                    name="promises",
                    source="collections_backend_read",
                    availability="available",
                    completeness="complete_for_scope",
                    window_seconds=10,
                    window_elapsed=True,
                    records=[],
                )
            ],
            started_at=datetime(2026, 11, 3, tzinfo=UTC),
        ),
    ]
    grader_data = GraderSpec(
        evaluator_digest=evaluator_digest(),
        control_set_hash=control_set_hash(check, controls),
        positive_control_ids=[UUID(ids[0])],
        negative_control_ids=[UUID(ids[1]), UUID(ids[2])],
        heldout_control_ids=[UUID(ids[2])],
    ).model_dump(mode="json")
    grader = version(world, grader_data)
    run = world["owner"].ok(
        "POST",
        f"/projects/{world['project']['id']}/jobs",
        {
            "kind": "calibration",
            "version_ids": [grader["id"], check_row["id"]],
        },
        headers={"Idempotency-Key": "actual-science-calibration"},
    )
    token = _job_runner(world, "calibration")
    with AppServer(world["app"]) as api, SimulatorServer() as simulator:
        client = PlatformClient(api.base_url + "/api/v1", token)
        try:
            claim = client.claim(["calibration"])
            inputs = {
                "job_kind": "calibration",
                "check_version_id": check_row["id"],
                "controls": [item.model_dump(mode="json") for item in controls],
            }
            profile = PlatformProfile(
                allowed_connection_adapters=["not-used"],
                allowed_connection_hashes=["0" * 64],
                advanced_inputs={approval_key(claim): inputs},
            )
            result = asyncio.run(
                execute_advanced(
                    client,
                    claim,
                    profile=profile,
                    journal_path=Path(tmp_path) / "science-calibration.sqlite",
                    simulator_url=simulator.base_url,
                    simulator_key=simulator.admin_key,
                )
            )
            assert result.execution_state == "completed", result
            assert result.business_verdict == "PASS"
        finally:
            client.close()
    reports = world["owner"].ok("GET", f"/runs/{run['id']}/reports")["items"]
    assert len(reports) == 1
    assert reports[0]["report_type"] == "deterministic_calibration"
    assert reports[0]["payload"]["accepted"] is True
    assert reports[0]["payload"]["control_set_hash"] == grader_data["control_set_hash"]


def test_actual_claim_runs_local_synthetic_media_websocket_and_stores_audio_locally(world, tmp_path):
    _, _, initial = __import__("test_runtime_integration").setup_runtime(world)
    old_suite = world["owner"].ok("GET", f"/versions/{initial['suite_version_id']}")
    old_case = world["owner"].ok("GET", f"/versions/{old_suite['data']['case_version_ids'][0]}")
    audio_case = version(world, {**old_case["data"], "modality": "audio"})
    suite = version(world, {"kind": "suite", "case_version_ids": [audio_case["id"]]})
    approve(world, suite)
    connection = version(
        world,
        {
            "kind": "connection",
            "family": "I12",
            "adapter": "websocket_media",
            "capabilities": {
                "invoke": True,
                "stream": True,
                "sessions": True,
                "multi_turn": True,
                "audio": True,
                "modalities": ["audio"],
            },
        },
    )
    voice = version(
        world,
        VoiceSpec(
            provider="synthetic", authorization_reference="local-synthetic-media", recording_consent=True
        ).model_dump(mode="json"),
    )
    approve(world, voice)
    run = world["owner"].ok(
        "POST",
        f"/projects/{world['project']['id']}/jobs",
        {
            "kind": "voice",
            "version_ids": [voice["id"], connection["id"], suite["id"]],
            "limits": {
                "case_timeout_seconds": 30,
                "invocation_timeout_seconds": 5,
                "max_agent_invocations": 1,
                "max_output_bytes": 131072,
                "max_storage_bytes": 1048576,
            },
        },
        headers={"Idempotency-Key": "actual-synthetic-voice"},
    )
    token = _job_runner(world, "voice")
    journal_path = Path(tmp_path) / "science-voice.sqlite"
    clip = {
        "codec": "mulaw",
        "sample_rate": 8000,
        "data": base64.b64encode(bytes([255] * 1600)).decode("ascii"),
        "metadata": {"fixture": "synthetic"},
    }
    with AppServer(world["app"]) as api, SimulatorServer() as simulator:
        client = PlatformClient(api.base_url + "/api/v1", token)
        try:
            claim = client.claim(["voice"])
            inputs = {"job_kind": "voice", "media_script": [{"label": "caller-1", "clip": clip}]}
            profile = PlatformProfile(
                allowed_connection_adapters=["websocket_media"],
                allowed_connection_hashes=[content_hash(connection["data"])],
                advanced_inputs={approval_key(claim): inputs},
            )
            result = asyncio.run(
                execute_advanced(
                    client,
                    claim,
                    profile=profile,
                    journal_path=journal_path,
                    simulator_url=simulator.base_url,
                    simulator_key=simulator.admin_key,
                )
            )
            assert result.execution_state == "completed", result
            assert result.business_verdict == "UNKNOWN"
        finally:
            client.close()
    reports = world["owner"].ok("GET", f"/runs/{run['id']}/reports")["items"]
    assert len(reports) == 1 and reports[0]["report_type"] == "media"
    metrics = reports[0]["payload"]
    assert metrics["modality"] == "audio"
    assert metrics["response_latency"]["count"] == 1
    assert metrics["response_latency"]["method"].startswith("agent audio start")
    assert metrics["transcript_quality"]["reason"].startswith("no reference transcript")
    local = Journal(journal_path)
    try:
        evidence = local.evidence(str(claim.attempt_id), "science_job")
    finally:
        local.close()
    session = next(row for row in evidence if row.get("provider") == "synthetic_local_websocket_echo")
    assert session["provider"] == "synthetic_local_websocket_echo"
    assert session["connection_status"] == "contract_tested" and session["live_verified"] is False
    assert session["sent_chunks"] > 0 and session["resent_chunks"] == 0
    assert session["stored_media"] and all(not item["uploadable"] for item in session["stored_media"])
    media_dir = Path(tmp_path) / "media" / str(claim.attempt_id)
    assert list(media_dir.glob("*.wav"))


def test_actual_claim_detects_shared_state_collision(world, tmp_path):
    from susanoo_simulator.domains import shared_ledger
    seed = shared_ledger.seed(datetime(2026, 11, 3, tzinfo=UTC), ["actor-1", "actor-2"],
                              [shared_ledger.account("ACC-1", balance=100000)])
    recipe = {"seed": seed.model_dump(mode="json")}
    fixture = version(world, {"kind": "fixture", "adapter": "susanoo_simulator", "recipe": recipe,
        "digest": content_hash(recipe), "compatibility_key": "shared-ledger-1"})
    operations = list(shared_ledger.DOMAIN.operations)
    environment = version(world, {"kind": "environment", "adapter": "susanoo_simulator",
        "version": shared_ledger.VERSION, "fixture_version_id": fixture["id"],
        "allowed_operations": operations, "identity_profile": "synthetic-two-actors"})
    approve(world, environment)
    collision = version(world, {"kind": "collision", "environment_version_id": environment["id"],
        "barriers": ["set_balance"], "repetitions": 1})
    approve(world, collision)
    run = world["owner"].ok("POST", f"/projects/{world['project']['id']}/jobs", {
        "kind": "collision", "version_ids": [collision["id"]]},
        headers={"Idempotency-Key": "actual-shared-state"})
    token = _job_runner(world, "collision")
    with AppServer(world["app"]) as api, SimulatorServer() as simulator:
        client = PlatformClient(api.base_url + "/api/v1", token)
        try:
            claim = client.claim(["collision"])
            scripts = {f"actor-{index}": [
                {"operation": "read_account", "request": {"account_id": "ACC-1"}},
                {"operation": "set_balance", "request": {"account_id": "ACC-1",
                    "balance_minor": 100000 - amount, "amount_minor": amount, "payment_ref": f"P-{index}"}},
            ] for index, amount in ((1, 10000), (2, 20000))}
            profile = PlatformProfile(allowed_connection_adapters=["unused"],
                allowed_connection_hashes=["a" * 64], advanced_inputs={approval_key(claim): {
                    "job_kind": "collision", "actor_scripts": scripts}})
            result = asyncio.run(execute_advanced(client, claim, profile=profile,
                journal_path=tmp_path / "collision.sqlite", simulator_url=simulator.base_url,
                simulator_key=simulator.admin_key))
            assert result.execution_state == "completed", result
        finally:
            client.close()
    reports = world["owner"].ok("GET", f"/runs/{run['id']}/reports")["items"]
    assert reports[0]["payload"]["caught"] is True
    assert "lost_update" in reports[0]["payload"]["defects_found"]
