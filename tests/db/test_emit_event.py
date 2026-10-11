"""emit_event against a LOCAL Supabase (`supabase start`). [PRD > Contracts (locked) > Event shape]"""

import asyncio
import os
import uuid
from urllib.parse import urlparse

import psycopg
import pytest

from susanoo.db import emit_event
from susanoo.schemas import RunEvent

DB_URL = os.environ.get("SUSANOO_TEST_DB_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")

assert urlparse(DB_URL).hostname in {"127.0.0.1", "localhost"}, "tests must only run against local Supabase"


@pytest.fixture
def run_id(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    try:
        c = psycopg.connect(DB_URL, connect_timeout=2, autocommit=True)
    except psycopg.OperationalError:
        pytest.skip("local Supabase not running (supabase start)")
    uid = uuid.uuid4()
    c.execute("insert into auth.users (id, email) values (%s, %s)", (uid, f"{uid}@example.com"))
    pid = c.execute("insert into public.projects (user_id, name, spec) values (%s, 'p', 's') returning id", (uid,))
    pid = pid.fetchone()[0]
    rid = c.execute("insert into public.runs (project_id) values (%s) returning id", (pid,)).fetchone()[0]
    try:
        yield str(rid)
    finally:
        c.execute("delete from auth.users where id = %s", (uid,))  # cascades to the run and its events
        c.close()


def stored(run_id: str) -> list[RunEvent]:
    with psycopg.connect(DB_URL) as c:
        rows = c.execute(
            "select run_id::text, seq, head, type, message, payload, trace_url, at::text "
            "from public.run_events where run_id = %s order by seq",
            (run_id,),
        ).fetchall()
    keys = ["run_id", "seq", "head", "type", "message", "payload", "trace_url", "at"]
    return [RunEvent(**dict(zip(keys, r, strict=True))) for r in rows]


async def test_row_matches_run_event_shape(run_id):
    ev = await emit_event(run_id, "planner", "started", "Reading your idea", {"k": 1}, trace_url="https://t/1")
    rows = stored(run_id)
    assert len(rows) == 1
    assert rows[0].model_dump(exclude={"at"}) == ev.model_dump(exclude={"at"})
    assert (ev.run_id, ev.seq, ev.head, ev.type, ev.message) == (run_id, 1, "planner", "started", "Reading your idea")
    assert ev.payload == {"k": 1} and ev.trace_url == "https://t/1" and ev.at


async def test_defaults_and_validation(run_id):
    ev = await emit_event(run_id, "system", "progress", "hi")
    assert ev.payload == {} and ev.trace_url is None
    with pytest.raises(ValueError):
        await emit_event(run_id, "nobody", "progress", "bad head")  # type: ignore[arg-type]
    assert [e.seq for e in stored(run_id)] == [1]  # the bad call left no row and no gap


async def test_seq_strictly_increasing_under_concurrency(run_id):
    n = 40
    events = await asyncio.gather(*(emit_event(run_id, "builder", "progress", f"step {i}", {"i": i}) for i in range(n)))
    assert sorted(e.seq for e in events) == list(range(1, n + 1))
    assert [e.seq for e in stored(run_id)] == list(range(1, n + 1))


async def test_seq_is_per_run(run_id):
    other = None
    with psycopg.connect(DB_URL, autocommit=True) as c:
        pid = c.execute("select project_id from public.runs where id = %s", (run_id,)).fetchone()[0]
        other = str(c.execute("insert into public.runs (project_id) values (%s) returning id", (pid,)).fetchone()[0])
    await emit_event(run_id, "planner", "started", "a")
    await emit_event(run_id, "planner", "finished", "b")
    assert (await emit_event(other, "planner", "started", "c")).seq == 1
