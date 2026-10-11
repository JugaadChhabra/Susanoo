"""Shared helpers for graph tests: run a graph, auto-answer interrupts, and record node order and events."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from langgraph.types import Command

from susanoo.emit import InMemorySink, use_sink
from susanoo.graph.build import Heads, build_graph
from susanoo.graph.checkpointer import memory_checkpointer
from susanoo.graph.config import run_config
from susanoo.graph.state import initial_state

DEFAULT_ANSWERS: dict[str, Any] = {
    "questions": {"answers": {}},
    "scenarios": None,  # approve everything
    "gate": {"approved": True},
}


@dataclass
class Run:
    sink: InMemorySink
    nodes: list[str] = field(default_factory=list)  # node names in execution order
    interrupts: list[dict] = field(default_factory=list)  # payload of every pause, in order
    final: dict = field(default_factory=dict)

    @property
    def events(self):
        return self.sink.events

    def types(self) -> list[tuple[str, str]]:
        return [(e.head, e.type) for e in self.sink.events]


async def run_graph(
    initial: dict | None = None,
    *,
    heads: Heads | None = None,
    answers: dict[str, Any] | Callable[[dict], Any] | None = None,
    run_id: str = "run-1",
) -> Run:
    """Run to completion, answering every interrupt (override per kind via `answers`)."""
    graph = build_graph(heads, checkpointer=memory_checkpointer())
    config = run_config(run_id)
    run = Run(sink=InMemorySink())
    state: dict = {**initial_state(run_id, "Review pull requests for PII in logs."), **(initial or {})}
    payload: Any = state
    with use_sink(run.sink):
        while True:
            async for chunk in graph.astream(payload, config, stream_mode="updates"):
                for name, update in chunk.items():
                    if name == "__interrupt__":
                        run.interrupts.append(update[0].value)
                    else:
                        run.nodes.append(name)
                        run.final.update(update)
            if not (await graph.aget_state(config)).next:
                break
            kind = run.interrupts[-1]["kind"]
            if callable(answers):
                reply = answers(run.interrupts[-1])
            else:
                reply = {**DEFAULT_ANSWERS, **(answers or {})}[kind]
            if kind == "scenarios" and reply is None:
                reply = {"approved_ids": [s["id"] for s in run.interrupts[-1]["scenarios"]]}
            payload = Command(resume=reply)
    run.final = dict((await graph.aget_state(config)).values)
    return run
