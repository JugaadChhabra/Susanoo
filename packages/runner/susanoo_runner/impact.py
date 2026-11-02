"""Fresh task impact and 2x2 experiments [Spec §15 › Fresh task impact, F04].

The same pinned agent, task and evaluator run on fresh simulator and reference fixtures, paired by
case and repetition. A changed task result is an *association* until a controlled intervention
(swap one configuration difference on the simulator side and rerun) flips it; only then is it
labelled ``discrepancy_induced``. Missing action logs leave dependency coverage ``uncertain``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from susanoo_contracts import FixtureSpec, content_hash
from susanoo_sdk import AgentAdapter
from susanoo_simulator import FaultProfile
from susanoo_simulator.adapter import SimulatorFixtureAdapter

from .executor import AttemptExecutor, AttemptResult
from .fidelity import EnvironmentRef
from .journal import Journal
from .suite import Limits, LocalCase

Relationship = Literal[
    "simulator_pass_reference_fail", "simulator_fail_reference_pass", "no_observed_change", "inconclusive"
]
# Which operation a policy key governs, for dependency mapping.
POLICY_OPERATIONS = {"single_open_promise": "create_promise", "max_promise_days": "create_promise"}


class ConfigDelta(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    kind: Literal["policy", "fault"]
    key: str
    operation: str | None
    simulator: Any = None
    reference: Any = None


class SideResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    business_verdict: str
    execution_state: str
    failed_checks: list[str] = Field(default_factory=list)
    operations: list[str] | None = None


class Intervention(BaseModel):
    model_config = ConfigDict(extra="forbid")
    delta_id: str
    simulator_after_swap: SideResult
    flipped_to_reference: bool


class PairOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str
    repetition: int
    simulator: SideResult
    reference: SideResult
    relationship: Relationship
    dependencies: dict[str, Literal["observed_dependency", "declared_dependency", "uncertain", "none"]]
    attribution: Literal["none", "association", "discrepancy_induced"]
    interventions: list[Intervention] = Field(default_factory=list)


def config_deltas(simulator: EnvironmentRef, reference: EnvironmentRef) -> list[ConfigDelta]:
    deltas = []
    for key in sorted(set(simulator.policy) | set(reference.policy)):
        if simulator.policy.get(key) != reference.policy.get(key):
            deltas.append(
                ConfigDelta(
                    id=f"policy:{key}",
                    kind="policy",
                    key=key,
                    operation=POLICY_OPERATIONS.get(key),
                    simulator=simulator.policy.get(key),
                    reference=reference.policy.get(key),
                )
            )
    sim_rules = {content_hash(r.model_dump(mode="json")): r for r in simulator.faults.rules}
    ref_rules = {content_hash(r.model_dump(mode="json")): r for r in reference.faults.rules}
    for digest in sorted(set(sim_rules) ^ set(ref_rules)):
        rule = sim_rules.get(digest) or ref_rules[digest]
        deltas.append(
            ConfigDelta(
                id=f"fault:{rule.kind}:{rule.operation}",
                kind="fault",
                key=rule.kind,
                operation=rule.operation,
                simulator=digest in sim_rules,
                reference=digest in ref_rules,
            )
        )
    return deltas


def apply_delta(env: EnvironmentRef, delta: ConfigDelta, reference: EnvironmentRef) -> EnvironmentRef:
    """Swap exactly one difference on ``env`` to the reference's value (the controlled intervention)."""
    if delta.kind == "policy":
        policy = dict(env.policy)
        if delta.reference is None:
            policy.pop(delta.key, None)
        else:
            policy[delta.key] = delta.reference
        return env.model_copy(update={"policy": policy})
    keep = [r for r in env.faults.rules if not (r.kind == delta.key and r.operation == delta.operation)]
    keep += [r for r in reference.faults.rules if r.kind == delta.key and r.operation == delta.operation]
    return env.model_copy(update={"faults": FaultProfile(name=f"{env.faults.name}+swap", rules=keep)})


def side_fixture(fixture: FixtureSpec, env: EnvironmentRef) -> FixtureSpec:
    """The case's seed and its own injected faults, with this side's policy and fault configuration."""
    recipe = dict(fixture.recipe)
    seed = dict(recipe["seed"])
    entities = {kind: rows for kind, rows in seed["entities"].items() if kind != "policy"}
    if env.policy:
        entities["policy"] = [{"id": "policy", **env.policy}]
    seed["entities"] = entities
    case_rules = recipe.get("faults", {}).get("rules", [])
    rules = case_rules + [r.model_dump(mode="json") for r in env.faults.rules]
    recipe.update(seed=seed, faults={"name": f"{env.label}:{env.faults.name}", "rules": rules})
    return fixture.model_copy(update={"recipe": recipe, "digest": content_hash(recipe)})


