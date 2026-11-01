"""M10 voice/media (I12, T37): audio import, WebSocket media, Twilio over fakes, audio metrics [Spec §19, §20].

Fake servers prove transport behavior only; nothing here is live-verified against Twilio.
"""

import asyncio
import base64
import importlib.util
import json
import wave
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from urllib.parse import parse_qsl

import pytest
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from susanoo_connectors.media import (
    AudioClip,
    AudioTurnBoundary,
    CallbackVerifier,
    MediaSocketConnection,
    MediaStore,
    NormalizationPolicy,
    ReferenceTranscript,
    ScriptedAudioTurn,
    SequenceTracker,
    StoragePolicy,
    TelephonyProvider,
    TwilioClient,
    TwilioCredentials,
    TwilioStreamNormalizer,
    VoiceProfile,
    WebSocketMediaAdapter,
    audio_metrics,
    compute_signature,
    detect_interruptions,
    load_raw,
    load_wav,
    mulaw_decode,
    mulaw_encode,
    preflight_call,
    response_latency,
    transcript_quality,
    validate_signature,
    write_wav,
)
from susanoo_connectors.media.audio import MediaChunk, check_metadata, detect_gaps
from susanoo_connectors.media.metrics import graphemes
from susanoo_connectors.media.twilio import CallWindow, connect_stream_twiml, media_message
from susanoo_runner import AppServer
from susanoo_sdk import AdapterError, Unavailable

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("voice_synth", ROOT / "examples/voice/synth.py")
synth = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(synth)

NOW = datetime(2026, 11, 2, 6, 0, tzinfo=UTC)  # 11:30 IST, a Monday
ACCOUNT = "AC" + "a" * 32
TOKEN = "test-auth-token"
DEST = "+919800000001"


def clip(name: str = "caller_turn_short") -> AudioClip:
    return AudioClip(codec="pcm16", sample_rate=8000, data=synth.render(name))


# --- Audio import, codec, gaps, storage --------------------------------------------------------------


def test_mulaw_roundtrip_and_wav_raw_import(tmp_path):
    pcm = synth.render("caller_turn_short")
    encoded = mulaw_encode(pcm)
    assert len(encoded) == len(pcm) // 2
    decoded = mulaw_decode(encoded)
    original = AudioClip(codec="pcm16", sample_rate=8000, data=pcm)
    error = max(abs(a - b) for a, b in zip(memoryview(pcm).cast("h"), memoryview(decoded).cast("h"), strict=True))
    assert error < 0.04 * 32768  # G.711 is lossy but bounded
    assert mulaw_encode(b"\x00\x00") == b"\xff" and mulaw_decode(b"\xff") == b"\x00\x00"

    written = synth.write_fixtures(tmp_path)
    loaded = load_wav(written[0])
    assert loaded.codec == "pcm16" and loaded.sample_rate == 8000 and loaded.data == pcm
    assert loaded.duration_ms == pytest.approx(400)
    (tmp_path / "turn.ulaw").write_bytes(encoded)
    raw = load_raw(tmp_path / "turn.ulaw", "mulaw", 8000)
    assert raw.pcm16() == decoded
    up = original.transcode("mulaw", 16000)
    assert up.sample_rate == 16000 and up.samples == 2 * original.samples
    out = write_wav(tmp_path / "out.wav", raw)
    assert load_wav(out).sample_rate == 8000
    committed = load_wav(ROOT / "examples/voice/fixtures/caller_turn_short.wav")
    assert committed.data == pcm  # fixtures are reproducible from code


