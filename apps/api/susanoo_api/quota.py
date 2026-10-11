"""Daily run quota and global spending cap. [PRD › Connectors and API budget › Anthropic budget]

`enforce_run_quota` is the dependency for Muaaz's `POST /projects/{id}/runs`: each user may start
RUNS_PER_USER_PER_DAY runs (default 3) per UTC day, and nobody may start one once the platform's spend
today reaches GLOBAL_DAILY_CAP_USD. Past either limit it answers 429 in plain English. /demo/* never
depends on this, so demo mode stays available.

Counts come from the runs table itself (runs created since midnight UTC), so there is no counter to reset.
"""

import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Annotated, Protocol

from fastapi import Depends, HTTPException

from susanoo.db import session
from susanoo_api.deps import CurrentUserDep
from susanoo_api.models import RunQuota

logger = logging.getLogger("susanoo_api.quota")

DEFAULT_RUNS_PER_DAY = 3
_UNAVAILABLE = "We couldn't check how many runs you have left today. Please try again in a minute."
_warned_no_cap = False
_MISCONFIGURED = "Starting new runs is switched off right now. Please try again later, or watch the demo."


class QuotaStore(Protocol):
    async def runs_started_since(self, user_id: str, since: datetime) -> int: ...
    async def spend_since_usd(self, since: datetime) -> float: ...


class PostgresQuotaStore:
    async def runs_started_since(self, user_id: str, since: datetime) -> int:
        async with session() as conn:
            cur = await conn.execute(
                """
                select count(*) as n from public.runs r
                join public.projects p on p.id = r.project_id
                where p.user_id = %s and r.created_at >= %s
                """,
                (user_id, since),
            )
            row = await cur.fetchone()
            return int(row["n"]) if row else 0

    async def spend_since_usd(self, since: datetime) -> float:
        """What every run started today has cost so far, across all users."""
        async with session() as conn:
            cur = await conn.execute(
                "select coalesce(sum(cost_usd), 0) as usd from public.runs where created_at >= %s", (since,)
            )
            row = await cur.fetchone()
            return float(row["usd"]) if row else 0.0


def quota_store() -> QuotaStore:
    """Tests override this dependency with an in-memory store."""
    return PostgresQuotaStore()


QuotaStoreDep = Annotated[QuotaStore, Depends(quota_store)]


def _start_of_day(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def _runs_per_day() -> int:
    raw = os.environ.get("RUNS_PER_USER_PER_DAY", "").strip()
    if not raw:
        return DEFAULT_RUNS_PER_DAY
    value = int(raw)  # ValueError on a typo; the caller refuses runs rather than guess
    if value < 0:
        raise ValueError("RUNS_PER_USER_PER_DAY must not be negative")
    return value


def _global_cap_usd() -> float | None:
    raw = os.environ.get("GLOBAL_DAILY_CAP_USD", "").strip()
    if not raw:
        return None
    value = float(raw)
    if not value >= 0:  # also rejects nan
        raise ValueError("GLOBAL_DAILY_CAP_USD must be a non-negative number")
    return value


async def current_quota(user_id: str, store: QuotaStore, now: datetime | None = None) -> RunQuota:
    """The caller's runs left today. Raises a plain-English 503 if the limits or the counts can't be read."""
    since = _start_of_day(now or datetime.now(UTC))
    try:
        per_day, cap = _runs_per_day(), _global_cap_usd()
    except ValueError:
        logger.exception("Run quota settings are invalid; refusing new runs until they are fixed")
        raise HTTPException(503, _MISCONFIGURED) from None
    global _warned_no_cap
    if cap is None and not _warned_no_cap:
        logger.warning("GLOBAL_DAILY_CAP_USD is not set; only the per-user daily run limit applies")
        _warned_no_cap = True
    try:
        used = await store.runs_started_since(user_id, since)
        paused = cap is not None and await store.spend_since_usd(since) >= cap
    except Exception:
        logger.exception("Could not read the run quota for user %s", user_id)
        raise HTTPException(503, _UNAVAILABLE) from None
    return RunQuota(
        runs_per_day=per_day,
        runs_used_today=used,
        runs_left_today=0 if paused else max(per_day - used, 0),
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
"""For Muaaz's start-run route: `async def start_run(project_id: str, user: CurrentUserDep, _: RunQuotaDep)`."""
