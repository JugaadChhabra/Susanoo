"""Generic WebSocket media adapter, protocol ``susanoo-media/1`` [Spec §19, §20; I12, T37].

Client -> agent JSON text frames::

    {"type": "start", "protocol": "susanoo-media/1", "session_id", "stream_id", "codec", "sample_rate"}
    {"type": "media", "stream_id", "seq", "timestamp_ms", "payload": "<base64>"}
    {"type": "mark", "stream_id", "name": "caller_turn_end:<n>"}
    {"type": "stop", "stream_id", "reason"}

Agent -> client: ``media`` frames (``seq``, ``timestamp_ms``, ``payload``), a ``mark`` named
``agent_turn_end_mark`` closing each agent turn, and ``stop``.

Scripted caller turns are sent as timestamped chunks (real-time pacing by default). Agent audio is
assembled into turns with local capture timestamps on one monotonic clock. A disconnect leaves partial
audio as incomplete evidence (``UNKNOWN``) and nothing is ever resent; deadlines and cancellation stop the
session the same way.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import time
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

import websockets
from pydantic import Field
from susanoo_contracts import Capabilities
from susanoo_sdk import AdapterError, CancelResult, CapabilityReport

from ..mapping import AuthRef
from .audio import (
    AudioClip,
    Codec,
    Direction,
    MediaEvent,
    MediaModel,
    SequenceGap,
    SequenceTracker,
)
from .metrics import AudioTurnBoundary, EndReason

PROTOCOL = "susanoo-media/1"


class MediaSocketConnection(MediaModel):
    url: str = Field(pattern=r"^wss?://")
    auth: AuthRef = Field(default_factory=AuthRef)
    codec: Codec = "mulaw"
    sample_rate: int = Field(default=8000, ge=4000, le=48000)
    chunk_ms: int = Field(default=20, ge=5, le=1000)
    pacing: Literal["realtime", "fast"] = "realtime"
    agent_turn_end_mark: str = "agent_turn_end"
    agent_turn_timeout_seconds: float = Field(default=15, gt=0, le=300)
    session_timeout_seconds: float = Field(default=300, gt=0, le=3600)
    max_total_bytes: int = Field(default=16 * 1024 * 1024, ge=4096)
    open_timeout_seconds: float = Field(default=10, gt=0, le=60)


class ScriptedAudioTurn(MediaModel):
    """One caller turn. ``barge_in_after_ms`` starts it that long after the agent's current (or next) turn began.

    For a scripted overlap, set ``expect_agent_reply=False`` on the preceding turn so the script does not
    wait for the agent turn it is meant to interrupt.
    """

    label: str = Field(min_length=1, max_length=100)
    clip: AudioClip
    barge_in_after_ms: int | None = Field(default=None, ge=0, le=60_000)
    expect_agent_reply: bool = True


class AudioTurn(MediaModel):
    index: int
    direction: Direction
    label: str | None = None
    codec: Codec
    sample_rate: int
    audio: bytes = b""
    chunk_count: int = 0
    started_at: datetime | None = None
    ended_at: datetime | None = None
    start_ns: int | None = None
    end_ns: int | None = None
    complete: bool = False
    end_reason: EndReason | None = None
    gaps: list[SequenceGap] = Field(default_factory=list)
    scripted_overlap: bool = False

    def boundary(self) -> AudioTurnBoundary:
        return AudioTurnBoundary(
            index=self.index,
            role="caller" if self.direction == "to_agent" else "agent",
            source="media_capture",
            start_at=self.started_at,
            end_at=self.ended_at if self.complete else None,
            start_ns=self.start_ns,
            end_ns=self.end_ns if self.complete else None,
            complete=self.complete,
            scripted_overlap=self.scripted_overlap,
            end_reason=self.end_reason or "disconnect",
        )


SessionStatus = Literal["completed", "incomplete", "cancelled", "deadline_exceeded"]


class MediaSessionResult(MediaModel):
    session_id: str
    stream_id: str
    status: SessionStatus
    evidence: Literal["complete", "UNKNOWN"]
    reason: str | None = None
    turns: list[AudioTurn]
    events: list[MediaEvent]
    sent_chunks: int
    resent_chunks: Literal[0] = 0

    def boundaries(self) -> list[AudioTurnBoundary]:
        return [turn.boundary() for turn in self.turns if turn.start_ns is not None]


def _now() -> tuple[datetime, int]:
    return datetime.now(UTC), time.monotonic_ns()


class _Turn:
    """Mutable builder for an ``AudioTurn``."""

    def __init__(self, index: int, direction: Direction, codec: Codec, rate: int, label: str | None, overlap: bool):
        self.index, self.direction, self.codec, self.rate = index, direction, codec, rate
        self.label, self.overlap = label, overlap
        self.audio = bytearray()
        self.chunks = 0
        self.started: tuple[datetime, int] | None = None
        self.ended: tuple[datetime, int] | None = None
        self.complete = False
        self.reason: EndReason | None = None
        self.tracker = SequenceTracker()

    def add(self, payload: bytes, seq: int | None) -> SequenceGap | None:
        stamp = _now()
        if self.started is None:
            self.started = stamp
        self.ended = stamp
        self.audio.extend(payload)
        self.chunks += 1
        return self.tracker.feed(seq) if seq is not None else None

    def close(self, reason: EndReason, complete: bool) -> None:
        """A turn boundary ends at its end mark (agent: mark received; caller: mark sent), else at its last chunk."""
        if self.reason is None:
            self.reason, self.complete = reason, complete
            if reason in {"mark", "script_end"} and self.started is not None:
                self.ended = _now()

    def freeze(self) -> AudioTurn:
        return AudioTurn(
            index=self.index,
            direction=self.direction,
            label=self.label,
            codec=self.codec,
            sample_rate=self.rate,
            audio=bytes(self.audio),
            chunk_count=self.chunks,
            started_at=self.started[0] if self.started else None,
            ended_at=self.ended[0] if self.ended else None,
            start_ns=self.started[1] if self.started else None,
            end_ns=self.ended[1] if self.ended else None,
            complete=self.complete and self.tracker.complete,
            end_reason=self.reason,
            gaps=list(self.tracker.gaps),
            scripted_overlap=self.overlap,
        )


class _State:
    def __init__(self) -> None:
        self.turns: list[_Turn] = []
        self.events: list[MediaEvent] = []
        self.agent_open: _Turn | None = None
        self.agent_closed = 0
        self.agent_started = 0
        self.agent_seq = SequenceTracker()
        self.disconnected = False
        self.stopped_by_agent = False
        self.changed = asyncio.Event()
        self.total_bytes = 0
        self.closing = False
        self.error: AdapterError | None = None


class WebSocketMediaAdapter:
    def __init__(self, connection: MediaSocketConnection):
        self.connection = connection
        self._cancel = asyncio.Event()
        self._session: str | None = None

    async def preflight(self) -> CapabilityReport:
        caps = Capabilities(invoke=True, stream=True, sessions=True, multi_turn=True, audio=True, modalities=["audio"])
        return CapabilityReport(
            adapter="websocket_media",
            adapter_version="1.0",
            capabilities=caps,
            notes=[f"protocol {PROTOCOL}", "disconnects are never resent; partial audio is UNKNOWN evidence"],
        )

    async def cancel(self, invocation_id: str) -> CancelResult:
        self._cancel.set()
        return CancelResult(
            invocation_id=invocation_id,
            acknowledged=True,
            remote_cancel_attempted=True,
            note="stop frame sent and socket closed; not proof the agent stopped acting",
        )

    async def run(
        self, turns: list[ScriptedAudioTurn], *, session_id: str | None = None, deadline: datetime | None = None
    ) -> MediaSessionResult:
        c = self.connection
        session_id = session_id or f"ms-{uuid.uuid4().hex[:16]}"
        stream_id = f"st-{uuid.uuid4().hex[:16]}"
        self._session = session_id
        self._cancel.clear()
        state = _State()
        timeout = c.session_timeout_seconds
        if deadline is not None:
            timeout = min(timeout, max(0.0, (deadline - datetime.now(UTC)).total_seconds()))
        status: SessionStatus = "completed"
        reason: str | None = None
        sent = 0
        receiver: asyncio.Task[None] | None = None
        socket: Any = None
        try:
            async with asyncio.timeout(timeout):
                async with websockets.connect(
                    c.url,
                    additional_headers=c.auth.headers(),
                    max_size=c.max_total_bytes,
                    open_timeout=c.open_timeout_seconds,
                ) as socket:
                    receiver = asyncio.create_task(self._receive(socket, state, session_id, stream_id))
                    await socket.send(
                        json.dumps(
                            {
                                "type": "start",
                                "protocol": PROTOCOL,
                                "session_id": session_id,
                                "stream_id": stream_id,
                                "codec": c.codec,
                                "sample_rate": c.sample_rate,
                            }
                        )
                    )
                    status, reason, sent = await self._script(socket, state, turns, stream_id)
                    if status in {"completed", "cancelled"} and not state.disconnected:
                        stop_reason = "cancelled" if status == "cancelled" else "script_complete"
                        await self._safe_send(socket, {"type": "stop", "stream_id": stream_id, "reason": stop_reason})
                    state.closing = True
        except TimeoutError:
            status, reason = "deadline_exceeded", "session deadline reached"
        except (OSError, websockets.WebSocketException) as error:
            if not state.turns:
                raise AdapterError("media_connect_failed", f"Media socket failed: {type(error).__name__}") from None
            status, reason = "incomplete", f"media socket failed: {type(error).__name__}"
        finally:
            state.closing = True
            if receiver is not None:
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)
        if state.error is not None:
            raise state.error
        end: EndReason = "disconnect"
        if status == "deadline_exceeded":
            end = "deadline"
        elif status == "cancelled":
            end = "cancelled"
        for turn in state.turns:
            turn.close(end, False)
        frozen = [t.freeze() for t in state.turns]
        evidence: Literal["complete", "UNKNOWN"] = "complete"
        if status != "completed":
            evidence = "UNKNOWN"
        elif any(not t.complete for t in frozen) or state.agent_seq.gaps:
            evidence, reason = "UNKNOWN", reason or "agent audio has missing chunks or incomplete turns"
        return MediaSessionResult(
            session_id=session_id,
            stream_id=stream_id,
            status=status,
            evidence=evidence,
            reason=reason,
            turns=frozen,
            events=state.events,
            sent_chunks=sent,
        )

    async def _script(
        self, socket: Any, state: _State, turns: list[ScriptedAudioTurn], stream_id: str
    ) -> tuple[SessionStatus, str | None, int]:
        c = self.connection
        sent = 0
        for scripted in turns:
            clip = scripted.clip
            if clip.codec != c.codec or clip.sample_rate != c.sample_rate:
                clip = clip.transcode(c.codec, c.sample_rate)  # raises AdapterError('transcode_failed')
            if scripted.barge_in_after_ms is not None:
                if state.agent_open is None:
                    started = state.agent_started
                    ok = await self._wait(state, lambda s, n=started: s.agent_started > n, c.agent_turn_timeout_seconds)
                    if ok is not None:
                        return ok, "agent turn to interrupt never started", sent
                opened = state.agent_open.started if state.agent_open is not None else None
                elapsed = (time.monotonic_ns() - opened[1]) / 1e9 if opened else 0.0
                await asyncio.sleep(max(0.0, scripted.barge_in_after_ms / 1000 - elapsed))
            turn = _Turn(
                len(state.turns),
                "to_agent",
                c.codec,
                c.sample_rate,
                scripted.label,
                scripted.barge_in_after_ms is not None,
            )
            state.turns.append(turn)
            pace = c.chunk_ms / 1000 if c.pacing == "realtime" else 0.0
            loop = asyncio.get_running_loop()
            begin = loop.time()
            for position, payload in enumerate(clip.chunks(c.chunk_ms)):
                if self._cancel.is_set():
                    turn.close("cancelled", False)
                    return "cancelled", "cancelled during a caller turn", sent
                if state.disconnected:
                    turn.close("disconnect", False)
                    return "incomplete", "agent disconnected during a caller turn; not resent", sent
                frame = {
                    "type": "media",
                    "stream_id": stream_id,
                    "seq": sent,
                    "timestamp_ms": int(position * c.chunk_ms),
                    "payload": base64.b64encode(payload).decode(),
                }
                if not await self._safe_send(socket, frame):
                    turn.close("disconnect", False)
                    state.disconnected = True
                    return "incomplete", "agent disconnected during a caller turn; not resent", sent
                turn.add(payload, sent)
                sent += 1
                await asyncio.sleep(max(0.0, begin + (position + 1) * pace - loop.time()) if pace else 0)
            await self._safe_send(
                socket, {"type": "mark", "stream_id": stream_id, "name": f"caller_turn_end:{turn.index}"}
            )
            turn.close("script_end", True)
            if scripted.expect_agent_reply:
                closed = state.agent_closed
                ok = await self._wait(state, lambda s, n=closed: s.agent_closed > n, c.agent_turn_timeout_seconds)
                if ok is not None:
                    return ok, "no complete agent turn before the reply timeout" if ok == "incomplete" else None, sent
        if self._cancel.is_set():
            return "cancelled", "cancelled", sent
        return "completed", None, sent

    async def _wait(self, state: _State, done: Any, timeout: float) -> SessionStatus | None:
        """Wait for ``done(state)``; returns a terminal status on cancel/disconnect/timeout, else None."""
        loop = asyncio.get_running_loop()
        until = loop.time() + timeout
        while True:
            state.changed.clear()
            if done(state):
                return None
            if self._cancel.is_set():
                return "cancelled"
            if state.disconnected or state.stopped_by_agent:
                return "incomplete"
            remaining = until - loop.time()
            if remaining <= 0:
                return "incomplete"
            cancel_wait = asyncio.ensure_future(self._cancel.wait())
            change_wait = asyncio.ensure_future(state.changed.wait())
            await asyncio.wait({cancel_wait, change_wait}, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
            cancel_wait.cancel()
            change_wait.cancel()

    @staticmethod
    async def _safe_send(socket: Any, frame: dict[str, Any]) -> bool:
        try:
            await socket.send(json.dumps(frame))
            return True
        except websockets.ConnectionClosed:
            return False

    async def _receive(self, socket: Any, state: _State, session_id: str, stream_id: str) -> None:
        c = self.connection
        try:
            async for raw in socket:
                state.total_bytes += len(raw)
                if state.total_bytes > c.max_total_bytes:
                    raise AdapterError("output_limit", "Agent media exceeded the configured size")
                self._handle(raw, state, session_id, stream_id)
                state.changed.set()
        except websockets.ConnectionClosed:
            pass
        except AdapterError as error:
            state.error = error
        finally:
            if not state.closing:
                self._lost(state, session_id, stream_id)
            state.changed.set()

    @staticmethod
    def _lost(state: _State, session_id: str, stream_id: str) -> None:
        """The agent side ended the stream: partial audio stays incomplete and nothing is resent."""
        if not state.stopped_by_agent:
            state.disconnected = True
            stamp, _ = _now()
            state.events.append(
                MediaEvent(kind="disconnect", stream_id=stream_id, session_id=session_id, captured_at=stamp)
            )
        if state.agent_open is not None:
            state.agent_open.close("stop" if state.stopped_by_agent else "disconnect", False)
            state.agent_open = None

    def _handle(self, raw: str | bytes, state: _State, session_id: str, stream_id: str) -> None:
        c = self.connection
        stamp, _ = _now()
        try:
            frame = json.loads(raw)
            kind = frame["type"]
        except (ValueError, KeyError, TypeError):
            raise AdapterError("malformed_event", "Agent media frame is not a typed JSON object") from None
        if kind == "media":
            try:
                payload = base64.b64decode(frame["payload"], validate=True)
                seq = int(frame["seq"])
            except (KeyError, ValueError, TypeError, binascii.Error):
                state.events.append(
                    MediaEvent(
                        kind="transcode_error",
                        stream_id=stream_id,
                        session_id=session_id,
                        captured_at=stamp,
                        detail={"reason": "undecodable agent media payload"},
                    )
                )
                return
            if state.agent_open is None:
                state.agent_open = _Turn(len(state.turns), "from_agent", c.codec, c.sample_rate, None, False)
                state.turns.append(state.agent_open)
                state.agent_started += 1
            gap = state.agent_seq.feed(seq)
            state.agent_open.add(payload, None)
            if gap is not None:
                state.agent_open.tracker.gaps.append(gap)
                state.events.append(
                    MediaEvent(
                        kind="gap",
                        stream_id=stream_id,
                        session_id=session_id,
                        direction="from_agent",
                        sequence=seq,
                        captured_at=stamp,
                        detail={"first_missing": gap.first_missing, "last_missing": gap.last_missing},
                    )
                )
        elif kind == "mark":
            name = str(frame.get("name", ""))[:200]
            state.events.append(
                MediaEvent(
                    kind="mark", stream_id=stream_id, session_id=session_id, captured_at=stamp, detail={"name": name}
                )
            )
            if name == c.agent_turn_end_mark and state.agent_open is not None:
                state.agent_open.close("mark", True)
                state.agent_open = None
                state.agent_closed += 1
        elif kind == "stop":
            state.stopped_by_agent = True
            state.events.append(MediaEvent(kind="stop", stream_id=stream_id, session_id=session_id, captured_at=stamp))
        else:
            raise AdapterError("malformed_event", f"Unknown agent media frame type {str(kind)[:50]!r}")
