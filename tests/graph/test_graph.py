"""End-to-end tests of the stub graph. [PRD › LangGraph flow and shared state]"""

from __future__ import annotations

import pytest
from langgraph.types import Command

from graph_helpers import run_graph
from susanoo.emit import InMemorySink, use_sink
from susanoo.graph.build import Heads, build_graph
from susanoo.graph.checkpointer import memory_checkpointer
from susanoo.graph.state import initial_state

# Every arrow in the PRD state diagram, and nothing else.
PRD_EDGES = {
    ("__start__", "Intake"),
    ("Intake", "UserQuestions"),
    ("UserQuestions", "ScenarioApproval"),
    ("Intake", "ScenarioApproval"),
    ("ScenarioApproval", "Planner"),
    ("Planner", "Builder"),
    ("Builder", "AgentSmith"),
    ("AgentSmith", "Simulator"),
    ("Simulator", "Runner"),
    ("Runner", "Done"),
    ("Runner", "Diagnostician"),
    ("Diagnostician", "Fixer"),
    ("Diagnostician", "HumanGate"),
    ("HumanGate", "Fixer"),
    ("HumanGate", "Runner"),
    ("Fixer", "Verifier"),
    ("Verifier", "Runner"),
    ("Verifier", "Diagnostician"),
    ("Verifier", "Done"),
    ("Done", "__end__"),
}


NODES = {
    "Intake", "UserQuestions", "ScenarioApproval", "Planner", "Builder", "AgentSmith", "Simulator",
    "Runner", "Diagnostician", "HumanGate", "Fixer", "Verifier", "Done",
}  # fmt: skip

# The one transition beyond the PRD diagram: a budget stop may jump from any head straight to Done.
BUDGET_EDGES = {(n, "Done") for n in NODES - {"Done"}}


def test_graph_has_exactly_the_prd_edges_plus_the_budget_stop_to_done():
    graph = build_graph(checkpointer=memory_checkpointer()).get_graph()
    edges = {(e.source, e.target) for e in graph.edges}
    assert edges == PRD_EDGES | BUDGET_EDGES
    assert edges - PRD_EDGES == BUDGET_EDGES - PRD_EDGES  # Runner/Verifier -> Done are already in the PRD
    assert {t for _, t in edges - PRD_EDGES} == {"Done"}
    assert set(graph.nodes) - {"__start__", "__end__"} == NODES


async def test_hero_story_visits_every_node_in_order():
    run = await run_graph()
    assert run.nodes == [
        "Intake", "UserQuestions", "ScenarioApproval", "Planner", "Builder", "AgentSmith", "Simulator",
        "Runner", "Diagnostician", "Fixer", "Verifier",  # prompt_policy fix, rolled back
        "Diagnostician", "HumanGate", "Fixer", "Verifier",  # test layer, human approves, kept
        "Runner", "Done",
    ]  # fmt: skip
    assert [i["kind"] for i in run.interrupts] == ["questions", "scenarios", "gate"]
    assert run.final["status"] == "done"
    assert run.final["stop_reason"] == "all_passed"
    assert [p.verdict for p in run.final["patches"]] == ["rolled_back", "kept"]
    assert [d.layer for d in run.final["failure_ledger"]] == ["prompt_policy", "test"]


async def test_events_are_sequenced_and_plain_english():
    run = await run_graph()
    assert [e.seq for e in run.events] == list(range(1, len(run.events) + 1))
    assert all(e.run_id == "run-1" and e.message and not e.message.lstrip().startswith(("{", "[")) for e in run.events)
    types = [e.type for e in run.events]
    assert types.count("question_needed") == 1  # exactly once, not re-emitted when the node re-runs on resume
    assert types.count("approval_needed") == 2  # scenario approval + human gate
    assert run.events[0].type == "started" and run.events[-1].type == "finished"
    assert run.events[-1].head == "system"
    # approval_needed comes from planner (scenarios) and fixer (human gate)
    assert [e.head for e in run.events if e.type == "approval_needed"] == ["planner", "fixer"]


async def test_intake_with_no_questions_skips_user_questions():
    run = await run_graph({"intake_questions": []})
    assert "UserQuestions" not in run.nodes
    assert run.nodes[:3] == ["Intake", "ScenarioApproval", "Planner"]
    assert "question_needed" not in [e.type for e in run.events]
    assert [i["kind"] for i in run.interrupts] == ["scenarios", "gate"]


async def test_user_answers_flow_into_the_plan():
    run = await run_graph(answers={"questions": {"answers": {"q-strictness": "block"}}})
    assert run.final["plan"].intake_answers == {"q-health-auth": "allow", "q-strictness": "block"}


async def test_scenario_approval_locks_approved_cards_and_drops_rejected_ones():
    run = await run_graph(answers={"scenarios": {"approved_ids": ["acc-1"]}})
    acceptance = [s for s in run.final["scenarios"] if s.split == "acceptance"]
    assert [s.id for s in acceptance] == ["acc-1"]
    assert acceptance[0].approved_by_user is True


