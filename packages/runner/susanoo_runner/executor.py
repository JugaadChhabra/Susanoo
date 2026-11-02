"""Execute one attempt end to end [Spec §11, §12, §13].

prepare fixture -> fresh session -> scripted turns (each a journaled, budgeted, deadline-bounded
step) -> independent observation -> evaluation -> reconciliation of uncertain steps -> cleanup ->
finalize. Execution state, business verdict, cleanup and reconciliation are kept separate.
The agent only ever receives an ``InvocationContext``; checks and fixture truth stay here.
"""

from __future__ import annotations

import asyncio
import time
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from susanoo_contracts import content_hash
from susanoo_evaluator import CheckOutcome, EvalContext, case_verdict, evaluate, evaluator_digest
from susanoo_sdk import (
    AdapterError,
    AgentAdapter,
    FixtureAdapter,
    FixtureContext,
    FixtureHandle,
    InvocationContext,
    Message,
    StepIntent,
)

from .journal import Journal, JournalError
from .suite import LocalCase, Limits

CANCELLED = "cancelled"


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def requested(self) -> bool:
        return self._event.is_set()


class Turn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int
    message: str
    reply: str | None = None
    dispatch_state: str
    error: str | None = None


class AttemptResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt_id: str
    case_id: str
    title: str
    required: bool
    visibility: str
    namespace: str
    execution_state: str
    business_verdict: str
    cleanup_state: str
    reconciliation_state: str
    reason: str = ""
    outcomes: list[CheckOutcome] = Field(default_factory=list)
    turns: list[Turn] = Field(default_factory=list)
    reconciliations: list[dict[str, Any]] = Field(default_factory=list)
    injected_faults: list[str] = Field(default_factory=list)
    evaluator_digest: str
    fixture_digest: str
    duration_seconds: float


