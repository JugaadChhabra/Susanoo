"""Row-level security against a LOCAL Supabase (`supabase start`). [PRD > Data model]

Skipped when no local database is reachable. Refuses to run against anything but localhost,
so it can never touch the hosted project.
"""

import json
import os
import uuid
from urllib.parse import urlparse

import psycopg
import pytest
from psycopg import errors

DB_URL = os.environ.get("SUSANOO_TEST_DB_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")

assert urlparse(DB_URL).hostname in {"127.0.0.1", "localhost"}, "RLS tests must only run against local Supabase"


@pytest.fixture
def conn():
    try:
        c = psycopg.connect(DB_URL, connect_timeout=2)
    except psycopg.OperationalError:
        pytest.skip("local Supabase not running (supabase start)")
    try:
        yield c
    finally:
        c.rollback()
        c.close()


def as_user(conn: psycopg.Connection, user_id: uuid.UUID | None) -> None:
    conn.execute("reset role")
    if user_id is None:
        conn.execute("set local role anon")
        return
    claims = json.dumps({"sub": str(user_id), "role": "authenticated"})
    # newer auth.uid() reads the JSON claims, older stacks read the per-claim setting; set both
    conn.execute("select set_config('request.jwt.claims', %s, true)", (claims,))
    conn.execute("select set_config('request.jwt.claim.sub', %s, true)", (str(user_id),))
    conn.execute("set local role authenticated")


def as_service(conn: psycopg.Connection) -> None:
    conn.execute("reset role")


@pytest.fixture
def world(conn):
    """Two users; user A owns a project, a run, an event, a scenario and a connection."""
    a, b = uuid.uuid4(), uuid.uuid4()
    for uid in (a, b):
        # the on_auth_user_created trigger mirrors this into public.users
        conn.execute("insert into auth.users (id, email) values (%s, %s)", (uid, f"{uid}@example.com"))
    project = conn.execute(
        "insert into public.projects (user_id, name, spec) values (%s, 'p', 's') returning id", (a,)
    ).fetchone()[0]
    run = conn.execute("insert into public.runs (project_id) values (%s) returning id", (project,)).fetchone()[0]
    conn.execute("insert into public.run_events (run_id, seq, head, type) values (%s, 1, 'planner', 'started')", (run,))
    conn.execute(
        "insert into public.scenarios (run_id, kind, split, title, diff) values (%s, 'planted', 'train', 't', 'd')",
        (run,),
    )
    connection = conn.execute("select public.save_connection(%s, 'github', 'ghp_supersecret', false)", (a,)).fetchone()[
        0
    ]
    return {"a": a, "b": b, "project": project, "run": run, "connection": connection}


READABLE = ["users", "projects", "runs", "run_events", "scenarios", "connections"]


@pytest.mark.parametrize("table", READABLE)
def test_owner_can_read_own_rows(conn, world, table):
    as_user(conn, world["a"])
    assert conn.execute(f"select count(*) from public.{table}").fetchone()[0] >= 1


@pytest.mark.parametrize("table", READABLE)
def test_user_b_cannot_read_user_a_rows(conn, world, table):
    as_user(conn, world["b"])
    # user B's own public.users row is the only thing visible to them
    own = "where id <> %s" if table == "users" else "where %s::uuid is not null"
    assert conn.execute(f"select count(*) from public.{table} {own}", (world["b"],)).fetchone()[0] == 0


def test_anonymous_sees_nothing(conn, world):
    as_user(conn, None)
    with pytest.raises(errors.InsufficientPrivilege):
        conn.execute("select * from public.projects")


def test_user_b_cannot_update_or_delete_user_a_project(conn, world):
    as_user(conn, world["b"])
    upd = conn.execute("update public.projects set name = 'pwned' where id = %s", (world["project"],))
    assert upd.rowcount == 0
    dele = conn.execute("delete from public.projects where id = %s", (world["project"],))
    assert dele.rowcount == 0
    as_service(conn)
    assert conn.execute("select name from public.projects where id = %s", (world["project"],)).fetchone()[0] == "p"


