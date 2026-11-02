"""Group B runtime: fidelity, differential replay, task impact, reduction and repair [Spec §14-15, F01-F06]."""

import pytest
from collections_demo import CollectionsAgent, build_suite
from collections_demo import fidelity as demo
from pydantic import ValidationError
from susanoo_evaluator.normalize import Normalizer, NormalizerSet
from susanoo_runner import Journal, Limits, LocalSuite, SimulatorServer
from susanoo_runner.fidelity import ActionPlan, PlannedAction, compare
from susanoo_runner.impact import ImpactRunner, config_deltas, two_by_two
from susanoo_runner.reduce import LABEL, reduce_reproduction
from susanoo_runner.repair import validate_simulator_repair
from susanoo_sdk import InProcessAgentAdapter
from susanoo_simulator.environment import SimulatorEnvironment

SUITE = LocalSuite.model_validate(build_suite())
CASES = {case.id: case for case in SUITE.cases}


@pytest.fixture(scope="module")
def server():
    with SimulatorServer() as running:
        yield running


@pytest.fixture
async def envs(server):
    sim = SimulatorEnvironment(server.base_url, server.admin_key, label="simulator")
    ref = SimulatorEnvironment(server.base_url, server.admin_key, label="reference")
    yield sim, ref
    await sim.aclose()
    await ref.aclose()


# --- Normalizers ----------------------------------------------------------------------------


def test_normalizers_cannot_target_meaning_bearing_fields():
    with pytest.raises(ValidationError):
        Normalizer(kind="generated_id", path="create_promise.status", reviewed_by="r", rationale="no")
    with pytest.raises(ValidationError):
        Normalizer(kind="generated_id", path="promise.id")  # reviewer and rationale are mandatory


def test_normalizers_never_hide_counts_or_unmapped_ids():
    reviewed = dict(reviewed_by="r", rationale="x")
    norms = NormalizerSet(
        [
            Normalizer(kind="unordered_collection", path="items", **reviewed),
            Normalizer(kind="generated_id", path="items.id", **reviewed),
        ],
        id_map={"S-1": "<a>", "R-9": "<a>"},
    )
    assert norms.diff({"items": [{"id": "S-1"}]}, {"items": [{"id": "R-9"}]}) == []
    assert norms.diff({"items": [{"id": "S-1"}, {"id": "S-2"}]}, {"items": [{"id": "R-9"}]})[0].kind == "length"
    assert norms.diff({"items": [{"id": "S-1"}]}, {"items": [{"id": "R-7"}]})[0].kind == "value"


# --- F01-F03: preflight and action comparison ----------------------------------------------


async def test_seeded_discrepancies_are_found_and_left_unclassified(envs):
    sim, ref = envs
    result = await compare(demo.contract(), demo.SESSION, sim, ref)
    assert result.status == "compared" and result.synthetic
    categories = {(c.category, c.step) for c in result.candidates}
    assert ("duplicate_write", "s5") in categories
    assert ("access_control", "s6") in categories
    assert {c.classification for c in result.candidates} == {"unresolved"}
    assert "promise.id" in result.normalizers_applied
    assert len(result.simulator_state["promise"]) == 2 and len(result.reference_state["promise"]) == 1


async def test_valid_controls_match(envs):
    sim, ref = envs
    for plan in demo.CONTROLS:
        assert (await compare(demo.contract(), plan, sim, ref)).matched, plan.id


async def test_mismatched_prerequisites_block_the_comparison(envs):
    sim, ref = envs
    contract = demo.contract()
    other = contract.reference.model_copy(update={"domain_version": "9.9"})
    blocked = await compare(contract.model_copy(update={"reference": other}), demo.SESSION, sim, ref)
    assert blocked.status == "blocked_preflight" and blocked.candidates == []
    narrow = contract.model_copy(update={"operations": ["delete_everything"]})
    assert (await compare(narrow, demo.SESSION, sim, ref)).status == "blocked_preflight"


async def test_declared_simplification_is_a_hint_not_a_classification(envs):
    sim, ref = envs
    contract = demo.contract()
    plan = ActionPlan(
        id="note",
        actions=[
            PlannedAction(
                step="h",
                operation="request_handoff",
                request={"account_id": "ACC-2001", "reason": "other", "note": "call after 6pm"},
            )
        ],
    )
    # Make the simulator drop notes by corrupting the field: an inspectable stand-in for a simplification.
    from susanoo_simulator import FaultProfile, FaultRule

    dropping = contract.simulator.model_copy(
        update={
            "faults": FaultProfile(
                rules=[FaultRule(kind="wrong_field", operation="request_handoff", field="note", value=None)]
            )
        }
    )
    result = await compare(contract.model_copy(update={"simulator": dropping}), plan, sim, ref)
    note = [c for c in result.candidates if c.paths == ["handoff.note"]]
    assert note and note[0].matches_declared_simplification and note[0].classification == "unresolved"


async def test_plans_with_broken_prerequisites_are_rejected(envs):
    sim, ref = envs
    plan = ActionPlan(
        id="bad",
        actions=[
            PlannedAction(
                step="b", operation="read_account", request={"account_id": {"$alias": "a.promise.account_id"}}
            ),
        ],
    )
    with pytest.raises(ValueError):
        await compare(demo.contract(), plan, sim, ref)


# --- F05: reduction --------------------------------------------------------------------------


async def test_reduction_keeps_the_violation_and_prerequisites(envs):
    sim, ref = envs
    result = await reduce_reproduction(demo.contract(), demo.SESSION, demo.duplicate_predicate, sim, ref, budget=40)
    assert result.label == LABEL
    assert result.stopped_because == "no_further_reduction"
    assert [a.step for a in result.reduced_plan.actions] == ["s3", "s5"]
    assert [a["id"] for a in result.reduced_fixture.entities["account"]] == ["ACC-2001"]
    assert result.runs_used <= result.budget
    assert len(result.attempts) == result.runs_used  # every candidate run is logged
    assert {a.outcome for a in result.attempts} <= {"reproduced", "not_reproduced"}


