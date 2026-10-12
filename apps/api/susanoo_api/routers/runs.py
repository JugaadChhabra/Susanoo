"""Runs: start, status, cancel. Also the run store every AI-serving router shares.
[PRD › Frontend (marketing site + app) and API], [PRD › Tech stack] (job queue = Postgres table, no Redis)

The API never runs the graph. `POST /projects/{id}/runs` inserts a run with status 'queued'; a worker claims it
(docs/proposals/006_run_queue.sql). A human's answer to a pause is stored on the run row and the worker resumes
the graph from its checkpoint. The API connects as the service role, so every query here joins through
projects.user_id: another user's run is simply not found (404, never 403).

`RunStore` is the one door to the database for runs.py, events.py, intake.py, approvals.py, feedback.py and
agent.py. Tests replace `run_store` with an in-memory fake; `PostgresRunStore` is the real thing.
"""

import logging
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from susanoo.db import emit_event, session
from susanoo.guards.budget import run_cost_cap_usd
from susanoo.schemas import IterationMetrics, RunEvent, Scenario
from susanoo_api import quota
from susanoo_api.auth import CurrentUser
from susanoo_api.deps import CurrentUserDep
from susanoo_api.quota import RunQuotaDep

logger = logging.getLogger("susanoo_api.runs")

router = APIRouter(tags=["runs"])

RunState = Literal["queued", "running", "awaiting_approval", "done", "failed", "cancelled"]
TERMINAL: frozenset[str] = frozenset({"done", "failed", "cancelled"})

RUN_NOT_FOUND = "We couldn't find that run."
PROJECT_NOT_FOUND = "We couldn't find that project."
STALE_PAUSE = "This question was already answered or has moved on; please reload."
UNAVAILABLE = "Your runs are unavailable right now. Please try again in a minute."

_PAUSE_EVENT_TYPES = ("question_needed", "approval_needed")

# ---------------------------------------------------------------- models


class RunStatus(BaseModel):
    """One run as the run view shows it. Never carries the resume payload, claims, or held-out content."""

    id: str
    project_id: str
    status: RunState
    message: str  # plain English, what is happening now
    cost_usd: float
    cost_cap_usd: float
    preview_url: str | None
    created_at: str
    cancel_requested: bool
    error: str | None  # plain English, only when the run failed
    metrics: list[IterationMetrics]  # one per iteration so far (numbers only)


@dataclass(frozen=True)
class RunRow:
    id: str
    project_id: str
    status: str
    cost_usd: float
    cost_cap_usd: float
    preview_url: str | None
    created_at: str
    cancel_requested: bool
    error: str | None


@dataclass(frozen=True)
class Pending:
    """What a run is waiting for. `waiting` is False unless it is paused with a token and a pause payload."""

    status: str
    waiting: bool
    kind: str | None = None  # questions | scenarios | gate
    payload: dict[str, Any] | None = None
    pause_token: str | None = None


@dataclass(frozen=True)
class EventBatch:
    status: str  # the run's status, read before the events so a terminal status means "no more events coming"
    events: list[RunEvent]


@dataclass(frozen=True)
class QuotaRefused:
    """create_run's answer when the user (or the platform) has no runs left. `message` is plain English."""

    message: str


class RunStore(Protocol):
    async def create_run(self, user_id: str, project_id: str, cost_cap_usd: float) -> RunRow | QuotaRefused | None: ...
    async def get_run(self, user_id: str, run_id: str) -> RunRow | None: ...
    async def request_cancel(self, user_id: str, run_id: str) -> RunRow | None: ...
    async def poll_events(self, user_id: str, run_id: str, after_seq: int, limit: int) -> EventBatch | None: ...
    async def pending(self, user_id: str, run_id: str) -> Pending | None: ...
    async def store_answer(self, user_id: str, run_id: str, value: dict[str, Any], pause_token: str) -> bool: ...
    async def add_cost(self, run_id: str, usd: float) -> None: ...
    async def load_state(self, run_id: str) -> dict[str, Any]: ...
    async def save_feedback_scenario(self, run_id: str, scenario: Scenario) -> None: ...


_RUN_COLUMNS = """
    r.id::text as id, r.project_id::text as project_id, r.status, r.cost_usd::float as cost_usd,
    r.cost_cap_usd::float as cost_cap_usd, r.preview_url, r.created_at::text as created_at,
    r.cancel_requested, r.error
"""
_OWNED = (
    "from public.runs r join public.projects p on p.id = r.project_id where r.id = %(run)s and p.user_id = %(user)s"
)


