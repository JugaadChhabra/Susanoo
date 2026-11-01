"""Bounded JSONL bridge between the runner and an agent process (I06/I07/I08) [Spec §10].

Frames are ``{protocol_version, request_id, type, payload}``, one per line. Standard output carries
protocol frames only; diagnostics go to stderr. Frame size, total bytes, outstanding requests and
parse deadlines are bounded. A malformed frame is an adapter error, never a passing result.
The process is started from an argument vector with a sanitized environment, never through a shell.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
import threading
import uuid
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .adapters import AdapterError
from .agent import SusanooAgent, to_agent_reply
from .models import (
    AgentReply,
    CancelResult,
    CapabilityReport,
    InvocationContext,
    JsonlFrame,
    Message,
    SessionHandle,
)

BASE_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "TZ", "SYSTEMROOT", "TMPDIR")


class FrameLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_frame_bytes: int = Field(default=1_048_576, ge=1024)
    max_total_bytes: int = Field(default=64 * 1_048_576, ge=1024)
    max_outstanding: int = Field(default=4, ge=1, le=64)
    request_deadline_seconds: float = Field(default=120.0, gt=0)
    max_stderr_bytes: int = Field(default=65_536, ge=0)


def encode(frame: JsonlFrame) -> bytes:
    return (json.dumps(frame.model_dump(mode="json"), separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def decode(line: bytes, limits: FrameLimits) -> JsonlFrame:
    if len(line) > limits.max_frame_bytes:
        raise AdapterError("frame_too_large", f"Frame of {len(line)} bytes exceeds {limits.max_frame_bytes}")
    try:
        return JsonlFrame.model_validate(json.loads(line))
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError) as error:
        raise AdapterError("malformed_frame", f"Malformed protocol frame: {type(error).__name__}") from None


def sanitized_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {key: os.environ[key] for key in BASE_ENV_KEYS if key in os.environ}
    env["PYTHONUNBUFFERED"] = "1"
    env.update(extra or {})
    return env


class JsonlAgentProcess:
    """``AgentAdapter`` over a subprocess speaking JSONL on stdin/stdout."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        limits: FrameLimits | None = None,
        name: str = "jsonl",
    ):
        if not argv or not all(isinstance(part, str) and part for part in argv):
            raise ValueError("argv must be a non-empty list of strings")
        self.argv, self.env, self.cwd, self.name = list(argv), env, cwd, name
        self.limits = limits or FrameLimits()
        self.stderr_tail = bytearray()
        self._proc: asyncio.subprocess.Process | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._broken: AdapterError | None = None
        self._total = 0
        self._tasks: list[asyncio.Task] = []
        self._abandoned: set[str] = set()
        self._job = None

    async def start(self) -> None:
        if self._proc is not None:
            return
        self._proc = await asyncio.create_subprocess_exec(
            *self.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=sanitized_env(self.env),
            cwd=self.cwd,
            start_new_session=True,
            limit=self.limits.max_frame_bytes + 1,
        )
        if os.name == "nt":
            from .process_tree import WindowsJob

            try:
                self._job = WindowsJob(self._proc.pid)
            except OSError as error:
                self._proc.kill()
                await self._proc.wait()
                raise AdapterError(
                    "process_isolation_unavailable", "Cannot own the Windows agent process tree"
                ) from error
        self._tasks = [asyncio.create_task(self._read_stdout()), asyncio.create_task(self._read_stderr())]

    def _fail(self, error: AdapterError) -> None:
        if self._broken is None:
            self._broken = error
        for future in self._pending.values():
            if not future.done():
                future.set_exception(self._broken)
        self._pending.clear()
        self._kill()

    async def _read_stdout(self) -> None:
        assert self._proc and self._proc.stdout
        try:
            while True:
                try:
                    line = await self._proc.stdout.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    raise AdapterError("frame_too_large", "Frame exceeds the configured limit") from None
                if not line:
                    raise AdapterError("process_exited", f"Agent process exited (code {self._proc.returncode})")
                self._total += len(line)
                if self._total > self.limits.max_total_bytes:
                    raise AdapterError("output_limit", "Agent exceeded the total output limit")
                frame = decode(line, self.limits)
                if frame.type == "event":
                    continue
                if frame.request_id in self._abandoned:
                    self._abandoned.discard(frame.request_id)  # late reply after the caller gave up
                    continue
                future = self._pending.pop(frame.request_id, None)
                if future is None:
                    raise AdapterError("unexpected_frame", f"No outstanding request {frame.request_id}")
                if future.done():
                    continue
                if frame.type == "error":
                    future.set_exception(
                        AdapterError(str(frame.payload.get("code", "agent_error")), str(frame.payload.get("message")))
                    )
                elif frame.type == "result":
                    future.set_result(frame.payload)
                else:
                    raise AdapterError("unexpected_frame", f"Agent sent a {frame.type} frame")
        except AdapterError as error:
            self._fail(error)
        except asyncio.CancelledError:
            raise

    async def _read_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        while chunk := await self._proc.stderr.read(4096):
            room = self.limits.max_stderr_bytes - len(self.stderr_tail)
            if room > 0:
                self.stderr_tail.extend(chunk[:room])

    async def _request(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        await self.start()
        if self._broken:
            raise self._broken
        if len(self._pending) >= self.limits.max_outstanding:
            raise AdapterError("too_many_outstanding", "Outstanding request limit reached")
        request_id = uuid.uuid4().hex
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        assert self._proc and self._proc.stdin
        try:
            self._proc.stdin.write(encode(JsonlFrame(request_id=request_id, type=kind, payload=payload)))
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            self._fail(AdapterError("process_exited", "Agent process closed its input"))
        try:
            return await asyncio.wait_for(future, self.limits.request_deadline_seconds)
        except TimeoutError:
            raise AdapterError("deadline_exceeded", f"{kind} exceeded the adapter deadline") from None
        finally:
            if self._pending.pop(request_id, None) is not None:
                self._abandoned.add(request_id)

    async def preflight(self) -> CapabilityReport:
        return CapabilityReport.model_validate(await self._request("preflight", {}))

    async def start_session(self, context: InvocationContext) -> SessionHandle:
        payload = {"context": _context_json(context)}
        return SessionHandle.model_validate(await self._request("start_session", payload))

    async def invoke(self, session: SessionHandle, message: Message, context: InvocationContext) -> AgentReply:
        invocation_id = f"inv-{uuid.uuid4().hex[:12]}"
        payload = {
            "invocation_id": invocation_id,
            "session": session.model_dump(mode="json"),
            "message": message.model_dump(mode="json"),
            "context": _context_json(context),
        }
        reply = AgentReply.model_validate(await self._request("invoke", payload))
        if reply.invocation_id != invocation_id or reply.session_id != session.session_id:
            raise AdapterError("correlation_mismatch", "Reply does not match the invocation")
        return reply

    async def cancel(self, invocation_id: str) -> CancelResult:
        return CancelResult.model_validate(await self._request("cancel", {"invocation_id": invocation_id}))

    async def close_session(self, session: SessionHandle) -> None:
        await self._request("close_session", {"session": session.model_dump(mode="json")})

    def _kill(self) -> None:
        # Kill the whole session group even if the leader already exited: children it spawned share it.
        if self._proc:
            if self._job:
                self._job.close()
                return
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self._proc.pid, signal.SIGKILL)

    async def aclose(self) -> None:
        """Stop the process and its whole process group; nothing the agent spawned outlives the attempt."""
        if self._proc is None:
            return
        if self._proc.returncode is None and not self._broken:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._request("shutdown", {}), 2)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._proc.wait(), 2)
        self._kill()
        await self._proc.wait()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)


