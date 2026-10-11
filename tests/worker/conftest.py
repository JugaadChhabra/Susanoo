"""Fixtures for worker tests: a throwaway Postgres (skipped cleanly without Docker) and queue helpers."""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).parent))
import pg_container  # noqa: E402


@pytest.fixture(scope="session")
def pg_server() -> Iterator[pg_container.ThrowawayPostgres]:
    if not pg_container.docker_available():
        pytest.skip("Docker is not available; worker tests need a throwaway Postgres")
    server = pg_container.ThrowawayPostgres()
    try:
        server.start()
    except Exception as exc:
        server.stop()
        pytest.skip(f"Could not start the throwaway Postgres ({type(exc).__name__}: {exc})")
    yield server
    server.stop()


@dataclass
class Db:
    url: str
    conn: psycopg.Connection

    def one(self, sql: str, *args: Any) -> dict:
        return self.conn.execute(sql, args).fetchone()  # type: ignore[return-value]

    def all(self, sql: str, *args: Any) -> list[dict]:
        return self.conn.execute(sql, args).fetchall()

    def run(self, run_id: str) -> dict:
        return self.one("select * from public.runs where id = %s", run_id)

    def events(self, run_id: str) -> list[dict]:
        return self.all(
            "select seq, head, type, message, payload from public.run_events where run_id = %s order by seq", run_id
        )

    def event_types(self, run_id: str) -> list[tuple[str, str]]:
        return [(e["head"], e["type"]) for e in self.events(run_id)]

    def make_run(self, status: str = "queued", spec: str = "Review pull requests for PII in logs.", **cols: Any) -> str:
        uid = uuid.uuid4()
        self.conn.execute("insert into auth.users (id, email) values (%s, %s)", (uid, f"{uid}@example.com"))
        pid = self.one(
            "insert into public.projects (user_id, name, spec) values (%s, 'p', %s) returning id", uid, spec
        )["id"]
        cols = {"status": status, **cols}
        names = ", ".join(cols)
        marks = ", ".join(["%s"] * len(cols))
        vals = [Jsonb(v) if isinstance(v, (dict, list)) else v for v in cols.values()]
        row = self.conn.execute(
            f"insert into public.runs (project_id, {names}) values (%s, {marks}) returning id", [pid, *vals]
        ).fetchone()
        return str(row["id"])  # type: ignore[index]

    def pending_kind(self, run_id: str) -> str:
        row = self.one(
            "select payload->>'kind' as kind from public.run_events where run_id = %s "
            "and type in ('question_needed','approval_needed') order by seq desc limit 1",
            run_id,
        )
        return row["kind"]


@pytest.fixture
def db(pg_server, monkeypatch) -> Iterator[Db]:
    url = pg_server.new_database()
    # Whatever DATABASE_URL the shell has (possibly the real Supabase), the code under test only sees the throwaway.
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("RUN_COST_CAP_USD", "100")
    conn = psycopg.connect(url, autocommit=True, row_factory=dict_row)
    assert "127.0.0.1" in conn.info.host
    try:
        yield Db(url, conn)
    finally:
        conn.close()
        pg_server.drop_database(url)


async def wait_for(predicate: Callable[[], Any], timeout: float = 30.0, interval: float = 0.1) -> Any:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out waiting for the condition")
        await asyncio.sleep(interval)
