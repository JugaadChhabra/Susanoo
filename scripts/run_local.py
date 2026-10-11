"""Run the real Susanoo graph end to end on this machine, for evidence and debugging.

    uv run python scripts/run_local.py specs/code-review.md --run-id hero-1 --out runs/hero-1.json

Uses the real heads (`real_heads()`), an in-memory checkpointer and an in-memory event sink that also
prints each event as it happens. Needs ANTHROPIC_API_KEY (and GITHUB_ORG/GITHUB_ORG_TOKEN for the Builder to
publish the repo; without them it stays local). NEATLOGS_API_KEY makes the traces real.

Pauses are answered without a human:
- intake runs unattended (SUSANOO_UNATTENDED=1): suggested defaults, and drafted cards are NOT marked as
  approved by a user;
- a test-layer HumanGate is always REJECTED: this script never approves a test change on anyone's behalf.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from susanoo.emit import InMemorySink, use_sink
from susanoo.graph.build import build_graph, real_heads
from susanoo.graph.checkpointer import memory_checkpointer
from susanoo.graph.config import run_config
from susanoo.graph.resume import resume_run
from susanoo.graph.state import initial_state
from susanoo.integrations import tracing


class PrintingSink(InMemorySink):
    async def __call__(self, *args, **kwargs):  # same signature as InMemorySink
        event = await super().__call__(*args, **kwargs)
        print(f"{event.seq:>3}  {event.head:<14} {event.type:<16} {event.message}", flush=True)
        return event


def auto_answer(payload: dict) -> object:
    kind = payload.get("kind")
    if kind == "questions":
        return {"answers": {}}  # every suggested default
    if kind == "scenarios":
        return {"approved_ids": [], "rejected_ids": []}  # nobody is here to approve cards
    if kind == "gate":
        return {"approved": False}  # never approve a test change on a human's behalf
    raise ValueError(f"Unknown pause: {payload!r}")


async def run(spec_path: Path, run_id: str, out: Path | None) -> int:
    os.environ.setdefault("SUSANOO_UNATTENDED", "1")
    tracing.setup()
    sink = PrintingSink()
    graph = build_graph(real_heads(), checkpointer=memory_checkpointer())
    config = run_config(run_id)
    state = initial_state(run_id, spec_path.read_text())
    result: dict = {}
    error: str | None = None
    with use_sink(sink):
        try:
            result = await graph.ainvoke(state, config)
            while result.get("__interrupt__"):
                payload = result["__interrupt__"][0].value
                print(f"     (pause: {payload.get('kind')}; answering without a human)", flush=True)
                result = await resume_run(graph, run_id, auto_answer(payload), config=config)
        except Exception as exc:  # report, keep the evidence, exit non-zero
            error = f"{type(exc).__name__}: {tracing.scrub_text(str(exc))}"
            print(f"\nRUN FAILED: {error}", flush=True)
            snapshot = await graph.aget_state(config)
            result = dict(snapshot.values)

    metrics = result.get("metrics", [])
    patches = result.get("patches", [])
    print("\niteration  train_f1  heldout_f1  acceptance  cost")
    for m in metrics:
        print(
            f"{m.iteration:>9}  {m.f1:>8.3f}  {m.heldout_f1:>10.3f}  {m.acceptance_pass_rate:>10.3f}  ${m.cost_usd:.2f}"
        )
    for p in patches:
        print(
            f"patch {p.id}: {p.verdict} ({p.lines_changed} lines, commit {p.commit_sha[:10]}, "
            f"checkpoint {p.checkpoint_id})"
        )
    print(
        f"\nstatus={result.get('status')} stop_reason={result.get('stop_reason')} "
        f"iteration={result.get('iteration')} cost=${float(result.get('cost_usd', 0.0)):.2f} "
        f"repo={result.get('repo_path')} head={str(result.get('head_commit', ''))[:10]}"
    )
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "spec": str(spec_path),
                    "error": error,
                    "status": result.get("status"),
                    "stop_reason": result.get("stop_reason"),
                    "repo_path": result.get("repo_path"),
                    "head_commit": result.get("head_commit"),
                    "cost_usd": result.get("cost_usd"),
                    "events": [e.model_dump() for e in sink.events],
                    "metrics": [m.model_dump() for m in metrics],
                    "patches": [p.model_dump() for p in patches],
                    "results": [r.model_dump() for r in result.get("results", [])],
                    "failure_ledger": [d.model_dump() for d in result.get("failure_ledger", [])],
                    "plan": result["plan"].model_dump() if result.get("plan") else None,
                },
                indent=2,
            )
        )
        print(f"saved {out}")
    return 1 if error else 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("spec", type=Path)
    ap.add_argument("--run-id", default="local-1")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    sys.exit(asyncio.run(run(args.spec, args.run_id, args.out)))


if __name__ == "__main__":
    main()
