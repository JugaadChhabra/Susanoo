"""I08 CLI and OCI container agents: argv/digest policy, restricted runtime, teardown, crash evidence [Spec §9, §19].

Policy tests need no docker. Container tests build a throwaway ``susanoo-test-*`` image from the local
``python:3.12-slim`` and skip cleanly when docker, the base image or the pip index is unavailable.
"""

import os
import shutil
import subprocess
import sys
import textwrap
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from collections_demo import build_suite
from pydantic import SecretStr, ValidationError
from susanoo_connectors.container import (
    CliAgentAdapter,
    CliProfile,
    ContainerAgentAdapter,
    ContainerProfile,
    docker_run_argv,
    redact,
    validate_image_ref,
)
from susanoo_runner import Journal, LocalSuite, SimulatorServer, run_suite
from susanoo_sdk import AdapterError, InvocationContext, Message, ToolRoute
from susanoo_sdk.jsonl import FrameLimits

ROOT = Path(__file__).resolve().parents[2]
SUITE = LocalSuite.model_validate(build_suite())
DIGEST = "sha256:" + "a" * 64
CTX = InvocationContext(attempt_id="a1", namespace="ns", deadline=datetime.now(UTC) + timedelta(minutes=5))
PYTHONPATH = os.pathsep.join(p for p in sys.path if p)


# --- policy (no docker) -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ref",
    [DIGEST, f"agent@{DIGEST}", f"ghcr.io/acme/agent@{DIGEST}", f"localhost:5000/team/agent_x@{DIGEST}"],
)
def test_digest_pinned_images_are_accepted(ref):
    assert validate_image_ref(ref) == ref


@pytest.mark.parametrize(
    "ref",
    [
        "python:3.12-slim",
        "agent",
        "agent:latest",
        f"agent:1.0@{DIGEST}",
        "agent@sha256:abc",
        f"agent@sha512:{'a' * 128}",
        f"Agent@{DIGEST}",
        f"agent@{DIGEST.upper()}",
        f"--privileged@{DIGEST}",
    ],
)
def test_tags_and_unpinned_images_are_rejected(ref):
    with pytest.raises(ValueError):
        validate_image_ref(ref)
    with pytest.raises(ValidationError):
        ContainerProfile(image=ref)


def test_docker_argv_is_restricted_and_carries_no_secret_values(tmp_path):
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    profile = ContainerProfile(
        image=f"agent@{DIGEST}",
        command=["--mode", "correct"],
        artifact_dir=str(artifact),
        env_allowlist=["MODEL_API_KEY"],
        memory_mb=256,
        cpus=0.5,
        pids_limit=64,
    )
    adapter = ContainerAgentAdapter(profile, env={"MODEL_API_KEY": "sk-live-canary-value"})
    argv = adapter.argv
    assert argv == docker_run_argv(profile, adapter.container_name)
    joined = " ".join(argv)
    for flag in (
        "run --rm -i",
        "--pull never",
        "--network none",
        "--read-only",
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
        "--pids-limit 64",
        "--memory 256m",
        "--memory-swap 256m",
        "--cpus 0.5",
        "--user 65532:65532",
        "--log-driver none",
        f"type=bind,source={artifact.resolve()},target=/artifact,readonly",
        "--env MODEL_API_KEY ",
    ):
        assert flag in joined, flag
    assert "sk-live-canary-value" not in joined  # values reach docker through its env, never argv
    assert "--privileged" not in argv and "-v" not in argv and "--volume" not in argv
    assert argv[-3:] == [f"agent@{DIGEST}", "--mode", "correct"]
    assert adapter._process.env["MODEL_API_KEY"] == "sk-live-canary-value"


@pytest.mark.parametrize(
    "field,value",
    [
        ("network", "host"),
        ("network", "container:other"),
        ("network", "bad name"),
        ("user", "0"),
        ("user", "0:0"),
        ("user", "1000:0"),
        ("user", "root"),
        ("env_allowlist", ["PATH"]),
        ("env_allowlist", ["DOCKER_HOST"]),
        ("env_allowlist", ["lower"]),
        ("artifact_dir", "/"),
        ("artifact_dir", "relative/dir"),
        ("artifact_dir", "/proc"),
        ("artifact_dir", str(Path.home())),
        ("workdir", "relative"),
    ],
)
def test_profile_refuses_unsafe_runtime_settings(field, value):
    with pytest.raises(ValidationError):
        ContainerProfile(image=DIGEST, **{field: value})


def test_env_must_be_allowlisted_and_never_inherited(monkeypatch):
    monkeypatch.setenv("TEST_BACKEND_TOKEN", "host-value")
    profile = ContainerProfile(image=DIGEST, env_allowlist=["TEST_BACKEND_TOKEN"])
    with pytest.raises(ValueError, match="no value"):
        ContainerAgentAdapter(profile)  # the host value is not picked up implicitly
    with pytest.raises(ValueError, match="allowlist"):
        ContainerAgentAdapter(ContainerProfile(image=DIGEST), env={"OTHER": "x"})


