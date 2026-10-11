"""Crash-resume demo for the worker. [PRD > LangGraph flow and shared state], IMPLEMENTATION_MUAAZ P4.4

Run: uv run python scripts/worker_crash_demo.py [--out trace.txt]

What it does, on a throwaway Postgres in Docker (never DATABASE_URL, no model calls, stub heads):
  1. queues a run and starts worker-A (a real OS process);
  2. plays the human, answering each pause;
  3. worker-A is killed with os._exit(137) while the Builder is running (no cleanup, like an OOM kill);
  4. worker-B starts, notices the stale heartbeat, claims the run and resumes it from the Postgres checkpoint;
  5. the run finishes; the script prints the event sequence and a per-node execution ledger.

The ledger is the proof of "no duplicate side effects for completed nodes": Intake, Planner and every node that
had checkpointed ran once; only the Builder, which was in flight when A died, ran twice (at-least-once).
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEARTBEAT_S, STALE_S = 0.5, 3.0


def _load_pg_container():
    spec = importlib.util.spec_from_file_location("pg_container", ROOT / "tests" / "worker" / "pg_container.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["pg_container"] = module
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------------------------------------------ child worker


async def child(name: str) -> None:
    """A worker process whose Builder dies the first time it runs (worker-A only)."""
    import dataclasses

    import psycopg
    from susanoo_worker.config import WorkerConfig
    from susanoo_worker.main import Worker

    from susanoo.graph import Heads
    from susanoo.graph import routing as r
    from susanoo.graph import stubs

    cfg = WorkerConfig(poll_seconds=0.2, heartbeat_seconds=HEARTBEAT_S, stale_seconds=STALE_S, shutdown_grace_seconds=2)
    ledger = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)
    crash_here = os.environ.get("DEMO_CRASH_NODE") if name == "worker-A" else None

    def wrap(node: str, head):
        async def run(state):
            ledger.execute("insert into demo_ledger (node, worker) values (%s, %s)", (node, name))
            if node == crash_here:
                print(f"[{name}] >>> hard-killing myself inside {node} (os._exit(137)), no cleanup", flush=True)
                os._exit(137)
            return await head(state)

        return run

    names = {
        "intake": r.INTAKE, "user_questions": r.USER_QUESTIONS, "scenario_approval": r.SCENARIO_APPROVAL,
        "planner": r.PLANNER, "builder": r.BUILDER, "agent_smith": r.AGENT_SMITH, "simulator": r.SIMULATOR,
        "runner": r.RUNNER, "diagnostician": r.DIAGNOSTICIAN, "human_gate": r.HUMAN_GATE, "fixer": r.FIXER,
        "verifier": r.VERIFIER, "done": r.DONE,
    }  # fmt: skip
    base = Heads()
    heads = dataclasses.replace(
        base, **{f.name: wrap(names[f.name], getattr(stubs, f.name)) for f in dataclasses.fields(Heads)}
    )
    worker = Worker(cfg, lambda: heads, worker_id=name)
    worker.install_signal_handlers()
    print(f"[{name}] started (heartbeat {HEARTBEAT_S}s, stale after {STALE_S}s)", flush=True)
    await worker.run()
    print(f"[{name}] shut down cleanly on SIGTERM", flush=True)


# ------------------------------------------------------------------------------------------------ parent


def spawn(name: str, url: str) -> subprocess.Popen:
    env = {**os.environ, "DATABASE_URL": url, "DEMO_CRASH_NODE": "Builder", "PYTHONUNBUFFERED": "1"}
    return subprocess.Popen([sys.executable, str(Path(__file__)), "--child", name], env=env, cwd=ROOT)


async def parent(out: Path | None) -> int:
    import psycopg
    from psycopg.rows import dict_row

    pg_container = _load_pg_container()
    if not pg_container.docker_available():
        print("Docker is not available; this demo needs a throwaway Postgres.")
        return 2
    server = pg_container.ThrowawayPostgres()
    print("Starting a throwaway Postgres (docker, label susanoo.test=true)...", flush=True)
    server.start()
    procs: list[subprocess.Popen] = []
    try:
        url = server.new_database()
        os.environ["DATABASE_URL"] = url  # for susanoo_worker.store in this process
        from susanoo_worker import store

        conn = psycopg.connect(url, autocommit=True, row_factory=dict_row)
        conn.execute(
            "create table demo_ledger (n serial primary key, node text, worker text, at timestamptz default now())"
        )
        uid = uuid.uuid4()
        conn.execute("insert into auth.users (id, email) values (%s, 'demo@example.com')", (uid,))
        pid = conn.execute(
            "insert into public.projects (user_id, name, spec) values (%s, 'demo', 'Review PRs for PII.') returning id",
            (uid,),
        ).fetchone()["id"]
        run_id = str(
            conn.execute("insert into public.runs (project_id) values (%s) returning id", (pid,)).fetchone()["id"]
        )
        status = conn.execute("select status from public.runs where id = %s", (run_id,)).fetchone()["status"]
        print(f"Queued run {run_id} (status '{status}')\n", flush=True)

        answers = {"questions": {"answers": {}}, "scenarios": {"approved_ids": []}, "gate": {"approved": True}}

        def human() -> None:
            row = conn.execute(
                "select status, paused_checkpoint_id from public.runs where id = %s", (run_id,)
            ).fetchone()
            if row["status"] == "awaiting_approval" and row["paused_checkpoint_id"]:
                kind = conn.execute(
                    "select payload->>'kind' as k from public.run_events where run_id = %s "
                    "and type in ('question_needed','approval_needed') order by seq desc limit 1",
                    (run_id,),
                ).fetchone()["k"]
                print(f"[human] answering the '{kind}' pause", flush=True)
                asyncio.get_running_loop().create_task(
                    store.store_resume_value(run_id, answers[kind], row["paused_checkpoint_id"])
                )

        a = spawn("worker-A", url)
        procs.append(a)
        while a.poll() is None:
            human()
            await asyncio.sleep(0.3)
        row = conn.execute(
            "select status, claimed_by, heartbeat_at from public.runs where id = %s", (run_id,)
        ).fetchone()
        print(f"\n[demo] worker-A exited with code {a.returncode}. Run row: status={row['status']!r}, "
              f"claimed_by={row['claimed_by']!r}, last heartbeat {row['heartbeat_at']:%H:%M:%S}")  # fmt: skip
        n_ck = conn.execute("select count(*) as n from checkpoints where thread_id = %s", (run_id,)).fetchone()["n"]
        print(f"[demo] the Postgres checkpointer holds {n_ck} checkpoints for thread {run_id}")
        print(f"[demo] starting worker-B; it must wait for the heartbeat to go stale (~{STALE_S:.0f}s)\n", flush=True)

        b = spawn("worker-B", url)
        procs.append(b)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            human()
            if conn.execute("select status from public.runs where id = %s", (run_id,)).fetchone()["status"] in (
                "done",
                "failed",
                "cancelled",
            ):
                break
            await asyncio.sleep(0.3)
        final = conn.execute("select * from public.runs where id = %s", (run_id,)).fetchone()
        b.terminate()  # SIGTERM: graceful shutdown
        b.wait(timeout=30)

        lines = [
            "",
            f"RUN {run_id}: final status={final['status']!r}, cost=${final['cost_usd']}, attempts={final['attempts']}",
            "",
        ]
        lines.append("EVENT SEQUENCE (public.run_events)")
        for e in conn.execute(
            "select seq, head, type, message from public.run_events where run_id = %s order by seq", (run_id,)
        ):
            lines.append(f"  {e['seq']:>3}  {e['head']:<13} {e['type']:<16} {e['message']}")
        lines += ["", "NODE EXECUTION LEDGER (every time a node started, in order)"]
        ledger = conn.execute("select node, worker, at from demo_ledger order by n").fetchall()
        for i, entry in enumerate(ledger, 1):
            lines.append(f"  {i:>3}  {entry['node']:<17} on {entry['worker']}")
        counts: dict[str, int] = {}
        for entry in ledger:
            counts[entry["node"]] = counts.get(entry["node"], 0) + 1
        straight = ("Intake", "Planner", "Builder", "AgentSmith", "Simulator", "Done")  # once per normal run
        repeated = [n for n in straight if counts.get(n, 0) > 1]
        lines += [
            "",
            f"Straight-line nodes that started more than once: {repeated}  (the loop and pause nodes repeat by design)",
        ]
        builder_workers = [e["worker"] for e in ledger if e["node"] == "Builder"]
        ok = final["status"] == "done" and builder_workers == ["worker-A", "worker-B"] and counts.get("Planner") == 1
        lines.append(
            f"Builder ran on {builder_workers}; Planner ran {counts.get('Planner')} time(s); "
            f"exit codes A={a.returncode} B={b.returncode}"
        )
        lines.append(
            "RESULT: " + ("OK, the run resumed from its checkpoint on worker-B" if ok else "UNEXPECTED, see above")
        )
        text = "\n".join(lines)
        print(text)
        if out:
            out.write_text(text + "\n")
        return 0 if ok else 1
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
        server.stop()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--child", metavar="NAME")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    if args.child:
        import logging

        logging.basicConfig(level=logging.INFO, format="    %(name)s: %(message)s")
        asyncio.run(child(args.child))
    else:
        sys.exit(asyncio.run(parent(args.out)))


if __name__ == "__main__":
    main()
