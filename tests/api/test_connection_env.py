"""A run's connections become its container's env, read from Vault.

[PRD › Connectors and API budget › Credential handling]

Against a throwaway Postgres with the Vault stand-in (needs Docker): connections are saved through the real
`save_connection()`, then `connection_env()` reads them back through the service-role Vault read.
"""

# ruff: noqa: F811  (fixtures imported from conftest_ai are used as test arguments)

import logging
import uuid

import pytest
from conftest_ai import pg, pg_server  # noqa: F401
from psycopg.types.json import Jsonb

from susanoo.connectors import known_secret_values, redact
from susanoo.integrations import tracing
from susanoo.connectors.runtime_env import ConnectionNotYours, connection_env, run_connection_env

TOKEN = "github_pat_" + "33ENVTOKEN" * 6  # built at runtime so it never looks like a real token in git
KEY = "sk-ant-" + "api03-" + "k" * 30


def _user(pg) -> str:
    uid = uuid.uuid4()
    pg.execute("insert into auth.users (id, email) values (%s, %s)", (uid, f"{uid}@example.com"))
    return str(uid)


def _connection(pg, user: str, connector: str, secret: str, settings: dict, writes: bool = False) -> str:
    cid = pg.execute(
        "select public.save_connection(%s, %s, %s, %s)::text as id", (user, connector, secret, writes)
    ).fetchone()["id"]
    pg.execute("update public.connections set settings = %s where id = %s", (Jsonb(settings), cid))
    return cid


async def test_values_come_from_vault_through_each_connectors_env(pg):
    alice = _user(pg)
    github = _connection(pg, alice, "github", TOKEN, {"repo": "acme/payments-api"})
    anthropic = _connection(pg, alice, "anthropic", KEY, {})

    env = await connection_env(alice, [github, anthropic])

    assert env == {
        "GITHUB_TOKEN": TOKEN,
        "GITHUB_REPO": "acme/payments-api",
        "GITHUB_WRITES_ENABLED": "false",
        "ANTHROPIC_API_KEY": KEY,
        "SUSANOO_ANTHROPIC_KEY_SOURCE": "byok",
    }
    assert {TOKEN, KEY} <= set(known_secret_values())  # registered for redaction before anyone sees them


async def test_a_secret_of_any_shape_is_hidden_from_logs_and_traces_once_read(pg):
    odd = "not-a-known-token-shape-" + uuid.uuid4().hex  # no pattern would catch this
    alice = _user(pg)
    github = _connection(pg, alice, "github", odd, {"repo": "acme/payments-api"})
    line = f"the app said {odd}"
    assert odd in redact(line) and odd in tracing.scrub_text(line)

    await connection_env(alice, [github])

    assert odd not in redact(line) and odd not in tracing.scrub_text(line)


async def test_the_write_setting_becomes_the_connectors_write_flag(pg):
    alice = _user(pg)
    github = _connection(pg, alice, "github", TOKEN, {"repo": "acme/payments-api"}, writes=True)
    assert (await connection_env(alice, [github]))["GITHUB_WRITES_ENABLED"] == "true"


async def test_revoked_connections_are_skipped(pg, caplog):
    alice = _user(pg)
    github = _connection(pg, alice, "github", TOKEN, {"repo": "acme/payments-api"})
    anthropic = _connection(pg, alice, "anthropic", KEY, {})
    pg.execute("delete from public.connections where id = %s", (github,))  # revoking deletes the row and secret

    with caplog.at_level(logging.DEBUG):
        env = await connection_env(alice, [github, anthropic])

    assert set(env) == {"ANTHROPIC_API_KEY", "SUSANOO_ANTHROPIC_KEY_SOURCE"}
    assert "revoked" in caplog.text
    assert KEY not in caplog.text


async def test_another_users_connection_is_refused_and_never_read(pg, caplog):
    alice, mallory = _user(pg), _user(pg)
    alices = _connection(pg, alice, "github", TOKEN, {"repo": "acme/payments-api"})
    mallorys = _connection(pg, mallory, "anthropic", KEY, {})

    with caplog.at_level(logging.DEBUG), pytest.raises(ConnectionNotYours) as refused:
        await connection_env(mallory, [mallorys, alices])

    assert TOKEN not in str(refused.value) and TOKEN not in caplog.text


async def test_no_connections_reads_nothing(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)  # would raise if it tried to connect
    assert await connection_env(str(uuid.uuid4()), []) == {}


async def test_a_run_resolves_its_projects_owner_and_connections(pg):
    alice = _user(pg)
    github = _connection(pg, alice, "github", TOKEN, {"repo": "acme/payments-api"})
    project = pg.execute(
        "insert into public.projects (user_id, name, spec, connection_ids) values (%s, 'p', 's', %s::uuid[]) "
        "returning id::text as id",
        (alice, [github]),
    ).fetchone()["id"]
    run = pg.execute(
        "insert into public.runs (project_id, status) values (%s, 'queued') returning id::text as id", (project,)
    ).fetchone()["id"]

    assert (await run_connection_env(run))["GITHUB_TOKEN"] == TOKEN
    assert await run_connection_env(str(uuid.uuid4())) == {}
