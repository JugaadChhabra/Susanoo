"""Fidelity contract, preflight and fixed-action comparison [Spec §14 › F01-F03, §15 › Action comparison].

Both sides start from one approved initial state and identity. They differ only in implementation
(policy and fault configuration), which is what the comparison is about. Status codes, access
denials, record counts and missing side effects are compared raw; only reviewed normalizers apply,
and only to values. The comparison proposes discrepancy candidates; it never classifies them.
"""

from __future__ import annotations

import copy
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from susanoo_contracts import content_hash
from susanoo_evaluator.normalize import Difference, Normalizer, NormalizerSet
from susanoo_sdk import ActionContext, ActionResult, ApprovedAction, SnapshotScope
from susanoo_simulator import FaultProfile, FixtureSeed

STEP = r"^[A-Za-z][A-Za-z0-9_-]{0,63}$"
ACCESS_CODES = frozenset({"permission_denied", "operation_not_allowed"})


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EnvironmentRef(Model):
    """One side's implementation: domain version, policy record and fault configuration."""

    label: str
    adapter: Literal["susanoo_simulator"] = "susanoo_simulator"
    domain_version: str
    policy: dict[str, Any] = Field(default_factory=dict)
    faults: FaultProfile = Field(default_factory=FaultProfile)

    @property
    def digest(self) -> str:
        return content_hash(self.model_dump(mode="json"))

    def seed_for(self, fixture: FixtureSeed) -> FixtureSeed:
        entities = {kind: rows for kind, rows in fixture.entities.items() if kind != "policy"}
        if self.policy:
            entities["policy"] = [{"id": "policy", **self.policy}]
        return fixture.model_copy(update={"entities": entities, "domain_version": self.domain_version})


class Simplification(Model):
    path: str
    rationale: str = Field(min_length=1)


class FidelityContract(Model):
    id: str
    synthetic: bool = True
    simulator: EnvironmentRef
    reference: EnvironmentRef
    fixture: FixtureSeed
    actor: str = "agent"
    operations: list[str] = Field(min_length=1)
    entity_kinds: list[str] = Field(min_length=1)
    normalizers: list[Normalizer] = Field(default_factory=list)
    intentional_simplifications: list[Simplification] = Field(default_factory=list)
    reference_limitations: list[str] = Field(default_factory=list)
    consistency_window_seconds: int = Field(default=10, ge=0, le=3600)

    @property
    def digest(self) -> str:
        return content_hash(self.model_dump(mode="json"))

    @property
    def criteria_digest(self) -> str:
        """What the comparison judges by; a repair must not change it."""
        return content_hash(
            {
                "normalizers": [n.model_dump(mode="json") for n in self.normalizers],
                "entity_kinds": self.entity_kinds,
                "operations": self.operations,
                "window": self.consistency_window_seconds,
            }
        )


class PlannedAction(Model):
    step: str = Field(pattern=STEP)
    operation: str
    request: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None


class ActionPlan(Model):
    id: str
    actions: list[PlannedAction] = Field(min_length=1)

    @property
    def digest(self) -> str:
        return content_hash(self.model_dump(mode="json"))

    def references(self, action: PlannedAction) -> set[str]:
        found: set[str] = set()

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                if set(value) == {"$alias"}:
                    found.add(value["$alias"].split(".", 1)[0])
                else:
                    for item in value.values():
                        walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(action.request)
        return found

    def valid(self) -> bool:
        seen: set[str] = set()
        for action in self.actions:
            if not self.references(action) <= seen:
                return False
            seen.add(action.step)
        return True

    def without(self, steps: set[str]) -> ActionPlan:
        return self.model_copy(update={"actions": [a for a in self.actions if a.step not in steps]})


class SideOutcome(Model):
    status: str
    code: str | None = None
    response: dict[str, Any] = Field(default_factory=dict)


class StepComparison(Model):
    step: str
    operation: str
    simulator: SideOutcome
    reference: SideOutcome
    differences: list[Difference] = Field(default_factory=list)


