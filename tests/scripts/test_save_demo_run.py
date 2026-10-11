"""scripts/save_demo_run.py: dry-run, a real insert into a throwaway Postgres, and the unknown-user refusal."""

from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[2]
HERO = ROOT / "docs" / "evidence" / "hero-2-run.json"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


save_demo_run = _load("save_demo_run", ROOT / "scripts" / "save_demo_run.py")
pg_container = _load("pg_container", ROOT / "tests" / "worker" / "pg_container.py")


@pytest.fixture(scope="module")
def pg_server() -> Iterator[object]:
    if not pg_container.docker_available():
        pytest.skip("Docker is not available; this test needs a throwaway Postgres")
    server = pg_container.ThrowawayPostgres()
    try:
        server.start()
    except Exception as exc:
        server.stop()
        pytest.skip(f"Could not start the throwaway Postgres ({type(exc).__name__}: {exc})")
    yield server
    server.stop()


@pytest.fixture
def db_url(pg_server, monkeypatch) -> Iterator[str]:
    url = pg_server.new_database()
    monkeypatch.setenv("DATABASE_URL", url)  # only ever the throwaway, whatever the shell has
    try:
        yield url
    finally:
        pg_server.drop_database(url)


def _make_user(url: str) -> str:
    uid = str(uuid.uuid4())
    with psycopg.connect(url) as conn:
        conn.execute("insert into auth.users (id, email) values (%s, %s)", (uid, f"{uid}@example.com"))
        conn.execute(
            "insert into public.users (id, email) values (%s, %s) on conflict do nothing", (uid, f"{uid}@example.com")
        )
    return uid


def _count(url: str, table: str) -> int:
    with psycopg.connect(url) as conn:
        return conn.execute(f"select count(*) from public.{table}").fetchone()[0]  # type: ignore[index]


def test_dry_run_validates_without_a_database(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    code = save_demo_run.main([str(HERO), "--user-id", str(uuid.uuid4()), "--dry-run"])
    out = capsys.readouterr().out
    n = len(json.loads(HERO.read_text())["events"])
    assert code == 0
    assert "Dry run" in out and f"events      {n}" in out


def test_refuses_without_database_url(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert save_demo_run.main([str(HERO), "--user-id", str(uuid.uuid4())]) == 1
    assert "DATABASE_URL is not set" in capsys.readouterr().err


def test_dry_run_rejects_a_gap_in_seq(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    data = json.loads(HERO.read_text())
    del data["events"][3]
    bad = tmp_path / "gap.json"
    bad.write_text(json.dumps(data))
    assert save_demo_run.main([str(bad), "--user-id", str(uuid.uuid4()), "--dry-run"]) == 1
    assert "seq" in capsys.readouterr().err


async def test_insert_then_demo_router_replays_same_messages(db_url, capsys):
    from susanoo_api.routers.demo import load_events

    uid = _make_user(db_url)
    assert save_demo_run.main([str(HERO), "--user-id", uid, "--project-name", "Demo: test"]) == 0
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if ln.startswith("DEMO_RUN_IDS="))
    run_id = line.split("=", 1)[1]
    assert str(uuid.UUID(run_id)) == run_id and db_url not in out

    source = json.loads(HERO.read_text())["events"]
    loaded = await load_events(run_id)
    assert [e.message for e in loaded] == [e["message"] for e in source]
    assert [e.seq for e in loaded] == list(range(1, len(source) + 1))
    assert {e.run_id for e in loaded} == {run_id}
    assert [(e.head, e.type, e.trace_url) for e in loaded] == [(e["head"], e["type"], e["trace_url"]) for e in source]

    with psycopg.connect(db_url) as conn:
        status, cost, name, spec = conn.execute(
            "select r.status, r.cost_usd, p.name, p.spec from public.runs r "
            "join public.projects p on p.id = r.project_id where r.id = %s",
            (run_id,),
        ).fetchone()  # type: ignore[misc]
    assert status == "done" and float(cost) == pytest.approx(1.7510, abs=1e-4)
    assert name == "Demo: test" and spec == (ROOT / "specs" / "code-review.md").read_text()


def test_unknown_user_is_refused_and_nothing_is_inserted(db_url, capsys):
    assert save_demo_run.main([str(HERO), "--user-id", str(uuid.uuid4())]) == 1
    assert "No user" in capsys.readouterr().err
    assert [_count(db_url, t) for t in ("projects", "runs", "run_events")] == [0, 0, 0]
