"""I07 TypeScript SDK: the Node agent speaks the same JSONL protocol through ``JsonlAgentProcess`` [Spec §10, §19].

Verdicts come from simulator evidence, not from the agent's words: the TS port in ``false_completion`` mode
claims success without writing and must FAIL. Skips when Node (>=22.18, native type stripping) is missing.
"""

import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from collections_demo import build_suite
from susanoo_runner import Journal, LocalSuite, SimulatorServer, run_suite
from susanoo_sdk import AdapterError, FrameLimits, InvocationContext, JsonlAgentProcess, Message

ROOT = Path(__file__).resolve().parents[2]
SERVE = ROOT / "examples/collections/ts-agent/serve.ts"
NODE = shutil.which("node")
SUITE = LocalSuite.model_validate(build_suite())
CTX = InvocationContext(attempt_id="a1", namespace="ns", deadline=datetime.now(UTC) + timedelta(minutes=5))


def _node_strips_types() -> bool:
    if NODE is None:
        return False
    probe = subprocess.run([NODE, "-e", "process.exit(process.features.typescript ? 0 : 1)"], capture_output=True)
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(not _node_strips_types(), reason="node with native TypeScript stripping not found")


def ts_agent(mode: str, **limits) -> JsonlAgentProcess:
    assert NODE is not None
    return JsonlAgentProcess(
        [NODE, str(SERVE), "--mode", mode], cwd=str(ROOT), limits=FrameLimits(**limits), name=f"ts-{mode}"
    )


@pytest.fixture(scope="module")
def simulator():
    with SimulatorServer() as server:
        yield server


async def run(simulator, tmp_path, mode: str, cases: list[str]):
    return await run_suite(
        SUITE,
        agent_factory=lambda: ts_agent(mode),
        agent_label=f"ts:{mode}",
        journal=Journal(tmp_path / f"ts-{mode}.sqlite"),
        simulator_url=simulator.base_url,
        simulator_admin_key=simulator.admin_key,
        case_ids=cases,
    )


async def test_correct_typescript_agent_passes(simulator, tmp_path):
    cases = ["C01", "C05", "C11", "C13", "C14", "C15", "C19", "C20"]
    report = await run(simulator, tmp_path, "correct", cases)
    assert {a.case_id: a.business_verdict for a in report.attempts} == dict.fromkeys(cases, "PASS")
    assert report.gate == "READY_FOR_ACCEPTANCE"


async def test_false_completion_typescript_agent_fails_on_evidence(simulator, tmp_path):
    report = await run(simulator, tmp_path, "false_completion", ["C01", "C11", "C19", "C20"])
    verdicts = {a.case_id: a.business_verdict for a in report.attempts}
    assert verdicts == {"C01": "FAIL", "C11": "FAIL", "C19": "FAIL", "C20": "PASS"}  # the dispute is still handed off
    assert report.gate == "BLOCKED"


async def test_protocol_round_trip_and_unknown_cancel():
    process = ts_agent("timeout_after_write", request_deadline_seconds=10)
    try:
        report = await process.preflight()
        assert report.adapter == "jsonl" and report.capabilities.invoke
        session = await process.start_session(CTX)
        for text in ("hello", "ACC-1001 here"):  # session continuity across turns; no route means no tools
            reply = await process.invoke(session, Message(text=text), CTX)
            assert reply.text == "I can't reach the account system right now."
            assert reply.usage.unavailable and reply.session_id == session.session_id
        cancel = await process.cancel("inv-not-running")
        assert cancel.acknowledged is False
        await process.close_session(session)
    finally:
        await process.aclose()


async def test_unknown_mode_is_a_process_exit_not_a_result():
    process = ts_agent("no_such_mode", request_deadline_seconds=10)
    try:
        with pytest.raises(AdapterError) as error:
            await process.preflight()
        assert error.value.code == "process_exited"
    finally:
        await process.aclose()
    assert b"unknown mode" in process.stderr_tail