class StateDifference(Model):
    kind: str
    entity: str | None = None
    difference: Difference


class Candidate(Model):
    id: str
    category: Literal[
        "access_control",
        "duplicate_write",
        "validation",
        "transport",
        "response_value",
        "state_value",
        "missing_side_effect",
        "extra_side_effect",
    ]
    step: str | None = None
    operation: str | None = None
    summary: str
    paths: list[str] = Field(default_factory=list)
    classification: Literal["unresolved"] = "unresolved"
    matches_declared_simplification: bool = False
    reproduction_steps: list[str] = Field(default_factory=list)


class Preflight(Model):
    ok: bool
    problems: list[str] = Field(default_factory=list)
    simulator_seed_digest: str | None = None
    reference_seed_digest: str | None = None


class ComparisonResult(Model):
    contract_id: str
    contract_digest: str
    criteria_digest: str
    plan_id: str
    plan_digest: str
    simulator_digest: str
    reference_digest: str
    status: Literal["compared", "blocked_preflight"]
    preflight: Preflight
    steps: list[StepComparison] = Field(default_factory=list)
    state_differences: list[StateDifference] = Field(default_factory=list)
    candidates: list[Candidate] = Field(default_factory=list)
    normalizers_applied: list[str] = Field(default_factory=list)
    simulator_state: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    reference_state: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    synthetic: bool = True

    @property
    def matched(self) -> bool:
        return self.status == "compared" and not self.candidates


def _resolve(value: Any, responses: dict[str, dict[str, Any]]) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$alias"}:
            step, _, path = value["$alias"].partition(".")
            target: Any = responses.get(step, {})
            for part in re.findall(r"[A-Za-z_][A-Za-z0-9_]*|\d+", path):
                target = target[int(part)] if isinstance(target, list) else target.get(part)
                if target is None:
                    raise KeyError(f"Alias {value['$alias']} does not resolve on this side")
            return target
        return {key: _resolve(item, responses) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve(item, responses) for item in value]
    return value


async def _prepare_side(env, contract: FidelityContract, ref: EnvironmentRef, namespace: str) -> str:
    return await env.prepare(namespace, ref.seed_for(contract.fixture), ref.faults)


async def preflight(contract: FidelityContract, sim_env, ref_env, sim_ns: str, ref_ns: str) -> Preflight:
    problems = []
    if contract.simulator.domain_version != contract.reference.domain_version:
        problems.append("simulator and reference pin different domain versions")
    actor = contract.fixture.actors.get(contract.actor)
    if actor is None:
        problems.append(f"fixture defines no actor {contract.actor}")
    elif not set(contract.operations) <= set(actor.operations):
        problems.append("contract operations exceed the actor's approved operations")
    if problems:
        return Preflight(ok=False, problems=problems)
    sim_digest = await _prepare_side(sim_env, contract, contract.simulator, sim_ns)
    ref_digest = await _prepare_side(ref_env, contract, contract.reference, ref_ns)
    kinds = [k for k in contract.entity_kinds if k != "policy"]
    sim_state = (await sim_env.snapshot(SnapshotScope(namespace=sim_ns, kinds=kinds))).state
    ref_state = (await ref_env.snapshot(SnapshotScope(namespace=ref_ns, kinds=kinds))).state
    differences = NormalizerSet(contract.normalizers).diff(sim_state, ref_state)
    if differences:
        problems.append(f"initial state differs at {sorted({d.path for d in differences})[:5]}")
    return Preflight(
        ok=not problems, problems=problems, simulator_seed_digest=sim_digest, reference_seed_digest=ref_digest
    )


def _outcome(result: ActionResult) -> SideOutcome:
    return SideOutcome(status=result.status, code=result.code, response=result.response)


