"""Run platform tests in a new disposable local PostgreSQL database, then remove only that database."""

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import psycopg
from dotenv import load_dotenv
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

ROOT = Path(__file__).resolve().parents[1]


def main():
    load_dotenv(ROOT / ".env.platform")
    url = os.environ.get("PLATFORM_TEST_ADMIN_URL") or os.environ.get("PLATFORM_DATABASE_URL", "")
    config = conninfo_to_dict(url)
    if config.get("host") not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("Tests require an explicitly local disposable PostgreSQL administrator connection.")
    database = "susanoo_test_" + uuid4().hex
    with psycopg.connect(url, autocommit=True, connect_timeout=5) as db:
        db.execute(sql.SQL("create database {}").format(sql.Identifier(database)))
    test_url = make_conninfo(**{**config, "dbname": database})
    env = {
        **os.environ,
        "PLATFORM_DATABASE_URL": test_url,
        "PLATFORM_TEST_DATABASE_URL": test_url,
        "PLATFORM_ENV": "development",
        "PLATFORM_SECRET_KEY": uuid4().hex + uuid4().hex,
        "PLATFORM_ORIGIN": "http://localhost:3000",
    }
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/platform", "tests/contract/platform", *sys.argv[1:]],
            cwd=ROOT,
            env=env,
            check=False,
        )
        return result.returncode
    finally:
        # The UUID-named database was created above; no user-selected database is ever dropped.
        if not database.startswith("susanoo_test_"):
            raise RuntimeError("Refusing unexpected cleanup target")
        with psycopg.connect(url, autocommit=True, connect_timeout=5) as db:
            db.execute(sql.SQL("drop database {} with (force)").format(sql.Identifier(database)))


if __name__ == "__main__":
    sys.exit(main())