def test_transcoding_failures_are_explicit(tmp_path):
    stereo = tmp_path / "stereo.wav"
    with wave.open(str(stereo), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\x00" * 64)
    with pytest.raises(AdapterError) as error:
        load_wav(stereo)
    assert error.value.code == "transcode_failed"
    (tmp_path / "bad.wav").write_bytes(b"RIFFnotawav")
    with pytest.raises(AdapterError) as error:
        load_wav(tmp_path / "bad.wav")
    assert error.value.code == "transcode_failed"
    (tmp_path / "odd.pcm").write_bytes(b"\x00\x01\x02")
    with pytest.raises(AdapterError) as error:
        load_raw(tmp_path / "odd.pcm", "pcm16")
    assert error.value.code == "transcode_failed"


def test_gap_detection_and_metadata_never_carries_secrets():
    tracker = SequenceTracker()
    for seq in (1, 2, 3, 6, 6, 5, 7):
        tracker.feed(seq)
    assert [(g.first_missing, g.last_missing) for g in tracker.gaps] == [(4, 5)]
    assert tracker.duplicates == 1 and tracker.out_of_order == 1 and not tracker.complete
    chunks = [
        MediaChunk(
            stream_id="s",
            session_id="x",
            codec="mulaw",
            sample_rate=8000,
            sequence=n,
            direction="from_agent",
            captured_at=NOW,
            payload=b"\xff",
            source="synthetic",
        )
        for n in (0, 1, 4)
    ]
    assert detect_gaps(chunks)[0].count == 2
    for bad in ({"auth_token": "x"}, {"api_key": "x"}, {"note": "Bearer abc"}):
        with pytest.raises(ValueError):
            check_metadata(bad)
    with pytest.raises(ValidationError):
        AudioClip(codec="pcm16", sample_rate=8000, data=b"", metadata={"twilio_signature": "x"})


def test_media_is_stored_locally_and_upload_needs_explicit_policy(tmp_path):
    store = MediaStore(StoragePolicy(root=tmp_path / "media"))
    stored = store.store(clip(), "case-1-turn-0", now=NOW)
    assert stored.path.exists() and oct(stored.path.stat().st_mode & 0o777) == "0o600"
    assert not stored.uploadable
    with pytest.raises(AdapterError) as error:
        store.upload_manifest(stored)
    assert error.value.code == "upload_denied"
    with pytest.raises(AdapterError):
        store.store(clip(), "../escape", now=NOW)
    with pytest.raises(ValidationError):
        StoragePolicy(root=tmp_path, upload_allowed=True)  # contradicts local_only
    hosted = MediaStore(StoragePolicy(root=tmp_path / "h", upload_allowed=True, data_location="hosted_allowed"))
    assert hosted.upload_manifest(hosted.store(clip(), "t", now=NOW))["sha256"] == stored.sha256


# --- Fake media WebSocket server ---------------------------------------------------------------------


def media_frame(seq: int, payload: bytes = b"\xff" * 160) -> str:
    return json.dumps(
        {"type": "media", "seq": seq, "timestamp_ms": seq * 20, "payload": base64.b64encode(payload).decode()}
    )


def media_app() -> FastAPI:
    app = FastAPI()
    app.state.received = {}

    def count(name: str, msg: dict) -> None:
        if msg["type"] == "media":
            app.state.received[name] = app.state.received.get(name, 0) + 1

    @app.websocket("/media/{mode}")
    async def media(ws: WebSocket, mode: str):
        await ws.accept()
        seq = 0
        start = json.loads(await ws.receive_text())
        assert start["type"] == "start" and start["codec"] == "mulaw"
        try:
            if mode == "barge":
                await barge(ws, app, count)
                return
            while True:
                msg = json.loads(await ws.receive_text())
                count(mode, msg)
                if mode == "drop" and app.state.received.get(mode, 0) >= 5:
                    await ws.close()
                    return
                if msg["type"] == "stop":
                    return
                if msg["type"] != "mark" or mode == "silent":
                    continue
                await asyncio.sleep(0.03)
                sends = 6 if mode == "partial" else 5
                for _ in range(sends):
                    if mode == "gap" and seq == 2:
                        seq += 1  # drop one chunk
                    await ws.send_text(media_frame(seq))
                    seq += 1
                if mode == "partial":
                    await ws.close()
                    return
                await ws.send_text(json.dumps({"type": "mark", "name": "agent_turn_end"}))
        except WebSocketDisconnect:
            return

    return app


async def barge(ws: WebSocket, app: FastAPI, count) -> None:
    turn_ends: asyncio.Queue = asyncio.Queue()
    speaking = {"on": False, "interrupted": False}

    async def receive():
        while True:
            msg = json.loads(await ws.receive_text())
            count("barge", msg)
            if msg["type"] == "media" and speaking["on"]:
                speaking["interrupted"] = True
            elif msg["type"] == "mark":
                await turn_ends.put(msg["name"])
            elif msg["type"] == "stop":
                return

    task = asyncio.create_task(receive())
    seq = 0
    await turn_ends.get()
    await asyncio.sleep(0.05)
    speaking["on"] = True
    for _ in range(100):  # a long disclosure, ~2 s, unless interrupted
        if speaking["interrupted"]:
            break
        await ws.send_text(media_frame(seq))
        seq += 1
        await asyncio.sleep(0.02)
    speaking["on"] = False
    await ws.send_text(json.dumps({"type": "mark", "name": "agent_turn_end"}))
    await turn_ends.get()
    await asyncio.sleep(0.05)
    for _ in range(3):
        await ws.send_text(media_frame(seq))
        seq += 1
    await ws.send_text(json.dumps({"type": "mark", "name": "agent_turn_end"}))
    try:
        await asyncio.wait_for(task, 5)
    except (TimeoutError, WebSocketDisconnect):
        task.cancel()


@pytest.fixture(scope="module")
def media_server():
    with AppServer(media_app()) as server:
        yield server


def adapter(server, mode: str, **overrides) -> WebSocketMediaAdapter:
    url = server.base_url.replace("http://", "ws://") + f"/media/{mode}"
    return WebSocketMediaAdapter(MediaSocketConnection(url=url, **{"pacing": "fast", **overrides}))


async def test_media_session_assembles_timestamped_turns_and_latency(media_server):
    turns = [ScriptedAudioTurn(label="t1", clip=clip()), ScriptedAudioTurn(label="t2", clip=clip("caller_turn_long"))]
    result = await adapter(media_server, "echo").run(turns)
    assert result.status == "completed" and result.evidence == "complete"
    assert [t.direction for t in result.turns] == ["to_agent", "from_agent", "to_agent", "from_agent"]
    agent = result.turns[1]
    assert agent.complete and agent.chunk_count == 5 and len(agent.audio) == 800
    assert result.turns[0].chunk_count == 20  # 400 ms at 20 ms chunks, transcoded to mu-law
    assert all(t.start_ns is not None and t.end_ns >= t.start_ns for t in result.turns)
    latency = response_latency(result.boundaries())
    assert latency.count == 2 and latency.min_ms > 0
    assert media_server.app.state.received["echo"] == result.sent_chunks
    report = await adapter(media_server, "echo").preflight()
    assert report.capabilities.audio and report.capabilities.modalities == ["audio"]


async def test_disconnect_mid_turn_is_unknown_and_never_resent(media_server):
    turns = [ScriptedAudioTurn(label="t1", clip=clip("caller_turn_long"))]
    result = await adapter(media_server, "drop").run(turns)
    assert result.status == "incomplete" and result.evidence == "UNKNOWN"
    assert "not resent" in result.reason and result.resent_chunks == 0
    assert not result.turns[0].complete and result.turns[0].end_reason == "disconnect"
    await asyncio.sleep(0.1)
    assert media_server.app.state.received["drop"] == 5  # exactly what was sent before the drop
    assert any(e.kind == "disconnect" for e in result.events)


async def test_partial_agent_audio_and_gaps_are_incomplete_evidence(media_server):
    partial = await adapter(media_server, "partial").run([ScriptedAudioTurn(label="t", clip=clip())])
    agent = [t for t in partial.turns if t.direction == "from_agent"][0]
    assert partial.evidence == "UNKNOWN" and not agent.complete and agent.chunk_count == 6
    assert agent.end_reason == "disconnect"
    # Partial agent audio never yields a latency end or a complete boundary.
    assert agent.boundary().end_ns is None and not agent.boundary().complete

    gapped = await adapter(media_server, "gap").run([ScriptedAudioTurn(label="t", clip=clip())])
    assert gapped.status == "completed" and gapped.evidence == "UNKNOWN"
    gap_events = [e for e in gapped.events if e.kind == "gap"]
    assert gap_events and gap_events[0].detail == {"first_missing": 2, "last_missing": 2}


async def test_cancel_and_deadline_stop_the_session(media_server):
    media = adapter(media_server, "silent", agent_turn_timeout_seconds=5)
    task = asyncio.create_task(media.run([ScriptedAudioTurn(label="t", clip=clip())]))
    await asyncio.sleep(0.3)
    ack = await media.cancel("inv-1")
    result = await asyncio.wait_for(task, 3)
    assert ack.acknowledged and result.status == "cancelled" and result.evidence == "UNKNOWN"

    slow = adapter(media_server, "silent", agent_turn_timeout_seconds=30)
    result = await slow.run(
        [ScriptedAudioTurn(label="t", clip=clip())], deadline=datetime.now(UTC) + timedelta(seconds=0.4)
    )
    assert result.status == "deadline_exceeded" and result.evidence == "UNKNOWN"

    quiet = adapter(media_server, "silent", agent_turn_timeout_seconds=0.3)
    result = await quiet.run([ScriptedAudioTurn(label="t", clip=clip())])
    assert result.status == "incomplete" and "reply timeout" in result.reason


async def test_scripted_barge_in_measures_interruption(media_server):
    turns = [
        ScriptedAudioTurn(label="greet", clip=clip(), expect_agent_reply=False),
        ScriptedAudioTurn(label="barge", clip=clip("barge_in"), barge_in_after_ms=200),
    ]
    result = await adapter(media_server, "barge", pacing="realtime").run(turns)
    assert result.status == "completed", result.reason
    found = detect_interruptions(result.boundaries())
    assert len(found) == 1 and found[0].outcome == "agent_yielded"
    assert found[0].agent_continued_ms is not None and found[0].agent_continued_ms < 1000
    # Transport loss is not agent behavior.
    lost = [
        AudioTurnBoundary(
            index=0, role="agent", source="media_capture", start_ns=0, end_ns=None, end_reason="disconnect"
        ),
        AudioTurnBoundary(
            index=1, role="caller", source="media_capture", start_ns=10, end_ns=20, scripted_overlap=True
        ),
    ]
    assert detect_interruptions(lost)[0].outcome == "transport_loss"


# --- Twilio: fake REST server ------------------------------------------------------------------------


def twilio_app() -> FastAPI:
    app = FastAPI()
    app.state.mode = "ok"
    app.state.posts = []
    app.state.calls = {}
    app.state.counter = 0
    expected = "Basic " + base64.b64encode(f"{ACCOUNT}:{TOKEN}".encode()).decode()

    def call(sid: str, form: dict) -> dict:
        return {
            "sid": sid,
            "to": form.get("To"),
            "from": form.get("From"),
            "status": "queued",
            "date_created": "Mon, 02 Nov 2026 06:00:00 +0000",
            "duration": None,
            "price": None,
            "price_unit": "USD",
        }

    @app.post("/2010-04-01/Accounts/{account}/Calls.json")
    async def create(account: str, request: Request):
        if request.headers.get("authorization") != expected or account != ACCOUNT:
            return JSONResponse({"code": 20003, "message": "Authenticate"}, status_code=401)
        pairs = parse_qsl((await request.body()).decode())
        app.state.posts.append(pairs)
        form = dict(pairs)
        if app.state.mode == "400":
            return JSONResponse({"code": 21211, "message": "Invalid To"}, status_code=400)
        if app.state.mode == "500":
            return Response(status_code=503)
        app.state.counter += 1
        sid = "CA" + f"{app.state.counter:032x}"
        app.state.calls[sid] = call(sid, form)
        if app.state.mode == "slow":
            await asyncio.sleep(1.0)
        return JSONResponse(app.state.calls[sid], status_code=201)

    @app.get("/2010-04-01/Accounts/{account}/Calls.json")
    async def listing(account: str, request: Request):
        q = request.query_params
        assert "StartTime>" in q
        calls = [c for c in app.state.calls.values() if c["to"] == q.get("To") and c["from"] == q.get("From")]
        return {"calls": calls}

    @app.get("/2010-04-01/Accounts/{account}/Calls/{sid}.json")
    async def fetch(account: str, sid: str):
        if sid not in app.state.calls:
            return JSONResponse({"code": 20404}, status_code=404)
        return app.state.calls[sid]

    @app.post("/2010-04-01/Accounts/{account}/Calls/{sid}.json")
    async def update(account: str, sid: str, request: Request):
        form = dict(parse_qsl((await request.body()).decode()))
        app.state.calls[sid]["status"] = form["Status"]
        if form["Status"] == "completed":
            app.state.calls[sid]["duration"] = "12"
        return app.state.calls[sid]

    return app


@pytest.fixture
def twilio_server():
    with AppServer(twilio_app()) as server:
        yield server


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", ACCOUNT)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", TOKEN)
    return TwilioCredentials(account_sid_ref="TWILIO_ACCOUNT_SID", auth_token_ref="TWILIO_AUTH_TOKEN")


