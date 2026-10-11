"""GET /runs/{id}/events: the live event log over SSE. [PRD › Frontend and API], [PRD › Data model]

Streams the run's RUN_EVENTS rows in `seq` order as RunEvent JSON (`message` is plain English). Each frame carries
`id: <seq>`, so a dropped connection resumes with the browser's automatic `Last-Event-ID` header (or `?after=<seq>`,
also accepted as `?after_seq=`) and misses nothing. A comment line goes out every ~15 s so proxies keep the
connection open, and the stream ends once the run is done, failed or cancelled and every event has been sent.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, Header, Query
from fastapi.responses import StreamingResponse

from susanoo_api.deps import CurrentUserDep
from susanoo_api.routers.runs import TERMINAL, RunStore, RunStoreDep, owned_run

router = APIRouter(prefix="/runs", tags=["events"])

logger = logging.getLogger("susanoo_api.events")

POLL_SECONDS = 1.0  # how often the database is checked for new events
KEEPALIVE_SECONDS = 15.0  # a comment line at least this often while nothing else is sent
MAX_STREAM_SECONDS = 3600.0  # then close; the browser reconnects with Last-Event-ID
BATCH = 200


async def event_stream(store: RunStore, user_id: str, run_id: str, after: int) -> AsyncIterator[str]:
    """SSE frames for one run, starting after event `after`. Ends when the run is over and fully sent."""
    yield "retry: 3000\n\n"  # browsers wait 3 s before reconnecting
    last = after
    began = last_sent = time.monotonic()
    while time.monotonic() - began < MAX_STREAM_SECONDS:
        try:
            batch = await store.poll_events(user_id, run_id, last, BATCH)
        except Exception:
            logger.exception("Event stream for run %s hit a database error; closing so the client reconnects", run_id)
            return
        if batch is None:
            return
        for event in batch.events:
            yield f"id: {event.seq}\ndata: {event.model_dump_json()}\n\n"
            last = event.seq
            last_sent = time.monotonic()
        if not batch.events and batch.status in TERMINAL:
            return  # status was read before the events, so nothing more can still be coming
        if len(batch.events) == BATCH:
            continue  # more are waiting; don't sleep
        if time.monotonic() - last_sent >= KEEPALIVE_SECONDS:
            yield ": keep-alive\n\n"
            last_sent = time.monotonic()
        await asyncio.sleep(POLL_SECONDS)


def _resume_point(after: int | None, after_seq: int | None, last_event_id: str | None) -> int:
    if after is not None:
        return after
    if after_seq is not None:
        return after_seq
    try:
        return max(int((last_event_id or "").strip()), 0)
    except ValueError:
        return 0


@router.get("/{run_id}/events")
async def stream_events(
    run_id: str,
    user: CurrentUserDep,
    store: RunStoreDep,
    after: int | None = Query(None, ge=0),
    after_seq: int | None = Query(None, ge=0),
    last_event_id: str | None = Header(None),
) -> StreamingResponse:
    run = await owned_run(store, user.id, run_id)  # 404 for someone else's run, before any stream starts
    start = _resume_point(after, after_seq, last_event_id)
    return StreamingResponse(
        event_stream(store, user.id, run.id, start),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # nginx-style proxies; Caddy needs `flush_interval -1` on the reverse_proxy
            "Connection": "keep-alive",
        },
    )
