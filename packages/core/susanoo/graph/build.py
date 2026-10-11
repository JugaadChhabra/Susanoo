"""Wire the StateGraph exactly as the state diagram in [PRD › LangGraph flow and shared state].

    [*] -> Intake -> UserQuestions | ScenarioApproval -> Planner -> Builder -> AgentSmith -> Simulator -> Runner
    Runner -> Done | Diagnostician;  Diagnostician -> Fixer | HumanGate;  HumanGate -> Fixer | Runner
    Fixer -> Verifier;  Verifier -> Runner | Diagnostician | Done;  Done -> [*]

Node callables are injected through `Heads`, so real heads replace stubs without touching edges.
Heads are async, so run the compiled graph with `ainvoke`/`astream`. Resume paused runs with
resume.resume_run, and build the config with config.run_config(run_id).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.config import get_config
from langgraph.graph import END, START, StateGraph

from susanoo.emit import emit
from susanoo.graph import routing as r
from susanoo.graph import stubs
from susanoo.graph.pause import ALREADY_ANNOUNCED
from susanoo.graph.state import GraphState
from susanoo.guards import budget

Head = Callable[[GraphState], Awaitable[dict]]

# RunEvent.head to use for events the wiring itself emits on behalf of a node.
_EVENT_HEAD = {
    r.INTAKE: "planner",
    r.USER_QUESTIONS: "planner",
    r.SCENARIO_APPROVAL: "planner",
    r.PLANNER: "planner",
    r.BUILDER: "builder",
    r.AGENT_SMITH: "agent_smith",
    r.SIMULATOR: "simulator",
    r.RUNNER: "runner",
    r.DIAGNOSTICIAN: "diagnostician",
    r.HUMAN_GATE: "fixer",
    r.FIXER: "fixer",
    r.VERIFIER: "verifier",
}
_PAUSING = {r.USER_QUESTIONS, r.SCENARIO_APPROVAL, r.HUMAN_GATE}


async def _already_paused(graph_ref: list, name: str) -> bool:
    """True if `name` already has a pending interrupt on this thread (so its pause was announced)."""
    graph = graph_ref[0] if graph_ref else None
    thread_id = get_config().get("configurable", {}).get("thread_id")
    if graph is None or graph.checkpointer in (None, False) or thread_id is None:
        return False
    snapshot = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    return any(task.name == name and task.interrupts for task in snapshot.tasks)


def _or_done(route: Callable[[GraphState], str]) -> Callable[[GraphState], str]:
    """Edge function: a budget stop goes to Done, anything else follows `route`."""

    def router(state: GraphState) -> str:
        return r.DONE if state.get("stop_reason") == "budget" else route(state)

    return router


def _next(dst: str) -> Callable[[GraphState], str]:
    return lambda _state: dst


def _guarded(name: str, head: Head, graph_ref: list) -> Callable[[GraphState], Awaitable[dict]]:
    """Wrap a node at wiring time, so stubs and real heads get the same guards.

    - BudgetExceeded: emit one plain-English `error` event from the node's head and return
      stop_reason "budget" with status "done" (a before score exists) or "failed". Every outgoing edge goes
      through `_or_done`, which sends a budget stop to Done. That is the only transition beyond the PRD
      diagram: every node except Done may go to Done.
      (A `Command(goto=Done)` was tried first, but LangGraph also fires the node's static edge, so the
      next head would still have run in the same step.)
    - Pausing nodes: tell `pause()` whether its pause was already announced (see pause.py).
    """

    async def node(state: GraphState) -> dict:
        token = ALREADY_ANNOUNCED.set(await _already_paused(graph_ref, name)) if name in _PAUSING else None
        try:
            return await head(state)
        except budget.BudgetExceeded as exc:
            await emit(
                state,
                _EVENT_HEAD[name],
                "error",
                f"Stopped: this run reached its ${exc.cap_usd:.2f} cost cap.",
                {"cost_usd": exc.cost_usd, "cap_usd": exc.cap_usd},
            )
            return {"stop_reason": "budget", "status": r.budget_stop_status(state)}
        finally:
            if token is not None:
                ALREADY_ANNOUNCED.reset(token)

    node.__name__ = name
    return node


@dataclass(frozen=True)
class Heads:
    """The thirteen node callables. Swap any of them; the wiring stays the same."""

    intake: Head = stubs.intake
    user_questions: Head = stubs.user_questions
    scenario_approval: Head = stubs.scenario_approval
    planner: Head = stubs.planner
    builder: Head = stubs.builder
    agent_smith: Head = stubs.agent_smith
    simulator: Head = stubs.simulator
    runner: Head = stubs.runner
    diagnostician: Head = stubs.diagnostician
    human_gate: Head = stubs.human_gate
    fixer: Head = stubs.fixer
    verifier: Head = stubs.verifier
    done: Head = stubs.done


def real_heads() -> Heads:
    """The heads built so far; the rest stay stubs until their phase lands.

    Imported here, not at module top, because the heads themselves import graph.pause and graph.resume.
    """
    from susanoo.heads import agent_smith, builder, planner

    return Heads(
        intake=planner.intake,
        user_questions=planner.user_questions,
        scenario_approval=planner.scenario_approval,
        planner=planner.planner,
        builder=builder.run,
        agent_smith=agent_smith.run,
    )


def build_graph(heads: Heads | None = None, checkpointer: BaseCheckpointSaver | None = None) -> Any:
    """Return the compiled graph. Interrupts (UserQuestions, ScenarioApproval, HumanGate) need a checkpointer."""
    h = heads or Heads()
    g = StateGraph(GraphState)
    graph_ref: list = []  # filled after compile; the pause guard needs the compiled graph to read checkpoints

    guarded_nodes = {
        r.INTAKE: h.intake,
        r.USER_QUESTIONS: h.user_questions,
        r.SCENARIO_APPROVAL: h.scenario_approval,
        r.PLANNER: h.planner,
        r.BUILDER: h.builder,
        r.AGENT_SMITH: h.agent_smith,
        r.SIMULATOR: h.simulator,
        r.RUNNER: h.runner,
        r.DIAGNOSTICIAN: h.diagnostician,
        r.HUMAN_GATE: h.human_gate,
        r.FIXER: h.fixer,
        r.VERIFIER: h.verifier,
    }
    for name, head in guarded_nodes.items():
        g.add_node(name, _guarded(name, head, graph_ref))
    g.add_node(r.DONE, h.done)

    def edge(src: str, route: Callable[[GraphState], str], targets: list[str]) -> None:
        g.add_conditional_edges(src, _or_done(route), sorted({*targets, r.DONE}))

    g.add_edge(START, r.INTAKE)
    edge(r.INTAKE, r.after_intake, [r.USER_QUESTIONS, r.SCENARIO_APPROVAL])
    edge(r.USER_QUESTIONS, _next(r.SCENARIO_APPROVAL), [r.SCENARIO_APPROVAL])
    edge(r.SCENARIO_APPROVAL, _next(r.PLANNER), [r.PLANNER])
    edge(r.PLANNER, _next(r.BUILDER), [r.BUILDER])
    edge(r.BUILDER, _next(r.AGENT_SMITH), [r.AGENT_SMITH])
    edge(r.AGENT_SMITH, _next(r.SIMULATOR), [r.SIMULATOR])
    edge(r.SIMULATOR, _next(r.RUNNER), [r.RUNNER])
    edge(r.RUNNER, r.after_runner, [r.DIAGNOSTICIAN])
    edge(r.DIAGNOSTICIAN, r.after_diagnostician, [r.FIXER, r.HUMAN_GATE])
    edge(r.HUMAN_GATE, r.after_human_gate, [r.FIXER, r.RUNNER])
    edge(r.FIXER, _next(r.VERIFIER), [r.VERIFIER])
    edge(r.VERIFIER, r.after_verifier, [r.RUNNER, r.DIAGNOSTICIAN])
    g.add_edge(r.DONE, END)

    compiled = g.compile(checkpointer=checkpointer)
    graph_ref.append(compiled)
    return compiled
