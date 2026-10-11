"""Daily run quota and global cap. [PRD › Connectors and API budget › Anthropic budget]

Muaaz's POST /projects/{id}/runs isn't built yet, so these tests mount a stand-in start-run route that
depends on `enforce_run_quota` exactly as his will, and records a run each time it gets through.
"""

import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from susanoo.schemas import RunEvent
from susanoo_api import main, quota
from susanoo_api.deps import CurrentUserDep
from susanoo_api.models import RunQuota
from susanoo_api.routers import demo

SECRET = "test-jwt-secret-that-is-long-enough-for-hs256"
ALICE = "11111111-1111-4111-8111-111111111111"
BOB = "22222222-2222-4222-8222-222222222222"
PROJECT = "33333333-3333-4333-8333-333333333333"

FIXTURE = Path(__file__).parent.parent / "fixtures" / "run_events.json"
EVENTS = [RunEvent(**e) for e in json.loads(FIXTURE.read_text())]


def bearer(user_id: str) -> dict[str, str]:
    claims = {"sub": user_id, "aud": "authenticated", "role": "authenticated", "exp": int(time.time()) + 3600}
    return {"Authorization": f"Bearer {jwt.encode(claims, SECRET, algorithm='HS256')}"}


class FakeQuotaStore:
    """Runs started today per user, and today's platform spend across everyone."""

    def __init__(self) -> None:
        self.runs: dict[str, int] = {}
        self.spend_usd = 0.0
        self.since: datetime | None = None
        self.broken = False

    async def runs_started_since(self, user_id: str, since: datetime) -> int:
        if self.broken:
            raise RuntimeError("database down")
        self.since = since
        return self.runs.get(user_id, 0)

    async def spend_since_usd(self, since: datetime) -> float:
        if self.broken:
            raise RuntimeError("database down")
        return self.spend_usd


@pytest.fixture
def store() -> FakeQuotaStore:
    return FakeQuotaStore()


@pytest.fixture
def client(monkeypatch, store) -> TestClient:
    monkeypatch.setenv("SUPABASE_JWT_SECRET", SECRET)
    monkeypatch.delenv("RUNS_PER_USER_PER_DAY", raising=False)
    monkeypatch.setenv("GLOBAL_DAILY_CAP_USD", "50")

    async def fake_load(run_id: str) -> list[RunEvent]:
        return EVENTS if run_id == EVENTS[0].run_id else []

    monkeypatch.setattr(demo, "load_events", fake_load)
    monkeypatch.setenv("DEMO_RUN_IDS", EVENTS[0].run_id)

    app = main.create_app()
    start_run = APIRouter()

    @start_run.post("/projects/{project_id}/runs", status_code=201)
    async def start(project_id: str, user: CurrentUserDep, _: quota.RunQuotaDep) -> dict[str, str]:
        store.runs[user.id] = store.runs.get(user.id, 0) + 1
        return {"id": str(uuid.uuid4())}

    app.include_router(start_run)
    app.dependency_overrides[quota.quota_store] = lambda: store
    return TestClient(app)


def start(client: TestClient, user: str = ALICE):
    return client.post(f"/projects/{PROJECT}/runs", headers=bearer(user))


def test_starting_a_run_needs_auth(client):
    assert client.post(f"/projects/{PROJECT}/runs").status_code == 401


def test_fourth_run_of_the_day_is_refused_with_a_plain_english_429(client, store):
    for _ in range(3):
        assert start(client).status_code == 201
    response = start(client)
    assert response.status_code == 429
    error = response.json()["error"]
    assert error["code"] == "rate_limited"
    assert "3 runs" in error["message"] and "{" not in error["message"]
    assert store.runs[ALICE] == 3


def test_one_users_quota_does_not_touch_another_users(client, store):
    store.runs[ALICE] = 3
    assert start(client, ALICE).status_code == 429
    assert start(client, BOB).status_code == 201


def test_the_day_starts_at_midnight_utc(client, store):
    start(client)
    assert store.since is not None
    assert store.since.utcoffset() == timedelta(0)
    assert (store.since.hour, store.since.minute, store.since.second) == (0, 0, 0)


def test_runs_per_day_comes_from_the_environment(client, monkeypatch):
    monkeypatch.setenv("RUNS_PER_USER_PER_DAY", "1")
    assert start(client).status_code == 201
    assert start(client).status_code == 429


def test_global_cap_refuses_new_runs_once_reached(client, store):
    store.spend_usd = 50.0
    response = start(client, BOB)  # Bob hasn't run anything today
    assert response.status_code == 429
    assert "paused" in response.json()["error"]["message"]
    assert BOB not in store.runs


def test_runs_are_allowed_just_under_the_global_cap(client, store):
    store.spend_usd = 49.99
    assert start(client).status_code == 201


def test_no_global_cap_configured_means_only_the_per_user_limit_applies(client, store, monkeypatch):
    monkeypatch.delenv("GLOBAL_DAILY_CAP_USD")
    store.spend_usd = 10_000.0
    assert start(client).status_code == 201


@pytest.mark.parametrize(("name", "value"), [("RUNS_PER_USER_PER_DAY", "three"), ("GLOBAL_DAILY_CAP_USD", "-5")])
def test_a_bad_limit_setting_refuses_runs_instead_of_allowing_unlimited_ones(client, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    response = start(client)
    assert response.status_code == 503
    assert "{" not in response.json()["error"]["message"]


def test_quota_store_failure_refuses_runs_with_a_plain_english_503(client, store):
    store.broken = True
    response = start(client)
    assert response.status_code == 503
    assert "try again" in response.json()["error"]["message"]


def test_demo_replay_is_unaffected_by_quota_and_global_cap(client, store):
    store.runs[ALICE] = 99
    store.spend_usd = 10_000.0
    store.broken = True  # demo never even asks the quota store
    response = client.get(f"/demo/{EVENTS[0].run_id}")
    assert response.status_code == 200
    assert len(response.json()) == len(EVENTS)


def test_runs_left_today_needs_auth(client):
    assert client.get("/me/quota").status_code == 401


def test_runs_left_today_counts_down(client, store):
    store.runs[ALICE] = 1
    response = client.get("/me/quota", headers=bearer(ALICE))
    assert response.status_code == 200
    body = RunQuota.model_validate(response.json())
    assert (body.runs_per_day, body.runs_used_today, body.runs_left_today) == (3, 1, 2)
    assert body.paused is False
    resets = datetime.fromisoformat(body.resets_at)
    assert resets > datetime.now(UTC) and resets.utcoffset() == timedelta(0)
    assert (resets.hour, resets.minute, resets.second) == (0, 0, 0)


def test_runs_left_today_never_goes_negative_and_shows_the_global_pause(client, store):
    store.runs[ALICE] = 5
    store.spend_usd = 50.0
    body = RunQuota.model_validate(client.get("/me/quota", headers=bearer(ALICE)).json())
    assert body.runs_left_today == 0
    assert body.paused is True


def test_runs_left_today_fails_with_a_plain_english_503(client, store):
    store.broken = True
    response = client.get("/me/quota", headers=bearer(ALICE))
    assert response.status_code == 503
