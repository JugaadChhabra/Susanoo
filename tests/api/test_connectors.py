"""Connectors: list with guides, save, test, revoke; auth on every route; no secret in any response.

[PRD › Connectors and API budget], [PRD › Connectors and API budget › Credential handling]
"""

import logging
import time
import uuid

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from susanoo.connectors import Credential, redact
from susanoo.connectors.anthropic import AnthropicConnector
from susanoo.connectors.github import GitHubConnector
from susanoo_api import main
from susanoo_api.models import ConnectionStatus, ConnectionSummary, ConnectionTest, ConnectorInfo
from susanoo_api.routers import connectors

SECRET = "test-jwt-secret-that-is-long-enough-for-hs256"
ALICE = "11111111-1111-4111-8111-111111111111"
BOB = "22222222-2222-4222-8222-222222222222"
TOKEN = "github_pat_" + "11ABCDEFG0" * 6  # built at runtime so it never looks like a real token in git


def bearer(user_id: str) -> dict[str, str]:
    claims = {"sub": user_id, "aud": "authenticated", "role": "authenticated", "exp": int(time.time()) + 3600}
    return {"Authorization": f"Bearer {jwt.encode(claims, SECRET, algorithm='HS256')}"}


class FakeStore:
    """In-memory store that, like the real one, only ever answers for the user it is asked about."""

    def __init__(self) -> None:
        self.rows: dict[str, tuple[str, ConnectionSummary, SecretStr]] = {}
        self.fail = False

    def _check(self) -> None:
        if self.fail:
            raise RuntimeError(f"database down while holding {TOKEN}")

    async def list_for_user(self, user_id: str) -> list[ConnectionSummary]:
        self._check()
        return [c for owner, c, _ in self.rows.values() if owner == user_id]

    async def save(self, user_id, connector, secret, settings, writes_enabled) -> ConnectionSummary:
        self._check()
        for cid, (owner, c, _) in list(self.rows.items()):
            if owner == user_id and c.connector == connector:
                del self.rows[cid]  # one connection per connector, like the unique index
        summary = ConnectionSummary(
            id=str(uuid.uuid4()),
            connector=connector,
            status="untested",
            writes_enabled=writes_enabled,
            settings=settings,
        )
        self.rows[summary.id] = (user_id, summary, secret)
        return summary

    async def credential(self, user_id, connection_id):
        self._check()
        owner, summary, secret = self.rows.get(connection_id, (None, None, None))
        if owner != user_id or summary is None or secret is None:
            return None
        return summary, Credential(secret, summary.settings, summary.writes_enabled), str(id(secret))

    async def set_status(self, user_id, connection_id, status: ConnectionStatus, secret_ref: str):
        owner, summary, secret = self.rows.get(connection_id, (None, None, None))
        if owner != user_id or summary is None or secret is None or str(id(secret)) != secret_ref:
            return None
        updated = summary.model_copy(update={"status": status})
        self.rows[connection_id] = (owner, updated, secret)
        return updated

    async def delete(self, user_id, connection_id) -> bool:
        self._check()
        owner, _, _ = self.rows.get(connection_id, (None, None, None))
        if owner != user_id:
            return False
        del self.rows[connection_id]
        return True


class GitHubStub:
    """What GitHub answers; flip `token_ok` to make it reject the token."""

    def __init__(self) -> None:
        self.token_ok = True

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.headers["Authorization"] != f"Bearer {TOKEN}" or not self.token_ok:
            return httpx.Response(401, json={"message": "Bad credentials"})
        return httpx.Response(200, json=[] if request.url.path.endswith(("/pulls", "/contents/")) else {})


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def gh() -> GitHubStub:
    return GitHubStub()


@pytest.fixture
def client(monkeypatch, store, gh) -> TestClient:
    monkeypatch.setenv("SUPABASE_JWT_SECRET", SECRET)
    app = main.create_app()
    app.dependency_overrides[connectors.connection_store] = lambda: store
    app.dependency_overrides[connectors.connectors] = lambda: {
        "github": GitHubConnector(transport=httpx.MockTransport(gh))
    }
    return TestClient(app, raise_server_exceptions=False)


def save(client: TestClient, user: str = ALICE, **overrides) -> httpx.Response:
    body = {"connector": "github", "values": {"token": TOKEN, "repo": "acme/payments-api"}, **overrides}
    return client.post("/connections", headers=bearer(user), json=body)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/connectors"),
        ("post", "/connections"),
        ("post", f"/connections/{uuid.uuid4()}/test"),
        ("delete", f"/connections/{uuid.uuid4()}"),
    ],
)
def test_every_connector_route_needs_auth(client, method, path):
    response = client.request(method, path, json={"connector": "github", "values": {}})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_list_connectors_shows_spec_and_guide_and_no_connection_yet(client):
    response = client.get("/connectors", headers=bearer(ALICE))
    assert response.status_code == 200
    [github] = [ConnectorInfo.model_validate(c) for c in response.json()]
    assert github.spec.id == "github" and github.connection is None
    assert [f.name for f in github.spec.fields if f.secret] == ["token"]
    assert any(a.write for a in github.spec.actions)
    assert len(github.guide.steps) >= 4


