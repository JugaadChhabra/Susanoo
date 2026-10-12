"""Daily run quota and global spending cap. [PRD › Connectors and API budget › Anthropic budget]

`enforce_run_quota` is the dependency for Muaaz's `POST /projects/{id}/runs`: each user may start
RUNS_PER_USER_PER_DAY runs (default 3) per UTC day, and nobody may start one once the platform's spend
today reaches GLOBAL_DAILY_CAP_USD. Past either limit it answers 429 in plain English. /demo/* never
depends on this, so demo mode stays available. Every run counts: Susanoo's own heads always use the
platform key (BYOK only covers the generated agent's calls, [PRD › Connectors and API budget]).

Counts come from the runs table itself (runs created since midnight UTC), so there is no counter to reset.
A queued run counts the moment it is inserted. The start-run route checks again inside the transaction that
inserts the run (runs.PostgresRunStore.create_run), so two requests racing in the same instant can't both pass.
"""

import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Annotated, NamedTuple, Protocol

from fastapi import Depends, HTTPException

from susanoo.db import session
from susanoo_api.deps import CurrentUserDep
from susanoo_api.models import RunQuota

logger = logging.getLogger("susanoo_api.quota")

DEFAULT_RUNS_PER_DAY = 3
_UNAVAILABLE = "We couldn't check how many runs you have left today. Please try again in a minute."
_SWITCHED_OFF = "Starting new runs is switched off right now. You can still watch the demo."


class Usage(NamedTuple):
    runs_today: int  # runs this user started since midnight UTC
    spend_usd: float  # platform spend counted against today's global cap


class QuotaStore(Protocol):
    async def usage_since(self, user_id: str, since: datetime) -> Usage: ...


# Every run created today counts towards the user's quota, whatever its status (queued and cancelled ones too).
# Runs not yet finished (queued, running, paused) count at their cost cap against the global cap, whenever they
# started: that much may still be spent. runs.PostgresRunStore re-runs this inside its insert transaction.
USAGE_SQL = """
    select
        (select count(*) from public.runs r join public.projects p on p.id = r.project_id
         where p.user_id = %(user)s and r.created_at >= %(since)s) as runs_today,
        (select coalesce(sum(case when status in ('queued', 'running', 'awaiting_approval')
                                  then greatest(cost_usd, cost_cap_usd) else cost_usd end), 0)
         from public.runs
         where created_at >= %(since)s or status in ('queued', 'running', 'awaiting_approval')) as spend_usd
"""


class PostgresQuotaStore:
    async def usage_since(self, user_id: str, since: datetime) -> Usage:
        async with session() as conn:
            cur = await conn.execute(USAGE_SQL, {"user": user_id, "since": since})
            row = await cur.fetchone()
            assert row is not None
            return Usage(int(row["runs_today"]), float(row["spend_usd"]))


def quota_store() -> QuotaStore:
    """Tests override this dependency with an in-memory store."""
    return PostgresQuotaStore()


QuotaStoreDep = Annotated[QuotaStore, Depends(quota_store)]


def _start_of_day(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def _limits() -> tuple[int, float]:
    """(runs per user per day, global daily cap in USD). Raises ValueError when either is missing or invalid."""
    raw_runs = os.environ.get("RUNS_PER_USER_PER_DAY", "").strip()
    runs = int(raw_runs) if raw_runs else DEFAULT_RUNS_PER_DAY
    if runs < 1:
        raise ValueError("RUNS_PER_USER_PER_DAY must be at least 1")
    # No default: the PRD requires a global cap, so an unset one refuses runs rather than allow unlimited spend.
    cap = float(os.environ.get("GLOBAL_DAILY_CAP_USD", "").strip())
    if not cap >= 0:  # also rejects nan
        raise ValueError("GLOBAL_DAILY_CAP_USD must be a non-negative number")
    return runs, cap


async def current_quota(user_id: str, store: QuotaStore, now: datetime | None = None) -> RunQuota:
    """The caller's runs left today. Raises a plain-English 503 if the limits or the counts can't be read."""
    since = _start_of_day(now or datetime.now(UTC))
    try:
        per_day, cap = _limits()
    except ValueError:
        logger.exception("RUNS_PER_USER_PER_DAY / GLOBAL_DAILY_CAP_USD are missing or invalid; refusing new runs")
        raise HTTPException(503, _SWITCHED_OFF) from None
    try:
        usage = await store.usage_since(user_id, since)
    except Exception:
        logger.exception("Could not read the run quota for user %s", user_id)
        raise HTTPException(503, _UNAVAILABLE) from None
    paused = usage.spend_usd >= cap
    return RunQuota(
        runs_per_day=per_day,
        runs_used_today=usage.runs_today,
        runs_left_today=0 if paused else max(per_day - usage.runs_today, 0),
        paused=paused,
        resets_at=(since + timedelta(days=1)).isoformat(),
    )


async def enforce_run_quota(user: CurrentUserDep, store: QuotaStoreDep) -> RunQuota:
    """Let a run start only if the caller has runs left today and the platform is under its daily cap."""
    quota = await current_quota(user.id, store)
    if quota.paused:
        raise HTTPException(
            429,
            "Susanoo has reached its spending limit for today, so new runs are paused until midnight UTC. "
            "You can still watch the demo.",
        )
    if quota.runs_left_today == 0:
        runs = "run" if quota.runs_per_day == 1 else "runs"
        raise HTTPException(
            429,
            f"You've used all {quota.runs_per_day} {runs} for today. "
            "You can start another after midnight UTC, or watch the demo in the meantime.",
        )
    return quota


RunQuotaDep = Annotated[RunQuota, Depends(enforce_run_quota)]
"""The start-run route's dependency: `async def start_run(project_id: str, user: CurrentUserDep, _: RunQuotaDep)`."""
