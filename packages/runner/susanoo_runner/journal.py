"""Durable local journal [Spec §11 › Durable dispatch sequence].

SQLite in WAL mode with ``synchronous=FULL``: every commit is fsynced before the call returns.
Before any possible mutation the runner records a ``prepared`` intent, then ``dispatching``, and only
then invokes. On restart, ``dispatching`` without a recorded response means *potentially dispatched*
(``uncertain``) and goes to read-back reconciliation; ``prepared`` means it was never sent.
Nothing here ever re-dispatches a started step.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

ActionKind = Literal["agent_invoke", "environment_action", "fixture_prepare", "observe", "cleanup", "reference_call"]
DispatchState = Literal[
    "prepared", "dispatching", "acknowledged", "rejected_before_effect", "uncertain", "not_dispatched"
]
SideEffectState = Literal["none_expected", "requires_observation", "confirmed_by_readback", "unresolved"]
EventType = Literal[
    "step_prepared",
    "step_dispatching",
    "step_response_recorded",
    "observation_recorded",
    "check_recorded",
    "cleanup_recorded",
    "reconciliation_recorded",
    "cancel_acknowledged",
    "progress",
]

SCHEMA = """
create table if not exists attempts(
    attempt_id text primary key, run_id text not null, case_id text not null, namespace text not null,
    manifest_hash text not null, manifest text not null, fencing_generation integer not null, deadline text not null,
    execution_state text not null, business_verdict text, cleanup_state text not null default 'pending',
    reconciliation_state text not null default 'not_needed', reason text, created_at text not null,
    finalized_at text);
create table if not exists steps(
    attempt_id text not null, step_id text not null, operation_id text not null unique, action_kind text not null,
    request_digest text not null, dispatch_state text not null, side_effect_state text not null,
    response text, error text, prepared_at text not null, updated_at text not null,
    primary key(attempt_id, step_id));
create table if not exists events(
    attempt_id text not null, sequence integer not null, event_id text not null unique, type text not null,
    step_id text, payload text not null, occurred_at text not null, uploaded integer not null default 0,
    primary key(attempt_id, sequence));
create table if not exists evidence(
    attempt_id text not null, kind text not null, item_id text not null, data text not null,
    primary key(attempt_id, kind, item_id));
create table if not exists budgets(scope text not null, kind text not null, cap integer not null,
    used integer not null default 0, primary key(scope, kind));
create table if not exists reservations(scope text not null, operation_id text not null, kind text not null,
    amount integer not null, primary key(scope, operation_id));
