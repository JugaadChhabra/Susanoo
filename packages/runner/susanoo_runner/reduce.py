"""Bounded reduction of a discrepancy reproduction [Spec §15 › Reduction, F05].

Delta debugging over action chunks, then fixture simplification (dropping seeded entities the plan
does not reference). Every candidate runs on fresh fixtures with the same fixed violation predicate
and counts against the budget. Candidates that break prerequisites (an alias to a removed step) are
rejected without running. Every attempt is logged. The result is "reduced under attempted
transformations", never claimed to be minimal.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from susanoo_simulator import FixtureSeed

from .fidelity import ActionPlan, ComparisonResult, FidelityContract, compare

Predicate = Callable[[ComparisonResult], bool]
LABEL = "reduced under attempted transformations"


class ReductionAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int
    transformation: Literal["drop_actions", "drop_entity"]
    description: str
    steps: list[str]
    outcome: Literal["reproduced", "not_reproduced", "inconsistent", "invalid_prerequisites", "preflight_blocked"]
    runs: int


class ReductionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = LABEL
    contract_id: str
    original_plan: ActionPlan
    reduced_plan: ActionPlan
    original_fixture: FixtureSeed
    reduced_fixture: FixtureSeed
    attempts: list[ReductionAttempt] = Field(default_factory=list)
    budget: int
    runs_used: int
    stopped_because: Literal["no_further_reduction", "budget_exhausted", "original_not_reproduced"]
    reproduction: list[str] = Field(default_factory=list)


class _Reducer:
    def __init__(self, contract, predicate, sim_env, ref_env, budget: int, repeats: int):
        self.contract, self.predicate = contract, predicate
        self.sim_env, self.ref_env = sim_env, ref_env
        self.budget, self.repeats = budget, repeats
        self.runs = 0
        self.attempts: list[ReductionAttempt] = []

    def exhausted(self) -> bool:
        return self.runs + self.repeats > self.budget

    async def reproduces(self, contract: FidelityContract, plan: ActionPlan, transformation: str, description: str):
        if not plan.valid():
            self._log(transformation, description, plan, "invalid_prerequisites", 0)
            return False
        hits = 0
        for _ in range(self.repeats):
            self.runs += 1
            result = await compare(contract, plan, self.sim_env, self.ref_env)
            if result.status != "compared":
                self._log(transformation, description, plan, "preflight_blocked", hits + 1)
                return False
            hits += self.predicate(result)
        # A stochastic failure must reproduce on every repetition, not once by luck.
        outcome = "reproduced" if hits == self.repeats else "not_reproduced" if hits == 0 else "inconsistent"
        self._log(transformation, description, plan, outcome, self.repeats)
        return outcome == "reproduced"

    def _log(self, transformation, description, plan, outcome, runs) -> None:
        self.attempts.append(
            ReductionAttempt(
                index=len(self.attempts) + 1,
                transformation=transformation,
                description=description,
                steps=[a.step for a in plan.actions],
                outcome=outcome,
                runs=runs,
            )
        )


def _referenced(plan: ActionPlan) -> str:
    return json.dumps([a.request for a in plan.actions])


async def reduce_reproduction(
    contract: FidelityContract,
    plan: ActionPlan,
    predicate: Predicate,
    sim_env,
    ref_env,
    *,
    budget: int = 40,
    repeats: int = 1,
) -> ReductionResult:
    reducer = _Reducer(contract, predicate, sim_env, ref_env, budget, repeats)

    def result(reduced_plan, reduced_contract, stopped):
        return ReductionResult(
            contract_id=contract.id,
            original_plan=plan,
            reduced_plan=reduced_plan,
            original_fixture=contract.fixture,
            reduced_fixture=reduced_contract.fixture,
            attempts=reducer.attempts,
            budget=budget,
            runs_used=reducer.runs,
            stopped_because=stopped,
            reproduction=[
                "Create fresh simulator and reference fixtures from reduced_fixture with each side's pinned config.",
                *[f"Run {a.step}: {a.operation} {json.dumps(a.request, sort_keys=True)}" for a in reduced_plan.actions],
                "Compare responses and state with the contract's reviewed normalizers.",
            ],
        )

    if not await reducer.reproduces(contract, plan, "drop_actions", "original plan"):
        return result(plan, contract, "original_not_reproduced")

    current = plan
    granularity = 2
    while len(current.actions) >= 2:
        if reducer.exhausted():
            return result(current, contract, "budget_exhausted")
        steps = [a.step for a in current.actions]
        size = max(1, len(steps) // granularity)
        chunks = [steps[i : i + size] for i in range(0, len(steps), size)]
        reduced = False
        for chunk in chunks:
            if reducer.exhausted():
                return result(current, contract, "budget_exhausted")
            candidate = current.without(set(chunk))
            if candidate.actions and await reducer.reproduces(
                contract, candidate, "drop_actions", f"drop {', '.join(chunk)}"
            ):
                current, granularity, reduced = candidate, max(granularity - 1, 2), True
                break
        if not reduced:
            if size == 1:
                break
            granularity = min(granularity * 2, len(steps))

    reduced_contract = contract
    for kind, rows in contract.fixture.entities.items():
        for row in rows:
            if reducer.exhausted():
                return result(current, reduced_contract, "budget_exhausted")
            if row["id"] in _referenced(current) or row["id"] in contract.fixture.aliases.values():
                continue  # still a prerequisite of the remaining plan
            entities = {
                k: [r for r in rs if not (k == kind and r["id"] == row["id"])]
                for k, rs in reduced_contract.fixture.entities.items()
            }
            candidate_contract = reduced_contract.model_copy(
                update={"fixture": reduced_contract.fixture.model_copy(update={"entities": entities})}
            )
            if await reducer.reproduces(candidate_contract, current, "drop_entity", f"drop {kind} {row['id']}"):
                reduced_contract = candidate_contract
    return result(current, reduced_contract, "no_further_reduction")
