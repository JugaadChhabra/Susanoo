"""Projects: create, list, edit rules as sentences. [PRD › Frontend and API], [PRD › Non-technical users › Extra API]"""

import time
import uuid
from pathlib import Path

import jwt
import pytest
from fastapi.testclient import TestClient

from susanoo_api import main
from susanoo_api.models import ProjectSummary
from susanoo_api.routers import projects

SECRET = "test-jwt-secret-that-is-long-enough-for-hs256"
ALICE = "11111111-1111-4111-8111-111111111111"
BOB = "22222222-2222-4222-8222-222222222222"
SPEC = "Build a code review app for our Python services.\nDevelopers paste a diff and get a review."


def bearer(user_id: str) -> dict[str, str]:
    claims = {"sub": user_id, "aud": "authenticated", "role": "authenticated", "exp": int(time.time()) + 3600}
    return {"Authorization": f"Bearer {jwt.encode(claims, SECRET, algorithm='HS256')}"}


ALICE_GITHUB = "33333333-3333-4333-8333-333333333333"
BOB_GITHUB = "44444444-4444-4444-8444-444444444444"


class FakeStore:
    """In-memory store that, like the real one, only ever answers for the user it is asked about."""

    def __init__(self) -> None:
        self.rows: dict[str, tuple[str, ProjectSummary]] = {}
        self.connections = {ALICE_GITHUB: ALICE, BOB_GITHUB: BOB}  # connection id -> owner

    async def list_for_user(self, user_id: str) -> list[ProjectSummary]:
        return [p for owner, p in reversed(self.rows.values()) if owner == user_id]

    async def foreign_connections(self, user_id: str, connection_ids: list[str]) -> list[str]:
        return [cid for cid in connection_ids if self.connections.get(cid) != user_id]

    async def create(
        self, user_id: str, name: str, spec: str, rules: list[str], connection_ids: list[str]
    ) -> ProjectSummary:
        project = ProjectSummary(
            id=str(uuid.uuid4()),
            name=name,
            spec=spec,
            rules=rules,
            connection_ids=connection_ids,
            repo_url=None,
            runs=[],
        )
        self.rows[project.id] = (user_id, project)
        return project

    async def set_rules(
        self, user_id: str, project_id: str, rules: list[str], connection_ids: list[str] | None
    ) -> ProjectSummary | None:
        owner, project = self.rows.get(project_id, (None, None))
        if owner != user_id or project is None:
            return None
        update: dict[str, object] = {"rules": rules}
        if connection_ids is not None:
            update["connection_ids"] = connection_ids
        updated = project.model_copy(update=update)
        self.rows[project_id] = (owner, updated)
        return updated


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def client(monkeypatch, store) -> TestClient:
    monkeypatch.setenv("SUPABASE_JWT_SECRET", SECRET)
    app = main.create_app()
    app.dependency_overrides[projects.project_store] = lambda: store
    return TestClient(app)


@pytest.mark.parametrize(
    ("method", "path"),
    [("get", "/projects"), ("post", "/projects"), ("put", f"/projects/{uuid.uuid4()}/rules")],
)
def test_every_projects_route_needs_auth(client, method, path):
    response = client.request(method, path, json={"spec": SPEC, "rules": []})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_create_then_list_happy_path(client):
    response = client.post(
        "/projects",
        headers=bearer(ALICE),
        json={"spec": SPEC, "rules": ["  Never log customer emails.  ", "", "Every endpoint needs a login check."]},
    )
    assert response.status_code == 201
    created = ProjectSummary.model_validate(response.json())
    assert created.name == "Build a code review app for our Python services."
    assert created.rules == ["Never log customer emails.", "Every endpoint needs a login check."]
    assert created.runs == []

    listed = client.get("/projects", headers=bearer(ALICE))
    assert listed.status_code == 200
    assert [ProjectSummary.model_validate(p) for p in listed.json()] == [created]


def test_another_users_projects_never_appear(client):
    mine = client.post("/projects", headers=bearer(ALICE), json={"spec": SPEC, "rules": []}).json()
    client.post("/projects", headers=bearer(BOB), json={"spec": "Bob's private spec", "rules": []})

    alice_sees = client.get("/projects", headers=bearer(ALICE)).json()
    assert [p["id"] for p in alice_sees] == [mine["id"]]
    assert "Bob's private spec" not in str(alice_sees)


def test_update_rules_as_sentences(client):
    project = client.post("/projects", headers=bearer(ALICE), json={"spec": SPEC, "rules": ["Old rule."]}).json()
    response = client.put(
        f"/projects/{project['id']}/rules",
        headers=bearer(ALICE),
        json={"rules": ["Never let anyone log customer emails.", "   "]},
    )
    assert response.status_code == 200
    assert response.json()["rules"] == ["Never let anyone log customer emails."]
    assert client.get("/projects", headers=bearer(ALICE)).json()[0]["rules"] == [
        "Never let anyone log customer emails."
    ]


def test_cannot_edit_another_users_rules(client):
    project = client.post("/projects", headers=bearer(ALICE), json={"spec": SPEC, "rules": ["Keep me."]}).json()
    response = client.put(f"/projects/{project['id']}/rules", headers=bearer(BOB), json={"rules": ["Hijacked."]})
    assert response.status_code == 404
    assert response.json()["error"]["message"] == "We couldn't find that project."
    assert client.get("/projects", headers=bearer(ALICE)).json()[0]["rules"] == ["Keep me."]


