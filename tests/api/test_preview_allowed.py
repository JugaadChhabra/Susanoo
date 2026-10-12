"""GET /internal/preview-allowed: Caddy's on-demand TLS ask. [PRD › Deployment]"""

import pytest
from fastapi.testclient import TestClient

from susanoo_api import main
from susanoo_api.routers import internal

RUN_ID = "3f2b6c1e-8a4d-4e2f-9b7a-1c2d3e4f5a6b"
NO_PREVIEW = "00000000-0000-4000-8000-000000000001"
DOMAIN = "susanoo.example.dev"


@pytest.fixture
def client(monkeypatch) -> TestClient:
    async def fake_has_preview(run_id: str) -> bool:
        return run_id == RUN_ID

    monkeypatch.setattr(internal, "has_preview", fake_has_preview)
    monkeypatch.setenv("PREVIEW_DOMAIN", DOMAIN)
    return TestClient(main.create_app())


def ask(client: TestClient, domain: str, **headers: str):
    return client.get("/internal/preview-allowed", params={"domain": domain}, headers=headers)


def test_run_with_a_recorded_preview_gets_a_certificate(client):
    assert ask(client, f"run-{RUN_ID}.{DOMAIN}").status_code == 200


def test_host_case_does_not_matter_like_dns(client):
    assert ask(client, f"RUN-{RUN_ID.upper()}.{DOMAIN.upper()}").status_code == 200


def test_unknown_run_or_run_without_preview_is_refused(client):
    assert ask(client, f"run-{NO_PREVIEW}.{DOMAIN}").status_code == 404
    assert ask(client, f"run-11111111-1111-4111-8111-111111111111.{DOMAIN}").status_code == 404


@pytest.mark.parametrize(
    "domain",
    [
        "",
        DOMAIN,
        f"api.{DOMAIN}",
        f"run-.{DOMAIN}",
        f"run-not-a-uuid.{DOMAIN}",
        f"run-{RUN_ID}x.{DOMAIN}",
        f"x.run-{RUN_ID}.{DOMAIN}",
        f"run-{RUN_ID}.{DOMAIN}.evil.example",
        f"run-{RUN_ID}.evil.example",
        f"run-{RUN_ID}.{DOMAIN}:443",
        f"run-{RUN_ID.replace('-', '')}.{DOMAIN}",
    ],
)
def test_malformed_hosts_and_other_domains_are_refused(client, domain):
    assert ask(client, domain).status_code == 404


def test_missing_domain_is_refused(client):
    assert client.get("/internal/preview-allowed").status_code == 404


def test_not_reachable_through_the_public_api_domain(client):
    # Caddy's reverse_proxy always adds X-Forwarded-For; its own ask call goes straight to api:8000 without it.
    host = f"run-{RUN_ID}.{DOMAIN}"
    assert ask(client, host, **{"X-Forwarded-For": "203.0.113.7"}).status_code == 404
    assert ask(client, host, **{"X-Forwarded-Host": "api.susanoo.example.dev"}).status_code == 404


def test_requests_addressed_to_the_api_domain_are_refused(client, monkeypatch):
    monkeypatch.setenv("API_DOMAIN", "api.susanoo.example.dev")
    host = f"run-{RUN_ID}.{DOMAIN}"
    assert ask(client, host, Host="api.susanoo.example.dev").status_code == 404
    assert ask(client, host, Host="api:8000").status_code == 200  # how Caddy itself calls it


def test_the_api_domain_itself_gets_its_certificate(client, monkeypatch):
    # api.<PREVIEW_DOMAIN> is covered by the preview wildcard, so Caddy asks for it too (live outage, 12 Oct).
    monkeypatch.setenv("API_DOMAIN", f"api.{DOMAIN}")
    assert ask(client, f"api.{DOMAIN}").status_code == 200
    assert ask(client, f"API.{DOMAIN.upper()}").status_code == 200
    assert ask(client, f"api.{DOMAIN}", **{"X-Forwarded-For": "203.0.113.7"}).status_code == 404


def test_bad_preview_domain_setting_refuses_with_a_plain_message(client, monkeypatch):
    monkeypatch.setenv("PREVIEW_DOMAIN", "Not A Domain!")
    response = ask(client, f"run-{RUN_ID}.{DOMAIN}")
    assert response.status_code == 503
    assert "set up" in response.json()["error"]["message"]


def test_database_trouble_refuses_rather_than_issuing(client, monkeypatch):
    async def broken(run_id: str) -> bool:
        raise RuntimeError("DATABASE_URL is not set")

    monkeypatch.setattr(internal, "has_preview", broken)
    response = ask(client, f"run-{RUN_ID}.{DOMAIN}")
    assert response.status_code == 503
    assert "error" in response.json()
