"""Request and response models for platform routes that aren't part of the frozen contracts.

scripts/export_types.py exports these into apps/web/types/generated.ts next to the schemas.
"""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, Field, SecretStr, StringConstraints

from susanoo.connectors.base import ConnectorGuide, ConnectorSpec

MAX_RULES = 30
MAX_RULE_LENGTH = 500

Sentence = Annotated[str, StringConstraints(strip_whitespace=True, max_length=MAX_RULE_LENGTH)]


def _drop_blank(rules: list[str]) -> list[str]:
    kept = [rule for rule in rules if rule]
    if len(kept) > MAX_RULES:
        raise ValueError(f"Please keep it to {MAX_RULES} rules or fewer.")
    return kept


# Blank lines from the rules box are dropped (and don't count), so ["Never log emails.", ""] saves one rule.
Rules = Annotated[list[Sentence], Field(max_length=MAX_RULES * 4), AfterValidator(_drop_blank)]


MAX_CONNECTIONS = 10


def _distinct(ids: list[UUID]) -> list[UUID]:
    return list(dict.fromkeys(ids))


# The caller's own connection ids, as picked on the new-project screen; the same id twice is kept once.
ConnectionIds = Annotated[list[UUID], Field(max_length=MAX_CONNECTIONS), AfterValidator(_distinct)]


class RunSummary(BaseModel):
    """One run as the projects screen shows it. heldout_f1 is the latest iteration's, if any."""

    id: str
    status: Literal["queued", "running", "awaiting_approval", "done", "failed", "cancelled"]  # migration 006
    heldout_f1: float | None
    cost_usd: float
    cost_cap_usd: float
    created_at: str


class ProjectSummary(BaseModel):
    id: str
    name: str
    spec: str
    rules: list[str]  # plain-English sentences, compiled into prompt.md and policy.yaml later
    connection_ids: list[str]  # the owner's connections the agent may use; revoked ones are left out
    repo_url: str | None
    runs: list[RunSummary]  # newest first


class NewProject(BaseModel):
    spec: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20_000)]
    rules: Rules
    connection_ids: ConnectionIds = []


class ProjectRules(BaseModel):
    rules: Rules
    connection_ids: ConnectionIds | None = None  # left out (or null) keeps the project's current connections


class RunQuota(BaseModel):
    """How many runs the caller can still start today. [PRD › Connectors and API budget › Anthropic budget]"""

    runs_per_day: int
    runs_used_today: int
    runs_left_today: int  # never negative; 0 when paused too
    paused: bool  # the platform hit its global daily spending cap, so nobody can start a run today
    resets_at: str  # ISO timestamp of the next midnight UTC, when the counts start over


MAX_CONNECTION_VALUE = 1000


def _short_values(values: dict[str, SecretStr]) -> dict[str, SecretStr]:
    if any(len(v.get_secret_value()) > MAX_CONNECTION_VALUE for v in values.values()):
        raise ValueError(f"Each value must be under {MAX_CONNECTION_VALUE} characters.")
    return values


ConnectionStatus = Literal["untested", "connected", "failed"]


class ConnectionSummary(BaseModel):
    """A saved connection. It never carries the secret: that lives only in Vault. [PRD › Credential handling]"""

    id: str
    connector: str
    status: ConnectionStatus
    writes_enabled: bool  # off means write actions are dry-run
    settings: dict[str, str]  # the non-secret fields, e.g. {"repo": "acme/payments-api"}


class ConnectorInfo(BaseModel):
    """One available connector with its setup guide, and the caller's connection to it if they have one."""

    spec: ConnectorSpec
    guide: ConnectorGuide
    connection: ConnectionSummary | None


class NewConnection(BaseModel):
    connector: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]
    # Every value is a SecretStr so no field (token or not) can show up in a repr, a log line or a traceback.
    values: Annotated[dict[str, SecretStr], Field(max_length=10), AfterValidator(_short_values)]
    writes_enabled: bool = False


class ConnectionTest(BaseModel):
    ok: bool
    message: str  # plain English, e.g. "Connected to acme/payments-api, read access confirmed."
    connection: ConnectionSummary
