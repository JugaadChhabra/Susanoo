"""Passive production evidence: opt-in, bounded, redacted, fail-open [Spec §18; T34]."""

from __future__ import annotations

import asyncio
import json
import socket
import statistics
import threading
import time

import pytest
from susanoo_runner.collector import CollectorReceiver, token_digest
from susanoo_runner.local import AppServer
from susanoo_sdk.passive import PassiveConfig, PassiveRecorder, Redactor

SECRET_EMAIL = "asha.real@bank.example"
CARD = "4111 1111 1111 1111"


def config(**kw) -> PassiveConfig:
    base = {"enabled": True, "sample_rate": 1.0, "endpoint": "http://127.0.0.1:9", "flush_interval_seconds": 0.05}
    return PassiveConfig(**{**base, **kw})


class Capture:
    def __init__(self, block: threading.Event | None = None, fail: bool = False):
        self.bodies: list[dict] = []
        self.block, self.fail = block, fail

    def __call__(self, body: bytes, headers: dict, timeout: float) -> None:
        if self.block is not None:
            self.block.wait(timeout)
        if self.fail:
            raise ConnectionRefusedError("collector down")
        self.bodies.append({"body": json.loads(body), "headers": headers})


def test_off_by_default_records_and_sends_nothing():
    calls = Capture()
    recorder = PassiveRecorder(transport=calls)
    assert PassiveConfig().enabled is False and PassiveConfig().sample_rate == 0
    assert recorder.trace("c-1", "turn", input="hi") is False
    assert recorder.outcome("c-1", "promise", "created") is False

    @recorder.instrument()
    def answer(message, conversation_id):
        return "ok"

    assert answer("hi", conversation_id="c-1") == "ok"
    coverage = recorder.coverage()
    assert coverage.enabled is False and coverage.spans_enqueued == 0 and recorder._thread is None
    enabled_but_unsampled = PassiveRecorder(PassiveConfig(enabled=True, endpoint="http://x"), transport=calls)
    assert enabled_but_unsampled.trace("c-1", "turn", input="hi") is False
    assert calls.bodies == []


def test_redaction_happens_before_enqueue():
    gate = threading.Event()
    calls = Capture(block=gate)
    recorder = PassiveRecorder(config(), transport=calls)
    recorder.trace(
        "c-1",
        "turn",
        input=f"my card is {CARD} and email {SECRET_EMAIL}",
        tool="create_promise",
        arguments={"account": "ACC-1", "password": "hunter2", "authorization": "Bearer abcdefghijkl"},
    )
    queued = json.dumps(list(recorder._queue.queue))  # what sits in memory waiting for export
    gate.set()
    recorder.flush(5)
    sent = json.dumps(calls.bodies)
    for blob in (queued, sent):
        assert SECRET_EMAIL not in blob and "4111" not in blob and "hunter2" not in blob and "abcdefghijkl" not in blob
    assert "[EMAIL]" in sent and "[CARD]" in sent and "[REDACTED]" in sent
    assert recorder.coverage().redactions >= 4
    recorder.shutdown()


def test_queue_is_bounded_and_drops_are_counted():
    gate = threading.Event()
    recorder = PassiveRecorder(config(max_queue=5, batch_size=1), transport=Capture(block=gate))
    started = time.perf_counter()
    results = [recorder.trace(f"c-{i}", "turn", input="hello") for i in range(50)]
    elapsed = time.perf_counter() - started
    coverage = recorder.coverage()
    assert coverage.dropped_queue_full >= 40 and coverage.dropped_queue_full == results.count(False)
    assert coverage.spans_enqueued + coverage.dropped_queue_full == 50
    assert elapsed < 1.0  # never blocks on a full queue
    gate.set()
    recorder.shutdown()


def test_exporter_errors_never_reach_the_application():
    recorder = PassiveRecorder(config(), transport=Capture(fail=True))

    @recorder.instrument(conversation_id=lambda message, conversation_id: conversation_id)
    def answer(message, conversation_id):
        return f"echo {message}"

    assert [answer("hi", conversation_id=f"c-{i}") for i in range(10)] == ["echo hi"] * 10
    recorder.flush(5)
    coverage = recorder.coverage()
    assert coverage.export_failures == 10 and coverage.exported_spans == 0
    recorder.shutdown()


def test_internal_recorder_bugs_fail_open():
    class Broken(Redactor):
        def value(self, value, _depth=0):
            raise RuntimeError("redactor bug")

    recorder = PassiveRecorder(config(), transport=Capture(), redactor=Broken())

    @recorder.instrument()
    def answer(message, conversation_id=None):
        return "fine"

    assert answer("hi", conversation_id="c-1") == "fine"
    assert recorder.coverage().internal_errors == 1


