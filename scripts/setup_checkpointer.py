"""Create LangGraph's checkpoint tables in the database at DATABASE_URL, once, and show what exists.

    DATABASE_URL=... uv run python scripts/setup_checkpointer.py

Use a direct or session connection (port 5432), not the transaction pooler (6543): setup() builds its
indexes CONCURRENTLY. Idempotent: running it again only re-checks. Never prints DATABASE_URL.
"""

from __future__ import annotations

import asyncio
import os
import sys

import psycopg

from susanoo.graph.checkpointer import postgres_checkpointer

TABLES_SQL = (
    "select table_name from information_schema.tables "
    "where table_schema = 'public' and table_name like 'checkpoint%' order by 1"
)
INDEXES_SQL = (
    "select indexname from pg_indexes "
    "where schemaname = 'public' and indexname like 'checkpoint%thread_id_idx' order by 1"
)


async def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("Set DATABASE_URL first (a direct or session connection, port 5432).")
        return 1
    if ":6543" in url:
        print("This is the transaction pooler (port 6543); use the session connection on port 5432 instead.")
        return 1
    try:
        async with postgres_checkpointer(url):
            pass
        async with await psycopg.AsyncConnection.connect(url, autocommit=True, prepare_threshold=0) as conn:
            tables = [r[0] for r in await (await conn.execute(TABLES_SQL)).fetchall()]
            indexes = [r[0] for r in await (await conn.execute(INDEXES_SQL)).fetchall()]
            version = (await (await conn.execute("select max(v) from checkpoint_migrations")).fetchone())[0]
    except psycopg.Error as exc:
        print(f"Could not set up the checkpoint tables: {type(exc).__name__}: {str(exc).splitlines()[0]}")
        return 1
    print("tables:", ", ".join(tables))
    print("indexes:", ", ".join(indexes))
    print("migration version:", version)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
