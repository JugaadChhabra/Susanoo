from susanoo.graph import build_graph, real_heads
from susanoo.graph import stubs
from susanoo.graph.state import GraphState
from susanoo.heads import agent_smith, builder, diagnostician, fixer, planner, runner, simulator, verifier


def test_real_heads_replace_the_built_stubs_and_leave_the_rest():
    h = real_heads()
    assert h.intake is planner.intake
    assert h.user_questions is planner.user_questions
    assert h.scenario_approval is planner.scenario_approval
    assert h.planner is planner.planner
    assert h.builder is builder.run
    assert h.agent_smith is agent_smith.run
    assert h.simulator is simulator.run
    assert h.runner is runner.run
    assert h.diagnostician is diagnostician.run
    assert h.fixer is fixer.run
    assert h.verifier is verifier.run
    for name in ("human_gate", "done"):  # the HumanGate stays the stub gate implementation
        assert getattr(h, name) is getattr(stubs, name)


def test_graph_compiles_with_real_heads_and_keeps_the_same_nodes():
    real = build_graph(real_heads()).get_graph()
    stub = build_graph().get_graph()
    assert set(real.nodes) == set(stub.nodes)
    assert {(e.source, e.target) for e in real.edges} == {(e.source, e.target) for e in stub.edges}
    assert GraphState  # state type unchanged
