"""Shared pieces for worker tests: instrumented stub heads, a crash signal, and fast worker settings."""

from __future__ import annotations

import asyncio
import dataclasses
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any

from susanoo_worker import store
from susanoo_worker.config import WorkerConfig
from susanoo_worker.main import Worker

from susanoo.graph import Heads
from susanoo.graph import routing as r

NODE_OF = {
    "intake": r.INTAKE,
    "user_questions": r.USER_QUESTIONS,
    "scenario_approval": r.SCENARIO_APPROVAL,
    "planner": r.PLANNER,
    "builder": r.BUILDER,
    "agent_smith": r.AGENT_SMITH,
    "simulator": r.SIMULATOR,
    "runner": r.RUNNER,
    "diagnostician": r.DIAGNOSTICIAN,
    "human_gate": r.HUMAN_GATE,
    "fixer": r.FIXER,
    "verifier": r.VERIFIER,
    "done": r.DONE,
}

FAST = WorkerConfig(
    poll_seconds=0.1,
    heartbeat_seconds=0.2,
    stale_seconds=1.0,
    concurrency=2,
    max_attempts=3,
    shutdown_grace_seconds=0.5,
)
TERMINAL = ("done", "failed", "cancelled")
ANSWERS: dict[str, Any] = {
    "questions": {"answers": {}},
    "scenarios": {"approved_ids": []},
    "gate": {"approved": True},
}


class SimulatedCrash(BaseException):
    """Escapes the worker's error handling on purpose, like a process that died: nothing is written."""


class Ledger:
    """Counts how many times each node started, per run, so tests can prove completed nodes never re-run."""

    def __init__(self) -> None:
        self.started: list[tuple[str, str]] = []  # (run_id, node)

    def count(self, node: str, run_id: str | None = None) -> int:
        return Counter(n for r, n in self.started if run_id in (None, r))[node]


def instrumented(
    ledger: Ledger, hooks: dict[str, Callable[[dict], Awaitable[None]]] | None = None, base: Heads | None = None
) -> Callable[[], Heads]:
    """A heads factory: every stub head records itself, and `hooks[node]` runs first (may raise or block)."""
    hooks = hooks or {}
    base = base or Heads()

    def wrap(name: str, head: Callable[[dict], Awaitable[dict]]) -> Callable[[dict], Awaitable[dict]]:
        async def node(state: dict) -> dict:
            ledger.started.append((state["run_id"], name))
            if name in hooks:
                await hooks[name](state)
            return await head(state)

        return node

    def factory() -> Heads:
        return dataclasses.replace(
            base, **{f.name: wrap(NODE_OF[f.name], getattr(base, f.name)) for f in dataclasses.fields(Heads)}
        )

    return factory


async def forever(_state: dict) -> None:
    await asyncio.Event().wait()


def start(
    factory: Callable[[], Heads], worker_id: str, config: WorkerConfig = FAST
) -> tuple[Worker, asyncio.Task[None]]:
    worker = Worker(config, factory, worker_id=worker_id)
    return worker, asyncio.create_task(worker.run(), name=f"worker-{worker_id}")


async def stop(worker: Worker, task: asyncio.Task[None]) -> None:
    worker.request_stop()
    await asyncio.wait_for(task, 30)


async def answer_until_terminal(db, run_id: str, timeout: float = 60.0) -> dict:
    """Play the human: answer every pause with the usual answer until the run reaches a final status."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        row = db.run(run_id)
        if row["status"] in TERMINAL:
            return row
        if row["status"] == "awaiting_approval" and row["paused_checkpoint_id"]:
            await store.store_resume_value(run_id, ANSWERS[db.pending_kind(run_id)], row["paused_checkpoint_id"])
        await asyncio.sleep(0.1)
    raise AssertionError(f"run did not finish: {db.run(run_id)['status']}")


def play_human(db, run_id: str) -> asyncio.Task[None]:
    """Background human: answers every pause for `run_id` until cancelled. Cancel the task when done."""

    async def loop() -> None:
        while True:
            row = db.run(run_id)
            if row["status"] == "awaiting_approval" and row["paused_checkpoint_id"]:
                await store.store_resume_value(run_id, ANSWERS[db.pending_kind(run_id)], row["paused_checkpoint_id"])
            await asyncio.sleep(0.1)

    return asyncio.create_task(loop())
