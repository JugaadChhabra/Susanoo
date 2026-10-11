"""Unit tests for integrations/tracing.py. No network and no keys: neatlogs runs with export disabled."""

import asyncio
import logging

import neatlogs
import pytest
from opentelemetry.trace import StatusCode

from susanoo.integrations import tracing
from susanoo.integrations.tracing import REDACTED


@pytest.fixture(autouse=True)
def no_keys(monkeypatch):
    monkeypatch.delenv("NEATLOGS_API_KEY", raising=False)
    monkeypatch.delenv("NEATLOGS_TRACE_URL_TEMPLATE", raising=False)


# --- setup ---------------------------------------------------------------------------------------------


def test_setup_is_idempotent_and_warns_once_without_key(monkeypatch, caplog):
    calls = []
    monkeypatch.setattr(tracing, "_initialised", False)
    monkeypatch.setattr(neatlogs, "init", lambda **kw: calls.append(kw))

    with caplog.at_level(logging.WARNING, logger=tracing.log.name):
        tracing.setup()
        tracing.setup()

    assert len(calls) == 1
    assert calls[0]["instrumentations"] == ["anthropic"]
    assert calls[0]["mask"] is tracing._mask_snapshot
    assert calls[0]["workflow_name"] == "susanoo"
    warnings = [r.getMessage() for r in caplog.records]
    assert warnings == ["NEATLOGS_API_KEY is not set, so traces will not be sent to neatlogs."]


def test_setup_does_not_warn_or_log_the_key_when_set(monkeypatch, caplog):
    monkeypatch.setenv("NEATLOGS_API_KEY", "nl-secret-value-123456")
    monkeypatch.setattr(tracing, "_initialised", False)
    monkeypatch.setattr(neatlogs, "init", lambda **kw: None)
    with caplog.at_level(logging.DEBUG, logger=tracing.log.name):
        tracing.setup()
    assert "nl-secret-value-123456" not in caplog.text
    assert caplog.records == []


# --- spans ---------------------------------------------------------------------------------------------


def test_span_context_manager_sets_attributes_and_ids():
    assert tracing.current_ids() is None
    with tracing.span("planner", run_id="r1", attempt=2, ratio=0.5, ok=True, nothing=None) as s:
        ids = tracing.current_ids()
        assert ids is not None
        trace_id, span_id = ids
        assert len(trace_id) == 32 and len(span_id) == 16
        assert f"{s.get_span_context().span_id:016x}" == span_id
    assert tracing.current_ids() is None
    assert s.name == "planner"
    assert s.attributes["run_id"] == "r1"
    assert s.attributes["attempt"] == 2
    assert s.attributes["ratio"] == 0.5
    assert s.attributes["ok"] is True
    assert "nothing" not in s.attributes
    assert s.attributes["openinference.span.kind"] == "CHAIN"


def test_span_kind_and_non_primitive_attrs_become_strings():
    with tracing.span("builder", kind="AGENT", files=["a.py", "b.py"], meta={"n": 1}) as s:
        pass
    assert s.attributes["openinference.span.kind"] == "AGENT"
    assert s.attributes["files"] == '["a.py", "b.py"]'
    assert s.attributes["meta"] == '{"n": 1}'


def test_span_attribute_names_may_shadow_neatlogs_arguments():
    with tracing.span("x", version="v1", mask="m", session_id="s") as s:
        pass
    assert s.attributes["version"] == "v1"
    assert s.attributes["mask"] == "m"
    assert s.attributes["session_id"] == "s"


def test_span_redacts_attributes():
    with tracing.span(
        "fixer",
        api_key="sk-ant-api03-abcdefghijklmnop",
        note="called with Bearer abcdefghijklmnop1234",
        creds={"password": "hunter2", "user": "bob"},
        input_tokens=120,
    ) as s:
        pass
    assert s.attributes["api_key"] == REDACTED
    assert s.attributes["note"] == f"called with {REDACTED}"
    assert "hunter2" not in s.attributes["creds"]
    assert "bob" in s.attributes["creds"]
    assert s.attributes["input_tokens"] == 120


