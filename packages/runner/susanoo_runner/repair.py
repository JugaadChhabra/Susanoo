"""Repair gate for the simulator layer [Spec §15 › Repair gate, F06].

A repaired simulator configuration must (1) no longer reproduce the reduced discrepancy, (2) keep
every previously matching valid control matching and (3) match on fresh held-out plans never used
during reduction. The comparison criteria (normalizers, compared kinds, operations, window) must be
unchanged: a simulator repair cannot quietly weaken how it is judged. Each layer is pinned
separately. If the reduced case no longer reproduces even before the repair, the result is
``inconclusive``, not a success.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from susanoo_contracts import content_hash

from susanoo_evaluator.normalize import Normalizer

from .fidelity import ActionPlan, EnvironmentRef, FidelityContract, compare
from .reduce import Predicate, ReductionResult


class RepairProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    layer: Literal["simulator"] = "simulator"
    before: EnvironmentRef
    after: EnvironmentRef
    rationale: str = Field(min_length=1)
    normalizers: list[Normalizer] | None = Field(
        default=None, description="Any change here alters the comparison criteria and fails the gate"
    )

    @property
    def digest(self) -> str:
        return content_hash(self.model_dump(mode="json"))


class GateCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    passed: bool
    detail: str


class RepairValidation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposal_digest: str
    layer: str
    pinned: dict[str, str]
    outcome: Literal["accepted", "rejected", "inconclusive"]
    checks: list[GateCheck]


async def validate_simulator_repair(
    contract: FidelityContract,
    proposal: RepairProposal,
    reduction: ReductionResult,
    predicate: Predicate,
    *,
    controls: list[ActionPlan],
    heldout: list[ActionPlan],
    sim_env,
    ref_env,
) -> RepairValidation:
    if proposal.before.digest != contract.simulator.digest:
        raise ValueError("The proposal does not start from the contract's pinned simulator")
    reduced_before = contract.model_copy(update={"fixture": reduction.reduced_fixture})
    reduced_after = reduced_before.model_copy(update={"simulator": proposal.after})
    repaired = contract.model_copy(update={"simulator": proposal.after})
    if proposal.normalizers is not None:
        repaired = repaired.model_copy(update={"normalizers": proposal.normalizers})
    checks: list[GateCheck] = []

    still_there = await compare(reduced_before, reduction.reduced_plan, sim_env, ref_env)
    if still_there.status != "compared" or not predicate(still_there):
        checks.append(
            GateCheck(
                name="reproduction_before_repair",
                passed=False,
                detail="The reduced case no longer reproduces before the repair",
            )
        )
        return _result(contract, proposal, "inconclusive", checks)
    checks.append(GateCheck(name="reproduction_before_repair", passed=True, detail="reduced case reproduces"))

    after = await compare(reduced_after, reduction.reduced_plan, sim_env, ref_env)
    fixed = after.status == "compared" and not predicate(after)
    checks.append(
        GateCheck(
            name="reduced_reproduction_fixed",
            passed=fixed,
            detail=f"{len(after.candidates)} candidate(s) remain on the reduced plan",
        )
    )

    criteria = repaired.criteria_digest == contract.criteria_digest
    checks.append(
        GateCheck(
            name="criteria_unchanged",
            passed=criteria,
            detail="normalizers, compared kinds, operations and window unchanged",
        )
    )

    broken = []
    for plan in controls:
        baseline = await compare(contract, plan, sim_env, ref_env)
        if not baseline.matched:
            raise ValueError(f"Control {plan.id} does not match before the repair; it is not a valid control")
        if not (await compare(repaired, plan, sim_env, ref_env)).matched:
            broken.append(plan.id)
    checks.append(
        GateCheck(
            name="valid_controls_preserved",
            passed=not broken,
            detail=f"broken controls: {broken}" if broken else f"{len(controls)} control(s) still match",
        )
    )

    used = {reduction.original_plan.digest, reduction.reduced_plan.digest}
    failing = []
    for plan in heldout:
        if plan.digest in used:
            raise ValueError(f"Held-out plan {plan.id} was used during reduction")
        if not (await compare(repaired, plan, sim_env, ref_env)).matched:
            failing.append(plan.id)
    checks.append(
        GateCheck(
            name="heldout_matches",
            passed=not failing,
            detail=f"held-out mismatches: {failing}" if failing else f"{len(heldout)} held-out plan(s) match",
        )
    )
    outcome = "accepted" if all(c.passed for c in checks) else "rejected"
    return _result(contract, proposal, outcome, checks)


def _result(contract, proposal, outcome, checks) -> RepairValidation:
    return RepairValidation(
        proposal_digest=proposal.digest,
        layer=proposal.layer,
        pinned={
            "simulator_before": proposal.before.digest,
            "simulator_after": proposal.after.digest,
            "reference": contract.reference.digest,
            "criteria": contract.criteria_digest,
        },
        outcome=outcome,
        checks=checks,
    )