def test_loopback_tool_route_is_rewritten_for_a_bridged_container():
    route = ToolRoute(base_url="http://127.0.0.1:8123", namespace="ns", token=SecretStr("tok-123456"))
    context = CTX.model_copy(update={"tool_route": route})
    bridged = ContainerAgentAdapter(
        ContainerProfile(image=DIGEST, network="bridge", tool_route_host="host.docker.internal")
    )
    assert bridged._route(context).tool_route.base_url == "http://host.docker.internal:8123"
    assert bridged._route(context).tool_route.token.get_secret_value() == "tok-123456"
    remote = context.model_copy(update={"tool_route": route.model_copy(update={"base_url": "https://sim.example"})})
    assert bridged._route(remote).tool_route.base_url == "https://sim.example"
    assert ContainerAgentAdapter(ContainerProfile(image=DIGEST))._route(context) is context


@pytest.mark.parametrize(
    "argv", ["python agent.py", ["sh", "-c", "python agent.py"], ["/bin/bash", "x"], ["env", "python"], ["pwsh"]]
)
def test_cli_refuses_shell_strings_and_shells(argv):
    with pytest.raises(ValidationError):
        CliProfile(argv=argv)


def test_cli_requires_an_existing_executable():
    with pytest.raises(ValueError, match="not found"):
        CliAgentAdapter(CliProfile(argv=["definitely-not-a-real-binary-susanoo"]))


def test_redaction_masks_known_values_and_credential_shapes():
    text = "auth failed for sk-live-canary; Authorization: Bearer abcdefgh12345678 api_key=zzzz9999 ok"
    out = redact(text, ["sk-live-canary"])
    assert "sk-live-canary" not in out and "abcdefgh12345678" not in out and "zzzz9999" not in out
    assert out.endswith("ok")


# --- CLI agent (real subprocess, no docker) -----------------------------------------------------------


@pytest.fixture(scope="module")
def simulator():
    with SimulatorServer() as server:
        yield server


async def test_cli_adapter_runs_the_collections_agent(simulator, tmp_path):
    def factory():
        profile = CliProfile(
            argv=[sys.executable, "-m", "collections_demo.serve", "--mode", "correct"], env={"PYTHONPATH": PYTHONPATH}
        )
        return CliAgentAdapter(profile)

    cases = ["C01", "C13", "C20"]
    report = await run_suite(
        SUITE,
        agent_factory=factory,
        agent_label="cli:correct",
        journal=Journal(tmp_path / "cli.sqlite"),
        simulator_url=simulator.base_url,
        simulator_admin_key=simulator.admin_key,
        case_ids=cases,
    )
    assert {a.case_id: a.business_verdict for a in report.attempts} == dict.fromkeys(cases, "PASS")


async def test_cli_crash_keeps_redacted_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("SUSANOO_RUNNER_TOKEN", "runner-canary")
    script = tmp_path / "crash.py"
    script.write_text(
        textwrap.dedent(
            """
            import os, sys
            runner = os.environ.get("SUSANOO_RUNNER_TOKEN", "absent")
            print("leaking " + os.environ.get("AGENT_SECRET", "?") + " runner=" + runner, file=sys.stderr)
            sys.exit(3)
            """
        )
    )
    adapter = CliAgentAdapter(
        CliProfile(argv=[sys.executable, str(script)], env={"AGENT_SECRET": "s3cr3t-value"}, limits=FrameLimits())
    )
    try:
        with pytest.raises(AdapterError) as error:
            await adapter.start_session(CTX)
        assert error.value.code == "process_exited"
    finally:
        await adapter.aclose()
    evidence = adapter.evidence
    assert evidence is not None and evidence.exit_code == 3 and evidence.adapter == "cli"
    assert "s3cr3t-value" not in evidence.stderr_tail and "[REDACTED]" in evidence.stderr_tail
    assert "runner=absent" in evidence.stderr_tail  # host secrets were never in the agent's environment


# --- OCI container (docker) ---------------------------------------------------------------------------


def _docker_ready() -> str | None:
    if shutil.which("docker") is None:
        return "docker CLI not installed"
    try:
        info = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, timeout=20)
    except subprocess.TimeoutExpired:
        return "docker daemon did not answer"
    if info.returncode != 0:
        return "docker daemon not running"
    base = subprocess.run(["docker", "image", "inspect", "python:3.12-slim"], capture_output=True, timeout=20)
    if base.returncode != 0:
        return "python:3.12-slim not present locally (no implicit pull in tests)"
    return None


