"""SQL for the run queue. [PRD > Tech stack] (job queue = Postgres table), docs/proposals/006_run_queue.sql

Every function opens its own short transaction through `susanoo.db.session()`. Updates that a worker makes to
a run it owns are fenced with `claimed_by = <worker id>`: if another worker has taken the run over (this one
stalled past the stale window), the update touches nothing and the caller learns it lost the run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from psycopg.types.json import Jsonb

from susanoo.db import session


@dataclass(frozen=True)
class ClaimedRun:
    id: str
    spec: str
    prev_status: str  # 'queued' (new, or answered after a pause) or 'running' (its worker crashed)
    attempts: int  # claims since the last pause, including this one
    cost_usd: float
    resume_value: Any  # the human's answer, if the API stored one
    paused_checkpoint_id: str | None  # the checkpoint the stored answer belongs to
    connectors: list[str] = field(default_factory=list)  # connector names the API copied from the project


_CLAIM = """
with next as (
    select r.id, r.status as prev_status
    from public.runs r
    where not r.cancel_requested
      and (r.status = 'queued'
           or (r.status = 'running'
               and coalesce(r.heartbeat_at, r.created_at) < now() - make_interval(secs => %(stale)s)))
    order by r.created_at
    limit 1
    for update skip locked
)
update public.runs r
   set status = 'running', claimed_by = %(me)s, claimed_at = now(), heartbeat_at = now(),
       attempts = r.attempts + 1, error = null
  from next
 where r.id = next.id
returning r.id::text as id, next.prev_status, r.attempts, r.cost_usd::float as cost_usd,
          r.resume_value, r.paused_checkpoint_id, r.connectors,
          (select p.spec from public.projects p where p.id = r.project_id) as spec
"""


async def claim(worker_id: str, stale_seconds: float) -> ClaimedRun | None:
    """Atomically take the oldest claimable run: a queued one, or a running one whose heartbeat went stale.

    `FOR UPDATE SKIP LOCKED` makes concurrent workers skip rows another worker is claiming, so two workers
    never take the same run. Runs with a cancel request are left for `sweep_cancelled`.
    """
    async with session() as conn:
        cur = await conn.execute(_CLAIM, {"me": worker_id, "stale": stale_seconds})
        row = await cur.fetchone()
    if row is None:
        return None
    return ClaimedRun(
        id=row["id"],
        spec=row["spec"],
        prev_status=row["prev_status"],
        attempts=row["attempts"],
        cost_usd=row["cost_usd"],
        resume_value=row["resume_value"],
        paused_checkpoint_id=row["paused_checkpoint_id"],
        connectors=list(row["connectors"] or []),
    )


async def sweep_cancelled(stale_seconds: float) -> list[str]:
    """Cancel runs nobody is executing (queued, awaiting an answer, or orphaned by a crash). Returns their ids."""
    async with session() as conn:
        cur = await conn.execute(
            """
            update public.runs
               set status = 'cancelled', claimed_by = null, heartbeat_at = null,
                   resume_value = null, paused_checkpoint_id = null
             where cancel_requested
               and (status in ('queued', 'awaiting_approval')
                    or (status = 'running'
                        and coalesce(heartbeat_at, created_at) < now() - make_interval(secs => %(stale)s)))
            returning id::text as id
            """,
            {"stale": stale_seconds},
        )
        return [r["id"] for r in await cur.fetchall()]


async def heartbeat(run_id: str, worker_id: str) -> bool | None:
    """Prove the worker is alive. Returns whether a cancel was requested, or None if this worker lost the run."""
    async with session() as conn:
        cur = await conn.execute(
            """
            update public.runs set heartbeat_at = now()
             where id = %s and claimed_by = %s and status = 'running'
            returning cancel_requested
            """,
            (run_id, worker_id),
        )
        row = await cur.fetchone()
    return None if row is None else bool(row["cancel_requested"])


async def cancel_requested(run_id: str) -> bool:
    async with session() as conn:
        cur = await conn.execute("select cancel_requested from public.runs where id = %s", (run_id,))
        row = await cur.fetchone()
    return bool(row and row["cancel_requested"])


async def record_cost(run_id: str, worker_id: str, cost_usd: float) -> None:
    async with session() as conn:
        await conn.execute(
            "update public.runs set cost_usd = %s where id = %s and claimed_by = %s",
            (round(max(cost_usd, 0.0), 4), run_id, worker_id),
        )


async def finish(run_id: str, worker_id: str, status: str, cost_usd: float, error: str | None = None) -> bool:
    """End the run ('done', 'failed' or 'cancelled'). False means this worker no longer owns it."""
    async with session() as conn:
        cur = await conn.execute(
            """
            update public.runs
               set status = %s, cost_usd = %s, error = %s, claimed_by = null, heartbeat_at = null,
                   resume_value = null, paused_checkpoint_id = null
             where id = %s and claimed_by = %s and status = 'running'
            """,
            (status, round(max(cost_usd, 0.0), 4), error, run_id, worker_id),
        )
        return cur.rowcount > 0


async def set_preview_url(run_id: str, worker_id: str, url: str) -> bool:
    """Record where the finished run's app is live (preview.py). False means this worker no longer owns it."""
    async with session() as conn:
        cur = await conn.execute(
            "update public.runs set preview_url = %s where id = %s and claimed_by = %s and status = 'running'",
            (url, run_id, worker_id),
        )
        return cur.rowcount > 0


