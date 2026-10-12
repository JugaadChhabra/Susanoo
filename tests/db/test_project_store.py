"""The SQL behind /projects against a LOCAL Supabase (`supabase start`). [PRD › Data model]

Every query is scoped to the caller; this proves another user's projects never come back.
"""

import os
import uuid
from urllib.parse import urlparse

import psycopg
import pytest

from susanoo_api.routers.projects import PostgresProjectStore

DB_URL = os.environ.get("SUSANOO_TEST_DB_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")

assert urlparse(DB_URL).hostname in {"127.0.0.1", "localhost"}, "tests must only run against local Supabase"


@pytest.fixture
def users(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    try:
        c = psycopg.connect(DB_URL, connect_timeout=2, autocommit=True)
    except psycopg.OperationalError:
        pytest.skip("local Supabase not running (supabase start)")
    a, b = uuid.uuid4(), uuid.uuid4()
    for uid in (a, b):
        c.execute("insert into auth.users (id, email) values (%s, %s)", (uid, f"{uid}@example.com"))
    try:
        yield str(a), str(b), c
    finally:
        c.execute("delete from auth.users where id = any(%s)", ([a, b],))  # cascades to projects and runs
        c.close()


async def test_create_list_and_runs_with_latest_heldout_f1(users):
    a, _, c = users
    store = PostgresProjectStore()
    created = await store.create(a, "Reviewer", "spec", ["Never log emails."], [])
    assert created.rules == ["Never log emails."] and created.runs == [] and created.connection_ids == []

    old = c.execute(
        "insert into public.runs (project_id, status, cost_usd, created_at) "
        "values (%s, 'done', 0.5, now() - interval '1 day') returning id",
        (created.id,),
    ).fetchone()[0]
    new = c.execute(
        "insert into public.runs (project_id, cost_usd) values (%s, 1.25) returning id", (created.id,)
    ).fetchone()[0]
    c.execute("insert into public.iterations (run_id, n, heldout_f1) values (%s, 0, 0.4), (%s, 1, 0.8)", (old, old))

    [project] = await store.list_for_user(a)
    assert project.id == created.id
    assert [r.id for r in project.runs] == [str(new), str(old)]  # newest first
    assert project.runs[0].heldout_f1 is None and project.runs[0].status == "running"
    assert project.runs[1].heldout_f1 == pytest.approx(0.8)  # the latest iteration, not the first
    assert project.runs[1].cost_usd == pytest.approx(0.5)


async def test_another_users_projects_never_appear(users):
    a, b, _ = users
    store = PostgresProjectStore()
    mine = await store.create(a, "Mine", "spec a", [], [])
    await store.create(b, "Theirs", "spec b", [], [])
    assert [p.id for p in await store.list_for_user(a)] == [mine.id]


async def test_rules_are_only_editable_by_the_owner(users):
    a, b, _ = users
    store = PostgresProjectStore()
    project = await store.create(a, "Mine", "spec", ["Old."], [])
    assert await store.set_rules(b, project.id, ["Hijacked."], None) is None
    updated = await store.set_rules(a, project.id, ["New."], None)
    assert updated is not None and updated.rules == ["New."]
    [listed] = await store.list_for_user(a)
    assert listed.rules == ["New."]


def connection(c: psycopg.Connection, user_id: str, connector: str) -> str:
    return c.execute(
        "insert into public.connections (user_id, connector, vault_secret_id) "
        "values (%s, %s, gen_random_uuid()::text) returning id::text",
        (user_id, connector),
    ).fetchone()[0]


async def test_projects_keep_their_owners_connections(users):
    a, b, c = users
    store = PostgresProjectStore()
    github, slack, theirs = connection(c, a, "github"), connection(c, a, "slack"), connection(c, b, "github")

    unknown = str(uuid.uuid4())
    assert await store.foreign_connections(a, [github, theirs, unknown]) == [theirs, unknown]
    assert await store.foreign_connections(a, []) == []

    created = await store.create(a, "Reviewer", "spec", [], [slack, github])
    assert created.connection_ids == [github, slack]  # ordered by connector name

    updated = await store.set_rules(a, created.id, ["A rule."], None)
    assert updated is not None and updated.connection_ids == [github, slack]  # None leaves them alone
    updated = await store.set_rules(a, created.id, ["A rule."], [github])
    assert updated is not None and updated.connection_ids == [github]

    updated = await store.set_rules(a, created.id, [], [github, theirs])  # e.g. theirs was revoked mid-save
    assert updated is not None and updated.connection_ids == [github]  # only the owner's ids are ever written

    c.execute("delete from public.connections where id = %s", (github,))  # revoked
    [listed] = await store.list_for_user(a)
    assert listed.connection_ids == []