def profile(**overrides) -> VoiceProfile:
    base = dict(
        profile_id="vp-1",
        real_calls_enabled=True,
        authorized=True,
        authorized_by="owner@example.test",
        from_number="+15005550006",
        allowed_destinations=[DEST],
        allowed_country_codes=["+91"],
        allowed_media_endpoints=["wss://relay.customer.test/media"],
        call_window=CallWindow(start=time(9, 0), end=time(18, 0)),
        max_call_duration_seconds=120,
        max_concurrent_calls=1,
        cost_ceiling_minor=100,
        estimated_cost_per_minute_minor=10,
        status_callback_url="https://relay.customer.test/twilio/status",
    )
    base.update(overrides)
    return VoiceProfile(**base)


def client(server, creds, **kw) -> TwilioClient:
    return TwilioClient(kw.pop("profile", None) or profile(), creds, base_url=server.base_url, clock=lambda: NOW, **kw)


async def test_call_create_status_cancel(twilio_server, creds):
    twilio = client(twilio_server, creds)
    assert isinstance(twilio, TelephonyProvider)
    twiml = connect_stream_twiml("wss://relay.customer.test/media", {"case": "C01"})
    attempt = await twilio.create_call(DEST, twiml=twiml, media_endpoint="wss://relay.customer.test/media")
    assert attempt.state == "created" and attempt.call_sid.startswith("CA") and attempt.destination == "+91******0001"
    form = twilio_server.app.state.posts[0]
    keys = [k for k, _ in form]
    assert ("To", DEST) in form and ("TimeLimit", "120") in form and ("Record", "false") in form
    assert keys.count("StatusCallbackEvent") == 4
    callback = dict(form)["StatusCallback"]
    assert f"susanoo_corr={attempt.correlation_id}" in callback
    status = await twilio.get_call(attempt.call_sid)
    assert status.status == "queued" and not status.lifecycle_ended and status.task_outcome == "not_established"
    cancelled = await twilio.cancel_call(attempt.call_sid)
    assert cancelled.requested == "canceled" and cancelled.resulting_status == "canceled"
    # in-progress calls are ended with Status=completed, and completed is lifecycle only
    second = await twilio.create_call(DEST, twiml=twiml)
    twilio_server.app.state.calls[second.call_sid]["status"] = "in-progress"
    ended = await twilio.cancel_call(second.call_sid)
    assert ended.requested == "completed"
    final = await twilio.get_call(second.call_sid)
    assert final.status == "completed" and final.lifecycle_ended and final.task_outcome == "not_established"
    assert (await twilio.cancel_call(second.call_sid)).requested == "none"
    await twilio.aclose()


