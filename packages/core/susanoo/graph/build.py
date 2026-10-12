"""Wire the StateGraph exactly as the state diagram in [PRD › LangGraph flow and shared state].

    [*] -> Intake -> UserQuestions | ScenarioApproval -> Planner -> Builder -> AgentSmith -> Simulator -> Runner
    Runner -> Done | Diagnostician;  Diagnostician -> Fixer | HumanGate;  HumanGate -> Fixer | Runner
    Fixer -> Verifier;  Verifier -> Runner | Diagnostician | Done;  Done -> [*]
    plus stop-only edges: any node -> Done when a head sets stop_reason "budget" or "no_diagnosis".

Node callables are injected through `Heads`, so real heads replace stubs without touching edges.
Heads are async, so run the compiled graph with `ainvoke`/`astream`. Resume paused runs with
resume.resume_run, and build the config with config.run_config(run_id).
"""

from __future__ import annotations

import asyncio
import os
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
from susanoo.integrations import github, tracing
from susanoo.schemas import Patch

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
    """Edge function: a head-set stop (budget, no_diagnosis) goes to Done, anything else follows `route`."""

    def router(state: GraphState) -> str:
        return r.DONE if state.get("stop_reason") in r.HEAD_STOP_REASONS else route(state)

    return router


def _diagnostician_node(head: Head) -> Head:
    """A new diagnosis voids any earlier HumanGate approval, so an approval is never replayed."""

    async def node(state: GraphState) -> dict:
        update = await head(state)
        if len(update.get("failure_ledger", [])) > len(state.get("failure_ledger", [])):
            update = {**update, "gate_decision": None, "gate_diagnosis_id": None}
        return update

    return node


def _refused_update(state: GraphState, dx: Any, reason: str, *, cost: float | None, head_commit: str | None) -> dict:
    """Record a Fixer that produced no usable patch as a rolled-back one; the Verifier only does the bookkeeping."""
    patches = list(state.get("patches", []))
    sha = head_commit or state.get("head_commit") or "none"
    patch = Patch(
        id=f"patch-{len(patches) + 1}-refused",
        diagnosis_id=dx.id,
        commit_sha=sha,
        checkpoint_id=None,
        lines_changed=0,
        verdict="rolled_back",
    )
    update: dict = {"patches": [*patches, patch], "fix_rejection_reason": reason}
    if cost is not None:
        update["cost_usd"] = cost
    if head_commit:
        update["head_commit"] = head_commit
    return update


def _fixer_node(head: Head) -> Head:
    """Turn FixRefused / FixRejected into a rolled-back patch; also enforce the gate <-> diagnosis match.

    - A test-layer diagnosis may only be fixed if the HumanGate approved THAT diagnosis (`gate_diagnosis_id`).
    - FixRefused / FixRejected (heads/fixer.py) become a Patch(verdict="rolled_back") with the run's cost and
      repo head taken from the exception, plus `fix_rejection_reason`; so does a Fixer that changed nothing. A
      FixRejected whose revert failed (head_commit None) propagates, as do other exceptions and
      BudgetExceeded (the budget guard in `_guarded` handles it).
    """

    async def node(state: GraphState) -> dict:
        from susanoo.heads import fixer as fixer_head  # lazy: heads import graph.pause

        ledger = state.get("failure_ledger", [])
        dx = ledger[-1] if ledger else None
        if (
            dx is not None
            and dx.layer == "test"
            and not (state.get("gate_decision") == "approved" and state.get("gate_diagnosis_id") == dx.id)
        ):
            reason = "This fix changes a test, and you have not approved it for this cause, so I changed nothing."
            await emit(state, "fixer", "started", "Writing the smallest fix for the diagnosed cause…")
            await emit(state, "fixer", "error", reason, {"refused": True})
            return _refused_update(state, dx, reason, cost=None, head_commit=None)
        from susanoo.integrations import coding_worker

        try:
            update = await head(state)
        except coding_worker.NoChange:
            if dx is None:
                raise
            # Nothing was changed, so the repo is as it was: record a refused fix and re-diagnose.
            return _refused_update(
                state, dx, "The fixer finished without changing anything.", cost=None, head_commit=None
            )
        except (fixer_head.FixRefused, fixer_head.FixRejected) as exc:
            if dx is None:
                raise
            rejected = isinstance(exc, fixer_head.FixRejected)
            if rejected and exc.head_commit is None:
                # The patch was rejected AND its revert failed: the bad commit is still in the repo. Never record
                # that as a clean rollback; fail the run loudly so a human can roll it back.
                raise
            return _refused_update(
                state,
                dx,
                str(exc),
                cost=exc.cost_usd if rejected else None,
                head_commit=exc.head_commit if rejected else None,
            )
        return {**update, "fix_rejection_reason": None}

    return node


_GITHUB_HTTPS = "https://github.com/"
_PUSH_TIMEOUT_S = 180.0


async def _origin_url(repo: str) -> str | None:
    """The repo's `origin` URL, or None when there is no repo, no origin or git fails."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-c", "core.fsmonitor=false", "remote", "get-url", "origin",
            cwd=repo, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )  # fmt: skip
        out, _ = await proc.communicate()
    except OSError:
        return None
    return out.decode().strip() or None if proc.returncode == 0 else None


async def _publish_at_end(state: GraphState) -> None:
    """Push the generated repo's HEAD to its GitHub `origin`, so GitHub shows the agent, scenarios and fixes.

    Skipped silently with no `repo_path`, no github.com `origin` or no token. A failed push emits ONE plain-English
    progress event and never raises: the run still ends normally. The token never leaves `github.push_repo`.
    """
    repo = state.get("repo_path")
    token = next((v.strip() for name in github.TOKEN_ENVS if (v := os.environ.get(name, "")).strip()), "")
    if not repo or not token:
        return
    url = await _origin_url(str(repo))
    if not url or not url.startswith(_GITHUB_HTTPS):
        return
    try:
        await asyncio.wait_for(github.push_repo(repo, url, token), _PUSH_TIMEOUT_S)
    except Exception as exc:  # never fail the run's ending over a publish
        reason = tracing.scrub_text(str(exc).replace(token, "***")).strip() or type(exc).__name__
        await emit(state, "system", "progress", f"Couldn't publish the latest version to GitHub: {reason}")


def _done_node(head: Head) -> Head:
    """Publish the finished repo to GitHub (best effort), then run the Done head."""

    async def node(state: GraphState) -> dict:
        await _publish_at_end(state)
        return await head(state)

    return node


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
    from susanoo.heads import agent_smith, builder, diagnostician, fixer, planner, runner, simulator, verifier

    return Heads(
        intake=planner.intake,
        user_questions=planner.user_questions,
        scenario_approval=planner.scenario_approval,
        planner=planner.planner,
        builder=builder.run,
        agent_smith=agent_smith.run,
        simulator=simulator.run,
        runner=runner.run,
        diagnostician=diagnostician.run,
        fixer=fixer.run,
        verifier=verifier.run,
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
        r.DIAGNOSTICIAN: _diagnostician_node(h.diagnostician),
        r.HUMAN_GATE: h.human_gate,
        r.FIXER: _fixer_node(h.fixer),
        r.VERIFIER: h.verifier,
    }
    for name, head in guarded_nodes.items():
        g.add_node(name, _guarded(name, head, graph_ref))
    g.add_node(r.DONE, _done_node(h.done))

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
