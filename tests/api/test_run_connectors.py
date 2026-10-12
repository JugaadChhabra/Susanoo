"""A run's state lists the connector names of its project's connections. [PRD › Connectors and API budget]

Through the real app and the real worker on a throwaway Postgres (needs Docker): start a run over HTTP, let the
worker take it to its first pause with stub heads, then read the run's checkpointed state. Agent Smith and the
Runner read `state["connectors"]`; only names ever get there, never ids or secrets.
"""

# ruff: noqa: F811  (fixtures imported from conftest_ai are used as test arguments)

import asyncio
import uuid

from conftest_ai import SECRET, bearer, pg, pg_server  # noqa: F401
from fastapi.testclient import TestClient
from susanoo_worker.config import WorkerConfig
from susanoo_worker.main import Worker

from susanoo.graph import Heads
from susanoo_api import main
from susanoo_api.routers.runs import PostgresRunStore

FAST = WorkerConfig(poll_seconds=0.1, heartbeat_seconds=0.2, stale_seconds=5.0, shutdown_grace_seconds=0.5)


def _user(pg) -> str:
    uid = uuid.uuid4()
    pg.execute("insert into auth.users (id, email) values (%s, %s)", (uid, f"{uid}@example.com"))
    return str(uid)


def _connection(pg, user: str, connector: str = "github") -> str:
    return str(
        pg.execute(
            "insert into public.connections (user_id, connector, vault_secret_id) values (%s, %s, %s) returning id",
            (user, connector, str(uuid.uuid4())),
        ).fetchone()["id"]
    )


def _project(pg, user: str, connection_ids: list[str]) -> str:
    return str(
        pg.execute(
            "insert into public.projects (user_id, name, spec, connection_ids) values (%s, 'p', 's', %s::uuid[]) "
            "returning id",
            (user, connection_ids),
        ).fetchone()["id"]
    )


async def _state_at_first_pause(pg, run_id: str) -> dict:
    worker = Worker(FAST, Heads, worker_id="w-connectors")
    task = asyncio.create_task(worker.run())
    try:
        for _ in range(300):
            row = pg.execute("select status from public.runs where id = %s", (run_id,)).fetchone()
            if row["status"] == "awaiting_approval":
                break
            assert row["status"] in ("queued", "running"), row["status"]
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("the run never reached its first pause")
    finally:
        worker.request_stop()
        await asyncio.wait_for(task, 30)
    return await PostgresRunStore().load_state(run_id)


def _start(pg, monkeypatch, user: str, project: str) -> str:
    monkeypatch.setenv("SUPABASE_JWT_SECRET", SECRET)
    started = TestClient(main.create_app()).post(f"/projects/{project}/runs", headers=bearer(user))
    assert started.status_code == 201, started.text
    return started.json()["id"]


async def test_a_run_on_a_project_with_github_has_github_in_its_state(pg, monkeypatch):
    alice = _user(pg)
    connection = _connection(pg, alice)
    run = _start(pg, monkeypatch, alice, _project(pg, alice, [connection]))
    state = await _state_at_first_pause(pg, run)
    assert state["connectors"] == ["github"]
    assert connection not in str(state)  # names only, never connection ids


async def test_a_run_on_a_project_without_connections_has_none(pg, monkeypatch):
    alice = _user(pg)
    run = _start(pg, monkeypatch, alice, _project(pg, alice, []))
    assert (await _state_at_first_pause(pg, run))["connectors"] == []


async def test_revoked_and_other_users_connections_are_left_out(pg, monkeypatch):
    alice, bob = _user(pg), _user(pg)
    revoked = _connection(pg, alice)
    project = _project(pg, alice, [revoked, _connection(pg, bob)])  # bob's id can only get here by a direct write
    pg.execute("delete from public.connections where id = %s", (revoked,))  # revoking deletes the connection
    run = _start(pg, monkeypatch, alice, project)
    assert (await _state_at_first_pause(pg, run))["connectors"] == []