def test_user_b_cannot_insert_project_for_user_a(conn, world):
    as_user(conn, world["b"])
    with pytest.raises(errors.InsufficientPrivilege):
        conn.execute("insert into public.projects (user_id, name, spec) values (%s, 'x', 'y')", (world["a"],))


def test_user_b_cannot_move_own_project_to_user_a(conn, world):
    as_user(conn, world["b"])
    mine = conn.execute(
        "insert into public.projects (user_id, name, spec) values (%s, 'mine', 's') returning id", (world["b"],)
    ).fetchone()[0]
    with pytest.raises(errors.InsufficientPrivilege):
        conn.execute("update public.projects set user_id = %s where id = %s", (world["a"], mine))


def test_user_b_cannot_write_runs(conn, world):
    as_user(conn, world["b"])
    with pytest.raises(errors.InsufficientPrivilege):
        conn.execute("insert into public.runs (project_id) values (%s)", (world["project"],))


def test_owner_cannot_write_runs_directly(conn, world):
    # runs are created by the API (quota + budget checks), never straight from the browser
    as_user(conn, world["a"])
    with pytest.raises(errors.InsufficientPrivilege):
        conn.execute("update public.runs set status = 'done' where id = %s", (world["run"],))


def test_user_b_cannot_write_connections(conn, world):
    as_user(conn, world["b"])
    with pytest.raises(errors.InsufficientPrivilege), conn.transaction():
        conn.execute(
            "insert into public.connections (user_id, connector, vault_secret_id) values (%s, 'github', 'x')",
            (world["a"],),
        )
    with pytest.raises(errors.InsufficientPrivilege), conn.transaction():
        conn.execute("update public.connections set writes_enabled = true where id = %s", (world["connection"],))
    assert conn.execute("delete from public.connections where id = %s", (world["connection"],)).rowcount == 0


@pytest.mark.parametrize(
    "fn",
    ["public.read_connection_secret(uuid)", "public.save_connection(uuid, text, text, boolean)"],
)
def test_users_cannot_call_vault_functions(conn, fn):
    # Checked via the ACL: actually calling a denied function crashes the local Docker Postgres build
    # (segfault in the permission-error path), so we assert the privilege instead.
    for role in ("anon", "authenticated"):
        assert not conn.execute("select has_function_privilege(%s, %s, 'execute')", (role, fn)).fetchone()[0]
    assert conn.execute("select has_function_privilege('service_role', %s, 'execute')", (fn,)).fetchone()[0]


def test_secret_is_in_vault_not_in_plain_columns(conn, world):
    row = conn.execute("select * from public.connections where id = %s", (world["connection"],)).fetchone()
    assert "ghp_supersecret" not in " ".join(str(v) for v in row)
    secret_id = conn.execute(
        "select vault_secret_id from public.connections where id = %s", (world["connection"],)
    ).fetchone()[0]
    stored = conn.execute("select secret from vault.secrets where id = %s", (secret_id,)).fetchone()[0]
    assert stored != "ghp_supersecret"  # encrypted at rest
    assert (
        conn.execute("select public.read_connection_secret(%s)", (world["connection"],)).fetchone()[0]
        == "ghp_supersecret"
    )


def test_deleting_connection_removes_vault_secret(conn, world):
    secret_id = conn.execute(
        "select vault_secret_id from public.connections where id = %s", (world["connection"],)
    ).fetchone()[0]
    as_user(conn, world["a"])
    assert conn.execute("delete from public.connections where id = %s", (world["connection"],)).rowcount == 1
    as_service(conn)
    assert conn.execute("select count(*) from vault.secrets where id = %s", (secret_id,)).fetchone()[0] == 0


def test_required_indexes_exist(conn):
    names = {r[0] for r in conn.execute("select indexname from pg_indexes where schemaname = 'public'")}
    assert {"run_events_run_id_seq_idx", "runs_status_created_at_idx"} <= names


def test_every_table_has_rls_enabled(conn):
    rows = conn.execute(
        "select relname from pg_class c join pg_namespace n on n.oid = c.relnamespace "
        "where n.nspname = 'public' and c.relkind = 'r' and not c.relrowsecurity"
    ).fetchall()
    assert rows == []
