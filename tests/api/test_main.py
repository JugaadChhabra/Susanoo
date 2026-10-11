import httpx
import pytest
from fastapi.testclient import TestClient

from susanoo_api import main


@pytest.fixture
def client() -> TestClient:
    return TestClient(main.create_app())


def test_health_degrades_when_supabase_url_unset(client, monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    body = client.get("/health").json()
    assert body["status"] == "degraded"
    assert body["supabase"] == {"status": "unconfigured", "message": "SUPABASE_URL is not set."}


def test_health_ok_when_supabase_reachable(client, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/auth/v1/health"
        assert request.headers["apikey"] == "anon"
        return httpx.Response(200, json={"name": "GoTrue"})

    monkeypatch.setattr(main, "_transport", httpx.MockTransport(handler))
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["supabase"]["status"] == "ok"


def test_health_degrades_when_supabase_unreachable(client, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(main, "_transport", httpx.MockTransport(handler))
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["supabase"]["status"] == "unreachable"


def test_unknown_route_uses_error_shape(client):
    response = client.get("/nope")
    assert response.status_code == 404
    assert response.json() == {"error": {"code": "not_found", "message": "Not Found"}}


def test_validation_error_uses_error_shape(client):
    app = main.create_app()

    @app.get("/needs-int")
    def needs_int(n: int) -> int:
        return n

    response = TestClient(app).get("/needs-int?n=abc")
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["message"]


def test_unhandled_exception_uses_error_shape():
    app = main.create_app()

    @app.get("/explode")
    def explode() -> None:
        raise RuntimeError("secret internals")

    response = TestClient(app, raise_server_exceptions=False).get(
        "/explode", headers={"Origin": "http://localhost:3000"}
    )
    assert response.status_code == 500
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert response.json() == {
        "error": {"code": "internal_error", "message": "Something went wrong on our side."}
    }


def test_cors_allows_app_domain(monkeypatch):
    monkeypatch.setenv("APP_DOMAIN", "app.example.dev")
    client = TestClient(main.create_app())
    response = client.get("/health", headers={"Origin": "https://app.example.dev"})
    assert response.headers["access-control-allow-origin"] == "https://app.example.dev"
    other = client.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in other.headers