@pytest.fixture(scope="module")
def agent_image(tmp_path_factory):
    reason = _docker_ready()
    if reason:
        pytest.skip(reason)
    sys.path.insert(0, str(ROOT / "examples/collections/container"))
    try:
        from prepare_context import prepare
    finally:
        sys.path.pop(0)
    context = prepare(tmp_path_factory.mktemp("container") / "ctx")
    tag = f"susanoo-test-collections-{uuid.uuid4().hex[:8]}"
    build = subprocess.run(
        ["docker", "build", "--pull=false", "-q", "-t", tag, str(context)], capture_output=True, text=True, timeout=600
    )
    if build.returncode != 0:
        pytest.skip(f"image build failed (offline pip index?): {build.stderr.strip()[-300:]}")
    image_id = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", tag], capture_output=True, text=True, check=True
    ).stdout.strip()
    try:
        yield image_id  # content-addressed local image ID: pinned, never a tag
    finally:
        subprocess.run(["docker", "rmi", "-f", tag], capture_output=True, timeout=120)


def _leftover_containers() -> list[str]:
    out = subprocess.run(
        ["docker", "ps", "-a", "--filter", "label=susanoo.adapter=container", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
    )
    return out.stdout.split()


def bridged(image: str, mode: str, **overrides) -> ContainerProfile:
    return ContainerProfile(
        image=image,
        command=["--mode", mode],
        network="bridge",  # explicitly approved so the agent can reach the loopback simulator route
        tool_route_host="host.docker.internal",
        memory_mb=256,
        **overrides,
    )


async def _run(simulator, tmp_path, profile: ContainerProfile, cases: list[str], label: str, created=None):
    def factory() -> ContainerAgentAdapter:
        adapter = ContainerAgentAdapter(profile)
        if created is not None:
            created.append(adapter)
        return adapter

    return await run_suite(
        SUITE,
        agent_factory=factory,
        agent_label=label,
        journal=Journal(tmp_path / f"{label}.sqlite"),
        simulator_url=simulator.base_url,
        simulator_admin_key=simulator.admin_key,
        case_ids=cases,
    )


async def test_container_agent_passes_and_is_torn_down(agent_image, simulator, tmp_path):
    created: list[ContainerAgentAdapter] = []
    cases = ["C01", "C14", "C20"]
    report = await _run(simulator, tmp_path, bridged(agent_image, "correct"), cases, "container-correct", created)
    assert {a.case_id: a.business_verdict for a in report.attempts} == dict.fromkeys(cases, "PASS")
    assert report.gate == "READY_FOR_ACCEPTANCE"
    assert len(created) == len(cases)  # a fresh container per attempt, each removed before returning
    leftover = set(_leftover_containers())
    assert all(a.removed is True and a.container_name not in leftover for a in created)


async def test_container_false_completion_fails_on_evidence(agent_image, simulator, tmp_path):
    report = await _run(simulator, tmp_path, bridged(agent_image, "false_completion"), ["C01", "C19"], "container-fc")
    assert {a.case_id: a.business_verdict for a in report.attempts} == {"C01": "FAIL", "C19": "FAIL"}


async def test_default_no_network_container_cannot_reach_tools(agent_image, simulator, tmp_path):
    profile = ContainerProfile(image=agent_image, command=["--mode", "correct"], tool_route_host="host.docker.internal")
    report = await _run(simulator, tmp_path, profile, ["C01", "C20"], "container-nonet")
    assert "PASS" not in {a.business_verdict for a in report.attempts}


async def test_container_health_check_and_preflight(agent_image):
    adapter = ContainerAgentAdapter(ContainerProfile(image=agent_image, command=["--mode", "correct"]))
    try:
        report = await adapter.preflight()
        assert report.adapter == "container" and report.checks["image_present"] and report.checks["preflight"]
        assert any("network none" in note for note in report.notes)
        session = await adapter.start_session(CTX)
        reply = await adapter.invoke(session, Message(text="hello"), CTX)
        assert reply.text == "I can't reach the account system right now."
    finally:
        await adapter.aclose()
    assert adapter.removed is True and adapter.container_name not in _leftover_containers()


async def test_container_crash_records_exit_code_and_is_removed(agent_image):
    adapter = ContainerAgentAdapter(ContainerProfile(image=agent_image, command=["--mode", "no_such_mode"]))
    try:
        with pytest.raises(AdapterError) as error:
            await adapter.preflight()
        assert error.value.code == "health_check_failed"
    finally:
        await adapter.aclose()
    evidence = adapter.evidence
    assert evidence is not None and evidence.exit_code == 2 and evidence.container_name == adapter.container_name
    assert "invalid choice" in evidence.stderr_tail
    assert adapter.removed is True and adapter.container_name not in _leftover_containers()


async def test_missing_image_is_reported_not_pulled():
    reason = _docker_ready()
    if reason:
        pytest.skip(reason)
    adapter = ContainerAgentAdapter(ContainerProfile(image=f"susanoo-test-absent@sha256:{'0' * 64}"))
    try:
        with pytest.raises(AdapterError) as error:
            await adapter.preflight()
        assert error.value.code == "image_missing"
    finally:
        await adapter.aclose()
