"""POST /runs/{id}/feedback: thumbs down + "it should have..." becomes a new test. [PRD › Non-technical users]

The user never edits the agent. `feedback_to_scenario` (Haiku) turns the feedback into a `train` Scenario with an
answer key, or says in plain English why it can't. A scenario is persisted in `public.scenarios` (split 'train',
approved_by_user false, so it never joins the locked acceptance set) with a plain-English event in the run log. The
loop reads it once the graph loads that table (not yet wired), so the replies make no promise about the next round.
Nothing here touches the agent's files.
The model call's cost is added to the run's cost_usd (an atomic `cost_usd = cost_usd + x`), and a spent per-run
budget or the platform's global daily cap is reported as a rejection before any model call.
"""

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter
from pydantic import BaseModel, Field, StrictBool, StringConstraints

from susanoo.schemas import Finding
from susanoo.translate import FeedbackRejected, feedback_to_scenario
from susanoo_api.deps import CurrentUserDep
from susanoo_api.quota import QuotaStoreDep, current_quota
from susanoo_api.routers.agent import load_state, plan_from_state
from susanoo_api.routers.runs import RunStoreDep, db_call, owned_run

router = APIRouter(prefix="/runs", tags=["feedback"])

logger = logging.getLogger("susanoo_api.feedback")


class FeedbackIn(BaseModel):
    thumbs_down: StrictBool
    expectation: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]  # "It should have..."
    diff: Annotated[str, StringConstraints(max_length=50_000)]  # the change the agent reviewed ("Try it")
    findings: list[Finding] = Field(default_factory=list, max_length=50)  # what the agent said about it


class FeedbackOut(BaseModel):
    accepted: bool
    message: str  # plain English, shown as is
    code: str | None  # why not, when accepted is false (e.g. "unclear", "thumbs_up", "budget")
    scenario_id: str | None
    title: str | None  # the new test, in words


@router.post("/{run_id}/feedback", response_model=FeedbackOut)
async def give_feedback(
    run_id: str, body: FeedbackIn, user: CurrentUserDep, store: RunStoreDep, quota_store: QuotaStoreDep
) -> FeedbackOut:
    run = await owned_run(store, user.id, run_id)
    plan = plan_from_state(await load_state(store, user.id, run))
    if (await current_quota(user.id, quota_store)).paused:  # the platform's daily spending cap covers this model call
        return FeedbackOut(
            accepted=False,
            message="Susanoo has reached its spending limit for today, so we can't write a new test right now. "
            "Please try again after midnight UTC.",
            code="budget",
            scenario_id=None,
            title=None,
        )

    spent: list[float] = []
    result = await feedback_to_scenario(
        body.diff,
        body.findings,
        body.thumbs_down,
        body.expectation,
        plan.rules,
        scenario_id=str(uuid.uuid4()),
        state={"cost_usd": run.cost_usd, "cost_cap_usd": run.cost_cap_usd},
        on_cost=spent.append,
    )
    if spent:
        await db_call(store.add_cost(run.id, sum(spent)), "record a cost", user.id)
    if isinstance(result, FeedbackRejected):
        return FeedbackOut(accepted=False, message=result.reason, code=result.code, scenario_id=None, title=None)

    await db_call(store.save_feedback_scenario(run.id, result), "save a new test", user.id)
    return FeedbackOut(
        accepted=True,
        message="Saved as a new check for this agent.",
        code=None,
        scenario_id=result.id,
        title=result.title,
    )
