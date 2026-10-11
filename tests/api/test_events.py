"""GET /runs/{id}/events: SSE from RUN_EVENTS, resumable by seq, with keep-alive. [PRD › Frontend and API]"""

# ruff: noqa: F811  (fixtures imported from conftest_ai are used as test arguments)

import json
import uuid

import pytest
from conftest_ai import ALICE, BOB, bearer, client, quota_store, store  # noqa: F401

from susanoo.schemas import RunEvent
from susanoo_api.routers import events as events_module


def frames(text: str) -> list[dict]:
    """The `id` and parsed `data` of every event frame in an SSE body."""
    out = []
    for block in text.split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line and not line.startswith(":"))
        if "data" in lines:
            out.append({"id": int(lines["id"]), "event": RunEvent.model_validate(json.loads(lines["data"]))})
    return out


def finished_run(store, owner=ALICE, n=3) -> str:
    run = store.add_run(owner, "done")
    for i in range(n):
        store.add_event(run, "planner", "progress", f"Step {i + 1}.")
    return run


def test_events_need_auth(client):
    assert client.get(f"/runs/{uuid.uuid4()}/events").status_code == 401


def test_events_stream_in_seq_order_and_end_when_the_run_is_done(client, store):
    run = finished_run(store)
    response = client.get(f"/runs/{run}/events", headers=bearer(ALICE))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"].startswith("no-cache") and response.headers["x-accel-buffering"] == "no"
    got = frames(response.text)
    assert [f["id"] for f in got] == [1, 2, 3]
    assert [f["event"].message for f in got] == ["Step 1.", "Step 2.", "Step 3."]
    assert all(f["event"].run_id == run for f in got)


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"params": {"after": 1}}, [2, 3]),
        ({"params": {"after_seq": 2}}, [3]),
        ({"headers": {"Last-Event-ID": "2"}}, [3]),
        ({"headers": {"Last-Event-ID": "garbage"}}, [1, 2, 3]),
        ({"params": {"after": 3}}, []),
    ],
)
def test_events_resume_after_a_seq(client, store, kwargs, expected):
    run = finished_run(store)
    headers = {**bearer(ALICE), **kwargs.get("headers", {})}
    response = client.get(f"/runs/{run}/events", headers=headers, params=kwargs.get("params"))
    assert [f["id"] for f in frames(response.text)] == expected


def test_the_query_beats_the_header(client, store):
    run = finished_run(store)
    response = client.get(f"/runs/{run}/events?after=2", headers={**bearer(ALICE), "Last-Event-ID": "0"})
    assert [f["id"] for f in frames(response.text)] == [3]


def test_someone_elses_run_is_a_404_before_any_stream_starts(client, store):
    run = finished_run(store, owner=BOB)
    response = client.get(f"/runs/{run}/events", headers=bearer(ALICE))
    assert response.status_code == 404 and response.json()["error"]["code"] == "not_found"
    assert client.get("/runs/nope/events", headers=bearer(ALICE)).status_code == 404


def test_a_negative_resume_point_is_a_validation_error(client, store):
    run = finished_run(store)
    assert client.get(f"/runs/{run}/events?after=-1", headers=bearer(ALICE)).status_code == 422


# ---------------------------------------------------------------- the generator (a live run never ends)


async def collect(gen, n):
    out = []
    try:
        async for chunk in gen:
            out.append(chunk)
            if len(out) == n:
                break
    finally:
        await gen.aclose()
    return out


async def test_a_waiting_run_gets_keep_alive_comments_and_then_new_events(store, monkeypatch):
    monkeypatch.setattr(events_module, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(events_module, "KEEPALIVE_SECONDS", 0.05)
    run = store.add_run(ALICE, "running")
    store.add_event(run, "planner", "started", "Reading your spec.")
    gen = events_module.event_stream(store, ALICE, run, 0)
    got = await collect(gen, 3)  # retry hint, the one event, then a keep-alive
    assert got[0].startswith("retry:") and got[1].startswith("id: 1\n") and got[2] == ": keep-alive\n\n"

    store.add_event(run, "planner", "finished", "Done planning.")
    gen = events_module.event_stream(store, ALICE, run, 1)
    got = await collect(gen, 2)
    assert got[1].startswith("id: 2\n") and "Done planning." in got[1]


async def test_the_stream_sends_every_event_before_it_ends_on_a_finished_run(store, monkeypatch):
    monkeypatch.setattr(events_module, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(events_module, "BATCH", 2)
    run = finished_run(store, n=5)
    chunks = [c async for c in events_module.event_stream(store, ALICE, run, 0)]
    assert [c.split("\n")[0] for c in chunks[1:]] == [f"id: {i}" for i in range(1, 6)]


async def test_the_stream_closes_quietly_on_a_database_error(store):
    run = finished_run(store)
    store.broken = True
    chunks = [c async for c in events_module.event_stream(store, ALICE, run, 0)]
    assert len(chunks) == 1 and chunks[0].startswith("retry:")  # the browser reconnects with Last-Event-ID


async def test_the_stream_stops_after_the_max_duration(store, monkeypatch):
    monkeypatch.setattr(events_module, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(events_module, "MAX_STREAM_SECONDS", 0.05)
    run = store.add_run(ALICE, "running")
    chunks = [c async for c in events_module.event_stream(store, ALICE, run, 0)]
    assert chunks[0].startswith("retry:")
