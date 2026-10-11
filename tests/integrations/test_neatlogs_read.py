"""neatlogs read client with a fake HTTP transport. No network, no real token."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from susanoo.integrations import neatlogs_read as nr

TOKEN = "nl-read-" + "t" * 20  # built by concatenation so it never looks like a committed key
TRACE = "0123456789abcdef0123456789abcdef"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def make(handler, clock: Clock | None = None, **kw: Any) -> nr.NeatlogsReadClient:
    clock = clock or Clock()
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return nr.NeatlogsReadClient(
        TOKEN, "proj-1", http=http, sleep=clock.sleep, clock=clock, finalize_wait_s=5, poll_interval_s=1, **kw
    )


def span(i: int, parent: str | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "spanId": f"s{i}",
        "parentSpanId": parent,
        "spanName": f"n{i}",
        "spanType": "chain",
        "status": "ok",
        **extra,
    }


async def test_headers_pagination_and_cursor():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith(f"/traces/{TRACE}"):
            return httpx.Response(200, json={"finalizationStatus": "finalized"})
        cursor = request.url.params.get("cursor")
        if cursor is None:
            return httpx.Response(200, json={"spans": [span(1), span(2, "s1")], "nextCursor": "c2"})
        assert cursor == "c2"
        return httpx.Response(200, json={"spans": [span(3, "s1")], "nextCursor": None})

    client = make(handler)
    spans = await client.trace_spans(TRACE)
    assert [s["span_id"] for s in spans] == ["s1", "s2", "s3"]
    assert spans[1]["parent_id"] == "s1" and spans[0]["parent_id"] is None
    span_calls = [r for r in seen if r.url.path.endswith("/spans")]
    assert [r.url.params.get("limit") for r in span_calls] == ["50", "50"]
    assert all(r.headers["Authorization"] == f"Bearer {TOKEN}" and r.headers["x-project-id"] == "proj-1" for r in seen)
    assert str(seen[0].url).startswith("https://app.neatlogs.com/api/v1/public/traces/")


async def test_a_repeating_cursor_or_page_limit_stops_the_loop():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/spans"):
            return httpx.Response(200, json={"spans": [span(1)], "nextCursor": "same"})
        return httpx.Response(200, json={"finalizationStatus": "finalized"})

    assert len(await make(handler).fetch_spans(TRACE)) == 2  # first page, then the repeated cursor once


async def test_waits_until_finalized_with_a_bounded_poll():
    states = iter(["pending", "pending", "finalized"])
    clock = Clock()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/spans"):
            return httpx.Response(200, json={"data": {"spans": [span(1)]}})
        return httpx.Response(200, json={"data": {"finalizationStatus": next(states)}})

    spans = await make(handler, clock).trace_spans(TRACE)
    assert [s["span_id"] for s in spans] == ["s1"]
    assert clock.slept == [1, 1]


async def test_not_finalized_in_time_raises_not_ready():
    clock = Clock()
    client = make(lambda r: httpx.Response(200, json={"finalizationStatus": "pending"}), clock)
    with pytest.raises(nr.NeatlogsNotReady):
        await client.trace_spans(TRACE)
    assert clock.now >= 5  # waited the bounded time, not forever


async def test_404_right_after_a_run_is_retried_then_gives_up():
    clock = Clock()
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(404, json={"error": "not found"})

    with pytest.raises(nr.NeatlogsNotReady):
        await make(handler, clock).wait_finalized(TRACE)
    assert len(calls) > 1


@pytest.mark.parametrize("status", [401, 403])
async def test_unauthorised_raises_auth_error_without_the_token(status):
    client = make(lambda r: httpx.Response(status, json={"error": "nope"}))
    with pytest.raises(nr.NeatlogsAuthError) as err:
        await client.trace_spans(TRACE)
    assert TOKEN not in str(err.value) and TOKEN not in repr(err.value)


async def test_other_http_errors_and_network_errors_are_read_errors():
    with pytest.raises(nr.NeatlogsReadError):
        await make(lambda r: httpx.Response(400)).wait_finalized(TRACE)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    with pytest.raises(nr.NeatlogsReadError) as err:
        await make(boom).wait_finalized(TRACE)
    assert TOKEN not in str(err.value)


async def test_bad_trace_ids_never_reach_the_network():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request expected")

    client = make(handler)
    for bad in ("", "../etc/passwd", "a/b", "x" * 300, "short"):
        with pytest.raises(nr.NeatlogsReadError):
            await client.fetch_spans(bad)


async def test_non_json_answer_is_a_read_error():
    with pytest.raises(nr.NeatlogsReadError):
        await make(lambda r: httpx.Response(200, text="<html>")).wait_finalized(TRACE)


async def test_llm_spans_become_the_trace_excerpt_shape():
    llm_span = span(
        2,
        "s1",
        spanType="llm",
        spanName="anthropic.messages.create",
        output='{"findings": []}',
        costUsd=0.01,
        status="ERROR",
        attributes={"neatlogs": {"llm": {"finish_reason": "end_turn"}}, "scenario_id": "t1"},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/spans"):
            return httpx.Response(200, json={"spans": [span(1), llm_span]})
        return httpx.Response(200, json={"finalizationStatus": "finalized"})

    spans = await make(handler).trace_spans(TRACE)
    assert spans[1]["round"] == 1 and spans[1]["stop_reason"] == "end_turn"
    assert spans[1]["text_out"] == '{"findings": []}' and spans[1]["status"] == "error"
    assert spans[0]["name"] == "n1"


def test_to_raw_span_scrubs_secrets():
    key = "sk-ant-" + "api03-" + "C" * 30
    raw = nr.to_raw_span(span(1, attributes={"note": f"key {key}", "api_key": "abcdefgh"}), 0)
    assert key not in json.dumps(raw) and "abcdefgh" not in json.dumps(raw)


def test_from_env(monkeypatch):
    monkeypatch.delenv("NEATLOGS_TOKEN", raising=False)
    monkeypatch.delenv("NEATLOGS_PROJECT_ID", raising=False)
    assert nr.NeatlogsReadClient.from_env() is None
    monkeypatch.setenv("NEATLOGS_TOKEN", TOKEN)
    assert nr.NeatlogsReadClient.from_env() is None  # project id missing too
    monkeypatch.setenv("NEATLOGS_PROJECT_ID", "p")
    assert isinstance(nr.NeatlogsReadClient.from_env(), nr.NeatlogsReadClient)
    with pytest.raises(ValueError):
        nr.NeatlogsReadClient("", "p")
