"""The AI-serving routers as the real app mounts them (AI_ROUTERS). [PRD › Frontend and API], CLAUDE.md §5

Unlike conftest_ai's `client`, these tests use `main.create_app()` exactly as deployed, so they fail if a router
drops out of AI_ROUTERS. They check: every route needs auth except /health and /demo/*; one user's run is a 404
to another on every route; a stale pause_token is a plain-English 409.
"""

# ruff: noqa: F811  (fixtures imported from conftest_ai are used as test arguments)

import uuid

import pytest
from conftest_ai import ALICE, BOB, SECRET, TOKEN, FakeQuotaStore, FakeRunStore, bearer, pg, pg_server  # noqa: F401
from fastapi.testclient import TestClient

from susanoo_api import main, quota
from susanoo_api.routers import runs

PUBLIC = {"/health"}
PUBLIC_PREFIXES = ("/demo",)
AI_ROUTES = {
    ("POST", "/projects/{project_id}/runs"),
    ("GET", "/runs/{run_id}"),
    ("POST", "/runs/{run_id}/cancel"),
    ("GET", "/runs/{run_id}/events"),
    ("GET", "/runs/{run_id}/intake"),
    ("POST", "/runs/{run_id}/intake/answers"),
    ("POST", "/runs/{run_id}/intake/scenarios"),
    ("POST", "/runs/{run_id}/approvals/{patch_id}"),
    ("POST", "/runs/{run_id}/feedback"),
    ("GET", "/runs/{run_id}/agent-card"),
    ("GET", "/runs/{run_id}/report-card"),
}


@pytest.fixture
def store() -> FakeRunStore:
    return FakeRunStore()


@pytest.fixture
def app(monkeypatch, store):
    monkeypatch.setenv("SUPABASE_JWT_SECRET", SECRET)
    monkeypatch.setenv("GLOBAL_DAILY_CAP_USD", "50")
    monkeypatch.delenv("RUNS_PER_USER_PER_DAY", raising=False)
    monkeypatch.delenv("RUN_COST_CAP_USD", raising=False)
    app = main.create_app()
    app.dependency_overrides[runs.run_store] = lambda: store
    app.dependency_overrides[quota.quota_store] = FakeQuotaStore
    return app


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


def _routes(app) -> list[tuple[str, str]]:
    """Every documented route, read from the OpenAPI schema (included routers are not flat in app.routes)."""
    return sorted((method.upper(), path) for path, ops in app.openapi()["paths"].items() for method in ops)


def _fill(path: str, value: str) -> str:
    return path.replace("{project_id}", value).replace("{run_id}", value).replace("{patch_id}", "d1")


def test_all_six_ai_routers_are_mounted(app):
    assert AI_ROUTES <= set(_routes(app))


def test_every_route_except_health_and_demo_needs_auth(client, app):
    checked = 0
    for method, path in _routes(app):
        if path in PUBLIC or path.startswith(PUBLIC_PREFIXES):
            continue
        response = client.request(method, _fill(path, str(uuid.uuid4())), json={})
        assert response.status_code == 401, (method, path, response.status_code)
        assert response.json()["error"]["code"] == "unauthorized"
        checked += 1
    assert checked >= len(AI_ROUTES)


_BODIES = {
    "/runs/{run_id}/intake/answers": {"pause_token": TOKEN, "answers": {}},
    "/runs/{run_id}/intake/scenarios": {"pause_token": TOKEN, "approved_ids": []},
    "/runs/{run_id}/approvals/{patch_id}": {"pause_token": TOKEN, "approved": True},
    "/runs/{run_id}/feedback": {"thumbs_down": True, "expectation": "Flag it.", "diff": "+x\n"},
}


@pytest.mark.parametrize(("method", "path"), sorted(r for r in AI_ROUTES if "{run_id}" in r[1]))
def test_another_users_run_is_a_404_on_every_route(client, store, method, path):
    run = store.add_run(ALICE, "awaiting_approval")
    store.pauses[run] = ("gate", {"kind": "gate", "diagnosis_id": "d1"}, TOKEN)
    response = client.request(method, _fill(path, run), headers=bearer(BOB), json=_BODIES.get(path))
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "not_found"
    assert not store.answers and not store.saved and not store.costs
    assert store.rows[run][1].cancel_requested is False


def test_starting_a_run_on_another_users_project_is_a_404(client, store):
    project = store.add_project(ALICE)
    assert client.post(f"/projects/{project}/runs", headers=bearer(BOB)).status_code == 404
    assert not store.rows


