"""GET /demo/{run_id}: replay a saved run. Public by design: no auth, no quota. [PRD › Frontend and API]

Only runs listed in DEMO_RUN_IDS (comma-separated UUIDs) can be replayed, so this route can never
expose a real user's run. Muaaz saves a run with scripts/save_demo_run.py and tells Jugaad the id.
"""

import os
from uuid import UUID

from fastapi import APIRouter, HTTPException

from susanoo.db import session
from susanoo.schemas import RunEvent

router = APIRouter(prefix="/demo", tags=["demo"])

_NOT_FOUND = "We couldn't find that demo run."


def demo_run_ids() -> set[str]:
    raw = os.environ.get("DEMO_RUN_IDS", "")
    ids: set[str] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(str(UUID(part)))
        except ValueError:
            continue  # a typo in the env var must not open anything up
    return ids


async def load_events(run_id: str) -> list[RunEvent]:
    """Stored RUN_EVENTS for one run, in seq order. Tests replace this."""
    async with session() as conn:
        cur = await conn.execute(
            """
            select run_id::text, seq, head, type, message, payload, trace_url, at::text
            from public.run_events where run_id = %s order by seq
            """,
            (run_id,),
        )
        return [RunEvent(**row) for row in await cur.fetchall()]


@router.get("/{run_id}", response_model=list[RunEvent])
async def get_demo_run(run_id: str) -> list[RunEvent]:
    try:
        canonical = str(UUID(run_id))
    except ValueError:
        raise HTTPException(404, _NOT_FOUND) from None
    if canonical not in demo_run_ids():
        raise HTTPException(404, _NOT_FOUND)
    events = await load_events(canonical)
    if not events:
        raise HTTPException(404, _NOT_FOUND)
    return events
