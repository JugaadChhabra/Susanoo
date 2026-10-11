"""Run configuration for the compiled graph."""

from __future__ import annotations

from susanoo.graph.routing import MAX_ITERATIONS

# LangGraph counts one step per node, per invocation (each resume gets a fresh count).
#   build phase           : Intake, UserQuestions, ScenarioApproval, Planner, Builder, AgentSmith, Simulator, Runner = 8
#   one improvement round : at most Diagnostician, (HumanGate | Fixer), Verifier, Runner = 4
#   rounds                : MAX_ITERATIONS, every Verifier pass and every rejected gate counts as one
#   finish                : Done = 1
# 8 + 4 * MAX_ITERATIONS + 1 = 29 for the cap of 5; +11 of headroom, so a wiring bug raises
# GraphRecursionError instead of looping, but a legitimate run never gets near it.
RECURSION_LIMIT = 8 + 4 * MAX_ITERATIONS + 1 + 11


def run_config(run_id: str) -> dict:
    """The `config` for ainvoke/astream/aget_state: thread_id is the run id, so a run resumes by its id."""
    return {"configurable": {"thread_id": run_id}, "recursion_limit": RECURSION_LIMIT}