"""

BUDGETED = {"agent_invoke": "agent", "reference_call": "reference", "cleanup": "cleanup"}


class JournalError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class Step(BaseModel):
    model_config = ConfigDict(extra="ignore")

    attempt_id: str
    step_id: str
    operation_id: str
    action_kind: ActionKind
    request_digest: str
    dispatch_state: DispatchState
    side_effect_state: SideEffectState
    response: Any = None
    error: str | None = None
    prepared_at: str


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


class Journal:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        mode = self._db.execute("pragma journal_mode=wal").fetchone()[0]
        if mode != "wal":
            raise JournalError("journal_unsafe", f"SQLite refused WAL mode ({mode})")
        self._db.execute("pragma synchronous=full")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("begin immediate")
            try:
                yield self._db
            except BaseException:
                self._db.execute("rollback")
                raise
            self._db.execute("commit")

    # --- attempts ---------------------------------------------------------------------------

    def begin_attempt(
        self,
        *,
        attempt_id: str,
        run_id: str,
        case_id: str,
        namespace: str,
        manifest: dict[str, Any],
        manifest_hash: str,
        fencing_generation: int,
        deadline: datetime,
    ) -> None:
        with self._tx() as db:
            db.execute(
                """insert into attempts(attempt_id,run_id,case_id,namespace,manifest_hash,manifest,fencing_generation,
                deadline,execution_state,created_at) values(?,?,?,?,?,?,?,?,'preparing',?)""",
                (
                    attempt_id,
                    run_id,
                    case_id,
                    namespace,
                    manifest_hash,
                    json.dumps(manifest),
                    fencing_generation,
                    deadline.isoformat(),
                    now_iso(),
                ),
            )

    def set_state(self, attempt_id: str, execution_state: str) -> None:
        with self._tx() as db:
            db.execute(
                "update attempts set execution_state=? where attempt_id=? and finalized_at is null",
                (execution_state, attempt_id),
            )

    def attempt(self, attempt_id: str) -> dict[str, Any]:
        row = self._db.execute("select * from attempts where attempt_id=?", (attempt_id,)).fetchone()
        if row is None:
            raise JournalError("unknown_attempt", attempt_id)
        return dict(row)

    def finalize(
        self,
        attempt_id: str,
        *,
        execution_state: str,
        business_verdict: str,
        cleanup_state: str,
        reconciliation_state: str,
        reason: str = "",
    ) -> None:
        with self._tx() as db:
            updated = db.execute(
                """update attempts set execution_state=?,business_verdict=?,cleanup_state=?,reconciliation_state=?,
                reason=?,finalized_at=? where attempt_id=? and finalized_at is null""",
                (execution_state, business_verdict, cleanup_state, reconciliation_state, reason, now_iso(), attempt_id),
            ).rowcount
            if not updated:
                raise JournalError("already_finalized", f"Attempt {attempt_id} is already final")

    # --- budgets ----------------------------------------------------------------------------

    def set_budget(self, scope: str, kind: str, cap: int) -> None:
        with self._tx() as db:
            db.execute(
                "insert into budgets(scope,kind,cap) values(?,?,?) on conflict(scope,kind) do nothing",
                (scope, kind, cap),
            )

    def reserve(self, scope: str, kind: str, operation_id: str, amount: int = 1) -> bool:
        """Atomically reserve budget before dispatch. Idempotent per operation ID; False when exhausted."""
        with self._tx() as db:
            prior = db.execute(
                "select kind,amount from reservations where scope=? and operation_id=?", (scope, operation_id)
            ).fetchone()
            if prior:
                if (prior["kind"], prior["amount"]) != (kind, amount):
                    raise JournalError("idempotency_conflict", "Reservation reused with different input")
                return True
            budget = db.execute("select cap,used from budgets where scope=? and kind=?", (scope, kind)).fetchone()
            if budget is None:
                raise JournalError("no_budget", f"No {kind} budget configured for {scope}")
            if budget["used"] + amount > budget["cap"]:
                return False
            db.execute("update budgets set used=used+? where scope=? and kind=?", (amount, scope, kind))
            db.execute(
                "insert into reservations(scope,operation_id,kind,amount) values(?,?,?,?)",
                (scope, operation_id, kind, amount),
            )
            return True

    def budget(self, scope: str, kind: str) -> tuple[int, int]:
        row = self._db.execute("select cap,used from budgets where scope=? and kind=?", (scope, kind)).fetchone()
        return (row["cap"], row["used"]) if row else (0, 0)

    # --- steps ------------------------------------------------------------------------------

    def prepare_step(
        self,
        attempt_id: str,
        step_id: str,
        action_kind: ActionKind,
        request_digest: str,
        operation_id: str | None = None,
    ) -> Step:
        operation_id = operation_id or f"{attempt_id}:{step_id}"
        stamp = now_iso()
        with self._tx() as db:
            db.execute(
                """insert into steps(attempt_id,step_id,operation_id,action_kind,request_digest,dispatch_state,
                side_effect_state,prepared_at,updated_at) values(?,?,?,?,?,'prepared','none_expected',?,?)""",
                (attempt_id, step_id, operation_id, action_kind, request_digest, stamp, stamp),
            )
            self._event(
                db,
                attempt_id,
                "step_prepared",
                step_id,
                {"operation_id": operation_id, "request_digest": request_digest, "action_kind": action_kind},
            )
        return self.step(attempt_id, step_id)

    def mark_dispatching(self, attempt_id: str, step_id: str, *, scope: str | None = None) -> Step:
        step = self.step(attempt_id, step_id)
        if step.dispatch_state != "prepared":
            raise JournalError("already_dispatched", f"Step {step_id} is {step.dispatch_state}; never dispatch twice")
        kind = BUDGETED.get(step.action_kind)
        with self._tx() as db:
            if kind and scope:
                reserved = db.execute(
                    "select 1 from reservations where scope=? and operation_id=?", (scope, step.operation_id)
                ).fetchone()
                if not reserved:
                    raise JournalError("budget_required", "Reserve budget before dispatch")
            db.execute(
                """update steps set dispatch_state='dispatching',side_effect_state='requires_observation',updated_at=?
                where attempt_id=? and step_id=? and dispatch_state='prepared'""",
                (now_iso(), attempt_id, step_id),
            )
            self._event(db, attempt_id, "step_dispatching", step_id, {"operation_id": step.operation_id})
        return self.step(attempt_id, step_id)

    def record_response(
        self,
        attempt_id: str,
        step_id: str,
        *,
        dispatch_state: DispatchState,
        side_effect_state: SideEffectState,
        response: Any = None,
        error: str | None = None,
    ) -> Step:
        step = self.step(attempt_id, step_id)
        if step.dispatch_state not in {"dispatching", "uncertain"}:
            raise JournalError("invalid_state", f"Cannot record a response for a {step.dispatch_state} step")
        with self._tx() as db:
            db.execute(
                """update steps set dispatch_state=?,side_effect_state=?,response=?,error=?,updated_at=?
                where attempt_id=? and step_id=?""",
                (dispatch_state, side_effect_state, json.dumps(response), error, now_iso(), attempt_id, step_id),
            )
            self._event(
                db,
                attempt_id,
                "step_response_recorded",
                step_id,
                {
                    "operation_id": step.operation_id,
                    "dispatch_state": dispatch_state,
                    "side_effect_state": side_effect_state,
                },
            )
        return self.step(attempt_id, step_id)

    def abandon_step(self, attempt_id: str, step_id: str, reason: str) -> None:
        """A prepared step that will never be sent (budget, cancellation) is recorded as not dispatched."""
        with self._tx() as db:
            updated = db.execute(
                """update steps set dispatch_state='not_dispatched',error=?,updated_at=?
                where attempt_id=? and step_id=? and dispatch_state='prepared'""",
                (reason, now_iso(), attempt_id, step_id),
            ).rowcount
            if not updated:
                raise JournalError("invalid_state", f"Only a prepared step can be abandoned ({step_id})")

    def mark_inflight_uncertain(self, attempt_id: str, reason: str) -> list[str]:
        with self._tx() as db:
            rows = db.execute(
                "select step_id,operation_id from steps where attempt_id=? and dispatch_state='dispatching'",
                (attempt_id,),
            ).fetchall()
            for row in rows:
                db.execute(
                    """update steps set dispatch_state='uncertain',side_effect_state='requires_observation',error=?,
                    updated_at=? where attempt_id=? and step_id=?""",
                    (reason, now_iso(), attempt_id, row["step_id"]),
                )
                self._event(
                    db,
                    attempt_id,
                    "step_response_recorded",
                    row["step_id"],
                    {
                        "operation_id": row["operation_id"],
                        "dispatch_state": "uncertain",
                        "side_effect_state": "requires_observation",
                    },
                )
        return [row["step_id"] for row in rows]

    def resolve_step(self, attempt_id: str, step_id: str, side_effect_state: SideEffectState) -> None:
        with self._tx() as db:
            db.execute(
                "update steps set side_effect_state=?,updated_at=? where attempt_id=? and step_id=?",
                (side_effect_state, now_iso(), attempt_id, step_id),
            )

    def step(self, attempt_id: str, step_id: str) -> Step:
        row = self._db.execute("select * from steps where attempt_id=? and step_id=?", (attempt_id, step_id)).fetchone()
        if row is None:
            raise JournalError("unknown_step", step_id)
        data = dict(row)
        data["response"] = json.loads(data["response"]) if data["response"] else None
        return Step.model_validate(data)

    def steps(self, attempt_id: str) -> list[Step]:
        rows = self._db.execute(
            "select step_id from steps where attempt_id=? order by prepared_at, step_id", (attempt_id,)
        ).fetchall()
        return [self.step(attempt_id, row["step_id"]) for row in rows]

    def recover(self) -> list[Step]:
        """After a crash: dispatching -> uncertain (needs read-back); prepared -> not_dispatched."""
        uncertain = []
        with self._tx() as db:
            for row in db.execute("select attempt_id,step_id,operation_id,dispatch_state from steps").fetchall():
                if row["dispatch_state"] == "dispatching":
                    db.execute(
                        """update steps set dispatch_state='uncertain',side_effect_state='requires_observation',
                        error='runner stopped before the response was recorded',updated_at=?
                        where attempt_id=? and step_id=?""",
                        (now_iso(), row["attempt_id"], row["step_id"]),
                    )
                    self._event(
                        db,
                        row["attempt_id"],
                        "step_response_recorded",
                        row["step_id"],
                        {
                            "operation_id": row["operation_id"],
                            "dispatch_state": "uncertain",
                            "side_effect_state": "requires_observation",
                        },
                    )
                    uncertain.append((row["attempt_id"], row["step_id"]))
                elif row["dispatch_state"] == "prepared":
                    db.execute(
                        "update steps set dispatch_state='not_dispatched',updated_at=? "
                        "where attempt_id=? and step_id=?",
                        (now_iso(), row["attempt_id"], row["step_id"]),
                    )
            db.execute(
                """update attempts set execution_state='interrupted',reconciliation_state='pending',
                reason='runner restarted during the attempt' where finalized_at is null"""
            )
        return [self.step(attempt, step) for attempt, step in uncertain]

    def unfinished_attempts(self) -> list[dict[str, Any]]:
        rows = self._db.execute("select * from attempts where finalized_at is null").fetchall()
        return [dict(row) for row in rows]

    # --- events and evidence ----------------------------------------------------------------

    def append_event(self, attempt_id: str, event_type: EventType, payload: dict[str, Any], step_id: str | None = None):
        with self._tx() as db:
            return self._event(db, attempt_id, event_type, step_id, payload)

    def _event(self, db, attempt_id: str, event_type: str, step_id: str | None, payload: dict[str, Any]) -> int:
        sequence = db.execute(
            "select coalesce(max(sequence),0)+1 from events where attempt_id=?", (attempt_id,)
        ).fetchone()[0]
        db.execute(
            "insert into events(attempt_id,sequence,event_id,type,step_id,payload,occurred_at) values(?,?,?,?,?,?,?)",
            (attempt_id, sequence, str(uuid.uuid4()), event_type, step_id, json.dumps(payload), now_iso()),
        )
        return sequence

    def events(self, attempt_id: str) -> list[dict[str, Any]]:
        rows = self._db.execute("select * from events where attempt_id=? order by sequence", (attempt_id,)).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def store_evidence(self, attempt_id: str, kind: str, item_id: str, data: dict[str, Any]) -> None:
        encoded = json.dumps(data, sort_keys=True)
        with self._tx() as db:
            prior = db.execute(
                "select data from evidence where attempt_id=? and kind=? and item_id=?", (attempt_id, kind, item_id)
            ).fetchone()
            if prior:
                if prior["data"] != encoded:
                    raise JournalError("evidence_immutable", "Evidence is immutable; append reconciliation instead")
                return
            db.execute(
                "insert into evidence(attempt_id,kind,item_id,data) values(?,?,?,?)",
                (attempt_id, kind, item_id, encoded),
            )

    def evidence(self, attempt_id: str, kind: str) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "select data from evidence where attempt_id=? and kind=? order by item_id", (attempt_id, kind)
        ).fetchall()
        return [json.loads(row["data"]) for row in rows]
