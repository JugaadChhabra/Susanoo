"""Save a finished local run as a replayable demo run. [PRD > Frontend and API], PROMPTBOOK_MUAAZ Phase 5

Run: uv run python scripts/save_demo_run.py docs/evidence/hero-2-run.json --user-id <uuid> [--dry-run]

Reads a run JSON written by scripts/run_local.py and, in ONE transaction on DATABASE_URL, inserts a project, a run
and every run event (under the new run's id). GET /demo/{run_id} then replays it once the id is listed in the
DEMO_RUN_IDS env var. --dry-run validates the file and prints what would be inserted without touching the database.
DATABASE_URL is never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "core"))

from pydantic import ValidationError  # noqa: E402

from susanoo.schemas import RunEvent  # noqa: E402

DEFAULT_PROJECT_NAME = "Demo: Python Code Review"
# Statuses the base schema (001_init.sql) accepts; anything else is saved as a finished demo.
ALLOWED_STATUSES = {"running", "awaiting_approval", "done", "failed"}


class SaveError(Exception):
    """A problem the person can fix; printed as one plain line, no traceback."""


def load_run(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        raise SaveError(f"Run file not found: {path}") from None
    except (OSError, json.JSONDecodeError) as exc:
        raise SaveError(f"Could not read {path} as JSON: {exc}") from None
    if not isinstance(data, dict) or not isinstance(data.get("events"), list) or not data["events"]:
        raise SaveError(f"{path} has no events; it does not look like a run_local.py result")
    return data


def validate_events(raw_events: list[Any], run_id: str) -> list[RunEvent]:
    """Every event must satisfy the frozen RunEvent contract and seq must be 1..N with no gaps."""
    events: list[RunEvent] = []
    for i, raw in enumerate(raw_events, start=1):
        try:
            events.append(RunEvent(**{**raw, "run_id": run_id}))
        except (ValidationError, TypeError) as exc:
            raise SaveError(f"Event #{i} is not a valid RunEvent: {exc}") from None
    seqs = [e.seq for e in events]
    if seqs != list(range(1, len(events) + 1)):
        raise SaveError(f"Event seq numbers must be 1..{len(events)} with no gaps or repeats; got {seqs[:10]}...")
    return events


def read_spec(data: dict[str, Any]) -> str:
    """The spec text: the file the JSON points at if it exists, else the plan summary."""
    spec_path = data.get("spec")
    if isinstance(spec_path, str) and spec_path:
        for candidate in (Path(spec_path), ROOT / spec_path):
            if candidate.is_file():
                return candidate.read_text()
    plan = data.get("plan")
    summary = plan.get("summary") if isinstance(plan, dict) else None
    if isinstance(summary, str) and summary.strip():
        return summary
    raise SaveError("The run JSON has no readable spec file and no plan summary to use as the project spec")


def run_status(data: dict[str, Any]) -> str:
    status = data.get("status")
    return status if status in ALLOWED_STATUSES else "done"


def insert_run(
    database_url: str, *, user_id: str, name: str, spec: str, status: str, cost_usd: float, raw_events: list[Any]
) -> str:
    """One transaction: project, run, events. Refuses an unknown user before inserting anything."""
    with psycopg.connect(database_url) as conn:  # commits on clean exit, rolls back on any exception
        if conn.execute("select 1 from public.users where id = %s", (user_id,)).fetchone() is None:
            raise SaveError(f"No user {user_id} in public.users. Pass the id of an existing demo account.")
        project_id = conn.execute(
            "insert into public.projects (user_id, name, spec) values (%s, %s, %s) returning id",
            (user_id, name, spec),
        ).fetchone()[0]  # type: ignore[index]
        run_id = str(
            conn.execute(
                "insert into public.runs (project_id, status, cost_usd, created_at) values (%s, %s, %s, now()) "
                "returning id",
                (project_id, status, cost_usd),
            ).fetchone()[0]  # type: ignore[index]
        )
        events = validate_events(raw_events, run_id)
        with conn.cursor() as cur:
            cur.executemany(
                "insert into public.run_events (run_id, seq, head, type, message, payload, trace_url, at) "
                "values (%s, %s, %s, %s, %s, %s, %s, %s::timestamptz)",
                [(run_id, e.seq, e.head, e.type, e.message, Jsonb(e.payload), e.trace_url, e.at) for e in events],
            )
        return run_id


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Save a local run as a replayable demo run.")
    ap.add_argument("run_json", type=Path, help="result file from scripts/run_local.py")
    ap.add_argument("--user-id", required=True, help="uuid of an existing demo account in public.users")
    ap.add_argument("--project-name", default=DEFAULT_PROJECT_NAME)
    ap.add_argument("--dry-run", action="store_true", help="validate and print only; no database access")
    args = ap.parse_args(argv)

    try:
        try:
            user_id = str(uuid.UUID(args.user_id))
        except ValueError:
            raise SaveError(f"--user-id must be a uuid, got {args.user_id!r}") from None
        database_url = os.environ.get("DATABASE_URL")
        if not args.dry_run and not database_url:
            raise SaveError("DATABASE_URL is not set. Set it, or use --dry-run to only validate the file.")

        data = load_run(args.run_json)
        events = validate_events(data["events"], str(uuid.uuid4()))
        spec = read_spec(data)
        status = run_status(data)
        cost = float(data.get("cost_usd") or 0)

        if args.dry_run:
            print(f"Dry run: nothing was written. {args.run_json} is valid.")
            print(f"  user        {user_id}")
            print(f"  project     {args.project_name!r} (spec: {len(spec)} characters)")
            print(f"  run         status={status} cost_usd={cost:.4f}")
            print(f"  events      {len(events)} (seq 1..{len(events)})")
            for e in events:
                print(f"    {e.seq:>3} {e.head:<13} {e.type:<16} {e.message[:70]}")
            return 0

        assert database_url is not None
        run_id = insert_run(
            database_url,
            user_id=user_id,
            name=args.project_name,
            spec=spec,
            status=status,
            cost_usd=cost,
            raw_events=data["events"],
        )
    except SaveError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except psycopg.Error as exc:
        # str(exc) can echo connection details; show only the class and the server's own message
        print(
            f"Error: database problem ({type(exc).__name__}): {exc.diag.message_primary or 'see server logs'}",
            file=sys.stderr,
        )
        return 1

    print(f"Saved run {run_id} with {len(events)} events.")
    print("Add this to the api environment (comma-separate if several demo runs):")
    print(f"DEMO_RUN_IDS={run_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