# The same counts as quota.PostgresQuotaStore.usage_since, but on the caller's own connection so they run inside
# create_run's transaction. One query, shared, so the two can't drift apart.
_USAGE_SQL = quota.USAGE_SQL
_LOCK_SQL = "select pg_advisory_xact_lock(hashtextextended(%s, 0))"


class _TransactionQuotaStore:
    """quota.QuotaStore that counts on an open transaction instead of opening its own."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    async def usage_since(self, user_id: str, since: datetime) -> quota.Usage:
        cur = await self.conn.execute(_USAGE_SQL, {"user": user_id, "since": since})
        row = await cur.fetchone()
        assert row is not None
        return quota.Usage(int(row["runs_today"]), float(row["spend_usd"]))


class PostgresRunStore:
    async def create_run(self, user_id: str, project_id: str, cost_cap_usd: float) -> RunRow | QuotaRefused | None:
        """Enqueue a run if the user and the platform are under their daily limits, decided and inserted in ONE
        transaction. Two advisory locks (this user's, then the platform's; always in that order) make concurrent
        starts take turns, so the quota dependency's earlier check can't be raced past: at 2 of 3 runs only one of
        ten simultaneous POSTs gets in, and the global cap can't be overshot by simultaneous starts either."""
        async with session() as conn:
            await conn.execute(_LOCK_SQL, (f"susanoo-run-quota:{user_id}",))
            await conn.execute(_LOCK_SQL, ("susanoo-run-quota:global",))
            try:
                await quota.enforce_run_quota(CurrentUser(id=user_id), _TransactionQuotaStore(conn))
            except HTTPException as exc:
                if exc.status_code == 429:
                    return QuotaRefused(str(exc.detail))  # same plain-English message as the dependency
                raise
            cur = await conn.execute(
                """
                insert into public.runs (project_id, status, cost_cap_usd)
                select p.id, 'queued', %(cap)s from public.projects p
                 where p.id = %(project)s and p.user_id = %(user)s
                returning id::text as id
                """,
                {"cap": cost_cap_usd, "project": project_id, "user": user_id},
            )
            row = await cur.fetchone()
        return None if row is None else await self.get_run(user_id, row["id"])

    async def get_run(self, user_id: str, run_id: str) -> RunRow | None:
        async with session() as conn:
            cur = await conn.execute(f"select {_RUN_COLUMNS} {_OWNED}", {"run": run_id, "user": user_id})
            row = await cur.fetchone()
        return None if row is None else RunRow(**row)

    async def request_cancel(self, user_id: str, run_id: str) -> RunRow | None:
        """Ask for a cancel. The worker does the rest: a heartbeat stops a running run, and its sweep cancels a
        queued or paused one (with the plain-English event). Only a run that is not finished can be asked."""
        async with session() as conn:
            await conn.execute(
                """
                update public.runs r set cancel_requested = true
                  from public.projects p
                 where p.id = r.project_id and p.user_id = %(user)s and r.id = %(run)s
                   and r.status in ('queued', 'running', 'awaiting_approval')
                """,
                {"run": run_id, "user": user_id},
            )
        return await self.get_run(user_id, run_id)

    async def poll_events(self, user_id: str, run_id: str, after_seq: int, limit: int) -> EventBatch | None:
        async with session() as conn:
            cur = await conn.execute(f"select r.status {_OWNED}", {"run": run_id, "user": user_id})
            run = await cur.fetchone()
            if run is None:
                return None
            cur = await conn.execute(
                """
                select run_id::text, seq, head, type, message, payload, trace_url, at::text
                  from public.run_events where run_id = %s and seq > %s order by seq limit %s
                """,
                (run_id, after_seq, limit),
            )
            return EventBatch(run["status"], [RunEvent(**row) for row in await cur.fetchall()])

    async def pending(self, user_id: str, run_id: str) -> Pending | None:
        async with session() as conn:
            cur = await conn.execute(
                f"""
                select r.status, r.paused_checkpoint_id as token,
                       (select e.payload from public.run_events e
                         where e.run_id = r.id and e.type = any(%(types)s)
                         order by e.seq desc limit 1) as payload
                {_OWNED}
                """,
                {"run": run_id, "user": user_id, "types": list(_PAUSE_EVENT_TYPES)},
            )
            row = await cur.fetchone()
        if row is None:
            return None
        payload = row["payload"]
        if row["status"] != "awaiting_approval" or not row["token"] or not isinstance(payload, dict):
            return Pending(status=row["status"], waiting=False)
        kind = payload.get("kind")
        if not isinstance(kind, str):
            return Pending(status=row["status"], waiting=False)
        return Pending(status=row["status"], waiting=True, kind=kind, payload=payload, pause_token=row["token"])

    async def store_answer(self, user_id: str, run_id: str, value: dict[str, Any], pause_token: str) -> bool:
        """Queue the run with the answer, but only for the exact pause the human saw (same rule as the worker's
        store_resume_value: the token is matched in the UPDATE itself, so a stale tab can never answer a later
        pause). Also scoped to the caller."""
        async with session() as conn:
            cur = await conn.execute(
                """
                update public.runs r set status = 'queued', resume_value = %(value)s
                  from public.projects p
                 where p.id = r.project_id and p.user_id = %(user)s and r.id = %(run)s
                   and r.status = 'awaiting_approval' and r.paused_checkpoint_id = %(token)s
                """,
                {"value": Jsonb(value), "user": user_id, "run": run_id, "token": pause_token},
            )
            return cur.rowcount > 0

    async def add_cost(self, run_id: str, usd: float) -> None:
        if usd <= 0:
            return
        async with session() as conn:
            await conn.execute("update public.runs set cost_usd = cost_usd + %s where id = %s", (round(usd, 4), run_id))

    async def load_state(self, run_id: str) -> dict[str, Any]:
        """The run's latest graph state from its checkpoint (plan, scenarios, results, metrics...), or {}."""
        from susanoo.graph import run_config
        from susanoo.graph.checkpointer import postgres_checkpointer

        config = {"configurable": run_config(run_id)["configurable"]}
        async with postgres_checkpointer(run_setup=False) as saver:
            found = await saver.aget_tuple(config)  # type: ignore[arg-type]
        if found is None:
            return {}
        return dict(found.checkpoint.get("channel_values", {}))

    async def save_feedback_scenario(self, run_id: str, scenario: Scenario) -> None:
        """Park the feedback test in public.scenarios (split 'train', not approved) and say so in the run log."""
        async with session() as conn:
            await conn.execute(
                """
                insert into public.scenarios (id, run_id, kind, split, title, diff, answer_key, approved_by_user)
                values (%s, %s, %s, 'train', %s, %s, %s, false)
                """,
                (
                    scenario.id,
                    run_id,
                    scenario.kind,
                    scenario.title,
                    scenario.diff,
                    Jsonb(scenario.answer_key.model_dump()),
                ),
            )
        await emit_event(
            run_id,
            "system",
            "progress",
            "Saved as a new check for this agent.",
            {"scenario_id": scenario.id, "split": "train"},
        )