def test_span_sync_decorator():
    @tracing.span("sync_head", run_id="r2")
    def work(x, y=1):
        assert tracing.current_ids() is not None
        return x + y

    assert work.__name__ == "work"
    assert work(1, y=2) == 3
    assert tracing.current_ids() is None


async def test_span_async_decorator_covers_the_awaited_work():
    seen = []

    @tracing.span("async_head")
    async def work():
        seen.append(tracing.current_ids())
        await asyncio.sleep(0)
        seen.append(tracing.current_ids())
        return "done"

    assert asyncio.iscoroutinefunction(work)
    assert await work() == "done"
    assert seen[0] is not None and seen[0] == seen[1]
    assert tracing.current_ids() is None


def test_decorated_function_gets_a_fresh_span_each_call():
    @tracing.span("again")
    def work():
        return tracing.current_ids()

    first, second = work(), work()
    assert first is not None and second is not None
    assert first[0] != second[0]


def test_nested_spans_share_trace_and_differ_in_span_id():
    with tracing.span("outer") as outer:
        outer_ids = tracing.current_ids()
        with tracing.span("inner") as inner:
            inner_ids = tracing.current_ids()
        assert tracing.current_ids() == outer_ids
    assert outer_ids[0] == inner_ids[0]
    assert outer_ids[1] != inner_ids[1]
    assert inner.parent.span_id == outer.get_span_context().span_id


async def test_concurrent_spans_keep_their_own_ids():
    async def one(name):
        with tracing.span(name):
            first = tracing.current_ids()
            await asyncio.sleep(0)
            assert tracing.current_ids() == first
            return first

    a, b = await asyncio.gather(one("a"), one("b"))
    assert a != b


def test_exception_sets_error_status_and_reraises():
    with pytest.raises(ValueError, match="boom"):
        with tracing.span("verifier") as s:
            raise ValueError("boom with Bearer abcdefghijklmnop1234")
    assert s.status.status_code == StatusCode.ERROR
    assert "ValueError" in s.status.description
    assert "abcdefghijklmnop1234" not in s.status.description
    (event,) = [e for e in s.events if e.name == "exception"]
    assert event.attributes["exception.type"] == "ValueError"
    assert "abcdefghijklmnop1234" not in event.attributes["exception.message"]
    assert tracing.current_ids() is None


async def test_async_decorator_error_marks_span_and_reraises():
    captured = {}

    @tracing.span("async_fail")
    async def work():
        captured["ids"] = tracing.current_ids()
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError, match="nope"):
        await work()
    assert captured["ids"] is not None
    assert tracing.current_ids() is None


def test_current_ids_none_outside_a_span():
    assert tracing.current_ids() is None


# --- redaction -----------------------------------------------------------------------------------------

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoic2VydmljZSJ9.c2lnbmF0dXJlMTIzNA"


@pytest.mark.parametrize(
    "secret",
    [
        "sk-ant-api03-AbCdEfGhIjKlMnOp",
        "sk-proj-AbCdEfGhIjKlMnOpQrStUv",
        "sk_live_AbCdEfGh12345678",
        "pk_test_AbCdEfGh12345678",
        "abcdefghijklmnop1234",  # used behind "Bearer " below
        JWT,
        "AKIAIOSFODNN7EXAMPLE",
        "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2",
        "gho_" + "a1B2c3D4e5F6g7H8i9J0k1L2",
        "github_pat_" + "11ABCDEFG0abcdefghijkl_mnopqrstuv",
    ],
)
def test_secret_values_are_redacted(secret):
    text = f"Bearer {secret}" if secret == "abcdefghijklmnop1234" else secret
    out = tracing.redact("note", f"before {text} after")
    assert secret not in out
    assert out == f"before {REDACTED} after"


