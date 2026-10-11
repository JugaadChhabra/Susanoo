"""Request and response models for platform routes that aren't part of the frozen contracts.

scripts/export_types.py exports these into apps/web/types/generated.ts next to the schemas.
"""

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field, StringConstraints

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


class RunSummary(BaseModel):
    """One run as the projects screen shows it. heldout_f1 is the latest iteration's, if any."""

    id: str
    status: Literal["running", "awaiting_approval", "done", "failed"]
    heldout_f1: float | None
    cost_usd: float
    cost_cap_usd: float
    created_at: str


class ProjectSummary(BaseModel):
    id: str
    name: str
    spec: str
    rules: list[str]  # plain-English sentences, compiled into prompt.md and policy.yaml later
    repo_url: str | None
    runs: list[RunSummary]  # newest first


class NewProject(BaseModel):
    spec: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20_000)]
    rules: Rules


class ProjectRules(BaseModel):
    rules: Rules


class RunQuota(BaseModel):
    """How many runs the caller can still start today. [PRD › Connectors and API budget › Anthropic budget]"""

    runs_per_day: int
    runs_used_today: int
    runs_left_today: int  # never negative; 0 when paused too
    paused: bool  # the platform hit its global daily spending cap, so nobody can start a run today
    resets_at: str  # ISO timestamp of the next midnight UTC, when the counts start over
