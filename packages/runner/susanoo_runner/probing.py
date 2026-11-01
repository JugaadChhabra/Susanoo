"""Budgeted reference probing [Spec §21 › Reference probing; T36].

Two selection strategies over the same candidate pool: a seeded uniform baseline and a transparent
priority score (severity, untested transitions, recent changes, uncertainty, expected call cost) with
an exploration fraction. A ``ProbePlan`` keeps the pool, every score input, the seed, the ranked
selection and the exploration picks. Execution reserves budget atomically in the durable journal
*before* each outbound probe, covering the probe, its retries and a cleanup allowance, and stops
before the cap would be exceeded, including under concurrent workers. The checker is fixed outside
the strategy and pinned by digest: selection decides only *which* probes run, never how they are
judged. Strategies are compared only on equal pool, budget, allowance and checker.
"""

from __future__ import annotations

import random
import threading
import uuid
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from susanoo_contracts import content_hash

from .journal import Journal

Severity = Literal["critical", "high", "medium", "low"]
Strategy = Literal["uniform", "priority"]
ProbeStatus = Literal["finding", "no_finding", "unknown", "error"]
BUDGET_KIND = "reference"
SEVERITY_WEIGHT: dict[str, float] = {"critical": 1.0, "high": 0.7, "medium": 0.4, "low": 0.1}
DEFAULT_WEIGHTS = {"severity": 3.0, "untested": 2.0, "recent_change": 1.5, "uncertainty": 1.0}


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProbeCandidate(Model):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$")
    transition: str = Field(min_length=1, max_length=200)
    severity: Severity
    times_tested: int = Field(default=0, ge=0)
    recently_changed: bool = False
    uncertainty: float = Field(default=0.5, ge=0.0, le=1.0, allow_inf_nan=False)
    expected_cost: int = Field(default=1, ge=1, le=10_000, description="Cost units per reference call")
    max_retries: int = Field(default=0, ge=0, le=5)

    def reservation(self, cleanup_allowance: int) -> int:
        """Worst case: every retry used plus cleanup, reserved up front."""
        return self.expected_cost * (1 + self.max_retries) + cleanup_allowance


class ScoreInputs(Model):
    candidate_id: str
    severity: float
    untested: float
    recent_change: float
    uncertainty: float
    expected_cost: int
    score: float


class ProbePlan(Model):
    plan_id: str
    strategy: Strategy
    seed: int
    candidate_pool: list[ProbeCandidate]
    pool_digest: str
    weights: dict[str, float]
    score_inputs: list[ScoreInputs]
    exploration_fraction: float
    selected: list[str] = Field(description="Ranked order in which probes are attempted")
    exploration_picks: list[str]
    budget_cap: int
    cleanup_allowance: int
    checker_digest: str


class ProbeOutcome(Model):
    status: ProbeStatus
    finding_id: str | None = None
    detail: str | None = Field(default=None, max_length=4000)


class ProbeExecution(Model):
    candidate_id: str
    reserved: int
    calls: int
    cost: int
    cleanup_cost: int
    retries: int
    status: ProbeStatus
    finding_id: str | None = None
    detail: str | None = None


class ProbeRun(Model):
    run_id: str
    plan: ProbePlan
    scope: str
    executions: list[ProbeExecution]
    not_run: list[str]
    stopped_reason: Literal["completed", "budget_exhausted"]
    budget_cap: int
    budget_reserved: int
    remaining_budget: int
    reference_calls: int
    cost_used: int


class ProbeAccounting(Model):
    strategy: Strategy
    pool_digest: str
    budget_cap: int
    cleanup_allowance: int
    checker_digest: str
    reference_calls: int
    cost_used: int
    budget_reserved: int
    accepted_findings: int
    rejected_findings: int
    unknowns: int
    review_effort: int = Field(description="Findings plus unknowns an owner had to look at")
    accepted_per_reference_call: float
    accepted_per_cost_unit: float


class Comparison(Model):
    baseline: ProbeAccounting
    priority: ProbeAccounting
    accepted_delta: int
    note: str = "Seeded examples alone do not establish an optimisation advantage."


Probe = Callable[[ProbeCandidate, int], ProbeOutcome]
Cleanup = Callable[[ProbeCandidate], int]


def pool_digest(pool: Iterable[ProbeCandidate]) -> str:
    return content_hash(sorted((c.model_dump(mode="json") for c in pool), key=lambda c: c["id"]))