class AttemptExecutor:
    def __init__(
        self,
        journal: Journal,
        *,
        run_id: str,
        limits: Limits,
        agent_factory: Callable[[], AgentAdapter],
        fixtures: FixtureAdapter,
        budget_scope: str | None = None,
    ):
        self.journal, self.run_id, self.limits = journal, run_id, limits
        self.agent_factory, self.fixtures = agent_factory, fixtures
        self.scope = budget_scope or run_id

    async def run(self, case: LocalCase, cancel: CancelToken | None = None, *, assignment=None) -> AttemptResult:
        if self.limits.cleanup_reserve < 3:
            raise JournalError(
                "cleanup_budget", "Reserve fixture preparation, observation and cleanup before execution"
            )
        cancel = cancel or CancelToken()
        started = time.monotonic()
        attempt_id = str(assignment.attempt_id) if assignment else str(uuid.uuid4())
        namespace = assignment.namespace if assignment else f"attempt-{uuid.uuid4().hex[:16]}"
        deadline_at = started + self.limits.case_timeout_seconds
        deadline = datetime.now(UTC) + timedelta(seconds=self.limits.case_timeout_seconds)
        if assignment:
            deadline = min(deadline, assignment.deadline)
            deadline_at = started + max(0, (deadline - datetime.now(UTC)).total_seconds())
        manifest = (
            assignment.model_dump(mode="json")
            if assignment
            else {"case": case.model_dump(mode="json"), "limits": self.limits.model_dump(mode="json")}
        )
        self.journal.begin_attempt(
            attempt_id=attempt_id,
            run_id=self.run_id,
            case_id=case.id,
            namespace=namespace,
            manifest=manifest,
            manifest_hash=assignment.manifest_hash if assignment else content_hash(manifest),
            fencing_generation=assignment.fencing_generation if assignment else 1,
            deadline=deadline,
        )
        self.journal.set_budget(self.scope, "agent", self.limits.max_agent_invocations)
        self.journal.set_budget(f"{attempt_id}:cleanup", "cleanup", self.limits.cleanup_reserve)
        state = _AttemptState()
        fixture: FixtureHandle | None = None
        agent: AgentAdapter | None = None
        try:
            fixture = await self._prepare(attempt_id, case, namespace, deadline, state)
            if fixture is not None:
                agent = self.agent_factory()
                await self._converse(attempt_id, case, namespace, deadline, deadline_at, fixture, agent, cancel, state)
                if cancel.requested:
                    state.fail("cancelled", "Cancellation requested while the invocation was in flight")
        except Exception as error:
            # A runner bug must not leave a step looking undispatched: anything in flight is uncertain.
            state.uncertain += self.journal.mark_inflight_uncertain(attempt_id, f"runner error {type(error).__name__}")
            state.fail("error", f"Runner error: {type(error).__name__}")
        finally:
            outcomes: list[CheckOutcome] = []
            try:
                if fixture is not None:
                    self.journal.set_state(attempt_id, "observing")
                    outcomes = await self._observe_and_evaluate(attempt_id, case, fixture, state)
                    await self._reconcile(attempt_id, fixture, state)
            except Exception as error:
                state.fail("error", f"Evidence processing failed: {type(error).__name__}")
            finally:
                # Observer/evaluator/read-back failures must not bypass process and fixture cleanup.
                try:
                    cleanup_state = await self._cleanup(attempt_id, fixture, agent, state)
                except Exception as error:
                    self.journal.mark_inflight_uncertain(attempt_id, f"cleanup interrupted: {type(error).__name__}")
                    cleanup_state = "failed"
                    state.fail("interrupted", "Cleanup could not be dispatched safely; reconciliation required")
        verdict = case_verdict(case.parsed_checks(), outcomes).verdict
        reconciliation_state = state.reconciliation_state()
        self.journal.finalize(
            attempt_id,
            execution_state=state.execution_state,
            business_verdict=verdict,
            cleanup_state=cleanup_state,
            reconciliation_state=reconciliation_state,
            reason=state.reason,
        )
        return AttemptResult(
            attempt_id=attempt_id,
            case_id=case.id,
            title=case.title,
            required=case.required,
            visibility=case.visibility,
            namespace=namespace,
            execution_state=state.execution_state,
            business_verdict=verdict,
            cleanup_state=cleanup_state,
            reconciliation_state=reconciliation_state,
            reason=state.reason,
            outcomes=outcomes,
            turns=state.turns,
            reconciliations=state.reconciliations,
            injected_faults=case.injected_faults,
            evaluator_digest=evaluator_digest(),
            fixture_digest=case.fixture.digest,
            duration_seconds=round(time.monotonic() - started, 3),
        )

    async def _prepare(self, attempt_id, case, namespace, deadline, state) -> FixtureHandle | None:
        step = self._housekeeping_step(attempt_id, "fixture-prepare", "fixture_prepare", case.fixture.digest)
        self.journal.mark_dispatching(attempt_id, step.step_id, scope=f"{attempt_id}:cleanup")
        try:
            context = FixtureContext(
                attempt_id=attempt_id,
                namespace=namespace,
                fencing_generation=self.journal.attempt(attempt_id)["fencing_generation"],
                deadline=deadline,
            )
            fixture = await self.fixtures.prepare(case.fixture, context)
        except AdapterError as error:
            self.journal.record_response(
                attempt_id,
                step.step_id,
                dispatch_state="rejected_before_effect",
                side_effect_state="none_expected",
                error=error.code,
            )
            state.fail("error", f"Fixture preparation failed: {error.code}")
            return None
        except Exception as error:
            self.journal.record_response(
                attempt_id,
                step.step_id,
                dispatch_state="uncertain",
                side_effect_state="requires_observation",
                error=type(error).__name__,
            )
            state.uncertain.append(step.step_id)
            state.fail("error", f"Fixture preparation did not complete: {type(error).__name__}")
            return None
        self.journal.record_response(
            attempt_id,
            step.step_id,
            dispatch_state="acknowledged",
            side_effect_state="none_expected",
            response={"seed_digest": fixture.seed_digest},
        )
        self.journal.set_state(attempt_id, "running")
        return fixture

    async def _converse(self, attempt_id, case, namespace, deadline, deadline_at, fixture, agent, cancel, state):
        route = self.fixtures.tool_route(fixture)
        base = InvocationContext(
            attempt_id=attempt_id,
            case_id=case.id,
            run_id=self.run_id,
            namespace=namespace,
            deadline=deadline,
            tool_route=route,
        )
        try:
            session = await asyncio.wait_for(agent.start_session(base), self.limits.invocation_timeout_seconds)
        except (AdapterError, TimeoutError) as error:
            state.fail("error", f"Agent session did not start: {getattr(error, 'code', 'timeout')}")
            return
        state.session = session
        previous_reply = None
        for index, turn in enumerate(case.script, start=1):
            if turn.when != "always":
                if previous_reply is None or not (
                    turn.value in previous_reply if turn.when == "reply_contains" else turn.value == previous_reply
                ):
                    continue
            if len(state.turns) >= self.limits.max_turns:
                state.fail("error", "Approved turn budget exhausted before the script completed")
                return
            if cancel.requested:
                self.journal.append_event(attempt_id, "cancel_acknowledged", {})
                state.fail(CANCELLED, "Cancelled before the next turn; no new action was started")
                return
            remaining = deadline_at - time.monotonic()
            if remaining <= 0:
                state.fail("timed_out", "Case deadline reached before all turns ran")
                return
            step_id = f"turn-{index:03d}"
            digest = content_hash({"turn": index, "message": turn.message})
            step = self.journal.prepare_step(attempt_id, step_id, "agent_invoke", digest)
            if not self.journal.reserve(self.scope, "agent", step.operation_id):
                self.journal.abandon_step(attempt_id, step_id, "budget_exceeded")
                state.fail("error", "Agent invocation budget exhausted; turn not dispatched")
                state.turns.append(Turn(index=index, message=turn.message, dispatch_state="not_dispatched"))
                return
            self.journal.mark_dispatching(attempt_id, step_id, scope=self.scope)
            context = base.model_copy(update={"turn": index})
            if context.tool_route:
                context = context.model_copy(
                    update={"tool_route": context.tool_route.model_copy(update={"operation_id": step.operation_id})}
                )
            timeout = min(self.limits.invocation_timeout_seconds, remaining)
            try:
                reply = await asyncio.wait_for(agent.invoke(session, Message(text=turn.message), context), timeout)
            except TimeoutError:
                # The agent may have acted before the deadline. Never retry; reconcile by read-back.
                self.journal.record_response(
                    attempt_id,
                    step_id,
                    dispatch_state="uncertain",
                    side_effect_state="requires_observation",
                    error="invocation_timeout",
                )
                state.uncertain.append(step_id)
                state.turns.append(
                    Turn(index=index, message=turn.message, dispatch_state="uncertain", error="invocation timed out")
                )
                state.fail("timed_out", f"Turn {index} exceeded {timeout:.0f}s; outcome reconciled by read-back")
                return
            except AdapterError as error:
                self.journal.record_response(
                    attempt_id,
                    step_id,
                    dispatch_state="uncertain",
                    side_effect_state="requires_observation",
                    error=error.code,
                )
                state.uncertain.append(step_id)
                state.turns.append(
                    Turn(index=index, message=turn.message, dispatch_state="uncertain", error=error.code)
                )
                state.fail("error", f"Agent adapter failed on turn {index}: {error.code}")
                return
            self.journal.record_response(
                attempt_id,
                step_id,
                dispatch_state="acknowledged",
                side_effect_state="requires_observation",
                response={
                    "invocation_id": reply.invocation_id,
                    "text": reply.text,
                    "truncated": reply.truncated,
                    "tool_events": [e.model_dump(mode="json") for e in reply.tool_events]
                    if isinstance(reply.tool_events, list)
                    else None,
                },
            )
            state.turns.append(Turn(index=index, message=turn.message, reply=reply.text, dispatch_state="acknowledged"))
            previous_reply = None if reply.truncated else reply.text

    async def _observe_and_evaluate(self, attempt_id, case, fixture, state) -> list[CheckOutcome]:
        step = self._housekeeping_step(
            attempt_id,
            "fixture-observe",
            "observe",
            content_hash([query.model_dump(mode="json") for query in case.observations]),
        )
        try:
            # Simulator observation can advance its clock, so it needs the same durable authority gate.
            self.journal.mark_dispatching(attempt_id, step.step_id, scope=f"{attempt_id}:cleanup")
            observed = await self.fixtures.observe(fixture, case.observations)
            self.journal.record_response(
                attempt_id, step.step_id, dispatch_state="acknowledged", side_effect_state="none_expected"
            )
        except Exception as error:  # an observer crash is missing evidence, never a pass
            state.note(f"Observer failed: {type(error).__name__}")
            observed = []
            if self.journal.step(attempt_id, step.step_id).dispatch_state == "dispatching":
                self.journal.record_response(
                    attempt_id, step.step_id, dispatch_state="uncertain", side_effect_state="requires_observation",
                    error=type(error).__name__,
                )
                state.uncertain.append(step.step_id)
        observations = {item.name: item for item in observed}
        for item in observed:
            data = item.model_dump(mode="json")
            self.journal.store_evidence(attempt_id, "observation", item.name, data)
            self.journal.append_event(
                attempt_id,
                "observation_recorded",
                {"observation": item.name, "availability": item.availability, "digest": content_hash(data)},
            )
        self.journal.set_state(attempt_id, "evaluating")
        ctx = EvalContext(aliases=fixture.aliases, started_at=fixture.prepared_at)
        outcomes = []
        for check in case.parsed_checks():
            outcome = evaluate(check, observations, ctx)
            outcomes.append(outcome)
            self.journal.store_evidence(attempt_id, "check", check.id, outcome.model_dump(mode="json"))
            self.journal.append_event(attempt_id, "check_recorded", {"check": check.id, "verdict": outcome.verdict})
        return outcomes

    async def _reconcile(self, attempt_id, fixture, state) -> None:
        for step_id in state.uncertain:
            step = self.journal.step(attempt_id, step_id)
            intent = StepIntent(
                attempt_id=attempt_id,
                step_id=step_id,
                operation_id=step.operation_id,
                action_kind=step.action_kind,
                request_digest=step.request_digest,
                prepared_at=datetime.fromisoformat(step.prepared_at.replace("Z", "+00:00")),
            )
            result = await self.fixtures.reconcile(intent, fixture)
            resolved = "confirmed_by_readback" if result.state == "resolved" else "unresolved"
            self.journal.resolve_step(attempt_id, step_id, resolved)
            self.journal.append_event(
                attempt_id,
                "reconciliation_recorded",
                {"step_id": step_id, "state": result.state, "finding": result.finding},
                step_id,
            )
            state.reconciliations.append(result.model_dump(mode="json"))

    async def _cleanup(self, attempt_id, fixture, agent, state) -> str:
        if agent is not None:
            try:
                if state.session is not None:
                    await asyncio.wait_for(agent.close_session(state.session), 5)
            except Exception:  # closing a broken agent must not block cleanup
                state.note("Agent session did not close cleanly")
            closer = getattr(agent, "aclose", None)
            if closer:
                try:
                    await closer()
                except Exception:
                    state.note("Agent process did not stop cleanly")
        if fixture is None:
            if "fixture-prepare" in state.uncertain:
                self.journal.append_event(
                    attempt_id, "cleanup_recorded", {"state": "failed", "reason_code": "preparation_uncertain"}
                )
                state.note("Fixture preparation may have taken effect; authorized read-back is required")
                return "failed"
            self.journal.append_event(attempt_id, "cleanup_recorded", {"state": "not_required"})
            return "not_required"
        step = self._housekeeping_step(
            attempt_id, "fixture-cleanup", "cleanup", content_hash({"ns": fixture.namespace})
        )
        self.journal.mark_dispatching(attempt_id, step.step_id, scope=f"{attempt_id}:cleanup")
        try:
            result = await self.fixtures.cleanup(fixture)
        except Exception as error:
            self.journal.record_response(
                attempt_id,
                step.step_id,
                dispatch_state="uncertain",
                side_effect_state="unresolved",
                error=type(error).__name__,
            )
            self.journal.append_event(attempt_id, "cleanup_recorded", {"state": "failed", "reason_code": "exception"})
            return "failed"
        self.journal.record_response(
            attempt_id,
            step.step_id,
            dispatch_state="acknowledged",
            side_effect_state="none_expected",
            response=result.model_dump(mode="json"),
        )
        self.journal.append_event(
            attempt_id, "cleanup_recorded", {"state": result.state, "reason_code": result.reason_code}
        )
        if result.state == "failed":
            state.note(f"Cleanup failed ({result.reason_code}); fixture namespace quarantined")
        return result.state

    def _housekeeping_step(self, attempt_id, step_id, action_kind, digest):
        step = self.journal.prepare_step(attempt_id, step_id, action_kind, digest)
        if not self.journal.reserve(f"{attempt_id}:cleanup", "cleanup", step.operation_id):
            self.journal.abandon_step(attempt_id, step_id, "cleanup_budget")
            raise JournalError("cleanup_budget", "Housekeeping reserve exhausted; action not dispatched")
        return step


class _AttemptState:
    def __init__(self) -> None:
        self.execution_state = "completed"
        self.reason = ""
        self.session = None
        self.turns: list[Turn] = []
        self.uncertain: list[str] = []
        self.reconciliations: list[dict[str, Any]] = []

    def fail(self, execution_state: str, reason: str) -> None:
        if self.execution_state == "completed":
            self.execution_state = execution_state
        self.note(reason)

    def note(self, text: str) -> None:
        self.reason = f"{self.reason}; {text}" if self.reason else text

    def reconciliation_state(self) -> str:
        if not self.uncertain:
            return "not_needed"
        if len(self.reconciliations) == len(self.uncertain) and all(
            item["state"] == "resolved" for item in self.reconciliations
        ):
            return "resolved"
        return "unresolved"