async def test_reduction_respects_its_budget(envs):
    sim, ref = envs
    result = await reduce_reproduction(demo.contract(), demo.SESSION, demo.duplicate_predicate, sim, ref, budget=3)
    assert result.stopped_because == "budget_exhausted" and result.runs_used <= 3


async def test_reduction_reports_when_nothing_reproduces(envs):
    sim, ref = envs
    result = await reduce_reproduction(demo.contract(), demo.CONTROLS[0], demo.duplicate_predicate, sim, ref)
    assert result.stopped_because == "original_not_reproduced"


# --- F06: repair gate ------------------------------------------------------------------------


async def test_full_repair_is_accepted(envs):
    sim, ref = envs
    contract = demo.contract()
    reduction = await reduce_reproduction(contract, demo.SESSION, demo.duplicate_predicate, sim, ref)
    validation = await validate_simulator_repair(
        contract,
        demo.repair_proposal(),
        reduction,
        demo.duplicate_predicate,
        controls=demo.CONTROLS,
        heldout=demo.HELDOUT,
        sim_env=sim,
        ref_env=ref,
    )
    assert validation.outcome == "accepted", validation.checks
    assert validation.pinned["simulator_before"] != validation.pinned["simulator_after"]


async def test_partial_repair_fails_held_out_and_criteria_changes_fail(envs):
    sim, ref = envs
    contract = demo.contract()
    reduction = await reduce_reproduction(contract, demo.SESSION, demo.duplicate_predicate, sim, ref)
    partial = demo.repair_proposal().model_copy(
        update={
            "after": demo.SIMULATOR.model_copy(update={"policy": {"max_promise_days": 30, "single_open_promise": True}})
        }
    )
    result = await validate_simulator_repair(
        contract,
        partial,
        reduction,
        demo.duplicate_predicate,
        controls=demo.CONTROLS,
        heldout=demo.HELDOUT,
        sim_env=sim,
        ref_env=ref,
    )
    checks = {c.name: c.passed for c in result.checks}
    assert result.outcome == "rejected" and checks["reduced_reproduction_fixed"] and not checks["heldout_matches"]
    weakened = demo.repair_proposal().model_copy(update={"normalizers": []})
    result = await validate_simulator_repair(
        contract,
        weakened,
        reduction,
        demo.duplicate_predicate,
        controls=demo.CONTROLS,
        heldout=demo.HELDOUT,
        sim_env=sim,
        ref_env=ref,
    )
    assert result.outcome == "rejected"
    assert not {c.name: c.passed for c in result.checks}["criteria_unchanged"]


async def test_held_out_plans_cannot_come_from_the_reduction(envs):
    sim, ref = envs
    contract = demo.contract()
    reduction = await reduce_reproduction(contract, demo.SESSION, demo.duplicate_predicate, sim, ref)
    with pytest.raises(ValueError):
        await validate_simulator_repair(
            contract,
            demo.repair_proposal(),
            reduction,
            demo.duplicate_predicate,
            controls=demo.CONTROLS,
            heldout=[reduction.reduced_plan],
            sim_env=sim,
            ref_env=ref,
        )


# --- F04: fresh task impact ------------------------------------------------------------------


def test_config_deltas_list_every_difference():
    ids = {d.id for d in config_deltas(demo.SIMULATOR, demo.REFERENCE)}
    assert ids == {"policy:single_open_promise", "fault:permission_bypass:waive_fees"}


@pytest.mark.parametrize(
    "mode,case_id,relationship,attribution,delta",
    [
        ("duplicate", "C13", "simulator_fail_reference_pass", "discrepancy_induced", "policy:single_open_promise"),
        (
            "forbidden_change",
            "C01",
            "simulator_fail_reference_pass",
            "discrepancy_induced",
            "fault:permission_bypass:waive_fees",
        ),
        ("correct", "C01", "no_observed_change", "none", None),
    ],
)
async def test_fresh_pairs_attribute_only_after_intervention(
    server, tmp_path, mode, case_id, relationship, attribution, delta
):
    runner = ImpactRunner(
        Journal(tmp_path / "impact.sqlite"),
        simulator_url=server.base_url,
        admin_key=server.admin_key,
        limits=Limits(),
        agent_factory=lambda: InProcessAgentAdapter(CollectionsAgent(mode)),
    )
    try:
        pair = await runner.pair(CASES[case_id], demo.SIMULATOR, demo.REFERENCE)
    finally:
        await runner.aclose()
    assert (pair.relationship, pair.attribution) == (relationship, attribution)
    if delta:
        assert pair.dependencies[delta] == "observed_dependency"
        assert [i.delta_id for i in pair.interventions if i.flipped_to_reference] == [delta]


async def test_two_by_two_keeps_all_four_cells(server, tmp_path):
    cells = await two_by_two(
        Journal(tmp_path / "2x2.sqlite"),
        CASES["C13"],
        simulator_url=server.base_url,
        admin_key=server.admin_key,
        limits=Limits(),
        old_agent=lambda: InProcessAgentAdapter(CollectionsAgent("duplicate")),
        new_agent=lambda: InProcessAgentAdapter(CollectionsAgent("correct")),
        old_env=demo.SIMULATOR,
        new_env=demo.REFERENCE,
    )
    grid = {(c.agent, c.environment): c.results[0].business_verdict for c in cells}
    assert grid == {("old", "old"): "FAIL", ("old", "new"): "PASS", ("new", "old"): "PASS", ("new", "new"): "PASS"}