def score_inputs(candidate: ProbeCandidate, weights: dict[str, float]) -> ScoreInputs:
    parts = {
        "severity": SEVERITY_WEIGHT[candidate.severity],
        "untested": 1.0 if candidate.times_tested == 0 else 1.0 / (1 + candidate.times_tested),
        "recent_change": 1.0 if candidate.recently_changed else 0.0,
        "uncertainty": candidate.uncertainty,
    }
    value = sum(weights[name] * parts[name] for name in parts) / candidate.expected_cost
    return ScoreInputs(candidate_id=candidate.id, expected_cost=candidate.expected_cost, score=round(value, 9), **parts)


def plan_probes(
    pool: Iterable[ProbeCandidate],
    *,
    strategy: Strategy,
    seed: int,
    budget_cap: int,
    checker_digest: str,
    cleanup_allowance: int = 1,
    exploration_fraction: float = 0.2,
    weights: dict[str, float] | None = None,
) -> ProbePlan:
    """Rank the whole pool deterministically for the given seed; execution stops at the budget."""
    candidates = sorted(pool, key=lambda c: c.id)
    if len({c.id for c in candidates}) != len(candidates):
        raise ValueError("Candidate IDs must be unique")
    if not 0.0 <= exploration_fraction <= 1.0:
        raise ValueError("exploration_fraction must be within [0, 1]")
    if budget_cap < 0 or cleanup_allowance < 0:
        raise ValueError("Budget and cleanup allowance cannot be negative")
    weights = dict(weights or DEFAULT_WEIGHTS)
    if set(weights) != set(DEFAULT_WEIGHTS):
        raise ValueError(f"Weights must name exactly {sorted(DEFAULT_WEIGHTS)}")
    rng = random.Random(seed)
    inputs: list[ScoreInputs] = []
    explored: list[str] = []
    if strategy == "uniform":
        order = [c.id for c in candidates]
        rng.shuffle(order)
        fraction = 0.0
    else:
        fraction = exploration_fraction
        inputs = [score_inputs(c, weights) for c in candidates]
        ranked = [s.candidate_id for s in sorted(inputs, key=lambda s: (-s.score, s.candidate_id))]
        order = []
        while ranked:
            if rng.random() < fraction:
                pick = ranked.pop(rng.randrange(len(ranked)))
                explored.append(pick)
            else:
                pick = ranked.pop(0)
            order.append(pick)
    body = {
        "strategy": strategy,
        "seed": seed,
        "pool": pool_digest(candidates),
        "weights": weights,
        "exploration_fraction": fraction,
        "budget_cap": budget_cap,
        "cleanup_allowance": cleanup_allowance,
        "checker": checker_digest,
    }
    return ProbePlan(
        plan_id=content_hash(body)[:16],
        strategy=strategy,
        seed=seed,
        candidate_pool=candidates,
        pool_digest=body["pool"],
        weights=weights,
        score_inputs=inputs,
        exploration_fraction=fraction,
        selected=order,
        exploration_picks=explored,
        budget_cap=budget_cap,
        cleanup_allowance=cleanup_allowance,
        checker_digest=checker_digest,
    )


