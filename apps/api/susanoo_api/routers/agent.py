"""The agent in plain English: its card and its report card. [PRD › Non-technical users] (› Extra API)

`GET /runs/{id}/agent-card`   what the generated agent does, can do and will not do (Haiku, with a deterministic
                              fallback inside the translate module; the answer is cached per plan so a page reload
                              does not pay for a second model call).
`GET /runs/{id}/report-card`  "Caught 9 of 10 problems, 1 false alarm." per iteration (pure code, no model).

Both read the run's graph state from its checkpoint. Report cards carry counts only: never a scenario's diff,
answer key or title, and never anything from `scenarios/heldout/` beyond the scorer's numbers.
"""

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from susanoo.schemas import IterationMetrics, Plan, Scenario, ScenarioResult
from susanoo.translate import AgentCard, ReportCard, agent_card, fallback_card, report_card
from susanoo_api.deps import CurrentUserDep
from susanoo_api.quota import QuotaStoreDep, current_quota
from susanoo_api.routers.runs import (
    RunRow,
    RunStore,
    RunStoreDep,
    db_call,
    metrics_from_state,
    owned_run,
)

router = APIRouter(prefix="/runs", tags=["agent"])

logger = logging.getLogger("susanoo_api.agent")

NOT_BUILT = "The agent isn't planned yet, so there is nothing to show. Check back once the build has started."
MAX_PROMPT_BYTES = 40_000
_CACHE_LIMIT = 256
_cards: dict[tuple[str, str], AgentCard] = {}  # (run id, fingerprint of plan + prompt) -> card; per API process


class ReportCards(BaseModel):
    run_id: str
    message: str  # plain English; says so when nothing has been scored yet
    latest: ReportCard | None
    history: list[ReportCard]  # one per iteration, oldest first; each compared with the one before


async def load_state(store: RunStore, user_id: str, run: RunRow) -> dict[str, Any]:
    return await db_call(store.load_state(run.id), "read a run's progress", user_id)


def plan_from_state(state: dict[str, Any]) -> Plan:
    raw = state.get("plan")
    if raw is None:
        raise HTTPException(409, NOT_BUILT)
    return Plan.model_validate(raw)


def _read_prompt(repo_path: str, prompt_path: str) -> str:
    """The agent's own prompt.md if this process can see the generated repo, else "" (the card then uses the plan).
    The file must sit inside the repo folder."""
    try:
        root = Path(repo_path).resolve()
        target = (root / prompt_path).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            return ""
        return target.read_bytes()[:MAX_PROMPT_BYTES].decode("utf-8", errors="replace")
    except (OSError, ValueError):
        return ""


async def _prompt_text(state: dict[str, Any]) -> str:
    repo, agent = state.get("repo_path"), state.get("agent")
    prompt_path = getattr(agent, "prompt_path", None) or (agent.get("prompt_path") if isinstance(agent, dict) else None)
    if not repo or not prompt_path:
        return ""
    return await asyncio.to_thread(_read_prompt, str(repo), str(prompt_path))


@router.get("/{run_id}/agent-card", response_model=AgentCard)
async def get_agent_card(
    run_id: str, user: CurrentUserDep, store: RunStoreDep, quota_store: QuotaStoreDep
) -> AgentCard:
    run = await owned_run(store, user.id, run_id)
    state = await load_state(store, user.id, run)
    plan = plan_from_state(state)
    prompt = await _prompt_text(state)
    connectors = list(state.get("connectors") or [])
    fingerprint = hashlib.sha256(
        (plan.model_dump_json() + "\0" + prompt + "\0" + ",".join(map(str, connectors))).encode()
    ).hexdigest()
    key = (run.id, fingerprint)
    if key in _cards:
        return _cards[key]
    if (await current_quota(user.id, quota_store)).paused:
        # The platform's daily spending cap is reached: no model call. The plain card is not cached, so the real
        # one is written as soon as spending is allowed again.
        return fallback_card(plan, plan.rules, connectors)

    spent: list[float] = []
    card = await agent_card(
        plan,
        prompt,
        plan.rules,
        connectors,
        state={"cost_usd": run.cost_usd, "cost_cap_usd": run.cost_cap_usd},
        on_cost=spent.append,
    )
    if spent:
        await db_call(store.add_cost(run.id, sum(spent)), "record a cost", user.id)
    if len(_cards) >= _CACHE_LIMIT:
        _cards.pop(next(iter(_cards)))
    _cards[key] = card
    return card


def build_report_cards(state: dict[str, Any], run_id: str) -> ReportCards:
    metrics: list[IterationMetrics] = metrics_from_state(state)
    if not metrics:
        return ReportCards(
            run_id=run_id,
            message="No examples have been scored yet. The report card appears after the first test round.",
            latest=None,
            history=[],
        )
    scenarios = [Scenario.model_validate(s) for s in state.get("scenarios") or []]
    results = [ScenarioResult.model_validate(r) for r in state.get("results") or []]
    history: list[ReportCard] = []
    for m in metrics:
        previous = history[-1] if history else None
        history.append(
            report_card(
                m,
                [r for r in results if r.iteration == m.iteration],
                scenarios,
                previous,
                previous_label_text="Before",
            )
        )
    latest = history[-1]
    return ReportCards(run_id=run_id, message=latest.headline, latest=latest, history=history)


@router.get("/{run_id}/report-card", response_model=ReportCards)
async def get_report_card(run_id: str, user: CurrentUserDep, store: RunStoreDep) -> ReportCards:
    run = await owned_run(store, user.id, run_id)
    state = await load_state(store, user.id, run)
    try:
        return build_report_cards(state, run.id)
    except ValueError:  # a pydantic ValidationError is a ValueError
        logger.exception("Run %s has results the report card could not read", run.id)
        raise HTTPException(503, "The report card isn't available right now. Please try again in a minute.") from None