async def test_ambiguous_create_is_uncertain_never_redialed_then_reconciled(twilio_server, creds):
    twilio_server.app.state.mode = "slow"
    twilio = client(twilio_server, creds, timeout_seconds=0.3)
    attempt = await twilio.create_call(DEST, twiml="<Response/>", correlation_id="corr-1")
    assert attempt.state == "uncertain"
    with pytest.raises(AdapterError) as error:
        await twilio.create_call(DEST, twiml="<Response/>", correlation_id="corr-1")
    assert error.value.code == "no_redial"
    with pytest.raises(AdapterError) as error:  # an uncertain call still occupies the concurrency slot
        await twilio.create_call(DEST, twiml="<Response/>", correlation_id="corr-2")
    assert error.value.code == "preflight_denied" and "concurrency_limit" in str(error.value)
    await asyncio.sleep(1.0)
    assert len(twilio_server.app.state.posts) == 1
    resolved = await twilio.reconcile("corr-1")
    assert resolved.state == "resolved" and resolved.finding == "effect_present"
    assert twilio.attempts["corr-1"].call_sid in twilio_server.app.state.calls
    await twilio.aclose()


async def test_5xx_create_is_uncertain_and_absence_is_not_proven(twilio_server, creds):
    twilio_server.app.state.mode = "500"
    twilio = client(twilio_server, creds)
    attempt = await twilio.create_call(DEST, twiml="<Response/>", correlation_id="corr-x")
    assert attempt.state == "uncertain" and attempt.http_status == 503
    rec = await twilio.reconcile("corr-x")
    assert rec.state == "unresolved" and rec.finding == "indeterminate" and "do not redial" in rec.note
    sid = "CA" + "f" * 32
    url = "https://relay.customer.test/twilio/status?susanoo_corr=corr-x"
    status = twilio.record_status_callback(url, {"CallSid": sid, "CallStatus": "completed"})
    assert status.lifecycle_ended and status.task_outcome == "not_established"
    rec = await twilio.reconcile("corr-x")
    assert rec.state == "resolved" and sid in rec.note
    assert len(twilio_server.app.state.posts) == 1
    twilio_server.app.state.mode = "400"
    rejected = await twilio.create_call(DEST, twiml="<Response/>")
    assert rejected.state == "rejected" and rejected.error_code == "21211"
    await twilio.aclose()


