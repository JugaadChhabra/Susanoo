"""Request and response models for platform routes that aren't part of the frozen contracts.

scripts/export_types.py exports these into apps/web/types/generated.ts next to the schemas.
"""

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field, StringConstraints

MAX_RULES = 30

Sentence = Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]


def _drop_blank(rules: list[str]) -> list[str]:
    return [rule for rule in rules if rule]


# Blank lines from the rules box are dropped, so ["Never log emails.", ""] saves one rule.
Rules = Annotated[list[Sentence], Field(max_length=MAX_RULES), AfterValidator(_drop_blank)]


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
