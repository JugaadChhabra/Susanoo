"""A throwaway Postgres in Docker for worker tests and the crash demo. Never touches DATABASE_URL.

Starts `postgres:16-alpine` labelled susanoo.test=true on a random localhost port, builds a template database
with a minimal Supabase stand-in (auth, vault, roles), migrations 001..005 and the run-queue proposal, and
hands out one fresh database per test. Everything is removed on exit.
"""

from __future__ import annotations

import atexit
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "postgres:16-alpine"
PASSWORD = "susanoo-test"

SUPABASE_STANDIN = """
create role anon nologin;
create role authenticated nologin;
create role service_role nologin;
create schema auth;
create table auth.users (id uuid primary key, email text);
create function auth.uid() returns uuid language sql stable as $$ select null::uuid $$;
create schema vault;
create table vault.secrets (id uuid primary key default gen_random_uuid(), name text, secret text);
create view vault.decrypted_secrets as select id, secret as decrypted_secret from vault.secrets;
create function vault.create_secret(s text, n text) returns uuid language sql as $$
    insert into vault.secrets (name, secret) values (n, s) returning id $$;
"""


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def migration_files() -> list[Path]:
    return sorted((ROOT / "supabase" / "migrations").glob("00[1-5]_*.sql")) + [
        ROOT / "docs" / "proposals" / "006_run_queue.sql"
    ]


class ThrowawayPostgres:
    def __init__(self) -> None:
        self.name = f"susanoo-test-pg-{uuid.uuid4().hex[:8]}"
        self.port = 0
        self._started = False

    def url(self, database: str = "postgres") -> str:
        return f"postgresql://postgres:{PASSWORD}@127.0.0.1:{self.port}/{database}"

    def start(self) -> None:
        subprocess.run(
            [
                "docker", "run", "-d", "--name", self.name, "--label", "susanoo.test=true",
                "-p", "127.0.0.1::5432", "-e", f"POSTGRES_PASSWORD={PASSWORD}",
                IMAGE, "-c", "fsync=off", "-c", "max_connections=200",
            ],
                check=True, capture_output=True, text=True, timeout=120,
        )  # fmt: skip
        self._started = True
        atexit.register(self.stop)
        out = subprocess.run(
            ["docker", "port", self.name, "5432/tcp"], check=True, capture_output=True, text=True
        ).stdout.strip()
        self.port = int(out.splitlines()[0].rsplit(":", 1)[1])
        deadline = time.monotonic() + 60
        while True:
            try:
                with psycopg.connect(self.url(), connect_timeout=2):
                    break
            except psycopg.OperationalError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.5)
        self._build_template()

    def _build_template(self) -> None:
        with psycopg.connect(self.url(), autocommit=True) as conn:
            conn.execute("create database tmpl")
        with psycopg.connect(self.url("tmpl"), autocommit=True) as conn:
            conn.execute(SUPABASE_STANDIN)
            for path in migration_files():
                conn.execute(path.read_text())

    def new_database(self) -> str:
        """A fresh database cloned from the migrated template. Returns its URL."""
        name = f"t_{uuid.uuid4().hex[:10]}"
        with psycopg.connect(self.url(), autocommit=True) as conn:
            conn.execute(f'create database "{name}" template tmpl')
        return self.url(name)

    def drop_database(self, url: str) -> None:
        name = url.rsplit("/", 1)[1]
        with psycopg.connect(self.url(), autocommit=True) as conn:
            conn.execute(f'drop database if exists "{name}" with (force)')

    def stop(self) -> None:
        if self._started:
            subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)
            self._started = False
