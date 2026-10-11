"""GET /demo/{run_id}: public replay of a saved run. [PRD › Frontend and API]"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from susanoo.schemas import RunEvent
from susanoo_api import main
from susanoo_api.routers import demo

FIXTURE = Path(__file__).parent.parent / "fixtures" / "run_events.json"
EVENTS = [RunEvent(**e) for e in json.loads(FIXTURE.read_text())]
RUN_ID = EVENTS[0].run_id


@pytest.fixture
def client(monkeypatch) -> TestClient:
    async def fake_load(run_id: str) -> list[RunEvent]:
        return EVENTS if run_id == RUN_ID else []

    monkeypatch.setattr(demo, "load_events", fake_load)
    monkeypatch.setenv("DEMO_RUN_IDS", RUN_ID)
    return TestClient(main.create_app())


def test_replay_needs_no_auth_and_returns_events_in_order(client):
    response = client.get(f"/demo/{RUN_ID}")  # no Authorization header, no cookies
    assert response.status_code == 200
    body = response.json()
    assert [e["seq"] for e in body] == sorted(e["seq"] for e in body)
    assert body == [e.model_dump() for e in EVENTS]


def test_unlisted_run_is_not_found_even_if_it_has_events(client, monkeypatch):
    monkeypatch.setenv("DEMO_RUN_IDS", "")
    response = client.get(f"/demo/{RUN_ID}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_unknown_or_malformed_id_is_not_found(client, monkeypatch):
    other = "00000000-0000-4000-8000-000000000000"
    monkeypatch.setenv("DEMO_RUN_IDS", f"{RUN_ID}, not-a-uuid, {other}")
    assert client.get(f"/demo/{other}").status_code == 404  # listed but nothing saved
    assert client.get("/demo/not-a-uuid").status_code == 404
