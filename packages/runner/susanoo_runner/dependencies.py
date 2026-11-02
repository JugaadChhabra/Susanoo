"""Dependency graph and evidence validity; automatic-run dedupe and budgets [Spec §18; T33].

Each conclusion (one case result in one run) depends on versioned nodes: case, check, fixture, adapter,
agent, tool schema, simulator, reference, policy, suite and evaluator. Nodes can depend on other nodes
(a fixture on a simulator domain, a case on the tool schemas it may call). A change event marks
affected conclusions, append-only, with the reason and the dependency path:

- criteria changes (case, check, policy, suite) -> ``scope_changed``
- implementation changes (agent, adapter, tool schema, simulator, fixture, evaluator) -> ``retest_recommended``
- authorized reference changes -> ``reference_unverified``
- a conclusion whose map does not cover the changed kind -> ``coverage_incomplete``, never "unaffected"

Prior results are never erased; marks are history. Observed dependencies come from the run journal
(observations, checks, step kinds and, when the adapter reports them, tool operations).

Scheduling and trigger storage are the platform's (Aarav); this module provides the dedupe key and
the budget decision every automatic run must pass.
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, get_args

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from susanoo_contracts import content_hash

DependencyKind = Literal[
    "case",
    "check",
    "fixture",
    "adapter",
    "agent",
    "tool_schema",
    "simulator",
    "reference",
    "policy",
    "suite",
    "evaluator",
]
ALL_KINDS: tuple[str, ...] = get_args(DependencyKind)
MarkStatus = Literal["retest_recommended", "scope_changed", "reference_unverified", "coverage_incomplete"]

CRITERIA = {"case", "check", "policy", "suite"}
IMPLEMENTATION = {"agent", "adapter", "tool_schema", "simulator", "fixture", "evaluator"}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


def node_key(kind: str, node_id: str) -> str:
    return f"{kind}:{node_id}"


class Conclusion(_Model):
    conclusion_id: str
    run_id: str
    case_id: str
    verdict: str
    recorded_at: AwareDatetime
    dependencies: dict[str, str] = Field(description="node key -> version digest at the time of the result")
    covered_kinds: list[str] = Field(description="kinds this map claims to cover completely")


class ChangeEvent(_Model):
    event_id: str
    kind: DependencyKind
    node_id: str
    change: Literal["modified", "removed", "added"] = "modified"
    old_version: str | None = None
    new_version: str | None = None
    reason: str
    occurred_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))


class ValidityMark(_Model):
    conclusion_id: str
    status: MarkStatus
    reason: str
    dependency_path: list[str]
    event_id: str
    marked_at: AwareDatetime


class Impact(_Model):
    event_id: str
    affected: list[ValidityMark]
    unaffected: list[str]
    coverage_incomplete: list[str]


class DependencyGraph:
    """In-memory, append-only. The platform persists conclusions and marks; the semantics live here."""

    def __init__(self) -> None:
        self.conclusions: dict[str, Conclusion] = {}
        self.edges: dict[str, set[str]] = {}  # node -> nodes it depends on
        self.marks: list[ValidityMark] = []
        self._events: set[str] = set()

    def depends(self, node: str, on: str) -> None:
        self.edges.setdefault(node, set()).add(on)

    def add(self, conclusion: Conclusion) -> None:
        if conclusion.conclusion_id in self.conclusions:
            raise ValueError(f"Conclusion {conclusion.conclusion_id} already recorded; results are immutable")
        self.conclusions[conclusion.conclusion_id] = conclusion

    def _path(self, conclusion: Conclusion, target: str) -> list[str] | None:
        start = [node for node in conclusion.dependencies]
        queue = deque([[node] for node in start])
        seen = set(start)
        while queue:
            path = queue.popleft()
            if path[-1] == target:
                return [f"conclusion:{conclusion.conclusion_id}", *path]
            for nxt in sorted(self.edges.get(path[-1], ())):
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append([*path, nxt])
        return None

    def apply(self, event: ChangeEvent) -> Impact:
        """Mark conclusions affected by one change. Idempotent per ``event_id``."""
        target = node_key(event.kind, event.node_id)
        status: MarkStatus = (
            "reference_unverified"
            if event.kind == "reference"
            else "scope_changed"
            if event.kind in CRITERIA
            else "retest_recommended"
        )
        affected, unaffected, incomplete = [], [], []
        now = datetime.now(UTC)
        for conclusion in self.conclusions.values():
            path = self._path(conclusion, target)
            if path is not None:
                version = conclusion.dependencies.get(target)
                if event.change != "added" and version is not None and version == event.new_version:
                    unaffected.append(conclusion.conclusion_id)  # already at the new version
                    continue
                mark = ValidityMark(
                    conclusion_id=conclusion.conclusion_id,
                    status=status,
                    reason=f"{event.kind} {event.node_id} {event.change}: {event.reason}",
                    dependency_path=path,
                    event_id=event.event_id,
                    marked_at=now,
                )
                affected.append(mark)
            elif event.kind not in conclusion.covered_kinds:
                incomplete.append(conclusion.conclusion_id)
                affected_mark = ValidityMark(
                    conclusion_id=conclusion.conclusion_id,
                    status="coverage_incomplete",
                    reason=f"Dependency map does not cover {event.kind}; cannot claim unaffected by {event.node_id}",
                    dependency_path=[f"conclusion:{conclusion.conclusion_id}", f"{target} (unmapped)"],
                    event_id=event.event_id,
                    marked_at=now,
                )
                affected.append(affected_mark)
            else:
                unaffected.append(conclusion.conclusion_id)
        if event.event_id not in self._events:
            self._events.add(event.event_id)
            self.marks.extend(affected)
        return Impact(
            event_id=event.event_id,
            affected=[m for m in affected if m.status != "coverage_incomplete"],
            unaffected=unaffected,
            coverage_incomplete=incomplete,
        )

    def history(self, conclusion_id: str) -> list[ValidityMark]:
        return [m for m in self.marks if m.conclusion_id == conclusion_id]

    def status(self, conclusion_id: str) -> str:
        marks = self.history(conclusion_id)
        return marks[-1].status if marks else "current"


# --- building conclusions from local runs -------------------------------------------------------


class ObservedDependencies(_Model):
    attempt_id: str
    observations: list[str]
    observation_sources: list[str]
    checks: list[str]
    action_kinds: list[str]
    tool_operations: list[str] | None = Field(description="None when the adapter did not report tool events")


def observed_dependencies(journal: Any, attempt_id: str) -> ObservedDependencies:
    """What a case actually touched, from journal events and step responses."""
    observations, sources, checks, operations = set(), set(), set(), set()
    reported = False
    for event in journal.events(attempt_id):
        payload = event["payload"]
        if event["type"] == "observation_recorded":
            observations.add(payload.get("observation", ""))
        elif event["type"] == "check_recorded":
            checks.add(payload.get("check", ""))
    for item in journal.evidence(attempt_id, "observation"):
        if item.get("source"):
            sources.add(item["source"])
    kinds = set()
    for step in journal.steps(attempt_id):
        kinds.add(step.action_kind)
        response = step.response if isinstance(step.response, dict) else {}
        events = response.get("tool_events")
        if isinstance(events, list):
            reported = True
            operations.update(str(e.get("operation")) for e in events if isinstance(e, dict) and e.get("operation"))
    return ObservedDependencies(
        attempt_id=attempt_id,
        observations=sorted(observations - {""}),
        observation_sources=sorted(sources),
        checks=sorted(checks - {""}),
        action_kinds=sorted(kinds),
        tool_operations=sorted(operations) if reported else None,
    )


def conclusions_from_run(
    report: Any, manifest: Any, suite: Any, journal: Any | None = None
) -> tuple[list[Conclusion], list[tuple[str, str]]]:
    """Declared dependencies from the frozen suite and manifest, narrowed by observed ones where known.

    Returns conclusions and node->node edges. Tool schemas are declared from the fixture's allowed agent
    operations (a sound over-approximation: the agent cannot call anything else). Reference and policy
    are not covered for local simulator runs, so changes there yield ``coverage_incomplete``.
    """
    cases = {case.id: case for case in suite.cases}
    entries = {entry.case_id: entry for entry in manifest.cases}
    conclusions, edges = [], []
    for attempt in report.attempts:
        case, entry = cases[attempt.case_id], entries[attempt.case_id]
        deps = {
            node_key("suite", report.suite_id): report.suite_hash,
            node_key("case", case.id): entry.case_digest,
            node_key("fixture", f"{case.id}"): entry.fixture_digest,
            node_key("adapter", entry.fixture_adapter): entry.fixture_compatibility_key or "",
            node_key("agent", report.agent): report.agent,
            node_key("evaluator", "susanoo-evaluator"): report.evaluator_digest,
        }
        for check_id, digest in entry.check_digests.items():
            deps[node_key("check", f"{case.id}/{check_id}")] = digest
        simulator = node_key("simulator", entry.fixture_compatibility_key or entry.fixture_adapter)
        edges.append((node_key("fixture", case.id), simulator))
        allowed = case.fixture.recipe.get("seed", {}).get("actors", {}).get("agent", {}).get("operations", [])
        observed = observed_dependencies(journal, attempt.attempt_id) if journal is not None else None
        tools = observed.tool_operations if observed and observed.tool_operations is not None else allowed
        for operation in tools:
            deps[node_key("tool_schema", operation)] = ""
        covered = ["suite", "case", "check", "fixture", "adapter", "agent", "evaluator", "simulator"]
        if allowed or (observed and observed.tool_operations is not None):
            covered.append("tool_schema")
        conclusions.append(
            Conclusion(
                conclusion_id=f"{report.run_id}:{attempt.case_id}:{attempt.attempt_id[:8]}",
                run_id=report.run_id,
                case_id=attempt.case_id,
                verdict=attempt.business_verdict,
                recorded_at=report.finished_at,
                dependencies=deps,
                covered_kinds=covered,
            )
        )
    return conclusions, edges


# --- automatic runs: dedupe and budgets ---------------------------------------------------------


def trigger_dedupe_key(project_id: str, trigger: str, version: str, suite_version: str) -> str:
    """Stable key for project/trigger/version/suite; the same key never starts a second run."""
    return content_hash({"project": project_id, "trigger": trigger, "version": version, "suite": suite_version})


class TriggerLedger:
    """In-memory reference implementation of dedupe; the platform keeps the durable unique index."""

    def __init__(self) -> None:
        self._keys: dict[str, str] = {}

    def admit(self, key: str, run_ref: str) -> tuple[bool, str]:
        if key in self._keys:
            return False, self._keys[key]
        self._keys[key] = run_ref
        return True, run_ref


class AutomaticRunBudget(_Model):
    max_runs_per_window: int = Field(default=10, ge=0)
    window_seconds: int = Field(default=3600, ge=1)
    max_cases_per_run: int = Field(default=50, ge=1)
    max_agent_invocations: int = Field(default=400, ge=1)
    max_cost_minor: int | None = Field(default=None, ge=0)
    allowed_suites: list[str] = Field(default_factory=list, description="resource scope; empty means none")


class BudgetDecision(_Model):
    allowed: bool
    reasons: list[str] = Field(default_factory=list)
    delayed_until: AwareDatetime | None = None
    secrets_allowed: bool


def check_automatic_run(
    budget: AutomaticRunBudget,
    *,
    suite_id: str,
    recent_runs: list[datetime],
    cases: int,
    agent_invocations: int,
    estimated_cost_minor: int | None = None,
    untrusted_source: bool = False,
    now: datetime | None = None,
) -> BudgetDecision:
    """Every automatic run must pass this before it is created. Reasons are shown on skipped triggers."""
    now = now or datetime.now(UTC)
    reasons = []
    delayed = None
    if suite_id not in budget.allowed_suites:
        reasons.append(f"suite {suite_id} is outside the automatic-run scope")
    window = [t for t in recent_runs if t > now - timedelta(seconds=budget.window_seconds)]
    if len(window) >= budget.max_runs_per_window:
        reasons.append(f"rate limit: {len(window)} run(s) in the last {budget.window_seconds}s")
        delayed = min(window) + timedelta(seconds=budget.window_seconds) if window else None
    if cases > budget.max_cases_per_run:
        reasons.append(f"{cases} cases exceed the per-run cap {budget.max_cases_per_run}")
    if agent_invocations > budget.max_agent_invocations:
        reasons.append(f"{agent_invocations} agent invocations exceed the cap {budget.max_agent_invocations}")
    if budget.max_cost_minor is not None:
        if estimated_cost_minor is None:
            reasons.append("cost cap configured but no cost estimate supplied")
        elif estimated_cost_minor > budget.max_cost_minor:
            reasons.append(f"estimated cost {estimated_cost_minor} exceeds the cap {budget.max_cost_minor}")
    return BudgetDecision(
        allowed=not reasons, reasons=reasons, delayed_until=delayed, secrets_allowed=not untrusted_source
    )
