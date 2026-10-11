"""The SQL behind the run quota against a LOCAL Supabase (`supabase start`). [PRD › Connectors and API budget]"""

import os
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import psycopg
import pytest

from susanoo_api.quota import PostgresQuotaStore

DB_URL = os.environ.get("SUSANOO_TEST_DB_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")

assert urlparse(DB_URL).hostname in {"127.0.0.1", "localhost"}, "tests must only run against local Supabase"


@pytest.fixture
def projects(monkeypatch):
    """Two users with one project each; yields (user a, project a, user b, project b, connection)."""
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    try:
        c = psycopg.connect(DB_URL, connect_timeout=2, autocommit=True)
    except psycopg.OperationalError:
        pytest.skip("local Supabase not running (supabase start)")
    a, b = uuid.uuid4(), uuid.uuid4()
    ids = []
    for uid in (a, b):
        c.execute("insert into auth.users (id, email) values (%s, %s)", (uid, f"{uid}@example.com"))
        ids.append(
            c.execute(
                "insert into public.projects (user_id, name, spec) values (%s, 'p', 's') returning id", (uid,)
            ).fetchone()[0]
        )
    try:
        yield str(a), ids[0], str(b), ids[1], c
    finally:
        c.execute("delete from auth.users where id = any(%s)", ([a, b],))  # cascades to projects and runs
        c.close()


def midnight() -> datetime:
    return datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


async def test_counts_only_this_users_runs_since_midnight(projects):
    a, project_a, _, project_b, c = projects
    c.execute(
        "insert into public.runs (project_id, created_at) values (%s, now()), (%s, now()), (%s, %s), (%s, now())",
        (project_a, project_a, project_a, midnight() - timedelta(seconds=1), project_b),
    )
    assert await PostgresQuotaStore().runs_started_since(a, midnight()) == 2


async def test_spend_adds_up_every_users_runs_since_midnight(projects):
    _, project_a, _, project_b, c = projects
    store = PostgresQuotaStore()
    before = await store.spend_since_usd(midnight())
    c.execute(
        "insert into public.runs (project_id, cost_usd, created_at) values (%s, 1.25, now()), (%s, 0.5, now()), "
        "(%s, 9, %s)",
        (project_a, project_b, project_a, midnight() - timedelta(seconds=1)),
    )
    assert await store.spend_since_usd(midnight()) - before == pytest.approx(1.75)