@pytest.mark.parametrize(
    ("kind", "path", "body"),
    [
        ("questions", "intake/answers", {"answers": {}}),
        ("scenarios", "intake/scenarios", {"approved_ids": []}),
        ("gate", "approvals/d1", {"approved": True}),
    ],
)
def test_a_stale_pause_token_is_a_plain_english_409(client, store, kind, path, body):
    run = store.add_run(ALICE)
    store.pause(run, kind, {"diagnosis_id": "d1", "questions": [], "scenarios": []})
    response = client.post(f"/runs/{run}/{path}", headers=bearer(ALICE), json={"pause_token": "chk-old", **body})
    assert response.status_code == 409
    message = response.json()["error"]["message"]
    assert "reload" in message and "{" not in message
    assert not store.answers


@pytest.mark.parametrize(("path", "body"), [("intake/answers", {"answers": {}}), ("approvals/d1", {"approved": True})])
def test_answers_and_approvals_without_a_pause_token_are_refused(client, store, path, body):
    run = store.add_run(ALICE)
    store.pause(run, "questions" if "intake" in path else "gate", {"diagnosis_id": "d1", "questions": []})
    response = client.post(f"/runs/{run}/{path}", headers=bearer(ALICE), json=body)
    assert response.status_code == 422
    assert "pause_token" in response.json()["error"]["message"]
    assert not store.answers


# ---------------------------------------------------------------- the real SQL through the real app (needs Docker)


def _user_with_project(pg) -> tuple[str, str]:
    uid = uuid.uuid4()
    pg.execute("insert into auth.users (id, email) values (%s, %s)", (uid, f"{uid}@example.com"))
    pid = pg.execute(
        "insert into public.projects (user_id, name, spec) values (%s, 'p', 's') returning id", (uid,)
    ).fetchone()["id"]
    return str(uid), str(pid)


def test_http_two_users_are_isolated_against_the_real_database(pg, monkeypatch):
    monkeypatch.setenv("SUPABASE_JWT_SECRET", SECRET)
    http = TestClient(main.create_app())
    alice, alice_project = _user_with_project(pg)
    bob, _ = _user_with_project(pg)

    started = http.post(f"/projects/{alice_project}/runs", headers=bearer(alice))
    assert started.status_code == 201 and started.json()["status"] == "queued"
    run = started.json()["id"]

    assert http.post(f"/projects/{alice_project}/runs", headers=bearer(bob)).status_code == 404
    for method, suffix in [("get", ""), ("get", "/intake"), ("post", "/cancel"), ("get", "/events")]:
        assert http.request(method, f"/runs/{run}{suffix}", headers=bearer(bob)).status_code == 404
    assert http.get(f"/runs/{run}", headers=bearer(alice)).status_code == 200
    row = pg.execute("select cancel_requested from public.runs where id = %s", (run,)).fetchone()
    assert row["cancel_requested"] is False


def test_http_queued_runs_count_towards_the_daily_quota(pg, monkeypatch):
    monkeypatch.setenv("SUPABASE_JWT_SECRET", SECRET)
    http = TestClient(main.create_app())
    alice, project = _user_with_project(pg)
    for _ in range(3):
        assert http.post(f"/projects/{project}/runs", headers=bearer(alice)).status_code == 201
    assert pg.execute("select count(*) n from public.runs where status = 'queued'").fetchone()["n"] == 3
    refused = http.post(f"/projects/{project}/runs", headers=bearer(alice))
    assert refused.status_code == 429 and "3 runs" in refused.json()["error"]["message"]
    quota_now = http.get("/me/quota", headers=bearer(alice)).json()
    assert (quota_now["runs_used_today"], quota_now["runs_left_today"]) == (3, 0)


def test_http_queued_runs_count_towards_the_global_cap(pg, monkeypatch):
    """A queued run may still spend up to its cap, so it is held against today's global cap like a running one."""
    monkeypatch.setenv("SUPABASE_JWT_SECRET", SECRET)
    monkeypatch.setenv("GLOBAL_DAILY_CAP_USD", "4")
    monkeypatch.setenv("RUN_COST_CAP_USD", "2")
    http = TestClient(main.create_app())
    alice, a_project = _user_with_project(pg)
    bob, b_project = _user_with_project(pg)
    pg.execute(
        "insert into public.runs (project_id, status, cost_cap_usd, created_at) "
        "values (%s, 'queued', 2, now() - interval '2 days')",
        (b_project,),
    )  # queued since before today: counts at its cap
    assert http.post(f"/projects/{a_project}/runs", headers=bearer(alice)).status_code == 201  # 2 + 2 = cap
    refused = http.post(f"/projects/{b_project}/runs", headers=bearer(bob))
    assert refused.status_code == 429 and "paused" in refused.json()["error"]["message"]