def run_store() -> RunStore:
    """Tests override this dependency with an in-memory store."""
    return PostgresRunStore()


RunStoreDep = Annotated[RunStore, Depends(run_store)]


# ---------------------------------------------------------------- helpers shared by the AI routers


def canonical_uuid(value: str, not_found: str = RUN_NOT_FOUND) -> str:
    """A path id as a canonical UUID string, or a 404 (a malformed id can't belong to anyone)."""
    try:
        return str(UUID(value))
    except ValueError:
        raise HTTPException(404, not_found) from None


async def db_call[T](awaitable: Awaitable[T], action: str, user_id: str) -> T:
    """Await a store call; an unexpected failure becomes a plain-English 503 (logged, never swallowed)."""
    try:
        return await awaitable
    except HTTPException:
        raise
    except Exception:
        logger.exception("Could not %s for user %s", action, user_id)
        raise HTTPException(503, UNAVAILABLE) from None


async def owned_run(store: RunStore, user_id: str, run_id: str) -> RunRow:
    """The caller's run, or a 404. Always the first call of every /runs/{id}/... route."""
    canonical = canonical_uuid(run_id)
    run = await db_call(store.get_run(user_id, canonical), "read a run", user_id)
    if run is None:
        raise HTTPException(404, RUN_NOT_FOUND)
    return run