async def test_human_gate_reject_marks_scenarios_disputed_and_returns_to_runner():
    run = await run_graph(answers={"gate": {"approved": False}})
    # after the rejection the graph goes HumanGate -> Runner; disputed scenarios no longer count as failures
    i = run.nodes.index("HumanGate")
    assert run.nodes[i + 1] == "Runner"
    assert run.nodes[-2:] == ["Runner", "Done"]
    assert run.final["disputed_scenario_ids"] == ["tr-1"]
    assert run.final["gate_decision"] == "rejected"
    assert run.final["iteration"] == 2  # round 1 was the rolled-back fix; the rejected gate used up round 2
    assert run.final["stop_reason"] == "all_passed"
    assert run.events[-1].message == "Every scenario passes except 1 you disputed. The agent is ready."
    rejected = [e for e in run.events if e.head == "fixer" and e.payload.get("decision") == "rejected"]
    assert rejected and rejected[0].payload["disputed_scenario_ids"] == ["tr-1"]
    assert len(run.final["patches"]) == 1  # no second fix was made


async def test_human_gate_approve_goes_to_fixer():
    run = await run_graph()
    i = run.nodes.index("HumanGate")
    assert run.nodes[i + 1] == "Fixer"
    assert run.final["gate_decision"] == "approved"


async def test_non_test_layer_skips_the_human_gate():
    run = await run_graph(answers={"gate": {"approved": True}})
    first_dx = run.nodes.index("Diagnostician")
    assert run.nodes[first_dx + 1] == "Fixer"  # prompt_policy layer
    assert run.nodes.count("HumanGate") == 1  # only the test-layer diagnosis went through it


# --- interrupts really pause and resume through a checkpointer --------------------------------


async def test_interrupts_pause_and_resume_on_the_same_thread():
    saver = memory_checkpointer()
    graph = build_graph(checkpointer=saver)
    config = {"configurable": {"thread_id": "t-1"}}
    sink = InMemorySink()
    with use_sink(sink):
        out = await graph.ainvoke(initial_state("t-1", "s"), config)
        assert out["__interrupt__"][0].value["kind"] == "questions"
        snap = await graph.aget_state(config)
        assert snap.next == ("UserQuestions",)
        assert snap.values["status"] == "awaiting_approval"
        assert "plan" not in snap.values  # nothing past the pause has run
        paused_events = len(sink.events)

        # a brand-new graph object on the same saver and thread resumes where the first stopped
        graph2 = build_graph(checkpointer=saver)
        out = await graph2.ainvoke(Command(resume={"answers": {"q-strictness": "ignore"}}), config)
        assert out["__interrupt__"][0].value["kind"] == "scenarios"
        snap = await graph2.aget_state(config)
        assert snap.next == ("ScenarioApproval",)
        assert snap.values["intake_answers"]["q-strictness"] == "ignore"

        out = await graph2.ainvoke(Command(resume={"approved_ids": ["acc-1", "acc-2"]}), config)
        assert out["__interrupt__"][0].value["kind"] == "gate"
        snap = await graph2.aget_state(config)
        assert snap.next == ("HumanGate",)
        assert snap.values["status"] == "awaiting_approval"

        out = await graph2.ainvoke(Command(resume={"approved": True}), config)
        assert "__interrupt__" not in out
        assert out["status"] == "done"
    assert paused_events == 4  # started, finished (intake), started, question_needed


async def test_separate_threads_do_not_share_state():
    saver = memory_checkpointer()
    graph = build_graph(checkpointer=saver)
    with use_sink(InMemorySink()):
        await graph.ainvoke(initial_state("a", "s"), {"configurable": {"thread_id": "a"}})
        await graph.ainvoke({**initial_state("b", "s"), "intake_questions": []}, {"configurable": {"thread_id": "b"}})
        assert (await graph.aget_state({"configurable": {"thread_id": "a"}})).next == ("UserQuestions",)
        assert (await graph.aget_state({"configurable": {"thread_id": "b"}})).next == ("ScenarioApproval",)


# --- stop conditions end the loop at Done (verifier replaced to force each one) ---------------


def _verifier_forcing(**overrides):
    from susanoo.graph import stubs

    async def verifier(state):
        update = await stubs.verifier(state)
        # make the latest patch "kept" so only the stop conditions can send the run to Done
        patches = list(update["patches"])
        patches[-1] = patches[-1].model_copy(update={"verdict": "kept"})
        return {**update, "patches": patches, **overrides}

    return verifier


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"iteration": 5}, "iteration_cap"),
        ({"cost_usd": 99.0}, "budget"),
    ],
)
async def test_verifier_stop_conditions_end_the_run(overrides, reason):
    heads = Heads(verifier=_verifier_forcing(**overrides))
    run = await run_graph(heads=heads)
    assert run.nodes[-2:] == ["Verifier", "Done"]
    assert run.final["status"] == "done"
    assert run.final["stop_reason"] == reason


async def test_verifier_stops_after_two_iterations_without_gain():
    from susanoo.graph import stubs

    async def flat_verifier(state):
        update = await stubs.verifier(state)
        patches = list(update["patches"])
        patches[-1] = patches[-1].model_copy(update={"verdict": "rolled_back"})  # never any gain
        metrics = list(update["metrics"])
        metrics[-1] = metrics[-1].model_copy(update={"heldout_f1": 0.5})
        return {**update, "patches": patches, "metrics": metrics}

    run = await run_graph(heads=Heads(verifier=flat_verifier))
    assert run.final["stop_reason"] == "no_gain"
    assert run.final["iteration"] == 2
    assert run.nodes.count("Verifier") == 2
