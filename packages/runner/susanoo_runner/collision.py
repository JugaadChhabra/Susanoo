"""Shared-state collision runs [Spec §21 › Shared-state collisions; T35].

Two or more actors share ONE intentionally shared fixture namespace. Every tool call goes through a
barrier-controlled ``ToolProxy``; a cooperative scheduler releases one call at a time once every live
actor is blocked at the proxy, so the interleaving of *local/router* operations is deterministic.
Nothing here claims to schedule a remote system's internals. A schedule declares barriers (all
actors read before anyone writes), delayed acknowledgements, and mid-run permission revocation at the
router; actors decide how they retry. After the actors settle the observer reports record counts,
lost updates, duplicates, invalid transitions, ownership conflicts and eventual state. Every granted
call, injected fault, late acknowledgement and revocation is kept in an ordered evidence log.
Repetitions and deadlines are bounded; there is no unbounded stress mode. Normal suites keep one
namespace per case; ``run_isolated_control`` shows the same actors cannot see each other there.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from susanoo_contracts import content_hash
from susanoo_simulator import ActionResult, Domain, FaultProfile, FixtureSeed, Simulator, SimulatorError
from susanoo_simulator.domains.shared_ledger import STATUS_TRANSITIONS
from susanoo_simulator.faults import NO_FAULTS

MAX_REPETITIONS = 20
MAX_DEADLINE_SECONDS = 120.0
MAX_CALLS_PER_ACTOR = 500

DefectKind = Literal[
    "lost_update", "duplicate_record", "invalid_transition", "ownership_conflict", "write_after_revocation"
]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadBeforeWrite(Model):
    """No mutating call is released until every listed actor is waiting on a write or has finished."""

    actors: list[str] = Field(min_length=2)


class DelayedAck(Model):
    """The actor's ``nth`` mutating call commits, but its acknowledgement misses the actor's deadline."""

    actor: str
    nth_mutation: int = Field(default=1, ge=1)
    late_by_steps: int = Field(default=1, ge=0, le=100)


class Revocation(Model):
    """The router stops honouring the actor's token once ``after_step`` calls have been granted."""

    actor: str
    after_step: int = Field(ge=0)


class CollisionSchedule(Model):
    id: str = Field(min_length=1, max_length=100)
    namespace: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$")
    intentionally_shared: bool = Field(description="Must be True: a collision run is the only shared fixture")
    actors: list[str] = Field(min_length=2, max_length=8)
    barriers: list[ReadBeforeWrite] = Field(default_factory=list, max_length=10)
    delayed_acks: list[DelayedAck] = Field(default_factory=list, max_length=20)
    revocations: list[Revocation] = Field(default_factory=list, max_length=8)
    repetitions: int = Field(default=3, ge=1, le=MAX_REPETITIONS)
    deadline_seconds: float = Field(default=10.0, gt=0, le=MAX_DEADLINE_SECONDS)
    max_calls_per_actor: int = Field(default=50, ge=1, le=MAX_CALLS_PER_ACTOR)
    settle_seconds: int = Field(default=0, ge=0, le=3600)
    rotate_priority: bool = Field(default=True, description="Rotate the tie-break order across repetitions")

    @model_validator(mode="after")
    def coherent(self) -> CollisionSchedule:
        if not self.intentionally_shared:
            raise ValueError("A collision run must declare its fixture intentionally shared")
        if len(set(self.actors)) != len(self.actors):
            raise ValueError("Actor names must be unique")
        named = [a for b in self.barriers for a in b.actors] + [d.actor for d in self.delayed_acks]
        named += [r.actor for r in self.revocations]
        unknown = sorted(set(named) - set(self.actors))
        if unknown:
            raise ValueError(f"Schedule names unknown actors: {unknown}")
        return self


class EvidenceEntry(Model):
    step: int
    repetition: int
    actor: str
    event: Literal["call", "router_rejected", "ack_withheld", "late_ack", "revoked", "fault", "actor_finished"]
    operation: str | None = None
    mutating: bool | None = None
    idempotency_key: str | None = None
    status: str | None = None
    code: str | None = None
    sim_seq: int | None = None
    faults: list[str] = Field(default_factory=list)
    elapsed_ms: float
    sim_clock: str | None = None
    detail: str | None = None


