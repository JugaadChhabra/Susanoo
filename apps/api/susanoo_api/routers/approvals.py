"""POST /runs/{id}/approvals/{patch_id}: decide a gated change. [PRD › Improvement loop and guardrails]

A `test`-layer change pauses the run at the HumanGate (kind "gate") before any patch exists, so `{patch_id}` is the
id the gate announced (`approval_id` from GET /runs/{id}/intake, the gate's diagnosis id) and must match the pause
the run is waiting on. The body carries that pause's `pause_token`; a stale or repeated decision gets a 409. Only an
explicit `approved: true` approves. The API stores the decision and the worker resumes the graph.
"""

from typing import Annotated

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, StrictBool, StringConstraints

from susanoo.graph.resume import validate_resume
from susanoo_api.deps import CurrentUserDep
from susanoo_api.routers.runs import STALE_PAUSE, RunStoreDep, canonical_uuid, pending_for_answer, submit_answer

router = APIRouter(prefix="/runs", tags=["approvals"])


class ApprovalIn(BaseModel):
    pause_token: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    approved: StrictBool  # only a real JSON true approves: "true", 1 and "yes" are refused


class ApprovalOut(BaseModel):
    ok: bool
    approved: bool
    message: str


@router.post("/{run_id}/approvals/{patch_id}", response_model=ApprovalOut)
async def decide(run_id: str, patch_id: str, body: ApprovalIn, user: CurrentUserDep, store: RunStoreDep) -> ApprovalOut:
    canonical = canonical_uuid(run_id)
    pending = await pending_for_answer(store, user.id, canonical, "gate", body.pause_token)
    payload = pending.payload or {}
    if str(payload.get("diagnosis_id", "")) != patch_id:
        raise HTTPException(409, STALE_PAUSE)
    value = {"approved": body.approved}
    try:
        validate_resume("gate", value, payload)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    await submit_answer(store, user.id, canonical, value, body.pause_token)
    if body.approved:
        return ApprovalOut(ok=True, approved=True, message="You approved the test change. The run is continuing.")
    return ApprovalOut(
        ok=True,
        approved=False,
        message="You rejected the test change. The run is continuing without it.",
    )