def _relationship(sim: SideResult, ref: SideResult) -> Relationship:
    if "UNKNOWN" in {sim.business_verdict, ref.business_verdict} or {sim.execution_state, ref.execution_state} != {
        "completed"
    }:
        return "inconclusive"
    if sim.business_verdict == ref.business_verdict:
        return "no_observed_change"
    return "simulator_pass_reference_fail" if sim.business_verdict == "PASS" else "simulator_fail_reference_pass"


class ImpactRunner:
    def __init__(
        self,
        journal: Journal,
        *,
        simulator_url: str,
        admin_key: str,
        limits: Limits,
        agent_factory: Callable[[], AgentAdapter],
    ):
        self.journal, self.limits, self.agent_factory = journal, limits, agent_factory
        self.fixtures = SimulatorFixtureAdapter(simulator_url, admin_key, keep_action_logs=True)

    async def run_side(self, case: LocalCase, env: EnvironmentRef, label: str) -> SideResult:
        sided = case.model_copy(update={"fixture": side_fixture(case.fixture, env)})
        executor = AttemptExecutor(
            self.journal,
            run_id=f"impact-{label}",
            limits=self.limits,
            agent_factory=self.agent_factory,
            fixtures=self.fixtures,
        )
        result: AttemptResult = await executor.run(sided)
        log = self.fixtures.action_logs.get(result.namespace)
        operations = None if log is None else sorted({e["operation"] for e in log if e["mutating"]})
        return SideResult(
            business_verdict=result.business_verdict,
            execution_state=result.execution_state,
            failed_checks=[o.check_id for o in result.outcomes if o.verdict == "FAIL"],
            operations=operations,
        )

    async def pair(
        self, case: LocalCase, simulator: EnvironmentRef, reference: EnvironmentRef, repetition: int = 0
    ) -> PairOutcome:
        sim = await self.run_side(case, simulator, "sim")
        ref = await self.run_side(case, reference, "ref")
        relationship = _relationship(sim, ref)
        deltas = config_deltas(simulator, reference)
        dependencies = {}
        for delta in deltas:
            if sim.operations is None:
                dependencies[delta.id] = "uncertain"
            elif delta.operation in sim.operations:
                dependencies[delta.id] = "observed_dependency"
            else:
                dependencies[delta.id] = "none"
        interventions = []
        attribution = "none"
        if relationship in {"simulator_pass_reference_fail", "simulator_fail_reference_pass"}:
            attribution = "association"
            for delta in deltas:
                if dependencies[delta.id] == "none":
                    continue
                swapped = await self.run_side(case, apply_delta(simulator, delta, reference), "swap")
                flipped = swapped.business_verdict == ref.business_verdict and swapped.execution_state == "completed"
                interventions.append(
                    Intervention(delta_id=delta.id, simulator_after_swap=swapped, flipped_to_reference=flipped)
                )
                if flipped:
                    attribution = "discrepancy_induced"
        return PairOutcome(
            case_id=case.id,
            repetition=repetition,
            simulator=sim,
            reference=ref,
            relationship=relationship,
            dependencies=dependencies,
            attribution=attribution,
            interventions=interventions,
        )

    async def aclose(self) -> None:
        await self.fixtures.aclose()


class TwoByTwoCell(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent: Literal["old", "new"]
    environment: Literal["old", "new"]
    results: list[SideResult]


async def two_by_two(
    journal: Journal,
    case: LocalCase,
    *,
    simulator_url: str,
    admin_key: str,
    limits: Limits,
    old_agent: Callable[[], AgentAdapter],
    new_agent: Callable[[], AgentAdapter],
    old_env: EnvironmentRef,
    new_env: EnvironmentRef,
    repetitions: int = 1,
) -> list[TwoByTwoCell]:
    """All four agent x environment cells with fixed criteria. No additive "accuracy gap" is derived."""
    cells = []
    for agent_label, factory in (("old", old_agent), ("new", new_agent)):
        runner = ImpactRunner(
            journal, simulator_url=simulator_url, admin_key=admin_key, limits=limits, agent_factory=factory
        )
        try:
            for env_label, env in (("old", old_env), ("new", new_env)):
                results = [
                    await runner.run_side(case, env, f"2x2-{agent_label}-{env_label}") for _ in range(repetitions)
                ]
                cells.append(TwoByTwoCell(agent=agent_label, environment=env_label, results=results))
        finally:
            await runner.aclose()
    return cells