def test_save_test_and_revoke_happy_path(client, store):
    saved = save(client)
    assert saved.status_code == 201
    connection = ConnectionSummary.model_validate(saved.json())
    assert connection.status == "untested"
    assert connection.settings == {"repo": "acme/payments-api"}
    assert connection.writes_enabled is False  # writes are off unless asked for
    assert store.rows[connection.id][2].get_secret_value() == TOKEN  # the secret went to the store (Vault)

    tested = client.post(f"/connections/{connection.id}/test", headers=bearer(ALICE))
    assert tested.status_code == 200
    result = ConnectionTest.model_validate(tested.json())
    assert result.ok and result.connection.status == "connected"
    assert result.message.startswith("Connected to acme/payments-api")

    [listed] = client.get("/connectors", headers=bearer(ALICE)).json()
    assert listed["connection"]["id"] == connection.id and listed["connection"]["status"] == "connected"

    assert client.delete(f"/connections/{connection.id}", headers=bearer(ALICE)).status_code == 204
    assert store.rows == {}
    assert client.get("/connectors", headers=bearer(ALICE)).json()[0]["connection"] is None
    assert client.delete(f"/connections/{connection.id}", headers=bearer(ALICE)).status_code == 404


def test_a_rejected_token_marks_the_connection_failed(client, gh):
    connection_id = save(client).json()["id"]
    gh.token_ok = False
    result = client.post(f"/connections/{connection_id}/test", headers=bearer(ALICE)).json()
    assert result["ok"] is False and result["connection"]["status"] == "failed"
    assert "didn't accept this token" in result["message"]


def test_saving_again_replaces_the_connection(client, store):
    first = save(client).json()["id"]
    second = save(client, writes_enabled=True).json()
    assert second["id"] != first and second["writes_enabled"] is True
    assert list(store.rows) == [second["id"]]


def test_other_users_cannot_see_test_or_revoke_a_connection(client):
    connection_id = save(client, ALICE).json()["id"]
    assert client.get("/connectors", headers=bearer(BOB)).json()[0]["connection"] is None
    assert client.post(f"/connections/{connection_id}/test", headers=bearer(BOB)).status_code == 404
    assert client.delete(f"/connections/{connection_id}", headers=bearer(BOB)).status_code == 404
    assert client.post(f"/connections/{connection_id}/test", headers=bearer(ALICE)).status_code == 200


@pytest.mark.parametrize("bad_id", ["not-a-uuid", "1"])
def test_malformed_ids_are_not_found(client, bad_id):
    assert client.post(f"/connections/{bad_id}/test", headers=bearer(ALICE)).status_code == 404
    assert client.delete(f"/connections/{bad_id}", headers=bearer(ALICE)).status_code == 404


@pytest.mark.parametrize(
    ("body", "phrase"),
    [
        ({"connector": "slack", "values": {"token": TOKEN}}, "don't have that connector"),
        ({"connector": "github", "values": {"token": TOKEN, "repo": "nope"}}, "owner/name"),
        ({"connector": "github", "values": {"token": "ghp_" + "a" * 36, "repo": "acme/x"}}, "classic token"),
        ({"connector": "github", "values": {"token": "x" * 1001, "repo": "acme/x"}}, "under 1000 characters"),
    ],
)
def test_bad_submissions_get_a_plain_english_422(client, store, body, phrase):
    response = client.post("/connections", headers=bearer(ALICE), json=body)
    assert response.status_code == 422
    assert phrase in response.json()["error"]["message"]
    assert store.rows == {}


def test_no_secret_in_any_response_or_log(client, store, caplog):
    """Walk every route, including failures, and check the token never comes back or gets logged."""
    with caplog.at_level(logging.DEBUG):
        responses = [save(client)]
        connection_id = responses[0].json()["id"]
        responses += [
            client.get("/connectors", headers=bearer(ALICE)),
            client.post(f"/connections/{connection_id}/test", headers=bearer(ALICE)),
            client.post("/connections", headers=bearer(ALICE), json={"connector": "github", "values": {"x": TOKEN}}),
        ]
        store.fail = True
        responses += [
            save(client),
            client.get("/connectors", headers=bearer(ALICE)),
            client.post(f"/connections/{connection_id}/test", headers=bearer(ALICE)),
            client.delete(f"/connections/{connection_id}", headers=bearer(ALICE)),
        ]
    assert [r.status_code for r in responses] == [201, 200, 200, 422, 503, 503, 503, 503]
    for response in responses:
        assert TOKEN not in response.text
        assert redact(response.text) == response.text  # nothing token-shaped at all
    assert "Could not save a connection" in caplog.text  # the failure was logged...
    assert TOKEN not in caplog.text  # ...with the token redacted from the exception message
    assert responses[-1].json()["error"]["message"] == connectors._UNAVAILABLE