async def test_preflight_denials_block_before_any_request(twilio_server, monkeypatch, creds):
    monkeypatch.delenv("TWILIO_AUTH_TOKEN")
    default = VoiceProfile(profile_id="vp", from_number="+15005550006")
    decision = preflight_call(default, DEST, credentials_configured=creds.configured(), now=NOW)
    assert not decision.allowed
    assert {"real_calls_disabled", "not_authorized", "credentials_missing", "destination_not_approved"} <= set(
        decision.codes()
    )
    twilio = client(twilio_server, creds, profile=default)
    with pytest.raises(AdapterError) as error:
        await twilio.create_call(DEST, twiml="<Response/>")
    assert error.value.code == "preflight_denied"
    assert twilio_server.app.state.posts == []
    ok = profile()
    night = datetime(2026, 11, 2, 18, 0, tzinfo=UTC)  # 23:30 IST
    assert "outside_call_window" in preflight_call(ok, DEST, credentials_configured=True, now=night).codes()
    assert "country_not_allowed" in preflight_call(ok, "+15550001111", credentials_configured=True, now=NOW).codes()
    assert "concurrency_limit" in preflight_call(ok, DEST, credentials_configured=True, now=NOW, active_calls=1).codes()
    assert (
        "cost_ceiling"
        in preflight_call(ok, DEST, credentials_configured=True, now=NOW, committed_cost_minor=90).codes()
    )
    assert (
        "media_endpoint_not_approved"
        in preflight_call(ok, DEST, credentials_configured=True, now=NOW, media_endpoint="wss://evil.test/x").codes()
    )
    assert preflight_call(ok, DEST, credentials_configured=True, now=NOW).allowed


