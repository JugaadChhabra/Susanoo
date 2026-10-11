"""Postgres checkpointer for the graph. [PRD › LangGraph flow and shared state › Checkpointer]

The graph's heads are async, so this is `AsyncPostgresSaver` (the async twin of PostgresSaver)
on DATABASE_URL. `from_conn_string` opens the connection with autocommit=True (needed: setup()
creates indexes CONCURRENTLY), prepare_threshold=0 (safe behind Supabase's transaction pooler) and
dict_row.

`setup()` creates, in the connection's default schema (public on Supabase):
  tables : checkpoint_migrations, checkpoints, checkpoint_blobs, checkpoint_writes
  indexes: checkpoints_thread_id_idx, checkpoint_blobs_thread_id_idx, checkpoint_writes_thread_id_idx
  column : checkpoint_writes.task_path (added by the last migration)
It is idempotent: re-running only applies migrations not yet recorded in checkpoint_migrations.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from pydantic import BaseModel

from susanoo.schemas import core as _core

_UNSET_MESSAGE = "DATABASE_URL is not set; point it at the Supabase Postgres connection string"


def checkpoint_serde() -> JsonPlusSerializer:
    """Serializer that explicitly allows the frozen schema models (Plan, Scenario, Patch, ...) in checkpoints."""
    allowed = [
        (_core.__name__, name)
        for name, obj in vars(_core).items()
        if isinstance(obj, type) and issubclass(obj, BaseModel) and obj is not BaseModel
    ]
    return JsonPlusSerializer(allowed_msgpack_modules=allowed)


def memory_checkpointer() -> InMemorySaver:
    """In-process checkpointer for tests and the offline demo (same serializer as Postgres)."""
    return InMemorySaver(serde=checkpoint_serde())


@asynccontextmanager
async def postgres_checkpointer(
    database_url: str | None = None, *, run_setup: bool = True
) -> AsyncIterator[AsyncPostgresSaver]:
    """Yield a ready AsyncPostgresSaver; the connection closes when the block exits."""
    url = database_url or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(_UNSET_MESSAGE)
    async with AsyncPostgresSaver.from_conn_string(url, serde=checkpoint_serde()) as saver:
        if run_setup:
            await saver.setup()
        yield saver
