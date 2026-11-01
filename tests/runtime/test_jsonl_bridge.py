"""JSONL bridge: real subprocesses, bounded frames, malformed output is an adapter error [Spec §10]."""

import os
import sys
import textwrap
from datetime import UTC, datetime, timedelta

import pytest
from susanoo_sdk import AdapterError, FrameLimits, InvocationContext, JsonlAgentProcess, Message

CTX = InvocationContext(attempt_id="a1", namespace="ns", deadline=datetime.now(UTC) + timedelta(minutes=5))
ENV = {"PYTHONPATH": os.pathsep.join(sys.path)}


def agent(tmp_path, body: str, **limits) -> JsonlAgentProcess:
    path = tmp_path / "agent.py"
    path.write_text(textwrap.dedent(body))
    return JsonlAgentProcess([sys.executable, str(path)], env=ENV, limits=FrameLimits(**limits))


ECHO = """
    from susanoo_sdk import Reply, SusanooAgent, serve
    class Echo(SusanooAgent):
        async def respond(self, session_id, message, context):
            print("diagnostic goes to stderr")  # must not corrupt the protocol channel
            return Reply(text="echo: " + message.text)
    serve(Echo())
"""


async def test_round_trip_and_stdout_isolation(tmp_path):
    process = agent(tmp_path, ECHO)
    try:
        report = await process.preflight()
        assert report.capabilities.invoke
        session = await process.start_session(CTX)
        for index in range(3):
            reply = await process.invoke(session, Message(text=f"hi {index}"), CTX)
            assert reply.text == f"echo: hi {index}"
            assert reply.usage.unavailable  # never fabricated
        await process.close_session(session)
    finally:
        await process.aclose()
    assert b"diagnostic goes to stderr" in process.stderr_tail


async def test_environment_is_sanitized(tmp_path, monkeypatch):
    monkeypatch.setenv("SUSANOO_RUNNER_TOKEN", "runner-secret-canary")
    process = agent(
        tmp_path,
        """
        import os
        from susanoo_sdk import Reply, SusanooAgent, serve
        class Leak(SusanooAgent):
            async def respond(self, session_id, message, context):
                return Reply(text=os.environ.get("SUSANOO_RUNNER_TOKEN", "absent"))
        serve(Leak())
        """,
    )
    try:
        session = await process.start_session(CTX)
        assert (await process.invoke(session, Message(text="x"), CTX)).text == "absent"
    finally:
        await process.aclose()


@pytest.mark.parametrize(
    "body,code",
    [
        (
            "import sys\nsys.stdout.write('not json\\n'); sys.stdout.flush()\nimport time; time.sleep(5)\n",
            "malformed_frame",
        ),
        (
            "import sys\nsys.stdout.write('x' * 5000 + '\\n'); sys.stdout.flush()\nimport time; time.sleep(5)\n",
            "frame_too_large",
        ),
        ("import sys\nsys.exit(3)\n", "process_exited"),
    ],
)
async def test_contract_violations_fail_loudly(tmp_path, body, code):
    process = agent(tmp_path, body, max_frame_bytes=1024, request_deadline_seconds=5)
    try:
        with pytest.raises(AdapterError) as error:
            await process.start_session(CTX)
        assert error.value.code == code
    finally:
        await process.aclose()


async def test_slow_agent_hits_the_adapter_deadline_and_late_reply_is_ignored(tmp_path):
    process = agent(
        tmp_path,
        """
        import asyncio
        from susanoo_sdk import Reply, SusanooAgent, serve
        class Slow(SusanooAgent):
            async def respond(self, session_id, message, context):
                if message.text == "slow":
                    await asyncio.sleep(1.5)
                return Reply(text=message.text)
        serve(Slow())
        """,
        request_deadline_seconds=5,
    )
    try:
        session = await process.start_session(CTX)
        process.limits = process.limits.model_copy(update={"request_deadline_seconds": 0.5})
        with pytest.raises(AdapterError) as error:
            await process.invoke(session, Message(text="slow"), CTX)
        assert error.value.code == "deadline_exceeded"
        import asyncio

        await asyncio.sleep(1.5)  # the late reply arrives and must not break the bridge
        assert (await process.invoke(session, Message(text="fast"), CTX)).text == "fast"
    finally:
        await process.aclose()


async def test_child_processes_die_with_the_attempt(tmp_path):
    marker = tmp_path / "child.pid"
    process = agent(
        tmp_path,
        f"""
        import subprocess, sys
        from susanoo_sdk import Reply, SusanooAgent, serve
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        open({str(marker)!r}, "w").write(str(child.pid))
        class A(SusanooAgent):
            async def respond(self, session_id, message, context):
                return Reply(text="ok")
        serve(A())
        """,
    )
    await process.start_session(CTX)
    await process.aclose()
    import time

    pid = int(marker.read_text())
    time.sleep(0.2)
    import psutil
    assert not psutil.pid_exists(pid)