class Defect(Model):
    kind: DefectKind
    entity: str
    detail: str


class LedgerObservation(Model):
    """What an independent observer saw after every actor settled."""

    record_counts: dict[str, int]
    payments_by_ref: dict[str, int]
    opening_balances: dict[str, int]
    final_balances: dict[str, int]
    expected_balances: dict[str, int]
    lost_update_minor: dict[str, int]
    status_history: dict[str, list[str]]
    owner_history: dict[str, list[str | None]]
    final_owner: dict[str, str | None]
    eventual_state: dict[str, list[dict[str, Any]]]
    defects: list[Defect] = Field(default_factory=list)


class RepetitionResult(Model):
    repetition: int
    namespace: str
    priority: list[str]
    outcome: Literal["settled", "deadline_exceeded", "call_limit_exceeded", "actor_error"]
    actor_errors: dict[str, str] = Field(default_factory=dict)
    observation: LedgerObservation
    evidence: list[EvidenceEntry]
    fixture_digest: str
    fault_profile: str


class CollisionReport(Model):
    schedule_id: str
    schedule_digest: str
    shared_namespace: bool
    isolated: bool
    seed_digest: str
    repetitions: list[RepetitionResult]
    defects_found: list[DefectKind]
    caught: bool
    note: str = "Scheduling covers approved local/router operations only; remote internals are not ordered."


