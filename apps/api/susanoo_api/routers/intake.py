"""Intake: what a paused run is asking, and the user's answers. [PRD › Intake: questions and approved scenarios]

`GET /runs/{id}/intake` reads the latest question_needed / approval_needed pause of the run and returns it for the
screen: up to 5 questions (kind "questions"), the example scenario cards (kind "scenarios"), or a test change that
needs a decision (kind "gate", answered through POST /runs/{id}/approvals/{patch_id}). It also carries a
`pause_token` naming that exact pause; the answer must send it back, so a stale tab or a double click gets a 409
instead of answering a later question. Answers are validated with `susanoo.graph.resume.validate_resume` before
they are stored; the worker then resumes the graph. The API never runs the graph.
"""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, StringConstraints

from susanoo.graph.resume import validate_resume
from susanoo_api.deps import CurrentUserDep
from susanoo_api.routers.runs import (
    RunStoreDep,
    canonical_uuid,
    db_call,
    owned_run,
    pending_for_answer,
    submit_answer,
)

router = APIRouter(prefix="/runs", tags=["intake"])

PauseToken = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Answer = Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]
ShortTitle = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
ScenarioId = Annotated[str, StringConstraints(min_length=1, max_length=100)]


class IntakeQuestionOut(BaseModel):
    id: str
    text: str
    options: list[str]  # empty means free text
    default: str  # the suggested answer
    why: str  # one line on why it matters


class ScenarioCardOut(BaseModel):
    id: str
    title: str  # plain English: what happens and what the agent should do


class GateOut(BaseModel):
    approval_id: str  # send this as {patch_id} in POST /runs/{id}/approvals/{patch_id}
    layer: str
    hypothesis: str  # plain English
    scenario_ids: list[str]


class IntakeOut(BaseModel):
    waiting: bool
    kind: Literal["questions", "scenarios", "gate"] | None
    pause_token: str | None  # echo this back in the answer
    message: str
    questions: list[IntakeQuestionOut]
    scenarios: list[ScenarioCardOut]
    gate: GateOut | None


class AnswersIn(BaseModel):
    pause_token: PauseToken
    answers: dict[str, Answer] = Field(max_length=20)  # question id -> the chosen or typed answer


class ScenarioEdit(BaseModel):
    title: ShortTitle


class ScenariosIn(BaseModel):
    pause_token: PauseToken
    approved_ids: list[ScenarioId] = Field(max_length=50)  # required: nothing is approved by default
    rejected_ids: list[ScenarioId] = Field(default_factory=list, max_length=50)
    edits: dict[ScenarioId, ScenarioEdit] = Field(default_factory=dict, max_length=50)


class IntakeAccepted(BaseModel):
    ok: bool
    message: str


def _strings(items: Any) -> list[str]:
    return [str(i) for i in items] if isinstance(items, list) else []


def _intake_from_pause(kind: str, payload: dict[str, Any], token: str) -> IntakeOut:
    questions: list[IntakeQuestionOut] = []
    scenarios: list[ScenarioCardOut] = []
    gate: GateOut | None = None
    if kind == "questions":
        message = "Please answer a few quick questions before we build. Each one has a suggested answer."
        for q in payload.get("questions") or []:
            questions.append(
                IntakeQuestionOut(
                    id=str(q.get("id", "")),
                    text=str(q.get("text", "")),
                    options=_strings(q.get("options")),
                    default=str(q.get("default", "")),
                    why=str(q.get("why", "")),
                )
            )
    elif kind == "scenarios":
        message = "Please approve, edit or reject these example scenarios. Approved ones become your acceptance tests."
        scenarios = [
            ScenarioCardOut(id=str(s.get("id", "")), title=str(s.get("title", "")))
            for s in payload.get("scenarios") or []
        ]
    else:
        message = "A change to a test needs your decision."
        gate = GateOut(
            approval_id=str(payload.get("diagnosis_id", "")),
            layer=str(payload.get("layer", "")),
            hypothesis=str(payload.get("hypothesis", "")),
            scenario_ids=_strings(payload.get("scenario_ids")),
        )
    return IntakeOut(
        waiting=True,
        kind=kind,  # type: ignore[arg-type]
        pause_token=token,
        message=message,
        questions=questions,
        scenarios=scenarios,
        gate=gate,
    )


@router.get("/{run_id}/intake", response_model=IntakeOut)
async def get_intake(run_id: str, user: CurrentUserDep, store: RunStoreDep) -> IntakeOut:
    run = await owned_run(store, user.id, run_id)
    pending = await db_call(store.pending(user.id, run.id), "read what a run is waiting for", user.id)
    if pending is None:
        raise HTTPException(404, "We couldn't find that run.")
    if pending.waiting and pending.kind in ("questions", "scenarios", "gate") and pending.payload is not None:
        assert pending.pause_token is not None
        return _intake_from_pause(pending.kind, pending.payload, pending.pause_token)
    nothing = "Nothing is waiting for you right now."
    if pending.status in ("done", "failed", "cancelled"):
        nothing = "This run has finished, so there is nothing left to answer."
    return IntakeOut(waiting=False, kind=None, pause_token=None, message=nothing, questions=[], scenarios=[], gate=None)


@router.post("/{run_id}/intake/answers", response_model=IntakeAccepted)
async def answer_questions(run_id: str, body: AnswersIn, user: CurrentUserDep, store: RunStoreDep) -> IntakeAccepted:
    canonical = canonical_uuid(run_id)
    pending = await pending_for_answer(store, user.id, canonical, "questions", body.pause_token)
    value: dict[str, Any] = {"answers": dict(body.answers)}
    try:
        validate_resume("questions", value, pending.payload)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    await submit_answer(store, user.id, canonical, value, body.pause_token)
    return IntakeAccepted(ok=True, message="Got your answers. We're carrying on with the build.")


@router.post("/{run_id}/intake/scenarios", response_model=IntakeAccepted)
async def review_scenarios(run_id: str, body: ScenariosIn, user: CurrentUserDep, store: RunStoreDep) -> IntakeAccepted:
    canonical = canonical_uuid(run_id)
    pending = await pending_for_answer(store, user.id, canonical, "scenarios", body.pause_token)
    value: dict[str, Any] = {
        "approved_ids": list(body.approved_ids),
        "rejected_ids": list(body.rejected_ids),
        "edits": {sid: {"title": edit.title} for sid, edit in body.edits.items()},
    }
    try:
        validate_resume("scenarios", value, pending.payload)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    await submit_answer(store, user.id, canonical, value, body.pause_token)
    return IntakeAccepted(
        ok=True, message="Got your review. The approved scenarios are locked in and the build continues."
    )