def run_probes(
    plan: ProbePlan,
    probe: Probe,
    *,
    journal: Journal,
    scope: str,
    cleanup: Cleanup | None = None,
    workers: int = 1,
) -> ProbeRun:
    """Execute the plan in rank order. Each probe's worst case is reserved atomically before it is sent.

    ``probe(candidate, attempt)`` performs one outbound reference call; status ``error`` is retried up
    to ``max_retries``. ``cleanup`` returns the cost it used, capped by the plan's allowance. When a
    reservation is refused every worker stops claiming; nothing is sent without a reservation. The
    scope's cap is set once; later runs and other plans in the same scope draw from what remains.
    """
    if not 1 <= workers <= 32:
        raise ValueError("workers must be 1-32")
    journal.set_budget(scope, BUDGET_KIND, plan.budget_cap)
    run_id = uuid.uuid4().hex
    journal.store_evidence(scope, "probe_plan", plan.plan_id, plan.model_dump(mode="json"))
    by_id = {c.id: c for c in plan.candidate_pool}
    queue = list(plan.selected)
    lock = threading.Lock()
    stop = threading.Event()
    executions: dict[str, ProbeExecution] = {}

    def claim() -> ProbeCandidate | None:
        with lock:
            if stop.is_set() or not queue:
                return None
            return by_id[queue.pop(0)]

    def work() -> None:
        while (candidate := claim()) is not None:
            amount = candidate.reservation(plan.cleanup_allowance)
            if not journal.reserve(scope, BUDGET_KIND, f"{run_id}:{candidate.id}", amount):
                with lock:
                    stop.set()
                    queue.insert(0, candidate.id)
                return
            executions[candidate.id] = _execute(candidate, amount, probe, cleanup, plan.cleanup_allowance)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in [pool.submit(work) for _ in range(workers)]:
            future.result()
    ran = [executions[cid] for cid in plan.selected if cid in executions]
    cap, used = journal.budget(scope, BUDGET_KIND)
    result = ProbeRun(
        run_id=run_id,
        plan=plan,
        scope=scope,
        executions=ran,
        not_run=[cid for cid in plan.selected if cid not in executions],
        stopped_reason="budget_exhausted" if stop.is_set() else "completed",
        budget_cap=cap,
        budget_reserved=used,
        remaining_budget=cap - used,
        reference_calls=sum(e.calls for e in ran),
        cost_used=sum(e.cost + e.cleanup_cost for e in ran),
    )
    journal.store_evidence(scope, "probe_run", run_id, result.model_dump(mode="json"))
    return result


def _execute(candidate: ProbeCandidate, reserved: int, probe: Probe, cleanup: Cleanup | None, allowance: int):
    calls, outcome = 0, ProbeOutcome(status="error", detail="not attempted")
    for attempt in range(candidate.max_retries + 1):
        calls += 1
        try:
            outcome = probe(candidate, attempt)
        except Exception as error:  # a crashing probe is an unknown, never a finding or a pass
            outcome = ProbeOutcome(status="error", detail=f"{type(error).__name__}: {error}")
        if outcome.status != "error":
            break
    cleanup_cost = min(max(cleanup(candidate), 0), allowance) if cleanup else 0
    status: ProbeStatus = "unknown" if outcome.status == "error" else outcome.status
    return ProbeExecution(
        candidate_id=candidate.id,
        reserved=reserved,
        calls=calls,
        cost=calls * candidate.expected_cost,
        cleanup_cost=cleanup_cost,
        retries=calls - 1,
        status=status,
        finding_id=outcome.finding_id if status == "finding" else None,
        detail=outcome.detail,
    )


def account(run: ProbeRun, accepted: Iterable[str]) -> ProbeAccounting:
    """Owner review: only findings whose IDs the owner accepted count as useful."""
    accepted = set(accepted)
    findings = [e for e in run.executions if e.status == "finding"]
    useful = sum(1 for e in findings if e.finding_id in accepted)
    unknowns = sum(1 for e in run.executions if e.status == "unknown")
    return ProbeAccounting(
        strategy=run.plan.strategy,
        pool_digest=run.plan.pool_digest,
        budget_cap=run.plan.budget_cap,
        cleanup_allowance=run.plan.cleanup_allowance,
        checker_digest=run.plan.checker_digest,
        reference_calls=run.reference_calls,
        cost_used=run.cost_used,
        budget_reserved=run.budget_reserved,
        accepted_findings=useful,
        rejected_findings=len(findings) - useful,
        unknowns=unknowns,
        review_effort=len(findings) + unknowns,
        accepted_per_reference_call=round(useful / run.reference_calls, 6) if run.reference_calls else 0.0,
        accepted_per_cost_unit=round(useful / run.cost_used, 6) if run.cost_used else 0.0,
    )


def compare(baseline: ProbeAccounting, priority: ProbeAccounting) -> Comparison:
    """Refuse to compare strategies on different scope, budget, allowance or checker."""
    if baseline.strategy != "uniform" or priority.strategy != "priority":
        raise ValueError("Compare a uniform baseline against a priority run")
    for name in ("pool_digest", "budget_cap", "cleanup_allowance", "checker_digest"):
        if getattr(baseline, name) != getattr(priority, name):
            raise ValueError(f"Strategies differ on {name}; comparison would not be equal-budget")
    return Comparison(
        baseline=baseline, priority=priority, accepted_delta=priority.accepted_findings - baseline.accepted_findings
    )