async def pending_for_answer(store: RunStore, user_id: str, run_id: str, kind: str, pause_token: str) -> Pending:
    """The pause the caller is answering. 404 if the run isn't theirs; 409 unless the run is still waiting on
    exactly this kind of pause and `pause_token` names it (a stale tab or a double click lands here)."""
    pending = await db_call(store.pending(user_id, canonical_uuid(run_id)), "read a pending question", user_id)
    if pending is None:
        raise HTTPException(404, RUN_NOT_FOUND)
    if not pending.waiting or pending.kind != kind or pending.pause_token != pause_token:
        raise HTTPException(409, STALE_PAUSE)
    return pending


async def submit_answer(store: RunStore, user_id: str, run_id: str, value: dict[str, Any], pause_token: str) -> None:
    stored = await db_call(
        store.store_answer(user_id, canonical_uuid(run_id), value, pause_token), "save an answer", user_id
    )
    if not stored:
        raise HTTPException(409, STALE_PAUSE)


_MESSAGES = {
    "queued": "Waiting for a free worker to start your run.",
    "running": "Your run is in progress.",
    "awaiting_approval": "Your run is waiting for you. Check the questions or approvals on this run.",
    "done": "Your run finished.",
    "failed": "Your run could not finish.",
    "cancelled": "This run was cancelled.",
}


def status_message(run: RunRow) -> str:
    if run.status == "failed" and run.error:
        return run.error
    if run.cancel_requested and run.status in ("queued", "running", "awaiting_approval"):
        return "Cancelling this run. It will stop shortly."
    return _MESSAGES.get(run.status, "Your run is in progress.")


def metrics_from_state(state: dict[str, Any]) -> list[IterationMetrics]:
    out: list[IterationMetrics] = []
    for item in state.get("metrics") or []:
        try:
            out.append(IterationMetrics.model_validate(item))
        except Exception:
            logger.warning("Skipping a metrics entry that does not match IterationMetrics")
    return sorted(out, key=lambda m: m.iteration)


async def run_status(store: RunStore, run: RunRow, user_id: str, with_metrics: bool = True) -> RunStatus:
    metrics: list[IterationMetrics] = []
    if with_metrics and run.status != "queued":
        try:
            metrics = metrics_from_state(await store.load_state(run.id))
        except Exception:
            logger.exception("Could not read the metrics of run %s; showing the run without them", run.id)
    return RunStatus(
        id=run.id,
        project_id=run.project_id,
        status=run.status,  # type: ignore[arg-type]
        message=status_message(run),
        cost_usd=run.cost_usd,
        cost_cap_usd=run.cost_cap_usd,
        preview_url=run.preview_url,
        created_at=run.created_at,
        cancel_requested=run.cancel_requested,
        error=run.error,
        metrics=metrics,
    )


# ---------------------------------------------------------------- routes


@router.post("/projects/{project_id}/runs", response_model=RunStatus, status_code=201)
async def start_run(project_id: str, user: CurrentUserDep, _quota: RunQuotaDep, store: RunStoreDep) -> RunStatus:
    """Start a run: quota check (3 a day per user plus the global cap), then enqueue for a worker."""
    canonical = canonical_uuid(project_id, PROJECT_NOT_FOUND)
    try:
        cap = run_cost_cap_usd()
    except ValueError:
        logger.exception("RUN_COST_CAP_USD is invalid; refusing new runs")
        raise HTTPException(503, "Starting new runs is switched off right now. You can still watch the demo.") from None
    run = await db_call(store.create_run(user.id, canonical, cap), "start a run", user.id)
    if isinstance(run, QuotaRefused):  # lost a race that the dependency's earlier check could not see
        raise HTTPException(429, run.message)
    if run is None:
        raise HTTPException(404, PROJECT_NOT_FOUND)
    return await run_status(store, run, user.id, with_metrics=False)


@router.get("/runs/{run_id}", response_model=RunStatus)
async def get_run(run_id: str, user: CurrentUserDep, store: RunStoreDep) -> RunStatus:
    return await run_status(store, await owned_run(store, user.id, run_id), user.id)


@router.post("/runs/{run_id}/cancel", response_model=RunStatus)
async def cancel_run(run_id: str, user: CurrentUserDep, store: RunStoreDep) -> RunStatus:
    run = await owned_run(store, user.id, run_id)
    if run.status in TERMINAL:
        raise HTTPException(409, "That run has already finished, so there is nothing to cancel.")
    updated = await db_call(store.request_cancel(user.id, run.id), "cancel a run", user.id)
    if updated is None:
        raise HTTPException(404, RUN_NOT_FOUND)
    return await run_status(store, updated, user.id, with_metrics=False)
