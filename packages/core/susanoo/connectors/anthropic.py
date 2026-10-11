"""Anthropic: the generated agent's own model calls. [PRD › Connectors and API budget › Anthropic budget]

Two ways to pay, each with its cap:
- BYOK: the user connects their own API key; their Anthropic Console spend limit is the cap.
- Platform key: without a connection the agent uses Susanoo's key (ANTHROPIC_API_KEY), capped at the
  per-run cost cap and, like every run, behind the daily run quota and global daily cap in quota.py.

`agent_model_access()` picks between them; its `env()` is what the generated app's container gets at start.
The key only ever goes into an x-api-key header or that env; it is never logged or returned.
"""

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

import httpx
from pydantic import SecretStr

from susanoo.connectors.base import (
    Connector,
    ConnectorAction,
    ConnectorField,
    ConnectorSpec,
    ConnectorTool,
    Credential,
    InvalidCredential,
    TestResult,
)
from susanoo.guards.budget import run_cost_cap_usd

logger = logging.getLogger("susanoo.connectors.anthropic")

API = "https://api.anthropic.com"
API_VERSION = "2023-06-01"
PLATFORM_KEY_ENV = "ANTHROPIC_API_KEY"

SPEC = ConnectorSpec(
    id="anthropic",
    name="Anthropic",
    summary="Lets your agent call Claude with your own API key and your own spend limit.",
    fields=[
        ConnectorField(
            name="api_key",
            label="API key",
            help="Starts with sk-ant-. Stored encrypted and never shown again.",
            secret=True,
            placeholder="sk-ant-api03-…",
        ),
    ],
    scopes=["Messages API"],
    actions=[
        ConnectorAction(name="messages", description="Call Claude to do the agent's work", write=False),
    ],
)


class AnthropicConnector(Connector):
    spec = SPEC

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport  # tests pass an httpx.MockTransport; None means the real network

    def validate(self, values: Mapping[str, SecretStr]) -> tuple[SecretStr, dict[str, str]]:
        key = values.get("api_key", SecretStr("")).get_secret_value().strip()
        if not key:
            raise InvalidCredential("Paste your Anthropic API key.")
        if key.startswith("sk-ant-admin"):
            raise InvalidCredential(
                "This is an Admin key, which can manage your whole Anthropic organisation. "
                "Please make a normal API key for Susanoo instead."
            )
        if not key.startswith("sk-ant-") or any(c.isspace() for c in key):
            raise InvalidCredential("That doesn't look like an Anthropic API key. It should start with sk-ant-.")
        return SecretStr(key), {}

    async def test_connection(self, credential: Credential) -> TestResult:
        """Lists models, which proves the key works without spending any tokens."""
        try:
            async with httpx.AsyncClient(base_url=API, transport=self._transport, timeout=15) as client:
                r = await client.get(
                    "/v1/models",
                    params={"limit": 1},
                    headers={
                        "x-api-key": credential.secret.get_secret_value(),
                        "anthropic-version": API_VERSION,
                    },
                )
        except httpx.HTTPError as exc:
            logger.warning("Could not reach Anthropic while testing a connection: %s", type(exc).__name__)
            return TestResult(ok=False, message="We couldn't reach Anthropic. Please try again in a minute.")
        if r.status_code == 401:
            return TestResult(
                ok=False, message="Anthropic didn't accept this key. It may be mistyped, disabled or deleted."
            )
        if r.status_code == 403:
            return TestResult(
                ok=False,
                message="This key isn't allowed to use the API. Check its workspace and your organisation "
                "in the Anthropic Console.",
            )
        if r.status_code == 429:
            return TestResult(
                ok=False,
                message="Anthropic accepted this key but it's over its rate limit right now. "
                "Please try again in a minute.",
            )
        if not r.is_success:
            logger.warning("Anthropic answered %s while testing a connection", r.status_code)
            return TestResult(ok=False, message="Anthropic had a problem checking this key. Please try again.")
        return TestResult(
            ok=True,
            message="Connected to Anthropic. Your agent will use your key, within the spend limit "
            "you set in the Anthropic Console.",
        )

    def tools(self, credential: Credential) -> list[ConnectorTool]:
        """None: the agent calls Claude itself, using the key from its container env."""
        return []

    def env(self, credential: Credential) -> dict[str, str]:
        return agent_model_access(credential).env()


@dataclass(frozen=True)
class ModelAccess:
    """Which Anthropic key a generated agent uses, and the spend cap Susanoo applies to it."""

    source: Literal["byok", "platform"]
    key: SecretStr = field(repr=False)
    cap_usd: float | None  # None: no Susanoo cap; the user's own Console spend limit applies

    def env(self) -> dict[str, str]:
        env = {"ANTHROPIC_API_KEY": self.key.get_secret_value(), "SUSANOO_ANTHROPIC_KEY_SOURCE": self.source}
        if self.cap_usd is not None:
            env["SUSANOO_AGENT_COST_CAP_USD"] = f"{self.cap_usd:.2f}"
        return env


def platform_model_access() -> ModelAccess:
    """Susanoo's own key, capped at the per-run cost cap. Raises RuntimeError if the key isn't configured."""
    key = os.environ.get(PLATFORM_KEY_ENV, "").strip()
    if not key:
        raise RuntimeError(f"{PLATFORM_KEY_ENV} is not set, so agents without their own key can't call Claude")
    return ModelAccess(source="platform", key=SecretStr(key), cap_usd=run_cost_cap_usd())


def agent_model_access(byok: Credential | None) -> ModelAccess:
    """The user's own key when they connected one, otherwise the platform key with the platform cap."""
    if byok is None:
        return platform_model_access()
    return ModelAccess(source="byok", key=byok.secret, cap_usd=None)