def test_application_exceptions_are_its_own_and_recorded_as_errors():
    calls = Capture()
    recorder = PassiveRecorder(config(), transport=calls)

    @recorder.instrument(name="agent.respond")
    def answer(message, conversation_id=None):
        raise ValueError("app bug")

    with pytest.raises(ValueError, match="app bug"):
        answer("hi", conversation_id="c-err")
    recorder.flush(5)
    span = calls.bodies[0]["body"]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert (
        span["status"]["code"] == 2
        and {"key": "error.type", "value": {"stringValue": "ValueError"}} in span["attributes"]
    )
    recorder.shutdown()


@pytest.mark.parametrize("collector", ["blackhole", "refused"])
def test_instrumented_latency_is_unaffected_when_the_collector_is_down(collector):
    """A collector that accepts TCP but never answers (timeouts) or refuses connections."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    if collector == "blackhole":
        sock.listen(0)  # never accept(): connects hang until the export timeout
    else:
        sock.close()
    recorder = PassiveRecorder(config(endpoint=f"http://127.0.0.1:{port}", export_timeout_seconds=2.0, batch_size=5))

    def plain(message, conversation_id):
        return message.upper()

    instrumented = recorder.instrument()(plain)

    def timings(fn, n=300):
        out = []
        for i in range(n):
            started = time.perf_counter()
            fn(f"message {i} from {SECRET_EMAIL}", conversation_id=f"c-{i % 20}")
            out.append(time.perf_counter() - started)
        return out

    timings(instrumented, 20)  # warm up and start the exporter thread
    base, inst = timings(plain), timings(instrumented)
    try:
        # The exporter blocks for up to its 2 s timeout per batch; no app call may ever wait on that.
        assert statistics.median(inst) - statistics.median(base) < 0.002
        assert sorted(inst)[int(len(inst) * 0.99)] < 0.01
        assert max(inst) < 0.5, f"slowest instrumented call took {max(inst) * 1000:.1f} ms"
        coverage = recorder.coverage()
        assert coverage.spans_enqueued + coverage.dropped_queue_full == 320
        assert coverage.exported_spans == 0
    finally:
        recorder.shutdown(timeout=0.1)
        if collector == "blackhole":
            sock.close()


async def test_async_instrumentation_and_outcome_coverage():
    calls = Capture()
    recorder = PassiveRecorder(config(), transport=calls)

    @recorder.instrument()
    async def respond(message, conversation_id=None):
        await asyncio.sleep(0)
        return {"text": "Recorded"}

    for conv in ("c-1", "c-2", "c-3"):
        assert await respond("pay 5000", conversation_id=conv) == {"text": "Recorded"}
    recorder.outcome("c-1", "promise", "created", {"amount_minor": 500000})
    recorder.outcome("c-2", "promise", "failed")
    coverage = recorder.coverage()
    assert coverage.conversations_sampled == 3 and coverage.outcome_events == 2
    assert (coverage.sampled_with_outcome, coverage.sampled_missing_outcome) == (2, 1)
    recorder.flush(5)
    recorder.shutdown()


def test_sampling_is_per_conversation_and_keeps_outcomes_together():
    recorder = PassiveRecorder(config(sample_rate=0.3), transport=Capture())
    ids = [f"conv-{i}" for i in range(400)]
    sampled = [c for c in ids if recorder.trace(c, "turn", input="hi")]
    assert 60 < len(sampled) < 180
    unsampled = next(c for c in ids if c not in sampled)
    assert recorder.outcome(sampled[0], "promise", "created") is True
    assert recorder.outcome(unsampled, "promise", "created") is False
    coverage = recorder.coverage()
    assert coverage.conversations_seen == 400 and coverage.conversations_sampled == len(sampled)
    recorder.shutdown()


def test_passive_traces_reach_the_collector_as_redacted_evidence():
    receiver = CollectorReceiver(tokens={token_digest("tok-live"): "proj-a"})
    with AppServer(receiver) as server:
        recorder = PassiveRecorder(
            config(endpoint=server.base_url, token="tok-live", project_id="proj-a", service_name="collections")
        )
        recorder.trace(
            "conv-9", "turn", input=f"Pay 5000, email {SECRET_EMAIL}", output="Recorded", started_ns=1, ended_ns=2
        )
        recorder.trace(
            "conv-9",
            "create_promise",
            tool="create_promise",
            arguments={"account_id": "ACC-1"},
            error="upstream_timeout",
            started_ns=3,
            ended_ns=4,
        )
        recorder.outcome("conv-9", "promise", "failed")
        assert recorder.flush(10)
        recorder.shutdown()
    assert recorder.coverage().exported_spans == 3
    records = receiver.received["proj-a"]
    assert len(records) == 1
    record = records[0]
    assert record.conversation_id == "conv-9" and record.outcomes[0].status == "failed"
    assert [t.role for t in record.turns] == ["user", "agent", "tool"]
    assert SECRET_EMAIL not in record.model_dump_json()
    assert record.evidence_class == "evidence_only" and record.replay_authorized is False