class RouterRejected(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class AckTimeout(Exception):
    """The call may have committed; the acknowledgement did not arrive before the actor's deadline."""

    def __init__(self, operation: str, key: str | None):
        super().__init__(f"No acknowledgement for {operation} before the deadline")
        self.operation, self.key = operation, key


class ScheduleLimit(Exception):
    pass


@dataclass
class _Request:
    actor: str
    operation: str
    request: dict[str, Any]
    key: str | None
    mutating: bool
    future: asyncio.Future


ActorScript = Callable[["ToolProxy"], Awaitable[None]]


class ToolProxy:
    """The only tool route an actor gets. Each call blocks until the scheduler grants it."""

    def __init__(self, scheduler: _Scheduler, actor: str):
        self._scheduler, self.actor = scheduler, actor

    async def call(self, operation: str, request: dict[str, Any], *, key: str | None = None) -> ActionResult:
        """Return the simulator result; raise ``RouterRejected``, ``AckTimeout`` or ``SimulatorError``."""
        return await self._scheduler.submit(self.actor, operation, request, key)


class _Scheduler:
    def __init__(self, sim: Simulator, schedule: CollisionSchedule, repetition: int, priority: list[str]):
        self.sim, self.schedule, self.repetition, self.priority = sim, schedule, repetition, priority
        self.pending: dict[str, _Request] = {}
        self.done: set[str] = set()
        self.expect: set[str] = set(priority)
        self.progress = asyncio.Event()
        self.released = [False] * len(schedule.barriers)
        self.revoked: set[str] = set()
        self.mutations: Counter[str] = Counter()
        self.calls: Counter[str] = Counter()
        self.step = 0
        self.evidence: list[EvidenceEntry] = []
        self.late: list[tuple[int, str, ActionResult, str]] = []
        self.started = time.monotonic()

    def log(self, actor: str, event: str, **fields: Any) -> None:
        self.evidence.append(
            EvidenceEntry(
                step=self.step,
                repetition=self.repetition,
                actor=actor,
                event=event,
                elapsed_ms=round((time.monotonic() - self.started) * 1000, 3),
                sim_clock=self.sim.now().isoformat(),
                **fields,
            )
        )

    async def submit(self, actor: str, operation: str, request: dict[str, Any], key: str | None) -> ActionResult:
        op = self.sim.domain.operations.get(operation)
        future = asyncio.get_running_loop().create_future()
        self.pending[actor] = _Request(actor, operation, dict(request), key, bool(op and op.mutating), future)
        self._arrived(actor)
        return await future

    def finished(self, actor: str) -> None:
        self.done.add(actor)
        self.log(actor, "actor_finished")
        self._arrived(actor)

    def _arrived(self, actor: str) -> None:
        self.expect.discard(actor)
        self.progress.set()

    async def _quiescent(self) -> None:
        while self.expect:
            await self.progress.wait()
            self.progress.clear()

    def _barrier_blocks(self, request: _Request) -> bool:
        if not request.mutating:
            return False
        for index, barrier in enumerate(self.schedule.barriers):
            if self.released[index] or request.actor not in barrier.actors:
                continue
            ready = all(a in self.done or (a in self.pending and self.pending[a].mutating) for a in barrier.actors)
            if not ready:
                return True
            self.released[index] = True
        return False

    def _next(self) -> _Request | None:
        ordered = [self.pending[a] for a in self.priority if a in self.pending]
        for request in ordered:
            if not self._barrier_blocks(request):
                return request
        return ordered[0] if ordered else None  # unreachable in practice: a waiting write always releases

    async def run(self, actors: dict[str, ActorScript]) -> dict[str, str]:
        errors: dict[str, str] = {}

        async def wrap(name: str) -> None:
            try:
                await actors[name](ToolProxy(self, name))
            except Exception as error:  # an actor crash is evidence, not a scheduler crash
                errors[name] = f"{type(error).__name__}: {error}"
            finally:
                self.finished(name)

        tasks = [asyncio.create_task(wrap(name)) for name in self.priority]
        try:
            while True:
                await self._quiescent()
                if len(self.done) == len(self.priority):
                    break
                request = self._next()
                if request is None:  # pragma: no cover - quiescent with no pending and not all done
                    break
                del self.pending[request.actor]
                self.expect.add(request.actor)
                self._grant(request)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return errors

    def _grant(self, request: _Request) -> None:
        actor = request.actor
        self.calls[actor] += 1
        if self.calls[actor] > self.schedule.max_calls_per_actor:
            raise ScheduleLimit(f"{actor} exceeded {self.schedule.max_calls_per_actor} calls")
        self.step += 1
        for revocation in self.schedule.revocations:
            if revocation.actor not in self.revoked and self.step > revocation.after_step:
                self.revoked.add(revocation.actor)
                self.log(revocation.actor, "revoked", detail=f"router token revoked after step {revocation.after_step}")
        base = {"operation": request.operation, "mutating": request.mutating, "idempotency_key": request.key}
        if actor in self.revoked:
            self.log(actor, "router_rejected", code="permission_revoked", **base)
            request.future.set_exception(RouterRejected("permission_revoked", "The router revoked this actor's token"))
            return
        if request.operation not in self.sim.domain.operations:
            self.log(actor, "router_rejected", code="operation_not_approved", **base)
            request.future.set_exception(RouterRejected("operation_not_approved", "Operation is not on the route"))
            return
        try:
            result = self.sim.execute(actor, request.operation, request.request, request.key)
        except SimulatorError as error:
            self.log(
                actor,
                "fault",
                status="error",
                code=error.code,
                sim_seq=error.result.seq,
                faults=error.result.faults,
                **base,
            )
            request.future.set_exception(error)
            self._deliver_late()
            return
        self.log(
            actor, "call", status=result.status, code=result.code, sim_seq=result.seq, faults=result.faults, **base
        )
        if request.mutating and result.status == "ok":
            self.mutations[actor] += 1
            delay = next(
                (d for d in self.schedule.delayed_acks if d.actor == actor and d.nth_mutation == self.mutations[actor]),
                None,
            )
            if delay is not None:
                self.log(
                    actor, "ack_withheld", sim_seq=result.seq, detail="committed; acknowledgement withheld", **base
                )
                self.late.append((self.step + delay.late_by_steps, actor, result, request.operation))
                request.future.set_exception(AckTimeout(request.operation, request.key))
                self._deliver_late()
                return
        request.future.set_result(result)
        self._deliver_late()

    def _deliver_late(self) -> None:
        due = [item for item in self.late if item[0] <= self.step]
        self.late = [item for item in self.late if item[0] > self.step]
        for _, actor, result, operation in due:
            self.log(
                actor,
                "late_ack",
                operation=operation,
                sim_seq=result.seq,
                status=result.status,
                detail="acknowledgement arrived after the actor's deadline (evidence only)",
            )

    def flush_late(self) -> None:
        for _, actor, result, operation in self.late:
            self.log(
                actor,
                "late_ack",
                operation=operation,
                sim_seq=result.seq,
                status=result.status,
                detail="acknowledgement arrived after the actor settled (evidence only)",
            )
        self.late = []


def observe_ledger(sim: Simulator, seed: FixtureSeed, evidence: list[EvidenceEntry]) -> LedgerObservation:
    """Independent read of authoritative state and version history; never uses actor replies."""
    state = sim.state()
    history: dict[str, list[dict[str, Any]]] = {}
    rows = sorted(sim.snapshot()["tables"]["entity_versions"], key=lambda row: (row["id"], row["version"]))
    for row in rows:
        if row["kind"] == "account":
            history.setdefault(row["id"], []).append(json.loads(row["data"]))
    payments = state.get("payment", [])
    opening = {row["id"]: row["balance_minor"] for row in seed.entities.get("account", [])}
    final = {row["id"]: row["balance_minor"] for row in state.get("account", [])}
    paid: Counter[str] = Counter()
    for payment in payments:
        paid[payment["account_id"]] += payment["amount_minor"]
    expected = {account: opening[account] - paid[account] for account in opening}
    lost = {account: final.get(account, 0) - expected[account] for account in opening}
    refs = Counter(f"{p['account_id']}:{p['payment_ref']}" for p in payments)
    defects: list[Defect] = []
    for account, gap in lost.items():
        if gap:
            defects.append(
                Defect(
                    kind="lost_update",
                    entity=account,
                    detail=f"final balance {final.get(account)} but payments imply {expected[account]}",
                )
            )
    for ref, count in refs.items():
        if count > 1:
            defects.append(Defect(kind="duplicate_record", entity=ref, detail=f"{count} payments share one reference"))
    statuses = {account: [v["status"] for v in versions] for account, versions in history.items()}
    owners = {account: [v["owner"] for v in versions] for account, versions in history.items()}
    for account, sequence in statuses.items():
        for old, new in zip(sequence, sequence[1:], strict=False):
            if (old, new) not in STATUS_TRANSITIONS:
                defects.append(Defect(kind="invalid_transition", entity=account, detail=f"status {old} -> {new}"))
    for account, sequence in owners.items():
        for old, new in zip(sequence, sequence[1:], strict=False):
            if old is not None and new is not None and old != new:
                defects.append(Defect(kind="ownership_conflict", entity=account, detail=f"owner {old} -> {new}"))
    revoked_at = {entry.actor: entry.step for entry in evidence if entry.event == "revoked"}
    revoked_seq: dict[str, int] = {}
    for entry in evidence:
        if entry.actor in revoked_at and entry.sim_seq is not None and entry.step <= revoked_at[entry.actor]:
            revoked_seq[entry.actor] = max(revoked_seq.get(entry.actor, 0), entry.sim_seq)
    for action in sim.action_log():
        actor = action["actor"]
        if actor in revoked_at and action["seq"] > revoked_seq.get(actor, 0) and action["status"] in {"ok", "replayed"}:
            if sim.domain.operations[action["operation"]].mutating:
                defects.append(
                    Defect(
                        kind="write_after_revocation",
                        entity=actor,
                        detail=f"{action['operation']} committed at seq {action['seq']}",
                    )
                )
    return LedgerObservation(
        record_counts={kind: len(records) for kind, records in state.items()},
        payments_by_ref=dict(sorted(refs.items())),
        opening_balances=opening,
        final_balances=final,
        expected_balances=expected,
        lost_update_minor={k: v for k, v in lost.items() if v},
        status_history=statuses,
        owner_history=owners,
        final_owner={row["id"]: row["owner"] for row in state.get("account", [])},
        eventual_state=state,
        defects=defects,
    )


def _priority(schedule: CollisionSchedule, repetition: int) -> list[str]:
    if not schedule.rotate_priority:
        return list(schedule.actors)
    shift = repetition % len(schedule.actors)
    return schedule.actors[shift:] + schedule.actors[:shift]


async def _repetition(
    schedule: CollisionSchedule,
    sim: Simulator,
    seed: FixtureSeed,
    scripts: dict[str, ActorScript],
    repetition: int,
    namespace: str,
    priority: list[str],
) -> RepetitionResult:
    scheduler = _Scheduler(sim, schedule, repetition, priority)
    outcome: str = "settled"
    errors: dict[str, str] = {}
    try:
        async with asyncio.timeout(schedule.deadline_seconds):
            errors = await scheduler.run(scripts)
    except TimeoutError:
        outcome = "deadline_exceeded"
    except ScheduleLimit as limit:
        outcome, errors = "call_limit_exceeded", {"scheduler": str(limit)}
    if outcome == "settled" and errors:
        outcome = "actor_error"
    scheduler.flush_late()
    if schedule.settle_seconds:
        sim.advance(schedule.settle_seconds)
    return RepetitionResult(
        repetition=repetition,
        namespace=namespace,
        priority=priority,
        outcome=outcome,
        actor_errors=errors,
        observation=observe_ledger(sim, seed, scheduler.evidence),
        evidence=scheduler.evidence,
        fixture_digest=sim.digest(),
        fault_profile=sim.faults.name,
    )


def _report(schedule: CollisionSchedule, seed: FixtureSeed, results: list[RepetitionResult], isolated: bool):
    found = sorted({d.kind for r in results for d in r.observation.defects})
    return CollisionReport(
        schedule_id=schedule.id,
        schedule_digest=content_hash(schedule.model_dump(mode="json")),
        shared_namespace=not isolated,
        isolated=isolated,
        seed_digest=seed.digest,
        repetitions=results,
        defects_found=found,
        caught=bool(found),
    )


def _validate(schedule: CollisionSchedule, actors: dict[str, ActorScript], seed: FixtureSeed) -> None:
    if set(actors) != set(schedule.actors):
        raise ValueError("Provide exactly one script per scheduled actor")
    missing = sorted(set(schedule.actors) - set(seed.actors))
    if missing:
        raise ValueError(f"Seed defines no actors {missing}")


async def run_collision(
    schedule: CollisionSchedule,
    actors: dict[str, ActorScript],
    *,
    domain: Domain,
    seed: FixtureSeed,
    root: Path,
    faults: FaultProfile = NO_FAULTS,
) -> CollisionReport:
    """Run every repetition on a fresh fixture that all actors share; destroy it afterwards."""
    _validate(schedule, actors, seed)
    Path(root).mkdir(parents=True, exist_ok=True)
    results = []
    for repetition in range(schedule.repetitions):
        namespace = f"{schedule.namespace}-shared-r{repetition}"
        sim = Simulator.create(Path(root) / f"{namespace}.sqlite", domain, seed, faults)
        try:
            priority = _priority(schedule, repetition)
            results.append(await _repetition(schedule, sim, seed, actors, repetition, namespace, priority))
        finally:
            sim.destroy()
    return _report(schedule, seed, results, isolated=False)


async def run_isolated_control(
    schedule: CollisionSchedule,
    actors: dict[str, ActorScript],
    *,
    domain: Domain,
    seed: FixtureSeed,
    root: Path,
    faults: FaultProfile = NO_FAULTS,
) -> CollisionReport:
    """Normal-suite control: each actor gets its own namespace, so no actor can observe another.

    Barriers need several actors and are dropped; per-actor delayed acks and revocations still apply.
    """
    _validate(schedule, actors, seed)
    Path(root).mkdir(parents=True, exist_ok=True)
    solo = schedule.model_copy(update={"barriers": []})
    results = []
    for repetition in range(schedule.repetitions):
        for actor in schedule.actors:
            namespace = f"{schedule.namespace}-{actor}-r{repetition}"
            sim = Simulator.create(Path(root) / f"{namespace}.sqlite", domain, seed, faults)
            try:
                results.append(
                    await _repetition(solo, sim, seed, {actor: actors[actor]}, repetition, namespace, [actor])
                )
            finally:
                sim.destroy()
    return _report(schedule, seed, results, isolated=True)