async def pause(run_id: str, worker_id: str, checkpoint_id: str, cost_usd: float) -> bool:
    """Park the run until a human answers: 'awaiting_approval', claim released, answer slot cleared."""
    async with session() as conn:
        cur = await conn.execute(
            """
            update public.runs
               set status = 'awaiting_approval', cost_usd = %s, claimed_by = null, heartbeat_at = null,
                   resume_value = null, paused_checkpoint_id = %s, attempts = 0
             where id = %s and claimed_by = %s and status = 'running'
            """,
            (round(max(cost_usd, 0.0), 4), checkpoint_id, run_id, worker_id),
        )
        return cur.rowcount > 0


async def requeue(run_id: str, worker_id: str) -> bool:
    """Hand a run back (worker shutting down). It resumes from its checkpoint on the next claim."""
    async with session() as conn:
        cur = await conn.execute(
            """
            update public.runs
               set status = 'queued', claimed_by = null, heartbeat_at = null, attempts = greatest(attempts - 1, 0)
             where id = %s and claimed_by = %s and status = 'running'
            """,
            (run_id, worker_id),
        )
        return cur.rowcount > 0


@dataclass(frozen=True)
class PendingPause:
    """What the API shows a human who must answer, and the token they must send back with the answer."""

    run_id: str
    kind: str  # 'questions' | 'scenarios' | 'gate'
    payload: dict  # the interrupt payload the UI renders
    pause_token: str  # echo this in store_resume_value; it names this exact pause


@dataclass(frozen=True)
class AnswerResult:
    stored: bool
    message: str  # plain English, safe to show; the API maps stored=False to HTTP 409

    def __bool__(self) -> bool:
        return self.stored


async def pending_pause(run_id: str) -> PendingPause | None:
    """The pause a run is waiting on (None if it is not awaiting an answer). The API returns `pause_token` to
    the browser, and the browser sends it back with the answer."""
    async with session() as conn:
        cur = await conn.execute(
            """
            select r.paused_checkpoint_id as token,
                   (select e.payload from public.run_events e
                     where e.run_id = r.id and e.type in ('question_needed', 'approval_needed')
                     order by e.seq desc limit 1) as payload
              from public.runs r
             where r.id = %s and r.status = 'awaiting_approval' and r.paused_checkpoint_id is not null
            """,
            (run_id,),
        )
        row = await cur.fetchone()
    if row is None or not isinstance(row["payload"], dict) or "kind" not in row["payload"]:
        return None
    return PendingPause(run_id, row["payload"]["kind"], row["payload"], row["token"])


async def store_resume_value(run_id: str, value: Any, pause_token: str) -> AnswerResult:
    """What the API does when a human answers: queue the run with the answer, but only for the pause the human saw.

    `pause_token` is mandatory and is matched in the same UPDATE, so a late or duplicate answer (a stale tab
    answering an earlier gate) can never be applied to a later pause. Fails closed: no match, nothing stored.
    """
    async with session() as conn:
        cur = await conn.execute(
            "update public.runs set status = 'queued', resume_value = %s"
            " where id = %s and status = 'awaiting_approval' and paused_checkpoint_id = %s",
            (Jsonb(value), run_id, pause_token),
        )
        if cur.rowcount > 0:
            return AnswerResult(True, "Got your answer. The run is continuing.")
        cur = await conn.execute("select status from public.runs where id = %s", (run_id,))
        row = await cur.fetchone()
    if row is None:
        return AnswerResult(False, "That run could not be found.")
    if row["status"] == "awaiting_approval":
        return AnswerResult(
            False,
            "Your answer was for an earlier step, and the run has moved on to a new question. "
            "Nothing was changed; please look at the new question and answer it.",
        )
    return AnswerResult(False, "This run is not waiting for an answer right now, so nothing was changed.")
