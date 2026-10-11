"""Anthropic connector: BYOK validation and test, and the platform-key fallback with its cap.

[PRD › Connectors and API budget › Anthropic budget]
"""

import logging

import httpx
import pytest
from pydantic import SecretStr

from susanoo.connectors import Credential, InvalidCredential, redact, registry
from susanoo.connectors.anthropic import AnthropicConnector, agent_model_access, platform_model_access
from susanoo.guards.budget import DEFAULT_RUN_COST_CAP_USD

KEY = "sk-ant-api03-" + "Ab1_-" * 16  # built at runtime so it never looks like a real key in git
PLATFORM_KEY = "sk-ant-api03-" + "Zz9_-" * 16


def cred(key: str = KEY) -> Credential:
    return Credential(secret=SecretStr(key), settings={})


def anthropic(status: int, seen: list[httpx.Request] | None = None) -> AnthropicConnector:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json={"data": [], "error": {"type": "x", "message": f"bad key {KEY}"}})

    return AnthropicConnector(transport=httpx.MockTransport(handler))


def test_registered_with_one_secret_field_and_no_write_actions():
    connector = registry()["anthropic"]
    assert [f.name for f in connector.spec.fields if f.secret] == ["api_key"]
    assert [f.name for f in connector.spec.fields if not f.secret] == []
    assert not any(a.write for a in connector.spec.actions)


def test_validate_trims_the_key_and_keeps_no_settings():
    secret, settings = AnthropicConnector().validate({"api_key": SecretStr(f"  {KEY}\n")})
    assert secret.get_secret_value() == KEY
    assert settings == {}


@pytest.mark.parametrize(
    ("key", "hint"),
    [
        ("", "Paste"),
        ("sk-ant-admin01-" + "a" * 60, "Admin key"),
        ("sk-proj-" + "a" * 40, "sk-ant-"),
        ("sk-ant-api03-abc def", "sk-ant-"),
    ],
)
def test_validate_rejects_bad_keys_without_echoing_them(key, hint):
    with pytest.raises(InvalidCredential) as exc:
        AnthropicConnector().validate({"api_key": SecretStr(key)})
    assert hint in str(exc.value)
    assert not key or key not in str(exc.value)


async def test_test_connection_lists_models_with_the_key_and_spends_nothing():
    seen: list[httpx.Request] = []
    result = await anthropic(200, seen).test_connection(cred())
    assert result.ok
    assert "Connected to Anthropic" in result.message and "spend limit" in result.message
    [request] = seen
    assert request.method == "GET" and request.url.path == "/v1/models"  # listing models costs no tokens
    assert request.headers["x-api-key"] == KEY
    assert request.headers["anthropic-version"]


@pytest.mark.parametrize(
    ("status", "phrase"),
    [
        (401, "didn't accept this key"),
        (403, "isn't allowed"),
        (429, "rate limit"),
        (500, "had a problem"),
        (529, "had a problem"),
    ],
)
async def test_test_connection_explains_each_failure(status, phrase):
    result = await anthropic(status).test_connection(cred())
    assert not result.ok
    assert phrase in result.message
    assert KEY not in result.message


async def test_test_connection_when_anthropic_is_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    result = await AnthropicConnector(transport=httpx.MockTransport(handler)).test_connection(cred())
    assert not result.ok and "couldn't reach Anthropic" in result.message


async def test_failures_never_log_the_key(caplog):
    with caplog.at_level(logging.DEBUG):
        await anthropic(500).test_connection(cred())
    assert KEY not in caplog.text


def test_no_tools_the_agent_calls_claude_through_its_env():
    assert AnthropicConnector().tools(cred()) == []


def test_byok_env_carries_the_users_key_and_no_platform_cap():
    env = AnthropicConnector().env(cred())
    assert env == {"ANTHROPIC_API_KEY": KEY, "SUSANOO_ANTHROPIC_KEY_SOURCE": "byok"}


def test_byok_is_used_when_the_user_connected_a_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", PLATFORM_KEY)
    access = agent_model_access(cred())
    assert access.source == "byok"
    assert access.cap_usd is None  # the user's own Console spend limit is the cap
    assert access.env() == {"ANTHROPIC_API_KEY": KEY, "SUSANOO_ANTHROPIC_KEY_SOURCE": "byok"}


def test_platform_key_is_used_without_a_connection_and_applies_the_platform_cap(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", PLATFORM_KEY)
    monkeypatch.delenv("RUN_COST_CAP_USD", raising=False)
    access = agent_model_access(None)
    assert access.source == "platform"
    assert access.cap_usd == DEFAULT_RUN_COST_CAP_USD
    assert access.env() == {
        "ANTHROPIC_API_KEY": PLATFORM_KEY,
        "SUSANOO_ANTHROPIC_KEY_SOURCE": "platform",
        "SUSANOO_AGENT_COST_CAP_USD": "2.00",
    }


def test_platform_cap_follows_the_configured_per_run_cap(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", PLATFORM_KEY)
    monkeypatch.setenv("RUN_COST_CAP_USD", "0.5")
    access = platform_model_access()
    assert access.cap_usd == 0.5
    assert access.env()["SUSANOO_AGENT_COST_CAP_USD"] == "0.50"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_platform_key_missing_fails_loudly_without_a_fallback(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    else:
        monkeypatch.setenv("ANTHROPIC_API_KEY", value)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        agent_model_access(None)


def test_model_access_never_shows_the_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", PLATFORM_KEY)
    for access in (agent_model_access(cred()), agent_model_access(None)):
        text = f"{access!r} {access!s}"
        assert KEY not in text and PLATFORM_KEY not in text


def test_redact_hides_anthropic_keys():
    assert KEY not in redact(f"x-api-key: {KEY}")


def test_guide_has_numbered_steps_a_spend_limit_and_hides_maintainer_notes():
    guide = registry()["anthropic"].guide()
    assert len(guide.steps) >= 4
    assert "API keys" in guide.steps[0]
    assert any("spend limit" in s.lower() for s in guide.steps)
    assert all("Click-paths a human must check" not in s for s in guide.steps + guide.notes)
    assert any("Susanoo's key" in n for n in guide.notes)  # what happens if you don't connect one
