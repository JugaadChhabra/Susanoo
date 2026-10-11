"""Supabase JWT verification and the current_user dependency. [PRD › Frontend and API › Auth flow]"""

import time

import jwt
import pytest
from fastapi.testclient import TestClient

from susanoo_api import main

SECRET = "test-jwt-secret-that-is-long-enough-for-hs256"
USER_ID = "5f0c1d2e-3a4b-4c5d-8e6f-7a8b9c0d1e2f"


def make_token(**overrides: object) -> str:
    claims: dict[str, object] = {
        "sub": USER_ID,
        "email": "dev@example.com",
        "aud": "authenticated",
        "role": "authenticated",
        "exp": int(time.time()) + 3600,
    }
    claims.update(overrides)
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, SECRET, algorithm="HS256")


@pytest.fixture
def client(monkeypatch) -> TestClient:
    monkeypatch.setenv("SUPABASE_JWT_SECRET", SECRET)
    return TestClient(main.create_app())


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_me_without_token_is_401(client):
    response = client.get("/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_me_with_valid_token_is_200(client):
    response = client.get("/me", headers=bearer(make_token()))
    assert response.status_code == 200
    assert response.json() == {"id": USER_ID, "email": "dev@example.com"}


@pytest.mark.parametrize(
    "token",
    [
        "not-a-jwt",
        jwt.encode(
            {"sub": USER_ID, "aud": "authenticated", "exp": int(time.time()) + 60},
            "wrong-secret-wrong-secret-wrong-secret",
            algorithm="HS256",
        ),
        make_token(exp=int(time.time()) - 10),
        make_token(aud="someone-else"),
        make_token(sub=None),
        make_token(role="anon"),
    ],
    ids=["garbage", "wrong-secret", "expired", "wrong-audience", "no-subject", "anon-role"],
)
def test_me_rejects_bad_tokens(client, token):
    response = client.get("/me", headers=bearer(token))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_unsigned_token_is_rejected(client):
    token = jwt.encode({"sub": USER_ID, "aud": "authenticated"}, key="", algorithm="none")
    assert client.get("/me", headers=bearer(token)).status_code == 401


def test_non_bearer_scheme_is_401(client):
    assert client.get("/me", headers={"Authorization": f"Basic {make_token()}"}).status_code == 401


def test_missing_server_secret_fails_closed(monkeypatch):
    monkeypatch.delenv("SUPABASE_JWT_SECRET", raising=False)
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    response = TestClient(main.create_app()).get("/me", headers=bearer(make_token()))
    assert response.status_code == 401


def test_health_and_demo_stay_public(client):
    assert client.get("/health").status_code == 200
    assert client.get("/demo/00000000-0000-0000-0000-000000000000").status_code != 401