def test_profile_rejects_unsafe_data_routes():
    with pytest.raises(ValidationError):
        profile(relay="susanoo_hosted")  # no explicit permission
    with pytest.raises(ValidationError):
        profile(relay="susanoo_hosted", hosted_relay_permitted=True)  # data_location local_only
    with pytest.raises(ValidationError):
        profile(recording_policy="provider_hosted")
    with pytest.raises(ValidationError):
        profile(authorized_by=None)
    with pytest.raises(ValidationError):
        profile(allowed_destinations=["+15550001111"])  # outside allowed countries
    assert profile(relay="susanoo_hosted", hosted_relay_permitted=True, data_location="hosted_allowed")
    with pytest.raises(ValueError):
        connect_stream_twiml("wss://relay.customer.test/media", {"auth_token": "x"})


# --- Signatures --------------------------------------------------------------------------------------

DOC_URL = "https://mycompany.com/myapp.php?foo=1&bar=2"
DOC_PARAMS = {
    "CallSid": "CA1234567890ABCDE",
    "Caller": "+12349013030",
    "Digits": "1234",
    "From": "+12349013030",
    "To": "+18005551212",
}


def test_signature_matches_documented_vectors_and_rejects_tampering():
    signature = compute_signature("12345", DOC_URL, DOC_PARAMS)
    assert signature == "0/KCTR6DLpKmkAf8muzZqo1nDgQ="  # Twilio's documented example
    assert validate_signature("12345", DOC_URL, signature, params=DOC_PARAMS).valid
    assert validate_signature(
        "12345", "https://mycompany.com:443/myapp.php?foo=1&bar=2", signature, params=DOC_PARAMS
    ).valid
    assert validate_signature("12345", DOC_URL, None, params=DOC_PARAMS).reason == "missing_signature"
    tampered = {**DOC_PARAMS, "Digits": "9999"}
    assert validate_signature("12345", DOC_URL, signature, params=tampered).reason == "signature_mismatch"
    assert not validate_signature("wrong", DOC_URL, signature, params=DOC_PARAMS).valid
    # Replayed to a different URL: the signature binds the full URL.
    replay_url = "https://mycompany.com/other.php?foo=1&bar=2"
    assert validate_signature("12345", replay_url, signature, params=DOC_PARAMS).reason == "signature_mismatch"

    body = b'{"property": "value", "boolean": true}'
    json_url = DOC_URL + "&bodySHA256=0a1ff7634d9ab3b95db5c9a2dfe9416e41502b283a80c7cf19632632f96e6620"
    assert validate_signature("12345", json_url, "a9nBmqA0ju/hNViExpshrM61xv4=", body=body).valid
    assert (
        validate_signature("12345", json_url, "a9nBmqA0ju/hNViExpshrM61xv4=", body=b"{}").reason == "body_hash_mismatch"
    )
    assert validate_signature("12345", json_url, "a9nBmqA0ju/hNViExpshrM61xv4=").reason == "body_missing"


def test_callback_verifier_uses_configured_url_and_flags_duplicates(creds):
    verifier = CallbackVerifier("https://relay.customer.test", creds)
    path = "/twilio/status?susanoo_corr=c1"
    params = {"CallSid": "CA" + "1" * 32, "CallStatus": "ringing"}
    signature = compute_signature(TOKEN, "https://relay.customer.test" + path, params)
    first = verifier.verify(path, signature, params=params, now=NOW)
    assert first.valid and not first.duplicate and first.version == "twilio-request-signature/1"
    again = verifier.verify(path, signature, params=params, now=NOW + timedelta(seconds=5))
    assert again.valid and again.duplicate  # replayed/retried delivery is not enqueued twice
    assert not verifier.verify("/twilio/other?susanoo_corr=c1", signature, params=params, now=NOW).valid
    attacker = compute_signature(TOKEN, "https://attacker.test" + path, params)
    assert not verifier.verify(path, attacker, params=params, now=NOW).valid


# --- Media Streams normalization ---------------------------------------------------------------------

STREAM = "MZ" + "0" * 32
CALL = "CA" + "2" * 32


