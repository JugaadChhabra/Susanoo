"""Offline demo: run the stub graph end to end and print the event sequence.

    uv run python -m susanoo.graph.demo

Uses InMemorySaver and the in-memory event sink (no database, no model calls). Every interrupt is
auto-answered with Command(resume=...): defaults for questions, approve-all for scenarios,
approve for the human gate.
"""

from __future__ import annotations

import asyncio


from susanoo.emit import InMemorySink, use_sink
from susanoo.graph.build import build_graph
from susanoo.graph.checkpointer import memory_checkpointer
from susanoo.graph.config import run_config
from susanoo.graph.resume import resume_run
from susanoo.graph.state import initial_state


def auto_answer(payload: dict) -> object:
    kind = payload.get("kind")
    if kind == "questions":
        return {"answers": {}}  # no answers -> every suggested default
    if kind == "scenarios":
        return {"approved_ids": [s["id"] for s in payload["scenarios"]]}
    if kind == "gate":
        return {"approved": True}
    raise ValueError(f"Unknown interrupt payload: {payload!r}")


async def run_demo(run_id: str = "demo-run") -> tuple[InMemorySink, dict]:
    sink = InMemorySink()
    graph = build_graph(checkpointer=memory_checkpointer())
    config = run_config(run_id)
    with use_sink(sink):
        state = initial_state(run_id, "Review pull requests for PII in logs.")
        result = await graph.ainvoke(state, config)
        while result.get("__interrupt__"):
            result = await resume_run(graph, run_id, auto_answer(result["__interrupt__"][0].value))
    return sink, result


async def main() -> None:
    sink, result = await run_demo()
    print(f"{'seq':>3}  {'head':<14} {'type':<16} message")
    for e in sink.events:
        print(f"{e.seq:>3}  {e.head:<14} {e.type:<16} {e.message}")
    print(
        f"\nfinal status={result['status']} stop_reason={result['stop_reason']} "
        f"iteration={result['iteration']} cost=${result['cost_usd']:.2f}"
    )


if __name__ == "__main__":
    asyncio.run(main())
