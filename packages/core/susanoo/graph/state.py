"""Graph-internal state. NOT part of the frozen contract in schemas/.

SusanooState (frozen) has no keys for routing data that only the graph needs, so the graph
works on this superset. Nothing outside graph/ should depend on the extra keys.

Everything below is "graph-internal, pending contract decision". Real heads MUST keep writing these,
because the conditional edges in routing.py read them:

  failure_ledger[-1].layer    Diagnostician appends one Diagnosis per pass; "test" routes to HumanGate.
  patches[-1].verdict         Fixer appends a "pending" Patch; Verifier sets "kept" or "rolled_back".
  results[].iteration         Runner stamps each ScenarioResult with the current iteration; only results
                              of `state["iteration"]` count (all_passed / current_failures).
  metrics[].iteration/.heldout_f1   Verifier (and Runner for the baseline) append one per iteration. This is
                              the Level 1 headline curve only; no stop condition reads it.
  "no_gain" stop              2 rounds in a row without a KEPT patch (patches[].verdict, plus rejected gates).
  iteration                   +1 for every Fixer -> Verifier pass and every rejected HumanGate (cap 5).
  cost_usd                    Heads add what they spend; the cap stops the run.
  gate_decision               HumanGate writes "approved" or "rejected" (rejected also adds to
                              disputed_scenario_ids and does iteration + 1).
  gate_diagnosis_id           HumanGate writes failure_ledger[-1].id when it approves (None when it rejects). The
                              Diagnostician wrapper in build.py clears gate_decision and gate_diagnosis_id whenever a
                              NEW diagnosis is recorded, and the Fixer wrapper refuses a test-layer fix unless
                              gate_decision == "approved" and gate_diagnosis_id == failure_ledger[-1].id, so an
                              approval can never be replayed for a later diagnosis.
  gate_rejected_rounds        HumanGate appends the round number (iteration + 1) of every rejection. Those rounds
                              have no patch; routing.iterations_without_gain uses them to line patches up with rounds.
  stop_reason (head-set)      "budget" (any head, via the budget guard; the Verifier also returns it) and
                              "no_diagnosis" (Diagnostician found nothing it can explain) send the run straight to
                              Done, and no other value does. Done writes the final reason (all_passed, iteration_cap,
                              no_gain, budget, no_diagnosis). Status at Done for a head stop: "done" if any
                              IterationMetrics exist, else "failed".
  fix_rejection_reason        Fixer wrapper: plain-English reason when the Fixer raised FixRefused/FixRejected; the
                              wrapper then appends a Patch(verdict="rolled_back") and the Verifier only does the
                              round's bookkeeping (None after a Fixer pass that produced a patch).
  level2_baseline             Verifier: {"commit": sha, "results": [...]}, a cache of Level 2 results.
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
    gate_decision: str | None  # HumanGate -> route: "approved" | "rejected"; cleared on a new diagnosis
    gate_diagnosis_id: str | None  # HumanGate: the diagnosis a human approved (None if rejected or cleared)
    gate_rejected_rounds: list[int]  # HumanGate: round numbers (iteration + 1) it rejected; no patch in them
    fix_rejection_reason: str | None  # Fixer wrapper -> Verifier: why the Fixer's patch was refused/rejected
    level2_baseline: dict | None  # Verifier-owned cache of Level 2 results for a commit
    disputed_scenario_ids: list[str]  # scenarios a human rejected a test change for; Runner ignores them
    stop_reason: str | None  # Done: all_passed | iteration_cap | no_gain | budget | no_diagnosis
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
