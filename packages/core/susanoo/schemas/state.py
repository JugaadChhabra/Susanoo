"""Shared LangGraph state. [PRD › LangGraph flow and shared state › State (the body)]

Frozen: change only with both owners' agreement (see the contract-change skill).
"""

from typing import Literal, TypedDict

from susanoo.schemas.core import (
    AgentBundle,
    Diagnosis,
    IterationMetrics,
    Patch,
    Plan,
    Scenario,
    ScenarioResult,
)


class SusanooState(TypedDict):
    run_id: str
    spec: str
    plan: Plan  # requirements, rules, file plan
    repo_path: str
    head_commit: str
    agent: AgentBundle  # prompt, policy, tools
    scenarios: list[Scenario]  # each with answer key + split
    results: list[ScenarioResult]  # per iteration
    failure_ledger: list[Diagnosis]
    patches: list[Patch]  # kept or rolled back, Entire checkpoint id
    metrics: list[IterationMetrics]
    iteration: int
    cost_usd: float
    status: Literal["running", "awaiting_approval", "done", "failed"]