def _context_json(context: InvocationContext) -> dict[str, Any]:
    data = context.model_dump(mode="json")
    if context.tool_route is not None:
        # The route token is the one secret an agent legitimately needs; it is bound server-side.
        data["tool_route"]["token"] = context.tool_route.token.get_secret_value()
    return data


async def _serve(agent: SusanooAgent, protocol_out) -> None:
    loop = asyncio.get_running_loop()
    write_lock = asyncio.Lock()
    invocations: dict[str, asyncio.Task] = {}
    handlers: set[asyncio.Task] = set()

    async def send(frame: JsonlFrame) -> None:
        async with write_lock:
            protocol_out.write(encode(frame))
            protocol_out.flush()

    async def handle(frame: JsonlFrame) -> None:
        payload = frame.payload
        try:
            if frame.type == "preflight":
                result = CapabilityReport(
                    adapter="jsonl", adapter_version=agent.version, capabilities=agent.capabilities
                ).model_dump(mode="json")
            elif frame.type == "start_session":
                context = InvocationContext.model_validate(payload["context"])
                session_id = await agent.start_session(context)
                result = SessionHandle(session_id=session_id, attempt_id=context.attempt_id).model_dump(mode="json")
            elif frame.type == "invoke":
                context = InvocationContext.model_validate(payload["context"])
                session = SessionHandle.model_validate(payload["session"])
                task = asyncio.create_task(
                    agent.respond(session.session_id, Message.model_validate(payload["message"]), context)
                )
                invocations[payload["invocation_id"]] = task
                try:
                    reply = await task
                finally:
                    invocations.pop(payload["invocation_id"], None)
                result = to_agent_reply(reply, payload["invocation_id"], session.session_id).model_dump(mode="json")
            elif frame.type == "cancel":
                task = invocations.get(payload["invocation_id"])
                if task:
                    task.cancel()
                result = CancelResult(invocation_id=payload["invocation_id"], acknowledged=task is not None).model_dump(
                    mode="json"
                )
            elif frame.type == "close_session":
                await agent.close_session(SessionHandle.model_validate(payload["session"]).session_id)
                result = {}
            elif frame.type == "shutdown":
                await send(JsonlFrame(request_id=frame.request_id, type="result", payload={}))
                loop.stop()
                return
            else:
                raise ValueError(f"Unsupported frame type {frame.type}")
            await send(JsonlFrame(request_id=frame.request_id, type="result", payload=result))
        except asyncio.CancelledError:
            await send(JsonlFrame(request_id=frame.request_id, type="error", payload={"code": "cancelled"}))
        except Exception as error:  # report, never crash the protocol loop
            print(f"agent error: {type(error).__name__}: {error}", file=sys.stderr)
            await send(
                JsonlFrame(
                    request_id=frame.request_id,
                    type="error",
                    payload={"code": "agent_error", "message": type(error).__name__},
                )
            )

    reader = asyncio.StreamReader(limit=FrameLimits().max_frame_bytes + 1)
    if os.name == "nt":
        # Windows inherited anonymous stdin pipes are not overlapped Proactor handles.
        def read_stdin():
            try:
                while line := sys.stdin.buffer.readline(FrameLimits().max_frame_bytes + 1):
                    loop.call_soon_threadsafe(reader.feed_data, line)
                loop.call_soon_threadsafe(reader.feed_eof)
            except RuntimeError:
                pass  # daemon reader may observe a closed loop during normal shutdown

        threading.Thread(target=read_stdin, daemon=True, name="susanoo-jsonl-stdin").start()
    else:
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    while line := await reader.readline():
        try:
            frame = JsonlFrame.model_validate_json(line)
        except ValidationError:
            print("agent: ignored malformed input frame", file=sys.stderr)
            continue
        task = asyncio.create_task(handle(frame))
        handlers.add(task)  # asyncio keeps only weak references to tasks
        task.add_done_callback(handlers.discard)


def serve(agent: SusanooAgent) -> None:
    """Serve ``agent`` on stdin/stdout. Anything the agent prints goes to stderr, not the protocol."""
    protocol_out = sys.stdout.buffer
    sys.stdout = sys.stderr
    loop = asyncio.new_event_loop()
    try:
        main_task = loop.create_task(_serve(agent, protocol_out))
        main_task.add_done_callback(lambda _: loop.stop())
        loop.run_forever()
    finally:
        loop.close()