def test_pem_private_key_is_redacted_even_when_truncated():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEabc\ndef/ghi\n-----END RSA PRIVATE KEY-----"
    assert tracing.redact("note", f"x {pem} y") == f"x {REDACTED} y"
    truncated = "-----BEGIN PRIVATE KEY-----\nMIIEabc\ndef"
    assert tracing.redact("note", f"x {truncated}") == f"x {REDACTED}"


def test_database_url_password_is_masked_but_host_kept():
    url = "postgresql://postgres:s3cr3tpass@db.example.supabase.co:5432/postgres"
    out = tracing.redact("db", url)
    assert "s3cr3tpass" not in out
    assert out == f"postgresql://postgres:{REDACTED}@db.example.supabase.co:5432/postgres"


@pytest.mark.parametrize(
    "text, value",
    [
        ('api_key = "q8Zr2Lm9Xv4Tn7Bc1Wd5"', "q8Zr2Lm9Xv4Tn7Bc1Wd5"),
        ("api_key = 'q8Zr2Lm9Xv4Tn7Bc1Wd5'", "q8Zr2Lm9Xv4Tn7Bc1Wd5"),
        ('def f(token="a1b2c3d4e5f6a7b8"):', "a1b2c3d4e5f6a7b8"),
        ('{"x-api-key": "0123456789abcdef"}', "0123456789abcdef"),
        ('headers={"X-Api-Key": "0123456789abcdef"}', "0123456789abcdef"),
        ('{"Authorization": "Token 0123456789abcdef"}', "0123456789abcdef"),
        ('password: str = "hunter2hunter2"', "hunter2hunter2"),
        ('DB_PASSWORD="s3cr3t-pass"', "s3cr3t-pass"),
        ('client(access_token="ya29.abcdefghijk")', "ya29.abcdefghijk"),
        ('{"private_key": "abcdef123456"}', "abcdef123456"),
    ],
)
def test_quoted_assigned_secrets_are_redacted(text, value):
    out = tracing.scrub_text(text)
    assert value not in out and REDACTED in out
    assert tracing.scrub_text(out) == out  # idempotent


def test_quoted_assignment_redaction_keeps_the_name_and_quotes():
    assert tracing.scrub_text('api_key = "q8Zr2Lm9Xv4Tn7Bc1Wd5"') == f'api_key = "{REDACTED}"'
    assert tracing.scrub_text('{"x-api-key": "0123456789abcdef"}') == f'{{"x-api-key": "{REDACTED}"}}'


@pytest.mark.parametrize(
    "text",
    [
        'token = os.environ["GITHUB_TOKEN"]',
        'headers = {"Authorization": f"Bearer {token}"}',
        "max_tokens=4096",
        '{"input_tokens": 12}',
        'name = "reviewer"',
        'api_key = ""',
    ],
)
def test_quoted_assignment_redaction_leaves_harmless_text_alone(text):
    assert tracing.scrub_text(text) == text


def test_looks_secret_shaped_ignores_assignment_forms_but_sees_value_shapes():
    assert tracing.looks_secret_shaped("use " + "sk-ant-" + "api03-AbCdEfGhIjKlMnOp here")
    assert tracing.looks_secret_shaped("postgres://u:pw1234@h/db")
    assert not tracing.looks_secret_shaped('for example API_KEY = "pay_live_..." in code')


def test_is_sensitive_name():
    assert tracing.is_sensitive_name("X-Api-Key") and tracing.is_sensitive_name("db_password")
    assert not tracing.is_sensitive_name("repo_name")


def test_assigned_secret_in_text_is_redacted():
    out = tracing.redact("log", "connect with password=hunter2 now")
    assert "hunter2" not in out and "connect with" in out