def test_a_test_that_outlives_a_replacement_does_not_stamp_the_new_token(client, store, monkeypatch):
    connection_id = save(client).json()["id"]
    real = GitHubConnector.test_connection

    async def slow_test(self, credential):
        # the user replaces the token while GitHub is still answering about the old one
        owner, summary, _ = store.rows[connection_id]
        store.rows[connection_id] = (owner, summary, SecretStr(TOKEN))
        return await real(self, credential)

    monkeypatch.setattr(GitHubConnector, "test_connection", slow_test)
    response = client.post(f"/connections/{connection_id}/test", headers=bearer(ALICE))
    assert response.status_code == 409
    assert "test it again" in response.json()["error"]["message"]
    assert store.rows[connection_id][1].status == "untested"


def test_a_connector_that_crashes_reports_a_plain_failure(client, store, monkeypatch):
    connection_id = save(client).json()["id"]

    async def boom(self, credential):
        raise KeyError("repo")

    monkeypatch.setattr(GitHubConnector, "test_connection", boom)
    result = client.post(f"/connections/{connection_id}/test", headers=bearer(ALICE)).json()
    assert result["ok"] is False and result["connection"]["status"] == "failed"
    assert "couldn't check" in result["message"]


ANTHROPIC_KEY = "sk-ant-api03-" + "Ab1_-" * 16  # built at runtime so it never looks like a real key in git


@pytest.fixture
def byok_client(monkeypatch, store) -> TestClient:
    """The Anthropic connector against a stub that only accepts ANTHROPIC_KEY. [PRD › Anthropic budget]"""

    def anthropic_api(request: httpx.Request) -> httpx.Response:
        if request.headers.get("x-api-key") != ANTHROPIC_KEY:
            return httpx.Response(401, json={"error": {"type": "authentication_error", "message": "invalid x-api-key"}})
        return httpx.Response(200, json={"data": [], "has_more": False})

    monkeypatch.setenv("SUPABASE_JWT_SECRET", SECRET)
    app = main.create_app()
    app.dependency_overrides[connectors.connection_store] = lambda: store
    app.dependency_overrides[connectors.connectors] = lambda: {
        "anthropic": AnthropicConnector(transport=httpx.MockTransport(anthropic_api))
    }
    return TestClient(app, raise_server_exceptions=False)


def save_key(client: TestClient, key: str = ANTHROPIC_KEY) -> httpx.Response:
    return client.post(
        "/connections", headers=bearer(ALICE), json={"connector": "anthropic", "values": {"api_key": key}}
    )


def test_byok_key_saves_tests_and_revokes(byok_client, store, caplog):
    with caplog.at_level(logging.DEBUG):
        saved = save_key(byok_client)
        assert saved.status_code == 201
        connection = ConnectionSummary.model_validate(saved.json())
        assert connection.connector == "anthropic" and connection.settings == {}
        assert store.rows[connection.id][2].get_secret_value() == ANTHROPIC_KEY  # the key went to the store (Vault)

        tested = byok_client.post(f"/connections/{connection.id}/test", headers=bearer(ALICE))
        result = ConnectionTest.model_validate(tested.json())
        assert result.ok and result.connection.status == "connected"
        assert "Connected to Anthropic" in result.message

        listed = byok_client.get("/connectors", headers=bearer(ALICE))
        assert listed.json()[0]["connection"]["status"] == "connected"

        revoked = byok_client.delete(f"/connections/{connection.id}", headers=bearer(ALICE))
        assert revoked.status_code == 204 and store.rows == {}

    for response in (saved, tested, listed, revoked):
        assert ANTHROPIC_KEY not in response.text
    assert ANTHROPIC_KEY not in caplog.text


def test_a_rejected_byok_key_marks_the_connection_failed(byok_client):
    wrong = "sk-ant-api03-" + "Zz9_-" * 16
    connection_id = save_key(byok_client, wrong).json()["id"]
    result = byok_client.post(f"/connections/{connection_id}/test", headers=bearer(ALICE)).json()
    assert result["ok"] is False and result["connection"]["status"] == "failed"
    assert "didn't accept this key" in result["message"] and wrong not in result["message"]


def test_an_admin_key_is_refused_before_it_is_stored(byok_client, store):
    admin = "sk-ant-admin01-" + "a" * 60
    response = save_key(byok_client, admin)
    assert response.status_code == 422 and "Admin key" in response.json()["error"]["message"]
    assert admin not in response.text and store.rows == {}