def _register_ids(normalizers: NormalizerSet, step: str, sim: dict, ref: dict, path: str = "") -> None:
    """Map generated identifiers on both sides to one logical alias (``<step.path>``)."""
    if isinstance(sim, dict) and isinstance(ref, dict):
        for key in set(sim) & set(ref):
            _register_ids(normalizers, step, sim[key], ref[key], f"{path}.{key}" if path else key)
    elif isinstance(sim, str) and isinstance(ref, str) and normalizers._match(path, "generated_id"):
        logical = f"<{step}.{path}>"
        normalizers.id_map[sim] = logical
        normalizers.id_map[ref] = logical


def _categorize_step(step: StepComparison) -> Candidate | None:
    sim, ref = step.simulator, step.reference
    if (sim.status, sim.code) == (ref.status, ref.code) and not step.differences:
        return None
    codes = {sim.code, ref.code}
    if "uncertain" in {sim.status, ref.status} or "error" in {sim.status, ref.status}:
        category = "transport"
    elif sim.status != ref.status and codes & ACCESS_CODES:
        category = "access_control"
    elif sim.status != ref.status and codes & {"open_promise_exists", "idempotency_conflict"}:
        category = "duplicate_write"
    elif sim.status != ref.status or sim.code != ref.code:
        category = "validation"
    else:
        category = "response_value"
    if category == "response_value":
        shown = ", ".join(f"{d.path} ({d.simulator!r} vs {d.reference!r})" for d in step.differences[:3])
        summary = f"{step.operation}: both succeeded but responses differ at {shown}"
    else:
        summary = (
            f"{step.operation}: simulator {sim.status}{f' ({sim.code})' if sim.code else ''}, "
            f"reference {ref.status}{f' ({ref.code})' if ref.code else ''}"
        )
    return Candidate(
        id="",
        category=category,
        step=step.step,
        operation=step.operation,
        summary=summary,
        paths=[d.path for d in step.differences],
    )


def _state_differences(
    normalizers: NormalizerSet, sim_state: dict[str, list[dict]], ref_state: dict[str, list[dict]]
) -> list[StateDifference]:
    out: list[StateDifference] = []
    for kind in sorted(set(sim_state) | set(ref_state)):
        sim_rows, ref_rows = sim_state.get(kind, []), ref_state.get(kind, [])
        if len(sim_rows) != len(ref_rows):
            out.append(
                StateDifference(
                    kind=kind,
                    difference=Difference(path=kind, simulator=len(sim_rows), reference=len(ref_rows), kind="length"),
                )
            )
        sim_by = {normalizers.logical(row.get("id")): row for row in sim_rows}
        ref_by = {normalizers.logical(row.get("id")): row for row in ref_rows}
        for entity in sorted(set(sim_by) & set(ref_by), key=str):
            for difference in normalizers.diff(sim_by[entity], ref_by[entity], kind):
                out.append(StateDifference(kind=kind, entity=str(entity), difference=difference))
    return out


def _state_candidates(diffs: list[StateDifference], steps: list[StepComparison]) -> list[Candidate]:
    out = []
    for diff in diffs:
        d = diff.difference
        if d.kind == "length":
            more = "simulator" if d.simulator > d.reference else "reference"
            category = "duplicate_write" if more == "simulator" else "missing_side_effect"
            summary = f"{diff.kind}: simulator holds {d.simulator} record(s), reference holds {d.reference}"
        else:
            category = "state_value"
            summary = f"{diff.kind} {diff.entity}: {d.path} is {d.simulator!r} vs {d.reference!r}"
        out.append(Candidate(id="", category=category, summary=summary, paths=[d.path]))
    return out


def _finish(candidate: Candidate, contract: FidelityContract, plan: ActionPlan) -> Candidate:
    declared = {s.path for s in contract.intentional_simplifications}
    steps = [a.step for a in plan.actions]
    if candidate.step:
        steps = steps[: steps.index(candidate.step) + 1]
    identity = {
        "category": candidate.category,
        "step": candidate.step,
        "paths": candidate.paths,
        "summary": candidate.summary,
    }
    return candidate.model_copy(
        update={
            "id": "cand-" + content_hash(identity)[:12],
            "matches_declared_simplification": bool(candidate.paths)
            and all(
                any(path == d or path.startswith(d + ".") or path.startswith(d + "[") for d in declared)
                for path in candidate.paths
            ),
            "reproduction_steps": steps,
        }
    )


