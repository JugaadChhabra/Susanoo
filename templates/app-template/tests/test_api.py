import pytest
from fastapi.testclient import TestClient

from backend.main import create_app
from tests.conftest import SQL_DIFF, SQL_FINDING, FakeClient


@pytest.fixture(autouse=True)
def no_real_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_APP_API_KEY", raising=False)


def test_health_ok():
    r = TestClient(create_app()).get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_index_serves_the_page():
    r = TestClient(create_app()).get("/")
    assert r.status_code == 200 and "Code review" in r.text


def test_review_and_fetch_it_back():
    api = TestClient(create_app(client=FakeClient([SQL_FINDING])))
    r = api.post("/review", json={"diff": SQL_DIFF})
    assert r.status_code == 200
    body = r.json()
    assert body["findings"] == [SQL_FINDING]
    again = api.get(f"/reviews/{body['id']}")
    assert again.status_code == 200 and again.json() == body


def test_invented_rule_never_leaves_the_api():
    invented = {**SQL_FINDING, "rule_id": "made-up"}
    r = TestClient(create_app(client=FakeClient([invented, SQL_FINDING]))).post("/review", json={"diff": SQL_DIFF})
    assert [f["rule_id"] for f in r.json()["findings"]] == ["no-raw-sql"]


def test_missing_key_is_a_plain_503():
    r = TestClient(create_app()).post("/review", json={"diff": SQL_DIFF})
    assert r.status_code == 503
    assert "ANTHROPIC_APP_API_KEY" in r.json()["detail"]


def test_only_the_app_key_name_is_used(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-ignored")
    assert TestClient(create_app()).post("/review", json={"diff": SQL_DIFF}).status_code == 503


def test_empty_diff_and_bad_body_get_plain_messages():
    api = TestClient(create_app(client=FakeClient([])))
    assert api.post("/review", json={"diff": "   "}).status_code == 400
    r = api.post("/review", json={"nope": 1})
    assert r.status_code == 422 and isinstance(r.json()["detail"], str)


def test_unknown_review_is_404():
    r = TestClient(create_app()).get("/reviews/nope")
    assert r.status_code == 404 and "could not find" in r.json()["detail"]


def test_model_failure_is_502():
    api = TestClient(create_app(client=FakeClient(RuntimeError("down"))))
    assert api.post("/review", json={"diff": SQL_DIFF}).status_code == 502