@pytest.mark.parametrize(
    "key",
    [
        "api_key",
        "API-KEY",
        "apikey",
        "access_token",
        "Token",
        "client_secret",
        "password",
        "passwd",
        "Authorization",
        "credentials",
        "set-cookie",
        "private_key",
        "private-key",
    ],
)
def test_sensitive_key_names_redact_the_value(key):
    assert tracing.redact(key, "plain-looking-value") == REDACTED
    assert tracing.redact(key, {"a": 1}) == REDACTED


def test_harmless_keys_and_token_counts_survive():
    assert tracing.redact("run_id", "run-123") == "run-123"
    assert tracing.redact("input_tokens", 42) == 42
    assert tracing.redact("max_tokens", 16) == 16
    assert tracing.redact("note", "nothing to see, sk- alone is fine") == "nothing to see, sk- alone is fine"
    assert tracing.redact("module", "susanoo.heads.planner") == "susanoo.heads.planner"


# --- _mask_snapshot --------------------------------------------------------------------------------------


def _snapshot():
    return {
        "signal": "span",
        "name": "simulator",
        "attributes": {
            "llm.input_messages.0.message.content": "use key sk-ant-api03-AbCdEfGhIjKlMnOp please",
            "llm.token_count.prompt": 12,
            "http.authorization": "Basic abc",
            "ok": True,
        },
        "events": [
            {"name": "exception", "timestamp": 1, "attributes": {"exception.message": f"token {JWT} leaked"}},
            {"name": "log", "timestamp": 2, "attributes": {"body": "postgres://u:pw1234@h/db"}},
        ],
        "links": [],
        "resource": {"attributes": {"service.name": "susanoo"}},
        "status": {"code": "ERROR", "description": "failed with Bearer abcdefghijklmnop1234"},
    }


def test_mask_snapshot_scrubs_attributes_events_and_status():
    out = tracing._mask_snapshot(_snapshot())
    flat = repr(out)
    for leaked in ("sk-ant-api03", JWT, "pw1234", "abcdefghijklmnop1234", "Basic abc"):
        assert leaked not in flat
    assert out["attributes"]["llm.token_count.prompt"] == 12
    assert out["attributes"]["ok"] is True
    assert out["events"][0]["timestamp"] == 1
    assert out["events"][0]["name"] == "exception"
    assert out["resource"]["attributes"]["service.name"] == "susanoo"
    assert out["name"] == "simulator"


def test_mask_snapshot_does_not_mutate_its_input():
    snap = _snapshot()
    tracing._mask_snapshot(snap)
    assert "sk-ant-api03" in snap["attributes"]["llm.input_messages.0.message.content"]


@pytest.mark.parametrize(
    "odd",
    [
        None,
        5,
        "text sk-ant-api03-AbCdEfGhIjKlMnOp",
        [],
        {},
        {"attributes": None},
        {"events": "nope"},
        {"attributes": {1: object()}},
        object(),
    ],
)
def test_mask_snapshot_never_raises_on_odd_input(odd):
    tracing._mask_snapshot(odd)


