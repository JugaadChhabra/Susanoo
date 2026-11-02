"""Domain-agnostic stateful simulator engine [Spec §14].

Each fixture is one SQLite file: versioned entities, an append-only action log, idempotency records,
a simulated clock and the active fault profile. Domains (collections, issue tracker) supply
operations; the engine supplies permissions, idempotency, faults, visibility and logging.
The simulator never grades anything; it only records what happened.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from susanoo_contracts import content_hash

from .faults import NO_FAULTS, FaultProfile, FaultRule

SCHEMA = """
create table if not exists meta(key text primary key, value text not null);
create table if not exists entity_versions(
    kind text not null, id text not null, version integer not null, data text not null,
    visible_at text not null, written_seq integer not null, primary key(kind, id, version));
create table if not exists action_log(
    seq integer primary key autoincrement, at text not null, actor text not null, operation text not null,
    idempotency_key text, request text not null, status text not null, code text, response text not null,
    faults text not null);
create table if not exists idempotency(
    key text primary key, operation text not null, request_digest text not null, response text not null);
create table if not exists call_counts(operation text primary key, calls integer not null);
"""


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Actor(Model):
    permissions: list[str] = Field(default_factory=list)
    operations: list[str] = Field(default_factory=list)


class FixtureSeed(Model):
    domain: str
    domain_version: str
    clock: AwareDatetime
    entities: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    aliases: dict[str, str] = Field(default_factory=dict)
    actors: dict[str, Actor] = Field(default_factory=dict)

    @property
    def digest(self) -> str:
        return content_hash(self.model_dump(mode="json"))


class ActionResult(Model):
    seq: int
    status: Literal["ok", "rejected", "error", "replayed"]
    code: str | None = None
    message: str | None = None
    response: dict[str, Any] = Field(default_factory=dict)
    faults: list[str] = Field(default_factory=list)


class Rejected(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class SimulatorError(Exception):
    """Transport-level failure as the agent would see it (possibly after the write committed)."""

    def __init__(self, code: str, message: str, result: ActionResult):
        super().__init__(message)
        self.code, self.message, self.result = code, message, result


@dataclass(frozen=True)
class Operation:
    name: str
    permission: str
    mutating: bool
    handler: Callable[[OpContext, dict[str, Any]], dict[str, Any]]
    request_model: type[BaseModel] | None = None


@dataclass(frozen=True)
class Domain:
    name: str
    version: str
    entity_kinds: tuple[str, ...]
    operations: dict[str, Operation]
    id_prefixes: dict[str, str] = field(default_factory=dict)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _set_path(data: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    target = data
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    target[parts[-1]] = value


class OpContext:
    """What an operation handler may do: read visible state, buffer writes, reject."""

    def __init__(self, sim: Simulator, actor: str, faults: list[FaultRule]):
        self._sim, self.actor, self._faults = sim, actor, faults
        self.now = sim.now()
        self.writes: list[tuple[str, str, dict[str, Any], bool]] = []
        self.applied: list[str] = []

    def _fault(self, kind: str) -> FaultRule | None:
        return next((rule for rule in self._faults if rule.kind == kind), None)

    def get(self, kind: str, entity_id: str) -> dict[str, Any] | None:
        for pending_kind, pending_id, data, _ in reversed(self.writes):
            if (pending_kind, pending_id) == (kind, entity_id):
                return dict(data)
        stale = self._fault("stale_read")
        if stale:
            self._note("stale_read")
        return self._sim._read(kind, entity_id, self.now, stale=bool(stale))

    def list(self, kind: str, where: Callable[[dict[str, Any]], bool] | None = None) -> list[dict[str, Any]]:
        stale = self._fault("stale_read")
        if stale:
            self._note("stale_read")
        rows = {row["id"]: row for row in self._sim._list(kind, self.now, stale=bool(stale))}
        for pending_kind, pending_id, data, _ in self.writes:
            if pending_kind == kind:
                rows[pending_id] = dict(data)
        return [row for row in rows.values() if where is None or where(row)]

    def create(self, kind: str, data: dict[str, Any]) -> dict[str, Any]:
        record = {"id": self._sim._next_id(kind, len([w for w in self.writes if w[0] == kind and w[3]])), **data}
        self.writes.append((kind, record["id"], record, True))
        return dict(record)

    def update(self, kind: str, entity_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        current = self.get(kind, entity_id)
        if current is None:
            raise Rejected("not_found", f"{kind} {entity_id} does not exist")
        record = {**current, **changes, "id": entity_id}
        self.writes.append((kind, entity_id, record, False))
        return dict(record)

    def reject(self, code: str, message: str):
        raise Rejected(code, message)

    def _note(self, kind: str) -> None:
        if kind not in self.applied:
            self.applied.append(kind)


class Simulator:
    """One isolated fixture. Thread-safe; every operation is a single SQLite transaction."""

    def __init__(self, path: Path, domain: Domain):
        self.path, self.domain = Path(path), domain
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("pragma journal_mode=wal")
        self._db.execute("pragma synchronous=full")
        self._db.executescript(SCHEMA)
        if "operation_id" not in {row[1] for row in self._db.execute("pragma table_info(action_log)")}:
            self._db.execute("alter table action_log add column operation_id text")
        self._active_operation_id = None

    # --- lifecycle ------------------------------------------------------------------------

    @classmethod
    def create(cls, path: Path, domain: Domain, seed: FixtureSeed, faults: FaultProfile = NO_FAULTS) -> Simulator:
        if seed.domain != domain.name or seed.domain_version != domain.version:
            raise ValueError(f"Seed targets {seed.domain}@{seed.domain_version}, not {domain.name}@{domain.version}")
        unknown = set(seed.entities) - set(domain.entity_kinds)
        if unknown:
            raise ValueError(f"Seed has unknown entity kinds: {sorted(unknown)}")
        path = Path(path)
        if path.exists():
            raise FileExistsError(f"Fixture store {path} already exists")
        sim = cls(path, domain)
        with sim._lock, sim._tx():
            sim._meta_set("seed", seed.model_dump(mode="json"))
            sim._meta_set("clock", _iso(seed.clock))
            sim._meta_set("faults", faults.model_dump(mode="json"))
            sim._meta_set("counters", {})
            for kind, rows in seed.entities.items():
                for row in rows:
                    sim._write(kind, row["id"], row, _iso(seed.clock), 0)
        sim._meta_set("seed_snapshot", sim.snapshot())
        return sim

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def clone(self, path: Path) -> Simulator:
        with self._lock:
            target = sqlite3.connect(path)
            self._db.backup(target)
            target.close()
        return Simulator(path, self.domain)

    def destroy(self) -> None:
        self.close()
        for suffix in ("", "-wal", "-shm"):
            Path(f"{self.path}{suffix}").unlink(missing_ok=True)

    # --- state accessors ------------------------------------------------------------------

    @property
    def seed(self) -> FixtureSeed:
        return FixtureSeed.model_validate(self._meta_get("seed"))

    @property
    def faults(self) -> FaultProfile:
        return FaultProfile.model_validate(self._meta_get("faults"))

    def set_faults(self, profile: FaultProfile) -> None:
        with self._lock, self._tx():
            self._meta_set("faults", profile.model_dump(mode="json"))

    def now(self) -> datetime:
        return _parse(self._meta_get("clock"))

    def advance(self, seconds: float) -> datetime:
        if seconds < 0:
            raise ValueError("The simulated clock only moves forward")
        with self._lock, self._tx():
            moment = self.now() + timedelta(seconds=seconds)
            self._meta_set("clock", _iso(moment))
        return moment

    def observe(self, kind: str) -> list[dict[str, Any]]:
        """Read-only observer view of currently visible state (honours eventual consistency)."""
        if kind not in self.domain.entity_kinds:
            raise KeyError(kind)
        with self._lock:
            return self._list(kind, self.now(), stale=False)

    def state(self) -> dict[str, list[dict[str, Any]]]:
        """Authoritative inspector view: latest version of every entity, visible or not."""
        with self._lock:
            return {kind: self._latest(kind) for kind in self.domain.entity_kinds}

    def action_log(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("select * from action_log order by seq").fetchall()
        return [
            {
                **dict(row),
                "request": json.loads(row["request"]),
                "response": json.loads(row["response"]),
                "faults": json.loads(row["faults"]),
            }
            for row in rows
        ]

    def digest(self) -> str:
        return content_hash({"clock": _iso(self.now()), "state": self.state()})

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            tables = {
                name: [dict(row) for row in self._db.execute(f"select * from {name}").fetchall()]
                for name in ("entity_versions", "action_log", "idempotency", "call_counts")
            }
            meta = {key: self._meta_get(key) for key in ("clock", "faults", "counters")}
        return {"meta": meta, "tables": tables}

    def restore(self, snapshot: dict[str, Any]) -> None:
        with self._lock, self._tx():
            for name, rows in snapshot["tables"].items():
                self._db.execute(f"delete from {name}")
                for row in rows:
                    columns = ",".join(row)
                    marks = ",".join("?" for _ in row)
                    self._db.execute(f"insert into {name}({columns}) values({marks})", tuple(row.values()))
            for key, value in snapshot["meta"].items():
                self._meta_set(key, value)

    def reset(self) -> None:
        self.restore(self._meta_get("seed_snapshot"))

    # --- operations -----------------------------------------------------------------------

    def execute(
        self,
        actor_name: str,
        operation: str,
        request: dict[str, Any],
        idempotency_key: str | None = None,
        *,
        operation_id: str | None = None,
    ) -> ActionResult:
        """Run one operation. Raises ``SimulatorError`` for transport-style failures (lost ack included)."""
        with self._lock:
            self._active_operation_id = operation_id
            op = self.domain.operations.get(operation)
            actor = self.seed.actors.get(actor_name)
            call = self._count_call(operation)
            rules = self.faults.active(operation, call)
            applied: list[str] = []
            try:
                if op is None:
                    raise Rejected("unknown_operation", f"{operation} is not a {self.domain.name} operation")
                if actor is None or operation not in actor.operations:
                    raise Rejected("operation_not_allowed", f"{operation} is not approved for this actor")
                if op.permission not in actor.permissions:
                    if any(rule.kind == "permission_bypass" for rule in rules):
                        applied.append("permission_bypass")
                    else:
                        raise Rejected("permission_denied", f"Actor lacks {op.permission}")
                if op.request_model is not None:
                    try:
                        request = op.request_model.model_validate(request).model_dump(mode="json")
                    except ValueError as error:
                        raise Rejected("invalid_request", str(error).splitlines()[0]) from None
                digest = content_hash({"operation": operation, "request": request})
                if idempotency_key:
                    prior = self._db.execute("select * from idempotency where key=?", (idempotency_key,)).fetchone()
                    if prior:
                        if prior["operation"] != operation or prior["request_digest"] != digest:
                            raise Rejected("idempotency_conflict", "Idempotency key reused with a different request")
                        response = json.loads(prior["response"])
                        return self._log(actor_name, operation, idempotency_key, request, "replayed", None, response)
                if any(rule.kind == "failure_before_commit" for rule in rules):
                    result = self._log(
                        actor_name,
                        operation,
                        idempotency_key,
                        request,
                        "error",
                        "unavailable",
                        {},
                        ["failure_before_commit"],
                    )
                    raise SimulatorError("unavailable", "Service unavailable; nothing was committed", result)
                ctx = OpContext(self, actor_name, rules)
                response = op.handler(ctx, request)
                applied += ctx.applied
                lost_ack = any(rule.kind == "failure_after_commit" for rule in rules)
                with self._tx():
                    if op.mutating:
                        applied += self._commit(ctx, rules)
                        if idempotency_key:
                            self._db.execute(
                                "insert into idempotency(key,operation,request_digest,response) values(?,?,?,?)",
                                (idempotency_key, operation, digest, json.dumps(response)),
                            )
                    if not lost_ack:
                        return self._log(actor_name, operation, idempotency_key, request, "ok", None, response, applied)
                applied.append("failure_after_commit")
                result = self._log(
                    actor_name, operation, idempotency_key, request, "error", "connection_reset", response, applied
                )
                raise SimulatorError("connection_reset", "Connection reset before the response arrived", result)
            except Rejected as rejected:
                return self._log(
                    actor_name,
                    operation,
                    idempotency_key,
                    request,
                    "rejected",
                    rejected.code,
                    {"error": rejected.message},
                    applied,
                )

    def _commit(self, ctx: OpContext, rules: list[FaultRule]) -> list[str]:
        applied = []
        writes = list(ctx.writes)
        if any(rule.kind == "missing_write" for rule in rules):
            return ["missing_write"]
        corrupt = next((rule for rule in rules if rule.kind == "wrong_field"), None)
        if corrupt and writes:
            kind, entity_id, data, created = writes[0]
            data = json.loads(json.dumps(data))
            _set_path(data, corrupt.field, corrupt.value)
            writes[0] = (kind, entity_id, data, created)
            applied.append("wrong_field")
        if any(rule.kind == "duplicate_write" for rule in rules):
            for kind, _, data, created in list(writes):
                if created:
                    copy = {**data, "id": self._next_id(kind, len([w for w in writes if w[0] == kind and w[3]]))}
                    writes.append((kind, copy["id"], copy, True))
                    applied.append("duplicate_write")
        delay = next((rule.delay_seconds for rule in rules if rule.kind == "delayed_visibility"), 0)
        if delay:
            applied.append("delayed_visibility")
        visible_at = _iso(ctx.now + timedelta(seconds=delay))
        seq = self._db.execute("select coalesce(max(seq),0)+1 n from action_log").fetchone()["n"]
        for kind, entity_id, data, created in writes:
            self._write(kind, entity_id, data, visible_at, seq)
            if created:
                self._bump_counter(kind)
        return applied

    # --- storage helpers ------------------------------------------------------------------

    def _tx(self):
        db = self._db

        class _Tx:
            def __enter__(self):
                if not db.in_transaction:
                    db.execute("begin immediate")
                    self.owner = True
                else:
                    self.owner = False

            def __exit__(self, kind, *_):
                if self.owner:
                    db.execute("rollback" if kind else "commit")

        return _Tx()

    def _meta_get(self, key: str) -> Any:
        row = self._db.execute("select value from meta where key=?", (key,)).fetchone()
        if row is None:
            raise KeyError(key)
        return json.loads(row["value"])

    def _meta_set(self, key: str, value: Any) -> None:
        self._db.execute(
            "insert into meta(key,value) values(?,?) on conflict(key) do update set value=excluded.value",
            (key, json.dumps(value)),
        )

    def _write(self, kind: str, entity_id: str, data: dict[str, Any], visible_at: str, seq: int) -> None:
        version = self._db.execute(
            "select coalesce(max(version),0)+1 v from entity_versions where kind=? and id=?", (kind, entity_id)
        ).fetchone()["v"]
        self._db.execute(
            "insert into entity_versions(kind,id,version,data,visible_at,written_seq) values(?,?,?,?,?,?)",
            (kind, entity_id, version, json.dumps(data), visible_at, seq),
        )

    def _versions(self, kind: str, now: datetime) -> dict[str, list[dict[str, Any]]]:
        rows = self._db.execute(
            "select id,data,visible_at from entity_versions where kind=? order by id,version", (kind,)
        ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            if _parse(row["visible_at"]) <= now:
                grouped.setdefault(row["id"], []).append(json.loads(row["data"]))
        return grouped

    def _read(self, kind: str, entity_id: str, now: datetime, *, stale: bool) -> dict[str, Any] | None:
        versions = self._versions(kind, now).get(entity_id, [])
        if stale:
            versions = versions[:-1]
        return versions[-1] if versions else None

    def _list(self, kind: str, now: datetime, *, stale: bool) -> list[dict[str, Any]]:
        result = []
        for versions in self._versions(kind, now).values():
            chosen = versions[:-1] if stale else versions
            if chosen:
                result.append(chosen[-1])
        return result

    def _latest(self, kind: str) -> list[dict[str, Any]]:
        rows = self._db.execute(
            """select data from entity_versions v where kind=? and version=(
            select max(version) from entity_versions where kind=v.kind and id=v.id) order by id""",
            (kind,),
        ).fetchall()
        return [json.loads(row["data"]) for row in rows]

    def _next_id(self, kind: str, pending: int = 0) -> str:
        counters = self._meta_get("counters")
        prefix = self.domain.id_prefixes.get(kind, kind.upper())
        return f"{prefix}-{counters.get(kind, 0) + pending + 1:04d}"

    def _bump_counter(self, kind: str) -> None:
        counters = self._meta_get("counters")
        counters[kind] = counters.get(kind, 0) + 1
        self._meta_set("counters", counters)

    def _count_call(self, operation: str) -> int:
        with self._tx():
            self._db.execute(
                "insert into call_counts(operation,calls) values(?,1) "
                "on conflict(operation) do update set calls=calls+1",
                (operation,),
            )
            return self._db.execute("select calls from call_counts where operation=?", (operation,)).fetchone()[0]

    def _log(self, actor, operation, key, request, status, code, response, faults=None) -> ActionResult:
        with self._tx():
            cursor = self._db.execute(
                """insert into action_log(at,actor,operation,idempotency_key,request,status,code,response,
                faults,operation_id)
                values(?,?,?,?,?,?,?,?,?,?)""",
                (
                    _iso(self.now()),
                    actor,
                    operation,
                    key,
                    json.dumps(request),
                    status,
                    code,
                    json.dumps(response),
                    json.dumps(faults or []),
                    self._active_operation_id,
                ),
            )
        message = response.get("error") if status == "rejected" else None
        return ActionResult(
            seq=cursor.lastrowid, status=status, code=code, message=message, response=response, faults=faults or []
        )