def twilio_messages() -> list[dict]:
    payload = base64.b64encode(mulaw_encode(synth.tone(440, 20))).decode()
    return [
        {"event": "connected", "protocol": "Call", "version": "1.0.0"},
        {
            "event": "start",
            "sequenceNumber": "1",
            "streamSid": STREAM,
            "start": {
                "streamSid": STREAM,
                "accountSid": ACCOUNT,
                "callSid": CALL,
                "tracks": ["inbound"],
                "customParameters": {"case": "C01", "auth_token": "leaked"},
                "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
            },
        },
        {
            "event": "media",
            "sequenceNumber": "2",
            "streamSid": STREAM,
            "media": {"track": "inbound", "chunk": "1", "timestamp": "5", "payload": payload},
        },
        {
            "event": "media",
            "sequenceNumber": "4",
            "streamSid": STREAM,
            "media": {"track": "inbound", "chunk": "3", "timestamp": "45", "payload": payload},
        },
        {"event": "dtmf", "sequenceNumber": "5", "streamSid": STREAM, "dtmf": {"track": "inbound_track", "digit": "1"}},
        {"event": "mark", "sequenceNumber": "6", "streamSid": STREAM, "mark": {"name": "greeting_done"}},
        {"event": "stop", "sequenceNumber": "7", "streamSid": STREAM, "stop": {"accountSid": ACCOUNT, "callSid": CALL}},
    ]


def test_media_streams_normalization_correlation_and_gaps():
    normalizer = TwilioStreamNormalizer("sess-1", expected_call_sid=CALL)
    events = [e for m in twilio_messages() for e in normalizer.feed(json.dumps(m), received_at=NOW)]
    kinds = [e.kind for e in events]
    assert kinds == ["connected", "start", "media", "gap", "media", "dtmf", "mark", "stop"]
    assert normalizer.call_sid == CALL and normalizer.stream_sid == STREAM
    assert normalizer.custom_parameters == {"case": "C01"}  # credential-like parameter dropped
    start = events[1]
    assert start.detail["dropped_parameters"] == 1 and "leaked" not in json.dumps(start.model_dump(mode="json"))
    chunk = events[2].chunk
    assert chunk.codec == "mulaw" and chunk.sample_rate == 8000 and chunk.direction == "from_agent"
    assert chunk.media_offset_ms == 5 and len(chunk.payload) == 160 and chunk.stream_id == STREAM
    assert events[3].detail == {"first_missing": 3, "last_missing": 3}
    assert events[5].detail == {"digit": "1"}
    assert normalizer.feed(json.dumps(twilio_messages()[2])) == []  # after stop: ignored

    foreign = TwilioStreamNormalizer("sess-2")
    for message in twilio_messages()[:2]:
        foreign.feed(message)
    with pytest.raises(AdapterError) as error:
        foreign.feed({**twilio_messages()[2], "streamSid": "MZ" + "9" * 32})
    assert error.value.code == "stream_correlation"
    with pytest.raises(AdapterError):
        TwilioStreamNormalizer("s", expected_call_sid="CA" + "3" * 32).feed(twilio_messages()[1])
    odd = TwilioStreamNormalizer("s")
    bad_start = json.loads(json.dumps(twilio_messages()[1]))
    bad_start["start"]["mediaFormat"]["encoding"] = "audio/l16"
    assert [e.kind for e in odd.feed(bad_start)] == ["start", "transcode_error"]
    assert odd.feed(twilio_messages()[2])[0].kind == "transcode_error"
    outbound = json.loads(media_message(STREAM, b"\xff\xff"))
    assert outbound["event"] == "media" and base64.b64decode(outbound["media"]["payload"]) == b"\xff\xff"


# --- Metrics -----------------------------------------------------------------------------------------


def reviewed(text: str, locale: str = "en-IN") -> ReferenceTranscript:
    return ReferenceTranscript(text=text, locale=locale, review_status="reviewed", reviewed_by="reviewer@example.test")


def test_wer_cer_with_disclosed_normalization():
    result = transcript_quality(reviewed("I want to pay ₹1,25,000 on Friday."), "i want pay ₹1,25,000 on friday")
    assert result.wer.deletions == 1 and result.wer.reference_length == 7  # grouping marks join digits
    assert result.wer.rate == pytest.approx(1 / 7)
    split = transcript_quality(reviewed("pay ₹1,25,000"), "pay ₹1 25 000")
    assert (split.wer.substitutions, split.wer.insertions) == (1, 2)
    assert result.normalization["version"] == "susanoo-asr-norm/1" and result.normalization["unicode_form"] == "NFC"
    assert transcript_quality(reviewed("Don't stop"), "dont stop").wer.errors == 0  # apostrophes deleted, disclosed
    perfect = transcript_quality(reviewed("hello"), "HELLO!")
    assert perfect.wer.rate == 0 and perfect.cer.rate == 0