def test_picked_connections_are_saved_and_returned(client):
    response = client.post(
        "/projects", headers=bearer(ALICE), json={"spec": SPEC, "rules": [], "connection_ids": [ALICE_GITHUB]}
    )
    assert response.status_code == 201
    assert response.json()["connection_ids"] == [ALICE_GITHUB]
    assert client.get("/projects", headers=bearer(ALICE)).json()[0]["connection_ids"] == [ALICE_GITHUB]


def test_no_connections_behaves_as_before(client):
    project = client.post("/projects", headers=bearer(ALICE), json={"spec": SPEC, "rules": []}).json()
    assert project["connection_ids"] == []
    response = client.put(f"/projects/{project['id']}/rules", headers=bearer(ALICE), json={"rules": ["A rule."]})
    assert response.status_code == 200
    assert response.json()["connection_ids"] == []


def test_updating_rules_can_change_connections_or_leave_them(client):
    project = client.post("/projects", headers=bearer(ALICE), json={"spec": SPEC, "rules": []}).json()
    path = f"/projects/{project['id']}/rules"
    added = client.put(path, headers=bearer(ALICE), json={"rules": [], "connection_ids": [ALICE_GITHUB]})
    assert added.status_code == 200 and added.json()["connection_ids"] == [ALICE_GITHUB]
    kept = client.put(path, headers=bearer(ALICE), json={"rules": ["New rule."]})
    assert kept.json()["connection_ids"] == [ALICE_GITHUB]  # leaving the field out keeps them
    cleared = client.put(path, headers=bearer(ALICE), json={"rules": [], "connection_ids": []})
    assert cleared.json()["connection_ids"] == []


def test_the_same_connection_twice_is_saved_once(client):
    body = {"spec": SPEC, "rules": [], "connection_ids": [ALICE_GITHUB, ALICE_GITHUB.upper()]}
    response = client.post("/projects", headers=bearer(ALICE), json=body)
    assert response.status_code == 201
    assert response.json()["connection_ids"] == [ALICE_GITHUB]


def test_another_users_connection_is_refused_in_plain_english(client):
    response = client.post(
        "/projects", headers=bearer(ALICE), json={"spec": SPEC, "rules": [], "connection_ids": [BOB_GITHUB]}
    )
    assert response.status_code == 422
    assert response.json()["error"]["message"] == projects.FOREIGN_CONNECTION
    assert client.get("/projects", headers=bearer(ALICE)).json() == []  # nothing was created

    project = client.post("/projects", headers=bearer(ALICE), json={"spec": SPEC, "rules": ["Keep me."]}).json()
    response = client.put(
        f"/projects/{project['id']}/rules",
        headers=bearer(ALICE),
        json={"rules": ["Changed."], "connection_ids": [ALICE_GITHUB, BOB_GITHUB]},
    )
    assert response.status_code == 422
    assert response.json()["error"]["message"] == projects.FOREIGN_CONNECTION
    assert client.get("/projects", headers=bearer(ALICE)).json()[0]["rules"] == ["Keep me."]


def test_a_connection_id_that_is_not_an_id_is_a_422(client):
    response = client.post(
        "/projects", headers=bearer(ALICE), json={"spec": SPEC, "rules": [], "connection_ids": ["github"]}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_blank_rules_do_not_count_toward_the_limit(client):
    rules = [f"Rule {i}." for i in range(30)] + ["", "   "]
    response = client.post("/projects", headers=bearer(ALICE), json={"spec": SPEC, "rules": rules})
    assert response.status_code == 201
    assert len(response.json()["rules"]) == 30


def test_malformed_project_id_is_not_found(client):
    response = client.put("/projects/not-a-uuid/rules", headers=bearer(ALICE), json={"rules": []})
    assert response.status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"spec": "   ", "rules": []},
        {"spec": "x" * 20_001, "rules": []},
        {"spec": SPEC, "rules": ["x" * 501]},
        {"spec": SPEC, "rules": [f"Rule {i}." for i in range(31)]},
    ],
    ids=["blank-spec", "huge-spec", "huge-rule", "too-many-rules"],
)
def test_bad_input_is_a_plain_english_422(client, body):
    response = client.post("/projects", headers=bearer(ALICE), json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_database_trouble_is_plain_english(client, store, monkeypatch):
    async def broken(user_id: str) -> list[ProjectSummary]:
        raise RuntimeError("DATABASE_URL is not set")

    monkeypatch.setattr(store, "list_for_user", broken)
    response = client.get("/projects", headers=bearer(ALICE))
    assert response.status_code == 503
    assert "DATABASE_URL" not in response.text


@pytest.mark.parametrize(
    ("spec", "name"),
    [
        ("Build a code review app. It reviews diffs.", "Build a code review app."),
        ("\n\n  A support bot\nthat answers tickets", "A support bot"),
        ("word " * 40, ("word " * 40).strip()[:60].rstrip() + "…"),
    ],
)
def test_project_name_comes_from_the_spec(spec, name):
    assert projects.name_from_spec(spec) == name


def test_run_summary_statuses_match_migration_006_and_the_runs_router():
    """The projects list shows every status a run can be in: the runs_status_check of migration 006."""
    import re
    import typing

    from susanoo_api.models import RunSummary
    from susanoo_api.routers.runs import RunState

    sql = (Path(__file__).parents[2] / "supabase" / "migrations" / "006_run_queue.sql").read_text()
    check = re.search(r"check \(status in \(([^)]*)\)\)", sql)
    assert check is not None
    in_db = set(re.findall(r"'(\w+)'", check.group(1)))
    summary = set(typing.get_args(RunSummary.model_fields["status"].annotation))
    assert summary == in_db == set(typing.get_args(RunState))
    assert {"queued", "cancelled"} <= summary
