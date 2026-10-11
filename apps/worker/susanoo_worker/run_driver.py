"""Run one claimed run to its next stopping point. [PRD > LangGraph flow and shared state]

A run stops in exactly one of these ways, and each writes one status:

  finished  the graph reached Done            -> 'done' or 'failed' (as the graph decided, e.g. a budget stop)
  paused    the graph hit an interrupt        -> 'awaiting_approval' (not claimable until the API stores an answer)
  cancelled cancel_requested was set          -> 'cancelled'
  failed    a node raised (not budget)        -> 'failed' + plain-English error event
  requeued  the worker is shutting down       -> 'queued' (resumes from its checkpoint on the next claim)
  lost      another worker took the run over  -> no write at all; that worker owns the run now

A hard crash (process killed, power loss) writes nothing: the heartbeat stops, the row stays 'running', and
after the stale window another claim resumes the run from its last checkpoint (thread_id = run id).
A custom BaseException escaping a node behaves the same on purpose; tests use it to simulate a crash.

Failure modes to know about (at-least-once, not exactly-once):
  - The node that was executing when the worker died runs again from its top. Its `started` event repeats, and
    side effects inside that one node repeat. Nodes that had already checkpointed never run again.
  - A worker that stalls (not dead) past the stale window can keep running a node while a second worker
    resumes the same run. The stalled worker's next heartbeat finds it lost the claim and cancels itself, but
    the overlap window is real. Keep stale_seconds well above the worst event-loop stall.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langgraph.types import Command
from susanoo_worker import store
from susanoo_worker.config import WorkerConfig

from susanoo.db import emit_event
from susanoo.graph import Heads, build_graph, initial_state, run_config
from susanoo.graph.checkpointer import postgres_checkpointer
from susanoo.graph.resume import pending_interrupt, validate_resume

log = logging.getLogger("susanoo_worker")

STALE_ANSWER_MESSAGE = "Your answer arrived for an earlier step, so please answer again."
CANCELLED_MESSAGE = "This run was cancelled, as you asked."
FAILED_MESSAGE = (
    "Something went wrong inside the run, so it could not continue. The team can see the details in the logs."
)


@dataclass
class Disposition:
    kind: str  # finished | paused | cancelled
    status: str | None = None  # finished: 'done' | 'failed'
    error: str | None = None
    checkpoint_id: str | None = None  # paused
    cost_usd: float = 0.0


def _checkpoint_id(snapshot: Any) -> str:
    return snapshot.config["configurable"]["checkpoint_id"]


def _interrupts(snapshot: Any) -> list[Any]:
    return [i for task in snapshot.tasks for i in task.interrupts]


def _finished(values: dict) -> Disposition:
    status = values.get("status")
    cost = float(values.get("cost_usd", 0.0))
    if status == "done":
        return Disposition("finished", "done", None, cost_usd=cost)
    if status == "failed":
        reason = (
            "The run reached its cost limit before the first test finished."
            if values.get("stop_reason") == "budget"
            else "The run ended without a usable result."
        )
        return Disposition("finished", "failed", reason, cost_usd=cost)
    return Disposition("finished", "failed", "The run ended in an unexpected state.", cost_usd=cost)


class RunSession:
    """Owns one claimed run: drives the graph, heartbeats, and writes the final status."""

    def __init__(
        self,
        run: store.ClaimedRun,
        *,
        worker_id: str,
        config: WorkerConfig,
        heads_factory: Callable[[], Heads],
    ) -> None:
        self.run = run
        self.worker_id = worker_id
        self.config = config
        self.heads_factory = heads_factory
        self.lost = False
        self.cancel = False
        self.shutdown = False
        self._cost = run.cost_usd
        self._drive_task: asyncio.Task[Disposition] | None = None

    # ------------------------------------------------------------------ public

    def request_shutdown(self) -> None:
        """Stop the graph now; `execute` hands the run back to the queue."""
        self.shutdown = True
        if self._drive_task and not self._drive_task.done():
            self._drive_task.cancel()

    async def execute(self) -> str:
        """Drive the run to a stopping point and record it. Returns the outcome name (for logs and tests)."""
        drive = self._drive_task = asyncio.create_task(self._drive())
        beat = asyncio.create_task(self._heartbeat())
        try:
            await asyncio.wait({drive})
        except asyncio.CancelledError:
            drive.cancel()
            raise
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat

        if drive.cancelled():
            if self.lost:
                log.warning("Run %s was taken over by another worker; stopping without writing.", self.run.id)
                return "lost"
            if self.cancel:
                return await self._cancelled()
            if self.shutdown:
                return await self._requeued()
            raise asyncio.CancelledError  # cancelled from outside with no reason we know: propagate
        exc = drive.exception()
        if exc is None:
            return await self._record(drive.result())
        if isinstance(exc, Exception):
            return await self._failed(exc)
        raise exc  # BaseException: behave like a crash, leave the row for stale reclaim

    # ------------------------------------------------------------------ recording

    async def _record(self, d: Disposition) -> str:
        rid, me = self.run.id, self.worker_id
        if d.kind == "cancelled":
            return await self._cancelled()
        if d.kind == "paused":
            assert d.checkpoint_id is not None
            owned = await store.pause(rid, me, d.checkpoint_id, d.cost_usd)
            return "paused" if owned else "lost"
        assert d.status is not None
        owned = await store.finish(rid, me, d.status, d.cost_usd, d.error)
        return d.status if owned else "lost"

    async def _cancelled(self) -> str:
        await emit_event(self.run.id, "system", "finished", CANCELLED_MESSAGE, {"cancelled": True})
        owned = await store.finish(self.run.id, self.worker_id, "cancelled", self._cost, None)
        return "cancelled" if owned else "lost"

    async def _requeued(self) -> str:
        await emit_event(
            self.run.id,
            "system",
            "progress",
            "The server is restarting. This run will pick up from its last saved step in a moment.",
        )
        owned = await store.requeue(self.run.id, self.worker_id)
        return "requeued" if owned else "lost"

    async def _failed(self, exc: Exception) -> str:
        log.error("Run %s failed in the graph", self.run.id, exc_info=exc)
        # The message is deliberately generic: exception text can contain connector credentials or user data.
        message = FAILED_MESSAGE
        await emit_event(self.run.id, "system", "error", message, {"error_type": type(exc).__name__})
        owned = await store.finish(self.run.id, self.worker_id, "failed", self._cost, message)
        return "failed" if owned else "lost"

    # ------------------------------------------------------------------ heartbeat

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(self.config.heartbeat_seconds)
            try:
                cancel = await store.heartbeat(self.run.id, self.worker_id)
            except Exception as exc:  # a DB blip must not kill the run; a long outage is handled by the stale window
                log.warning("Heartbeat for run %s failed (%s); will retry.", self.run.id, type(exc).__name__)
                continue
            if cancel is None:
                self.lost = True
            elif cancel:
                self.cancel = True
            else:
                continue
            if self._drive_task and not self._drive_task.done():
                self._drive_task.cancel()
            return

    # ------------------------------------------------------------------ the graph

    async def _drive(self) -> Disposition:
        run = self.run
        config = run_config(run.id)
        async with postgres_checkpointer(run_setup=False) as saver:
            graph = build_graph(self.heads_factory(), checkpointer=saver)
            snapshot = await graph.aget_state(config)
            payload: Any
            if snapshot.created_at is None:  # no checkpoint yet: a brand-new run
                payload = initial_state(run.id, run.spec)
            elif _interrupts(snapshot):
                bounced = await self._answer_or_park(graph, snapshot, config)
                if isinstance(bounced, Disposition):
                    return bounced
                payload = bounced
            elif snapshot.next:  # interrupted mid-run (crash or shutdown): continue from the checkpoint
                payload = None
                await emit_event(
                    run.id,
                    "system",
                    "progress",
                    "Picking this run back up after an interruption, from its last saved step.",
                    {"resumed_at": list(snapshot.next), "attempt": run.attempts},
                )
            else:  # the graph already finished; only the status write was lost
                return _finished(dict(snapshot.values))

            async for chunk in graph.astream(payload, config, stream_mode="updates"):
                for update in chunk.values():
                    if isinstance(update, dict) and "cost_usd" in update:
                        self._cost = float(update["cost_usd"])
                        await store.record_cost(run.id, self.worker_id, self._cost)
                if await store.cancel_requested(run.id):  # between graph steps
                    self.cancel = True
                    return Disposition("cancelled", cost_usd=self._cost)

            final = await graph.aget_state(config)
            self._cost = float(final.values.get("cost_usd", self._cost))
            if _interrupts(final):
                return Disposition("paused", checkpoint_id=_checkpoint_id(final), cost_usd=self._cost)
            if final.next:
                raise RuntimeError("The graph stopped before reaching Done and is not waiting for an answer")
            return _finished(dict(final.values))

    async def _answer_or_park(self, graph: Any, snapshot: Any, config: dict) -> Command | Disposition:
        """The thread sits at an interrupt: apply the stored answer if it is for this pause, else park it again."""
        run = self.run
        checkpoint = _checkpoint_id(snapshot)
        if run.resume_value is None:
            return Disposition("paused", checkpoint_id=checkpoint, cost_usd=self._cost)
        if run.paused_checkpoint_id != checkpoint:
            # An answer for an earlier pause (store_resume_value normally refuses these; this is the backstop for
            # a crash that moved the checkpoint before the status write). Never apply it, and say so.
            await emit_event(run.id, "system", "progress", STALE_ANSWER_MESSAGE, {})
            return Disposition("paused", checkpoint_id=checkpoint, cost_usd=self._cost)
        payload = await pending_interrupt(graph, {"configurable": config["configurable"]})
        try:
            validate_resume(payload["kind"], run.resume_value, payload)
        except ValueError as exc:  # the API validates first; this is the backstop. Plain English from resume.py
            await emit_event(
                run.id, "system", "error", f"That answer could not be used, so the run is still waiting: {exc}", {}
            )
            return Disposition("paused", checkpoint_id=checkpoint, cost_usd=self._cost)
        return Command(resume=run.resume_value)