def test_wer_cer_devanagari_and_code_switching():
    precomposed, decomposed = "\u0958", "\u0915\u093c"  # क़ written two ways; NFC makes them equal
    hi = transcript_quality(
        reviewed(f"नमस्ते, मैं कल {precomposed}र्ज़ भुगतान करूँगा।", "hi-IN"), f"नमस्ते मैं कल {decomposed}र्ज़ भुगतान करूंगा"
    )
    assert hi.wer.substitutions == 1 and hi.wer.reference_length == 6  # chandrabindu vs anusvara differs
    assert graphemes("क्षमा") == ["क्ष", "मा"]  # virama joins the conjunct
    assert hi.cer.errors == 1
    mixed = transcript_quality(reviewed("माझं payment उद्या होईल", "mr-IN"), "माझं पेमेंट उद्या होईल")
    assert mixed.wer.substitutions == 1 and mixed.wer.rate == pytest.approx(0.25)
    digits = NormalizationPolicy(ascii_digits=True)
    assert transcript_quality(reviewed("₹१,२५,०००", "hi-IN"), "₹1,25,000", digits).cer.errors == 0
    assert NormalizationPolicy().normalize("कृपया पुष्टि कीजिए।") == "कृपया पुष्टि कीजिए"  # danda is punctuation


def test_wer_requires_reviewed_reference():
    assert isinstance(transcript_quality(None, "hi"), Unavailable)
    draft = ReferenceTranscript(text="नमस्ते", locale="hi-IN", review_status="draft")
    refused = transcript_quality(draft, "नमस्ते")
    assert isinstance(refused, Unavailable) and "draft" in refused.reason
    with pytest.raises(ValidationError):
        ReferenceTranscript(text="x", locale="en-IN", review_status="reviewed")


def test_latency_requires_media_timestamps():
    text_log = [
        AudioTurnBoundary(index=0, role="caller", source="text_log", start_at=NOW, end_at=NOW),
        AudioTurnBoundary(index=1, role="agent", source="text_log", start_at=NOW + timedelta(seconds=1)),
    ]
    refused = response_latency(text_log)
    assert isinstance(refused, Unavailable) and "text_log" in refused.reason
    assert isinstance(response_latency([]), Unavailable)
    media = [
        AudioTurnBoundary(index=0, role="caller", source="media_capture", start_ns=0, end_ns=1_000_000_000),
        AudioTurnBoundary(index=1, role="agent", source="media_capture", start_ns=1_400_000_000, end_ns=2_000_000_000),
        AudioTurnBoundary(index=2, role="agent", source="media_capture", start_ns=2_500_000_000, end_ns=3_000_000_000),
        AudioTurnBoundary(index=3, role="caller", source="media_capture", start_ns=3_100_000_000, end_ns=4_000_000_000),
        AudioTurnBoundary(index=4, role="agent", source="media_capture", start_ns=4_900_000_000, end_ns=5_000_000_000),
    ]
    dist = response_latency(media)
    assert dist.samples_ms == [400.0, 900.0] and dist.p50_ms == 400.0 and dist.max_ms == 900.0
    mixed = [media[0], media[1].model_copy(update={"start_ns": None, "start_at": NOW})]
    assert isinstance(response_latency(mixed), Unavailable)


def test_transcript_only_run_refuses_audio_metrics():
    report = audio_metrics("transcript_only", reference=reviewed("hello"), hypothesis="hello")
    for field in (report.transcript_quality, report.response_latency, report.interruptions, report.speech_quality):
        assert isinstance(field, Unavailable)
    assert "transcript-only" in report.response_latency.reason
    audio = audio_metrics("audio", boundaries=[], reference=None, hypothesis=None)
    assert isinstance(audio.response_latency, Unavailable) and isinstance(audio.speech_quality, Unavailable)


# --- Multilingual drafts -----------------------------------------------------------------------------


def test_multilingual_cases_are_drafts_with_indian_locale_formatting():
    assert synth.group_indian(123456789) == "12,34,56,789"
    assert synth.format_inr(12500000) == "₹1,25,000.00"
    assert synth.format_inr(12500000, devanagari_digits=True) == "₹१,२५,०००.००"
    for locale in ("en-IN", "hi-IN", "mr-IN"):
        doc = json.loads((ROOT / f"examples/voice/cases/{locale}.json").read_text(encoding="utf-8"))
        assert doc["status"] == "draft — requires fluent review" and doc["validated"] is False
        assert doc["review"]["reviewed_by"] is None and "validated" not in doc["provenance"].replace(
            "not a validated", ""
        )
        for case in doc["cases"]:
            facts = case["facts"]
            deva = any("०" <= ch <= "९" for ch in facts["amount_display"])
            assert facts["amount_display"] == synth.format_inr(facts["amount_minor"], devanagari_digits=deva)
            assert facts["currency"] == "INR" and facts["timezone"] == "Asia/Kolkata"
            assert case["reference_transcript"]["review_status"] == "draft"
            # A draft reference can never produce WER/CER.
            ref = ReferenceTranscript(text=case["reference_transcript"]["text"], locale=locale, review_status="draft")
            assert isinstance(transcript_quality(ref, case["reference_transcript"]["text"]), Unavailable)
        for fixture in doc["audio"]["fixtures"]:
            assert load_wav((ROOT / "examples/voice/cases" / fixture).resolve()).sample_rate == 8000
