"""Conditional-edge functions. Each returns the name of the next node.

Edges follow [PRD › LangGraph flow and shared state]; stop conditions follow
[PRD › Improvement loop and guardrails › Budgets].
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from susanoo.guards import budget
from susanoo.schemas import ScenarioResult

MAX_ITERATIONS = 5  # [PRD › Improvement loop and guardrails › Budgets]
MAX_ITERATIONS_WITHOUT_GAIN = 2  # consecutive rounds without a KEPT patch

# stop_reason values a HEAD may set to end the run at Done (every other value is set by Done itself).
HEAD_STOP_REASONS = frozenset({"budget", "no_diagnosis"})

# Node names, exactly as in the PRD state diagram.
INTAKE = "Intake"
USER_QUESTIONS = "UserQuestions"
SCENARIO_APPROVAL = "ScenarioApproval"
PLANNER = "Planner"
BUILDER = "Builder"
AGENT_SMITH = "AgentSmith"
SIMULATOR = "Simulator"
RUNNER = "Runner"
DIAGNOSTICIAN = "Diagnostician"
HUMAN_GATE = "HumanGate"
FIXER = "Fixer"
VERIFIER = "Verifier"
DONE = "Done"


def current_failures(state: Mapping[str, Any]) -> list[ScenarioResult]:
    """Failed results of the current iteration, ignoring scenarios a human disputed."""
    disputed = set(state.get("disputed_scenario_ids", []))
    return [
        r
        for r in state.get("results", [])
        if r.iteration == state.get("iteration", 0) and not r.passed and r.scenario_id not in disputed
    ]


def all_passed(state: Mapping[str, Any]) -> bool:
    """True when the Runner produced results for the current iteration and none failed."""
    has_results = any(r.iteration == state.get("iteration", 0) for r in state.get("results", []))
    return has_results and not current_failures(state)


def iterations_without_gain(state: Mapping[str, Any]) -> int:
    """How many of the most recent rounds produced no KEPT patch (the "improvement" signal).

    A round is one Fixer -> Verifier pass (the patch is "kept" or "rolled_back", Fixer-refused patches are
    rolled back) or one rejected HumanGate (no patch at all). A patch is only kept if it fixed its targets,
    caused no regressions and did not lower held-out F1, so "kept" is the improvement signal; held-out F1
    values from different test levels are not compared. Rounds are numbered 1..`iteration`; the rounds
    listed in `gate_rejected_rounds` have no patch, so the judged patches map in order onto the others.
    """
    iteration = int(state.get("iteration", 0))
    gate_rejected = set(state.get("gate_rejected_rounds", []))
    patch_rounds = [n for n in range(1, iteration + 1) if n not in gate_rejected]
    judged = [p for p in state.get("patches", []) if p.verdict in ("kept", "rolled_back")]
    last_kept = 0
    for k, patch in enumerate(judged):
        if patch.verdict == "kept":
            last_kept = patch_rounds[k] if k < len(patch_rounds) else iteration
    return max(0, iteration - last_kept)


def stop_reason(state: Mapping[str, Any]) -> str | None:
    """Why the improvement loop must stop now, or None to keep going."""
    if float(state.get("cost_usd", 0.0)) >= budget.cap_for(state):
        return "budget"
    if state.get("iteration", 0) >= MAX_ITERATIONS:
        return "iteration_cap"
    if iterations_without_gain(state) >= MAX_ITERATIONS_WITHOUT_GAIN:
        return "no_gain"
    return None


def budget_stop_status(state: Mapping[str, Any]) -> str:
    """Status when the cost cap ends a run: "done" if we have at least a before score, else "failed"."""
    return "done" if state.get("metrics") else "failed"


def after_intake(state: Mapping[str, Any]) -> str:
    return USER_QUESTIONS if state.get("intake_questions") else SCENARIO_APPROVAL


def after_runner(state: Mapping[str, Any]) -> str:
    """Done when every non-disputed scenario passes or a stop condition holds (same ones as the Verifier)."""
    return DONE if all_passed(state) or stop_reason(state) is not None else DIAGNOSTICIAN


def head_stop_status(state: Mapping[str, Any], reason: str) -> str:
    """Status when a head-set stop ends the run: "done" if at least a before score exists, else "failed"."""
    if reason not in HEAD_STOP_REASONS:
        raise ValueError(f"Not a head stop reason: {reason!r}")
    return budget_stop_status(state)


def after_diagnostician(state: Mapping[str, Any]) -> str:
    if state.get("stop_reason") == "no_diagnosis":
        return DONE  # nothing was diagnosed; failure_ledger[-1] (if any) is an older, already-handled diagnosis
    ledger = state.get("failure_ledger", [])
    if not ledger:
        raise ValueError("Diagnostician finished without recording a diagnosis")
    return HUMAN_GATE if ledger[-1].layer == "test" else FIXER


def after_human_gate(state: Mapping[str, Any]) -> str:
    decision = state.get("gate_decision")
    if decision == "approved":
        return FIXER
    if decision == "rejected":
        return RUNNER
    raise ValueError(f"HumanGate left no usable decision: {decision!r}")


def after_verifier(state: Mapping[str, Any]) -> str:
    """Stop conditions first, then the verdict on the latest patch."""
    if stop_reason(state) is not None:
        return DONE
    patches = state.get("patches", [])
    if not patches:
        raise ValueError("Verifier finished without any patch to judge")
    verdict = patches[-1].verdict
    if verdict == "kept":
        return RUNNER
    if verdict == "rolled_back":
        return DIAGNOSTICIAN
    raise ValueError(f"Verifier left the latest patch undecided: {verdict!r}")
