"""Queue poller that runs the graph. [PRD > Tech stack], [PRD > LangGraph flow and shared state]

Loop: claim a queued (or crashed) run with SELECT ... FOR UPDATE SKIP LOCKED, run it on the Postgres
checkpointer until it finishes, pauses for a human, is cancelled, or fails. See run_driver.py for the
outcomes and docs/proposals/006_run_queue.sql for the columns this relies on.

Worker flow for a human answer: the graph pauses -> status 'awaiting_approval' (not claimable) -> the API
stores the validated answer and sets status 'queued' (store.store_resume_value) -> a worker claims it and
resumes the same thread with Command(resume=answer). The answer is bound to the pause the human saw: the API
reads `store.pending_pause(run_id)` (which carries `pause_token`), the browser echoes the token back, and
`store.store_resume_value(run_id, value, pause_token)` refuses a stale token (HTTP 409 with its plain-English
message), so a late or duplicate answer can never be applied to a later pause.

DATABASE_URL must be a direct or session-mode Postgres connection (port 5432), not Supabase's transaction pooler
(port 6543): startup holds a session advisory lock and LangGraph's setup() runs CREATE INDEX CONCURRENTLY. The
worker refuses to start on port 6543.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket
import uuid
from collections.abc import Callable

import psycopg
from susanoo_worker import store
from susanoo_worker.config import WorkerConfig, require_session_connection
from susanoo_worker.run_driver import CANCELLED_MESSAGE, RunSession

from susanoo.db import emit_event
from susanoo.graph import Heads, real_heads
from susanoo.graph.checkpointer import postgres_checkpointer

log = logging.getLogger("susanoo_worker")

SETUP_LOCK_KEY = 727_001  # serialises LangGraph's checkpoint table setup when several workers boot together
MAX_ATTEMPTS_MESSAGE = (
    "This run was interrupted several times in a row and has been stopped so it does not keep failing. "
    "You can start it again."
)


class Worker:
    def __init__(
        self,
        config: WorkerConfig | None = None,
        heads_factory: Callable[[], Heads] | None = None,
        *,
        worker_id: str | None = None,
    ) -> None:
        self.config = config or WorkerConfig.from_env()
        self.heads_factory = heads_factory or real_heads
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self._stop = asyncio.Event()
        self._sessions: dict[str, RunSession] = {}
        self._tasks: dict[str, asyncio.Task[str]] = {}

    # ------------------------------------------------------------------ lifecycle

    def request_stop(self) -> None:
        """Stop claiming new runs; `run()` then drains in-flight runs and returns."""
        self._stop.set()

    def install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.request_stop)

    def remove_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(Exception):
                loop.remove_signal_handler(sig)

    @property
    def active_run_ids(self) -> list[str]:
        return list(self._tasks)

    async def run(self) -> None:
        await self._setup_checkpoint_tables()
        log.info("Worker %s started (concurrency %d).", self.worker_id, self.config.concurrency)
        try:
            while not self._stop.is_set():
                claimed = False
                if len(self._tasks) < self.config.concurrency:
                    try:
                        claimed = await self._poll_once()
                    except Exception as exc:  # DB down or similar: back off and keep polling
                        log.error("Polling the run queue failed (%s); will retry.", type(exc).__name__)
                if not claimed:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._stop.wait(), self.config.poll_seconds)
        finally:
            await self._drain()
            log.info("Worker %s stopped.", self.worker_id)

    # ------------------------------------------------------------------ polling

    async def _setup_checkpoint_tables(self) -> None:
        # A session-level advisory lock on an autocommit connection serialises setup() across workers booting
        # together. It must not be a transaction lock: setup() runs CREATE INDEX CONCURRENTLY, which waits for
        # every other open transaction on the database, including ours (a deadlock).
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise RuntimeError("DATABASE_URL is not set; point it at the Supabase Postgres connection string")
        require_session_connection(url)
        async with await psycopg.AsyncConnection.connect(url, autocommit=True) as lock_conn:
            # Poll with try_lock instead of blocking in pg_advisory_lock: a blocked statement is an open
            # transaction, and the lock holder's CREATE INDEX CONCURRENTLY would wait for it forever.
            while True:
                cur = await lock_conn.execute("select pg_try_advisory_lock(%s) as got", (SETUP_LOCK_KEY,))
                row = await cur.fetchone()
                if row and row[0]:
                    break
                await asyncio.sleep(0.2)
            try:
                async with postgres_checkpointer(run_setup=True):
                    pass
            finally:
                await lock_conn.execute("select pg_advisory_unlock(%s)", (SETUP_LOCK_KEY,))

    async def _poll_once(self) -> bool:
        """Cancel what can be cancelled, then claim at most one run. True if a run was started."""
        for run_id in await store.sweep_cancelled(self.config.stale_seconds):
            await self._announce_cancelled(run_id)
        run = await store.claim(self.worker_id, self.config.stale_seconds)
        if run is None:
            return False
        if run.attempts > self.config.max_attempts:
            await emit_event(run.id, "system", "error", MAX_ATTEMPTS_MESSAGE, {"attempts": run.attempts})
            await store.finish(run.id, self.worker_id, "failed", run.cost_usd, MAX_ATTEMPTS_MESSAGE)
            return True
        session_ = RunSession(run, worker_id=self.worker_id, config=self.config, heads_factory=self.heads_factory)
        self._sessions[run.id] = session_
        task = asyncio.create_task(session_.execute(), name=f"run-{run.id}")
        self._tasks[run.id] = task
        task.add_done_callback(lambda t, rid=run.id: self._on_done(rid, t))
        log.info("Claimed run %s (attempt %d, was %s).", run.id, run.attempts, run.prev_status)
        return True

    async def _announce_cancelled(self, run_id: str) -> None:
        """The run is already 'cancelled'; its event must not be lost to a database blip, so retry once."""
        for attempt in (1, 2):
            try:
                await emit_event(run_id, "system", "finished", CANCELLED_MESSAGE, {"cancelled": True})
                return
            except Exception as exc:
                log.warning(
                    "Could not record the cancel event for run %s (%s, try %d).", run_id, type(exc).__name__, attempt
                )
                if attempt == 1:
                    await asyncio.sleep(0.5)

    def _on_done(self, run_id: str, task: asyncio.Task[str]) -> None:
        self._tasks.pop(run_id, None)
        self._sessions.pop(run_id, None)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            # A crash-like BaseException or a failed status write: the row stays 'running' and is reclaimed
            # after the stale window and resumed from its checkpoint.
            log.error("Run %s ended without recording a status (%s); it will be resumed.", run_id, type(exc).__name__)
        else:
            log.info("Run %s -> %s", run_id, task.result())

    async def _drain(self) -> None:
        """Give in-flight runs the grace period, then hand any still running back to the queue."""
        if not self._tasks:
            return
        pending = list(self._tasks.values())
        _, still = await asyncio.wait(pending, timeout=self.config.shutdown_grace_seconds)
        for session_ in list(self._sessions.values()):
            session_.request_shutdown()
        if still:
            await asyncio.wait(still, timeout=30)


async def serve() -> None:
    from susanoo.sandbox.docker import reap_forever

    worker = Worker()
    worker.install_signal_handlers()
    reaper = asyncio.create_task(reap_forever(worker.config.reaper_interval_seconds))
    try:
        await worker.run()
    finally:
        reaper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reaper
        worker.remove_signal_handlers()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from susanoo.integrations import tracing

    tracing.setup()
    asyncio.run(serve())


if __name__ == "__main__":
    main()
