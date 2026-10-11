"""Graph-internal state. NOT part of the frozen contract in schemas/.

SusanooState (frozen) has no keys for routing data that only the graph needs, so the graph
works on this superset. Nothing outside graph/ should depend on the extra keys.

Everything below is "graph-internal, pending contract decision". Real heads MUST keep writing these,
because the conditional edges in routing.py read them:

  failure_ledger[-1].layer    Diagnostician appends one Diagnosis per pass; "test" routes to HumanGate.
  patches[-1].verdict         Fixer appends a "pending" Patch; Verifier sets "kept" or "rolled_back".
  results[].iteration         Runner stamps each ScenarioResult with the current iteration; only results
                              of `state["iteration"]` count (all_passed / current_failures).
  metrics[].iteration/.heldout_f1   Verifier (and Runner for the baseline) append one per iteration;
                              the "two rounds without gain" stop reads them.
  iteration                   +1 for every Fixer -> Verifier pass and every rejected HumanGate (cap 5).
  cost_usd                    Heads add what they spend; the cap stops the run.
  gate_decision               HumanGate writes "approved" or "rejected" (rejected also adds to
                              disputed_scenario_ids and does iteration + 1).
  intake_questions            Intake writes the list ([] means nothing to ask); non-empty routes to
                              UserQuestions.
  intake_answers, disputed_scenario_ids, stop_reason   see GraphState below.

A head that precedes a pause sets status "awaiting_approval" (Intake, UserQuestions, Diagnostician when
layer == "test"); the pausing node sets it back to "running" on resume.

Pause (interrupt) payloads always carry a "kind"; the matching resume shapes are validated in resume.py:

  questions : payload {"kind", "questions": [IntakeQuestion]}   resume {"answers": {id: value}}
  scenarios : payload {"kind", "scenarios": [{"id","title"}]}   resume {"approved_ids": [...],
                                                                 "rejected_ids": [...]?, "edits": {id: {"title"}}?}
  gate      : payload {"kind", "diagnosis_id", "layer", "hypothesis", "scenario_ids"}
                                                                resume {"approved": bool}  (True to approve)

Pausing nodes call graph.pause.pause() so their pause events are emitted exactly once.
"""

from __future__ import annotations

from typing import TypedDict

from susanoo.schemas import SusanooState


class IntakeQuestion(TypedDict):
    """One intake question. [PRD › Intake: questions and approved scenarios › Rules for questions]"""

    id: str
    text: str
    options: list[str]  # multiple choice where possible
    default: str  # suggested default
    why: str  # one line on why it matters


class GraphState(SusanooState, total=False):
    # --- graph-internal extras (not part of the contract) ---
    intake_questions: list[IntakeQuestion]  # Intake -> route: non-empty means UserQuestions runs
    intake_answers: dict[str, str]  # UserQuestions -> Planner (becomes Plan.intake_answers)
    gate_decision: str  # HumanGate -> route: "approved" | "rejected"
    disputed_scenario_ids: list[str]  # scenarios a human rejected a test change for; Runner ignores them
    stop_reason: str | None  # Done: all_passed | iteration_cap | no_gain | budget
    answer_sources: dict[str, str]  # UserQuestions -> Planner: question id -> "user" | "default"
    scenario_feedback: list[dict]  # ScenarioApproval -> Planner: {"title", "action": edited|rejected, "note"}
    connectors: list[str]  # connector names this run may use (Agent Smith); pending contract decision
    cost_cap_usd: float  # this run's own cap (runs.cost_cap_usd); budget.cap_for prefers it over the env default


def initial_state(run_id: str, spec: str) -> GraphState:
    """The state a new run starts from: every list empty, iteration 0, nothing spent."""
    return {
        "run_id": run_id,
        "spec": spec,
        "scenarios": [],
        "results": [],
        "failure_ledger": [],
        "patches": [],
        "metrics": [],
        "disputed_scenario_ids": [],
        "iteration": 0,
        "cost_usd": 0.0,
        "status": "running",
    }  # type: ignore[typeddict-item]  # plan/agent/repo_path are set by later heads


__all__ = ["GraphState", "IntakeQuestion", "initial_state"]