def test_mask_snapshot_fails_closed_when_scrubbing_breaks(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("scrubber broke")

    monkeypatch.setattr(tracing, "_scrub_obj", boom)
    out = tracing._mask_snapshot(_snapshot())
    assert out["attributes"] == {}
    assert out["events"] == []
    assert out["status"]["description"] is None
    assert "sk-ant" not in repr(out)

    out = tracing._mask_snapshot(object())  # not even a mapping
    assert out == {"attributes": {}, "events": []}


# --- trace_url -----------------------------------------------------------------------------------------


def test_trace_url_none_without_template():
    assert tracing.trace_url("abc123") is None


def test_trace_url_uses_template(monkeypatch):
    monkeypatch.setenv("NEATLOGS_TRACE_URL_TEMPLATE", "https://app.example/traces/{trace_id}?tab=spans")
    assert tracing.trace_url("abc123") == "https://app.example/traces/abc123?tab=spans"


def test_trace_url_ignores_template_without_placeholder(monkeypatch):
    monkeypatch.setenv("NEATLOGS_TRACE_URL_TEMPLATE", "https://app.example/traces")
    assert tracing.trace_url("abc123") is None


# --- local span recorder -------------------------------------------------------------------------------------


def _record_demo_trace():
    """Spans shaped like the neatlogs Anthropic wrapper's, on a private provider with the recorder attached."""
    import json

    from opentelemetry.sdk.trace import TracerProvider

    recorder = tracing._Recorder()
    provider = TracerProvider()
    provider.add_span_processor(recorder)
    tracer = provider.get_tracer("t")
    key = "sk-ant-" + "api03-" + "D" * 30
    with tracer.start_as_current_span(
        "runner.scenario",
        attributes={"scenario_id": "t1", "split": "train", "iteration": 0, "level": 1, "api_key": "hunter2hunter2"},
    ) as root:
        trace_id = f"{root.get_span_context().trace_id:032x}"
        r1 = tracer.start_span(
            "anthropic.messages.create",
            attributes={
                "neatlogs.span.kind": "llm",
                "neatlogs.llm.input_messages.0.role": "system",
                "neatlogs.llm.input_messages.0.content": "SYSTEM PROMPT",
                "neatlogs.llm.input_messages.1.role": "user",
                "neatlogs.llm.input_messages.1.content": "review this diff",
                "neatlogs.llm.tool_calls.0.id": "tu_1",
                "neatlogs.llm.tool_calls.0.name": "read_file",
                "neatlogs.llm.tool_calls.0.arguments": json.dumps({"path": "app/db.py"}),
                "neatlogs.llm.finish_reason": "tool_use",
                "neatlogs.llm.token_count.prompt": 10,
            },
        )
        r1.end()
        results = [{"type": "tool_result", "tool_use_id": "tu_1", "content": f"line 1: token {key}", "is_error": True}]
        r2 = tracer.start_span(
            "anthropic.messages.create",
            attributes={
                "neatlogs.span.kind": "llm",
                "neatlogs.llm.input_messages.0.role": "system",
                "neatlogs.llm.input_messages.0.content": "SYSTEM PROMPT",
                "neatlogs.llm.input_messages.1.role": "user",
                "neatlogs.llm.input_messages.1.content": "review this diff",
                "neatlogs.llm.input_messages.2.role": "assistant",
                "neatlogs.llm.input_messages.2.content": "[]",
                "neatlogs.llm.input_messages.3.role": "user",
                "neatlogs.llm.input_messages.3.content": json.dumps(results),
                "neatlogs.llm.output_messages.0.content": '{"findings": []}',
                "neatlogs.llm.finish_reason": "end_turn",
            },
        )
        r2.end()
    return recorder, trace_id, key


def test_recorder_turns_model_call_spans_into_the_trace_excerpt():
    import json

    recorder, trace_id, key = _record_demo_trace()
    spans = tracing.trace_excerpt(recorder.get(trace_id))
    assert [s["name"] for s in spans] == ["runner.scenario", "anthropic.messages.create", "anthropic.messages.create"]
    root, first, second = spans
    assert root["parent_id"] is None and root["status"] == "ok"
    assert root["attrs"]["scenario_id"] == "t1" and root["attrs"]["level"] == 1
    assert root["attrs"]["api_key"] == REDACTED
    assert first["parent_id"] == root["span_id"] and len(first["span_id"]) == 16
    assert (first["round"], second["round"]) == (1, 2)
    assert first["stop_reason"] == "tool_use" and first["tool_results_in"] == []
    assert first["tool_calls_out"] == [{"name": "read_file", "input": {"path": "app/db.py"}}]
    assert second["tool_calls_out"] == [] and second["text_out"] == '{"findings": []}'
    (answer,) = second["tool_results_in"]
    assert answer["name"] == "read_file" and answer["is_error"] is True and key not in answer["content"]
    assert "SYSTEM PROMPT" not in json.dumps(recorder.get(trace_id))  # repeated every round, so not stored
    assert set(root) == {"span_id", "name", "parent_id", "status", "attrs"}


def test_recorder_marks_errors_and_is_a_bounded_lru():
    import json

    from opentelemetry.sdk.trace import TracerProvider

    recorder = tracing._Recorder(max_traces=2, max_spans=3)
    provider = TracerProvider()
    provider.add_span_processor(recorder)
    tracer = provider.get_tracer("t")
    ids = []
    for i in range(3):
        with tracer.start_as_current_span(f"root{i}") as s:
            ids.append(f"{s.get_span_context().trace_id:032x}")
    assert recorder.get(ids[0]) == []  # oldest trace evicted
    assert len(recorder.get(ids[1])) == 1 and len(recorder.get(ids[2])) == 1

    with tracer.start_as_current_span("many") as s:
        many = f"{s.get_span_context().trace_id:032x}"
        for j in range(6):
            tracer.start_span(f"c{j}").end()
    assert len(recorder.get(many)) == 3  # per-trace cap

    with pytest.raises(RuntimeError):
        with tracer.start_as_current_span("bad", record_exception=False, set_status_on_exception=True) as s:
            bad = f"{s.get_span_context().trace_id:032x}"
            raise RuntimeError("password=hunter2hunter2 failed")
    (failed,) = tracing.trace_excerpt(recorder.get(bad))
    assert failed["status"] == "error" and "hunter2hunter2" not in json.dumps(failed)
    assert "RuntimeError" in failed["attrs"]["error"]


def test_tracing_span_is_recorded_and_readable_by_trace_id():
    tracing.setup()
    tracing._install_recorder()
    tracing._install_recorder()  # idempotent
    with tracing.span("runner.scenario", kind="AGENT", scenario_id="t9", split="train", iteration=1, level=1):
        trace_id, span_id = tracing.current_ids()
    spans = tracing.recorded_spans(trace_id)
    assert [s["span_id"] for s in spans] == [span_id]
    assert spans[0]["attrs"]["scenario_id"] == "t9" and spans[0]["attrs"]["iteration"] == 1
    assert tracing.recorded_spans("0" * 32) == []
    tracing.clear_recorded_spans()
    assert tracing.recorded_spans(trace_id) == []


def test_recorder_failure_never_breaks_setup(monkeypatch):
    import neatlogs._wrap_utils as wrap

    def boom():
        raise RuntimeError("no provider")

    monkeypatch.setattr(wrap, "get_neatlogs_provider", boom)
    tracing._install_recorder()  # logs a warning, does not raise


def test_recorder_total_character_budget_evicts_oldest_traces_first():
    from opentelemetry.sdk.trace import TracerProvider

    recorder = tracing._Recorder(max_traces=100, max_spans=10, max_chars=2500)
    provider = TracerProvider()
    provider.add_span_processor(recorder)
    tracer = provider.get_tracer("t")
    ids = []
    for i in range(5):
        with tracer.start_as_current_span(f"root{i}", attributes={"blob": "x" * 900}) as s:
            ids.append(f"{s.get_span_context().trace_id:032x}")
    kept = [i for i in ids if recorder.get(i)]
    assert kept == ids[-2:]  # only the newest traces fit in the budget
    assert recorder._total <= 2500 and recorder._total == sum(recorder._sizes.values())

    # a single span bigger than the whole budget is not stored, and does not evict the others
    with tracer.start_as_current_span("huge", attributes={"blob": "y" * 3000}) as s:
        huge = f"{s.get_span_context().trace_id:032x}"
    assert recorder.get(huge) == [] and [i for i in ids if recorder.get(i)] == ids[-2:]
    recorder.clear()
    assert recorder._total == 0 and recorder._sizes == {}
