"""Database session helper and run-event writer for the api and the worker. [PRD › Contracts (locked)]

Lives in packages/core so heads can `from susanoo.db import emit_event` without importing apps/api.
Connects as the service role through DATABASE_URL (RLS is bypassed; only backend code runs this).
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from susanoo.schemas import RunEvent

_UNSET_MESSAGE = "DATABASE_URL is not set; point it at the Supabase Postgres connection string"


@asynccontextmanager
async def session() -> AsyncIterator[psycopg.AsyncConnection[dict[str, Any]]]:
    """One transaction on a fresh connection: commits on success, rolls back on any exception."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(_UNSET_MESSAGE)
    async with await psycopg.AsyncConnection.connect(url, row_factory=dict_row) as conn:
        yield conn  # the connection context manager commits on clean exit, rolls back on error


async def emit_event(
    run_id: str,
    head: str,
    type: str,
    message: str,
    payload: dict | None = None,
    trace_url: str | None = None,
) -> RunEvent:
    """Persist one RunEvent. `seq` is 1, 2, 3... per run, even when calls race.

    A transaction-scoped advisory lock keyed on the run serialises seq allocation for that run only;
    other runs are unaffected. The unique (run_id, seq) index is the backstop.
    """
    payload = payload or {}
    async with session() as conn:
        await conn.execute("select pg_advisory_xact_lock(hashtextextended(%s, 0))", (run_id,))
        cur = await conn.execute(
            """
            insert into public.run_events (run_id, seq, head, type, message, payload, trace_url)
            select %(run)s, coalesce(max(seq), 0) + 1, %(head)s, %(type)s, %(msg)s, %(payload)s, %(trace)s
            from public.run_events where run_id = %(run)s
            returning run_id::text, seq, head, type, message, payload, trace_url, at::text
            """,
            {"run": run_id, "head": head, "type": type, "msg": message, "payload": Jsonb(payload), "trace": trace_url},
        )
        row = await cur.fetchone()
        assert row is not None
        # validate against the frozen contract before commit; a bad head/type rolls the insert back
        return RunEvent(**row)