async def compare(contract: FidelityContract, plan: ActionPlan, sim_env, ref_env) -> ComparisonResult:
    """Run one fixed action plan on fresh simulator and reference fixtures and compare everything."""
    if not plan.valid():
        raise ValueError(f"Plan {plan.id} references a step before it runs")
    run = uuid.uuid4().hex[:12]
    sim_ns, ref_ns = f"cmp-{run}-sim", f"cmp-{run}-ref"
    base = dict(
        contract_id=contract.id,
        contract_digest=contract.digest,
        criteria_digest=contract.criteria_digest,
        plan_id=plan.id,
        plan_digest=plan.digest,
        simulator_digest=contract.simulator.digest,
        reference_digest=contract.reference.digest,
        synthetic=contract.synthetic,
    )
    try:
        checked = await preflight(contract, sim_env, ref_env, sim_ns, ref_ns)
        if not checked.ok:
            return ComparisonResult(status="blocked_preflight", preflight=checked, **base)
        normalizers = NormalizerSet(contract.normalizers)
        responses: dict[str, dict[str, dict[str, Any]]] = {"sim": {}, "ref": {}}
        steps: list[StepComparison] = []
        deadline = datetime.now(UTC) + timedelta(minutes=5)
        for action in plan.actions:
            outcomes = {}
            for side, env, namespace in (("sim", sim_env, sim_ns), ("ref", ref_env, ref_ns)):
                try:
                    request = _resolve(copy.deepcopy(action.request), responses[side])
                except KeyError as error:
                    outcomes[side] = SideOutcome(
                        status="rejected", code="alias_unresolved", response={"error": str(error)}
                    )
                    continue
                context = ActionContext(
                    attempt_id=run,
                    namespace=namespace,
                    operation_id=f"{plan.id}:{action.step}",
                    fencing_generation=1,
                    deadline=deadline,
                )
                result = await env.execute(
                    ApprovedAction(operation=action.operation, request=request, idempotency_key=action.idempotency_key),
                    context,
                )
                outcomes[side] = _outcome(result)
                responses[side][action.step] = result.response
            sim, ref = outcomes["sim"], outcomes["ref"]
            _register_ids(normalizers, action.step, sim.response, ref.response, action.operation)
            differences = []
            if sim.status == ref.status == "ok" or sim.status == ref.status == "replayed":
                differences = normalizers.diff(sim.response, ref.response, action.operation)
            steps.append(
                StepComparison(
                    step=action.step, operation=action.operation, simulator=sim, reference=ref, differences=differences
                )
            )
        await sim_env.settle(sim_ns, contract.consistency_window_seconds)
        await ref_env.settle(ref_ns, contract.consistency_window_seconds)
        sim_state = (await sim_env.snapshot(SnapshotScope(namespace=sim_ns, kinds=contract.entity_kinds))).state
        ref_state = (await ref_env.snapshot(SnapshotScope(namespace=ref_ns, kinds=contract.entity_kinds))).state
        state_diffs = _state_differences(normalizers, sim_state, ref_state)
        raw = [c for c in (_categorize_step(s) for s in steps) if c] + _state_candidates(state_diffs, steps)
        candidates = {c.id: c for c in (_finish(c, contract, plan) for c in raw)}
        return ComparisonResult(
            status="compared",
            preflight=checked,
            steps=steps,
            state_differences=state_diffs,
            candidates=list(candidates.values()),
            normalizers_applied=sorted(normalizers.applied),
            simulator_state=sim_state,
            reference_state=ref_state,
            **base,
        )
    finally:
        await sim_env.destroy(sim_ns)
        await ref_env.destroy(ref_ns)
