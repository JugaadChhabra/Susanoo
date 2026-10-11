"""The SQL behind /connections against a LOCAL Supabase (`supabase start`). [PRD › Credential handling]

Proves the secret lives only in Vault (never in public.connections), replacing a connection deletes the old
secret, revoking deletes the secret, and one user can never read another's.
"""

import os
import uuid
from urllib.parse import urlparse

import psycopg
import pytest
from pydantic import SecretStr

from susanoo_api.routers.connectors import PostgresConnectionStore

DB_URL = os.environ.get("SUSANOO_TEST_DB_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")
TOKEN = "github_pat_" + "22HIJKLMN0" * 6  # built at runtime so it never looks like a real token in git

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
        c.execute("delete from auth.users where id = any(%s)", ([a, b],))  # cascades; the trigger drops secrets
        c.close()


def secret_exists(c: psycopg.Connection, secret_id: str) -> bool:
    return c.execute("select count(*) from vault.secrets where id = %s::uuid", (secret_id,)).fetchone()[0] == 1


async def test_save_read_test_and_revoke(users):
    a, b, c = users
    store = PostgresConnectionStore()

    saved = await store.save(a, "github", SecretStr(TOKEN), {"repo": "acme/payments-api"}, False)
    assert saved.status == "untested" and saved.settings == {"repo": "acme/payments-api"}
    assert TOKEN not in saved.model_dump_json()
    row = c.execute("select to_jsonb(c)::text, vault_secret_id from public.connections c where id = %s", (saved.id,))
    as_json, secret_id = row.fetchone()
    assert TOKEN not in as_json  # the table only points at Vault
    assert secret_exists(c, secret_id)

    assert await store.list_for_user(a) == [saved]
    assert await store.list_for_user(b) == []
    assert await store.credential(b, saved.id) is None

    found = await store.credential(a, saved.id)
    assert found is not None
    summary, credential = found
    assert summary == saved and credential.secret.get_secret_value() == TOKEN
    assert credential.settings == {"repo": "acme/payments-api"} and credential.writes_enabled is False

    assert await store.set_status(b, saved.id, "connected") is None
    tested = await store.set_status(a, saved.id, "connected")
    assert tested is not None and tested.status == "connected"

    assert await store.delete(b, saved.id) is False
    assert await store.delete(a, saved.id) is True
    assert not secret_exists(c, secret_id)  # revoking removes the secret too
    assert await store.list_for_user(a) == []


async def test_saving_again_replaces_the_secret_and_resets_status(users):
    a, _, c = users
    store = PostgresConnectionStore()
    first = await store.save(a, "github", SecretStr(TOKEN), {"repo": "acme/one"}, False)
    await store.set_status(a, first.id, "connected")
    old_secret = c.execute("select vault_secret_id from public.connections where id = %s", (first.id,)).fetchone()[0]

    second = await store.save(a, "github", SecretStr(TOKEN + "x"), {"repo": "acme/two"}, True)
    assert second.id == first.id  # one connection per connector per user
    assert second.status == "untested" and second.writes_enabled and second.settings == {"repo": "acme/two"}
    assert not secret_exists(c, old_secret)
    found = await store.credential(a, second.id)
    assert found is not None and found[1].secret.get_secret_value() == TOKEN + "x"
