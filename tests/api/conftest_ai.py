"""Shared helpers for the AI-serving router tests (runs, events, intake, approvals, feedback, agent).

Import what you need from here (this file is not a conftest, so Jugaad's tests are untouched):

    from conftest_ai import ALICE, BOB, bearer, client, store  # noqa: F401

`FakeRunStore` answers like the real `PostgresRunStore`: every call is scoped to the user it is given, so another
user's run is simply not found. The quota store is Jugaad's own dependency, replaced the same way his tests do.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import replace
from datetime import datetime
from typing import Any

import jwt
import pytest
from fastapi.testclient import TestClient

from susanoo.schemas import RunEvent, Scenario
from susanoo_api import main, quota
from susanoo_api.routers import agent, approvals, events, feedback, intake, runs
from susanoo_api.routers.runs import EventBatch, Pending, QuotaRefused, RunRow

SECRET = "test-jwt-secret-that-is-long-enough-for-hs256"
ALICE = "11111111-1111-4111-8111-111111111111"
BOB = "22222222-2222-4222-8222-222222222222"
TOKEN = "chk-1"


def bearer(user_id: str) -> dict[str, str]:
    claims = {"sub": user_id, "aud": "authenticated", "role": "authenticated", "exp": int(time.time()) + 3600}
    return {"Authorization": f"Bearer {jwt.encode(claims, SECRET, algorithm='HS256')}"}


class FakeQuotaStore:
    def __init__(self) -> None:
        self.runs_today: dict[str, int] = {}
        self.spend_usd = 0.0

    async def usage_since(self, user_id: str, since: datetime) -> quota.Usage:
        return quota.Usage(self.runs_today.get(user_id, 0), self.spend_usd)


class FakeRunStore:
    """In-memory RunStore. Rows belong to an owner; only that owner can see them."""

    def __init__(self) -> None:
        self.projects: dict[str, str] = {}  # project id -> owner
        self.rows: dict[str, tuple[str, RunRow]] = {}  # run id -> (owner, row)
        self.event_log: dict[str, list[RunEvent]] = {}
        self.pauses: dict[str, tuple[str, dict[str, Any], str]] = {}  # run id -> (kind, payload, token)
        self.states: dict[str, dict[str, Any]] = {}
        self.answers: list[tuple[str, dict[str, Any], str]] = []
        self.costs: list[tuple[str, float]] = []
        self.saved: list[tuple[str, Scenario]] = []
        self.caps: list[float] = []
        self.broken = False
        self.refuse_start: str | None = None  # a message: create_run loses the quota race

    # ---- test setup
    def add_project(self, owner: str) -> str:
        pid = str(uuid.uuid4())
        self.projects[pid] = owner
        return pid

    def add_run(self, owner: str, status: str = "running", **cols: Any) -> str:
        pid = self.add_project(owner)
        rid = str(uuid.uuid4())
        row = RunRow(
            id=rid,
            project_id=pid,
            status=status,
            cost_usd=cols.pop("cost_usd", 0.1),
            cost_cap_usd=cols.pop("cost_cap_usd", 2.0),
            preview_url=cols.pop("preview_url", None),
            created_at="2026-10-12T10:00:00+00:00",
            cancel_requested=cols.pop("cancel_requested", False),
            error=cols.pop("error", None),
        )
        assert not cols, cols
        self.rows[rid] = (owner, row)
        self.event_log[rid] = []
        return rid

    def add_event(self, run_id: str, head: str, type_: str, message: str, payload: dict | None = None) -> RunEvent:
        seq = len(self.event_log[run_id]) + 1
        event = RunEvent(
            run_id=run_id,
            seq=seq,
            head=head,  # type: ignore[arg-type]
            type=type_,  # type: ignore[arg-type]
            message=message,
            payload=payload or {},
            at=f"2026-10-12T10:00:{seq:02d}Z",
        )
        self.event_log[run_id].append(event)
        return event

    def pause(self, run_id: str, kind: str, payload: dict[str, Any], token: str = TOKEN) -> None:
        owner, row = self.rows[run_id]
        self.rows[run_id] = (owner, replace(row, status="awaiting_approval"))
        self.pauses[run_id] = (kind, {"kind": kind, **payload}, token)
        self.add_event(
            run_id, "planner", "question_needed" if kind == "questions" else "approval_needed", "Waiting.", payload
        )

    def _check(self) -> None:
        if self.broken:
            raise RuntimeError("database down")

    def _own(self, user_id: str, run_id: str) -> RunRow | None:
        owner, row = self.rows.get(run_id, (None, None))  # type: ignore[assignment]
        return row if owner == user_id else None

    # ---- RunStore
    async def create_run(self, user_id: str, project_id: str, cost_cap_usd: float) -> RunRow | None:
        self._check()
        if self.refuse_start is not None:
            return QuotaRefused(self.refuse_start)
        if self.projects.get(project_id) != user_id:
            return None
        rid = str(uuid.uuid4())
        row = RunRow(rid, project_id, "queued", 0.0, cost_cap_usd, None, "2026-10-12T10:00:00+00:00", False, None)
        self.rows[rid] = (user_id, row)
        self.event_log[rid] = []
        self.caps.append(cost_cap_usd)
        return row

    async def get_run(self, user_id: str, run_id: str) -> RunRow | None:
        self._check()
        return self._own(user_id, run_id)

    async def request_cancel(self, user_id: str, run_id: str) -> RunRow | None:
        self._check()
        row = self._own(user_id, run_id)
        if row is None:
            return None
        if row.status in ("queued", "running", "awaiting_approval"):
            row = replace(row, cancel_requested=True)
            self.rows[run_id] = (user_id, row)
        return row

    async def poll_events(self, user_id: str, run_id: str, after_seq: int, limit: int) -> EventBatch | None:
        self._check()
        row = self._own(user_id, run_id)
        if row is None:
            return None
        return EventBatch(row.status, [e for e in self.event_log[run_id] if e.seq > after_seq][:limit])

    async def pending(self, user_id: str, run_id: str) -> Pending | None:
        self._check()
        row = self._own(user_id, run_id)
        if row is None:
            return None
        if row.status != "awaiting_approval" or run_id not in self.pauses:
            return Pending(status=row.status, waiting=False)
        kind, payload, token = self.pauses[run_id]
        return Pending(status=row.status, waiting=True, kind=kind, payload=payload, pause_token=token)

    async def store_answer(self, user_id: str, run_id: str, value: dict[str, Any], pause_token: str) -> bool:
        self._check()
        row = self._own(user_id, run_id)
        if row is None or row.status != "awaiting_approval" or self.pauses[run_id][2] != pause_token:
            return False
        self.rows[run_id] = (user_id, replace(row, status="queued"))
        self.answers.append((run_id, value, pause_token))
        return True

    async def add_cost(self, run_id: str, usd: float) -> None:
        self.costs.append((run_id, usd))

    async def load_state(self, run_id: str) -> dict[str, Any]:
        self._check()
        return self.states.get(run_id, {})

    async def save_feedback_scenario(self, run_id: str, scenario: Scenario) -> None:
        self.saved.append((run_id, scenario))


@pytest.fixture
def store() -> FakeRunStore:
    return FakeRunStore()


@pytest.fixture
def quota_store() -> FakeQuotaStore:
    return FakeQuotaStore()


@pytest.fixture
def client(monkeypatch, store, quota_store) -> TestClient:
    monkeypatch.setenv("SUPABASE_JWT_SECRET", SECRET)
    monkeypatch.delenv("RUNS_PER_USER_PER_DAY", raising=False)
    monkeypatch.delenv("RUN_COST_CAP_USD", raising=False)
    monkeypatch.setenv("GLOBAL_DAILY_CAP_USD", "50")
    app = main.create_app()
    # Mounted here so these tests do not depend on main.py's AI_ROUTERS list (a second mount is harmless).
    for module in (runs, events, intake, approvals, feedback, agent):
        app.include_router(module.router)
    app.dependency_overrides[runs.run_store] = lambda: store
    app.dependency_overrides[quota.quota_store] = lambda: quota_store
    return TestClient(app)


# ---------------------------------------------------------------- a throwaway Postgres for the real SQL
# Same pattern as tests/worker: Docker `postgres:16-alpine` labelled susanoo.test=true on a random localhost port,
# migrations 001..005 plus docs/proposals/006_run_queue.sql. Skipped cleanly without Docker. Never touches the
# DATABASE_URL in the shell: each test gets its own database and the env var is pointed at it.

import sys  # noqa: E402
from collections.abc import Iterator  # noqa: E402
from pathlib import Path  # noqa: E402

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
import pg_container  # noqa: E402


@pytest.fixture(scope="session")
def pg_server() -> Iterator[pg_container.ThrowawayPostgres]:
    if not pg_container.docker_available():
        pytest.skip("Docker is not available; the SQL tests need a throwaway Postgres")
    server = pg_container.ThrowawayPostgres()
    try:
        server.start()
    except Exception as exc:
        server.stop()
        pytest.skip(f"Could not start the throwaway Postgres ({type(exc).__name__}: {exc})")
    yield server
    server.stop()


@pytest.fixture
def pg(pg_server, monkeypatch) -> Iterator[psycopg.Connection]:
    url = pg_server.new_database()
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("GLOBAL_DAILY_CAP_USD", "1000")  # quota.py refuses to count runs without a global cap
    monkeypatch.delenv("RUNS_PER_USER_PER_DAY", raising=False)
    conn = psycopg.connect(url, autocommit=True, row_factory=dict_row)
    assert "127.0.0.1" in conn.info.host
    try:
        yield conn
    finally:
        conn.close()
        pg_server.drop_database(url)
