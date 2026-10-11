"""Stub heads that return canned data and emit events. [PRD › Contracts (locked) › Head interface]

Real heads replace these one by one (heads/<head>.py) through `Heads` in build.py; edges never change.
Each stub follows the head interface: `async def run(state) -> dict`, returns only changed keys,
emits `started` then `finished`, and runs the budget guard where a real head would call a model.

The canned story (used by the offline demo):
  baseline run fails -> prompt/policy fix is rolled back -> test-layer diagnosis, human approves ->
  that fix is kept -> everything passes.
"""

from __future__ import annotations

from typing import Any

from susanoo.emit import emit
from susanoo.graph.pause import pause
from susanoo.graph.resume import validate_resume
from susanoo.graph.routing import MAX_ITERATIONS, all_passed, budget_stop_status, stop_reason
from susanoo.graph.state import GraphState, IntakeQuestion
from susanoo.guards import budget
from susanoo.schemas import (
    AgentBundle,
    AnswerKey,
    Diagnosis,
    ExpectedIssue,
    IterationMetrics,
    Patch,
    Plan,
    Rule,
    Scenario,
    ScenarioResult,
)

# ---------------------------------------------------------------------------------------------
# canned data


def _scenario(sid: str, kind: str, split: str, title: str, approved: bool = False) -> Scenario:
    expected = (
        []
        if kind == "clean"
        else [ExpectedIssue(file="app/log.py", line_start=10, line_end=10, rule_id="no-pii-in-logs", severity="block")]
    )
    return Scenario(
        id=sid,
        kind=kind,  # type: ignore[arg-type]
        split=split,  # type: ignore[arg-type]
        title=title,
        diff="--- a/app/log.py\n+++ b/app/log.py\n@@ -9,0 +10 @@\n+    log.info(user.email)\n",
        answer_key=AnswerKey(expected=expected),
        approved_by_user=approved,
    )


CANNED_QUESTIONS: list[IntakeQuestion] = [
    {
        "id": "q-health-auth",
        "text": "Should missing auth on an internal health-check endpoint be blocked or allowed?",
        "options": ["block", "allow"],
        "default": "allow",
        "why": "It changes whether the reviewer flags a very common pattern.",
    },
    {
        "id": "q-strictness",
        "text": "How strict should the reviewer be about style nits?",
        "options": ["ignore", "mention", "block"],
        "default": "mention",
        "why": "It sets how noisy the review comments are.",
    },
]

CANNED_COSTS = {
    "planner": 0.05,
    "builder": 0.20,
    "agent_smith": 0.10,
    "simulator": 0.02,
    "diagnostician": 0.08,
    "fixer": 0.06,
}


def _cost(state: Any, head: str) -> float:
    return round(float(state.get("cost_usd", 0.0)) + CANNED_COSTS[head], 4)


# ---------------------------------------------------------------------------------------------
# heads


async def intake(state: GraphState) -> dict:
    """Planner (intake part): list assumptions, keep the ones that change behaviour. [PRD › Intake]"""
    head = "planner"
    # TODO(0.3): tracing.span(HEAD)
    await emit(state, head, "started", "Reading your spec and listing what I would have to assume…")
    budget.check(state)
    # A caller (tests) can preset "intake_questions", including [] for "nothing to ask".
    questions = state["intake_questions"] if "intake_questions" in state else CANNED_QUESTIONS[:]
    drafts = [
        _scenario("acc-1", "planted", "acceptance", "A developer logs a customer's email address."),
        _scenario("acc-2", "clean", "acceptance", "A developer renames a variable; nothing should be flagged."),
    ]
    if questions:
        message = f"I have {len(questions)} question{'s' if len(questions) != 1 else ''} before I build."
    else:
        message = "The spec already answers everything I need, so I have no questions."
    await emit(state, head, "finished", message, {"question_count": len(questions)})
    # Both next steps pause for a human, so mark the run as waiting.
    return {"intake_questions": questions, "scenarios": drafts, "status": "awaiting_approval"}


async def user_questions(state: GraphState) -> dict:
    """Pause for the user's answers (at most 5 questions, each with a default)."""
    head = "planner"
    questions = state.get("intake_questions", [])
    payload = {"kind": "questions", "questions": list(questions)}
    reply = await pause(
        state,
        head,
        "Waiting for your answers to a few questions…",
        "question_needed",
        f"Please answer {len(questions)} quick question{'s' if len(questions) != 1 else ''} "
        "(each has a suggested default).",
        payload,
    )
    validate_resume("questions", reply, payload)
    given = reply["answers"]
    answers = {q["id"]: str(given.get(q["id"], q["default"])) for q in questions}
    defaults_used = sum(1 for q in questions if q["id"] not in given)
    await emit(
        state,
        head,
        "finished",
        f"Got your answers ({len(questions) - defaults_used} answered, {defaults_used} using the suggested default).",
        {"answers": answers},
    )
    return {"intake_answers": answers, "status": "awaiting_approval"}  # next node pauses again


async def scenario_approval(state: GraphState) -> dict:
    """Pause so the user can approve, edit or reject each example scenario. [PRD › Scenario approval]"""
    head = "planner"
    cards = state.get("scenarios", [])
    payload = {"kind": "scenarios", "scenarios": [{"id": s.id, "title": s.title} for s in cards]}
    reply = await pause(
        state,
        head,
        "Waiting for you to review the example scenarios…",
        "approval_needed",
        f"Please approve, edit or reject {len(cards)} example scenarios.",
        payload,
    )
    validate_resume("scenarios", reply, payload)  # never approve by default: approved_ids is required
    approved_ids = set(reply["approved_ids"]) - set(reply.get("rejected_ids", []))
    edits = reply.get("edits", {})
    kept = []
    for card in cards:
        if card.id not in approved_ids:
            continue
        update: dict[str, Any] = {"split": "acceptance", "approved_by_user": True}
        if isinstance(edits.get(card.id, {}).get("title"), str):
            update["title"] = edits[card.id]["title"]
        kept.append(card.model_copy(update=update))
    rejected = [s.id for s in cards if s.id not in approved_ids]
    edited = sorted(i for i in edits if i in approved_ids)
    await emit(
        state,
        head,
        "finished",
        f"{len(kept)} scenarios approved and locked as the acceptance set.",
        {"approved": [s.id for s in kept], "rejected": rejected, "edited": edited},
    )
    return {"scenarios": kept, "status": "running"}


async def planner(state: GraphState) -> dict:
    head = "planner"
    # TODO(0.3): tracing.span(HEAD)
    await emit(state, head, "started", "Turning your answers into a build plan…")
    budget.check(state)
    plan = Plan(
        product_name="Code Review Agent",
        summary="Reviews pull-request diffs against your team's rules and flags violations.",
        rules=[Rule(id="no-pii-in-logs", text="Never log customer personal data.", default_severity="block")],
        intake_answers=dict(state.get("intake_answers", {})),
        files_to_generate=["backend/main.py", "web/index.html"],
        acceptance_criteria=["Flags PII in logs", "Leaves clean diffs alone"],
    )
    await emit(state, head, "finished", "Plan ready: 1 rule to enforce, 2 files to generate.")
    return {"plan": plan, "cost_usd": _cost(state, head)}


async def builder(state: GraphState) -> dict:
    head = "builder"
    # TODO(0.3): tracing.span(HEAD)
    await emit(state, head, "started", "Generating the app from the template…")
    budget.check(state)
    await emit(state, head, "finished", "App generated and committed.")
    return {"repo_path": f"runs/{state['run_id']}/repo", "head_commit": "stub0000", "cost_usd": _cost(state, head)}


async def agent_smith(state: GraphState) -> dict:
    head = "agent_smith"
    # TODO(0.3): tracing.span(HEAD)
    await emit(state, head, "started", "Writing the agent's prompt, policy and tools…")
    budget.check(state)
    agent = AgentBundle(
        prompt_path="agent/prompt.md",
        policy_path="agent/policy.yaml",
        tools_path="agent/tools.py",
        model="claude-sonnet-5-5",
    )
    await emit(state, head, "finished", "The first version of the agent is ready.")
    return {"agent": agent, "cost_usd": _cost(state, head)}


async def simulator(state: GraphState) -> dict:
    head = "simulator"
    # TODO(0.3): tracing.span(HEAD)
    await emit(state, head, "started", "Writing test scenarios for the agent…")
    budget.check(state)
    extra = [
        _scenario("tr-1", "planted", "train", "A developer logs a user's phone number."),
        _scenario("tr-2", "edge", "train", "An email appears only inside a comment."),
        _scenario("ho-1", "adversarial", "heldout", "Personal data is hidden in a base64 string."),
        _scenario("ho-2", "clean", "heldout", "A harmless log line with an order number."),
    ]
    scenarios = list(state.get("scenarios", [])) + extra
    await emit(
        state, head, "finished", f"{len(scenarios)} scenarios ready (4 new, {len(scenarios) - 4} approved by you)."
    )
    return {"scenarios": scenarios, "cost_usd": _cost(state, head)}


async def runner(state: GraphState) -> dict:
    """Code, no model. Stub: scenario tr-1 fails until some patch has been kept."""
    head = "runner"
    # TODO(0.3): tracing.span(HEAD)
    iteration = state.get("iteration", 0)
    await emit(
        state,
        head,
        "started",
        (
            "Running every scenario against the starting version of the agent…"
            if iteration == 0
            else f"Running every scenario against the improved agent (after round {iteration})…"
        ),
    )
    fixed = any(p.verdict == "kept" for p in state.get("patches", []))
    results = [
        ScenarioResult(
            scenario_id=s.id,
            iteration=iteration,
            findings=[],
            matched=1,
            missed=0 if (fixed or s.id != "tr-1") else 1,
            false_positives=0,
            severity_correct=1,
            passed=fixed or s.id != "tr-1",
            trace_id=f"stub-trace-{s.id}-{iteration}",
        )
        for s in state.get("scenarios", [])
    ]
    update: dict[str, Any] = {"results": results}
    metrics = list(state.get("metrics", []))
    if not any(m.iteration == iteration for m in metrics):  # baseline measurement
        # first measurement is the baseline; a round that changed nothing (rejected gate) repeats the last score
        metrics.append(_metrics(iteration, metrics[-1].heldout_f1 if metrics else 0.5, state.get("cost_usd", 0.0)))
        update["metrics"] = metrics
        text = "Baseline score recorded." if len(metrics) == 1 else "Score unchanged since the last round."
        await emit(state, head, "metrics", text, metrics[-1].model_dump())
    failed = [r.scenario_id for r in results if not r.passed]
    message = (
        "All scenarios pass."
        if not failed
        else f"{len(failed)} of {len(results)} scenarios failed: {', '.join(failed)}."
    )
    await emit(state, head, "finished", message, {"failed": failed})
    return update


def _metrics(iteration: int, heldout_f1: float, cost: float) -> IterationMetrics:
    return IterationMetrics(
        iteration=iteration,
        recall=heldout_f1,
        precision=heldout_f1,
        f1=heldout_f1,
        severity_accuracy=heldout_f1,
        heldout_f1=heldout_f1,
        acceptance_pass_rate=1.0 if heldout_f1 >= 0.8 else 0.5,
        cost_usd=cost,
    )


async def diagnostician(state: GraphState) -> dict:
    """Stub: the first diagnosis blames the prompt/policy, the next one blames a test."""
    head = "diagnostician"
    # TODO(0.3): tracing.span(HEAD)
    await emit(state, head, "started", "Reading the failing traces to find the cause…")
    budget.check(state)
    ledger = list(state.get("failure_ledger", []))
    layer = "prompt_policy" if not ledger else "test"
    disputed = set(state.get("disputed_scenario_ids", []))  # a human already refused to change these tests
    failing = [r.scenario_id for r in state.get("results", []) if not r.passed and r.scenario_id not in disputed]
    dx = Diagnosis(
        id=f"dx-{len(ledger) + 1}",
        scenario_ids=failing,
        layer=layer,  # type: ignore[arg-type]
        hypothesis=(
            "The agent's policy does not mention phone numbers as personal data."
            if layer == "prompt_policy"
            else "The scenario expects a line number that does not match its own diff."
        ),
        evidence_span_ids=[f"stub-span-{len(ledger) + 1}"],
        target_files=["agent/policy.yaml"] if layer == "prompt_policy" else ["scenarios/train/tr-1.json"],
    )
    where = "the agent's prompt and policy" if layer == "prompt_policy" else "the test itself"
    await emit(state, head, "finished", f"Found the likely cause in {where}.", {"layer": layer, "id": dx.id})
    update: dict[str, Any] = {"failure_ledger": ledger + [dx], "cost_usd": _cost(state, head)}
    if layer == "test":
        update["status"] = "awaiting_approval"  # the next node, HumanGate, pauses
    return update


async def human_gate(state: GraphState) -> dict:
    """`test`-layer changes need a human. [PRD › Improvement loop and guardrails › Test edits need a human]"""
    head = "fixer"
    dx = state["failure_ledger"][-1]
    payload = {
        "kind": "gate",
        "diagnosis_id": dx.id,
        "layer": dx.layer,
        "hypothesis": dx.hypothesis,
        "scenario_ids": dx.scenario_ids,
    }
    reply = await pause(
        state,
        head,
        "A change to a test needs your decision…",
        "approval_needed",
        f"Approve or reject changing a test: {dx.hypothesis}",
        payload,
    )
    # Only an explicit {"approved": True} approves; anything else counts as a rejection.
    approved = isinstance(reply, dict) and reply.get("approved") is True
    if approved:
        await emit(state, head, "finished", "You approved the test change.", {"decision": "approved"})
        return {"gate_decision": "approved", "status": "running"}
    disputed = sorted(set(state.get("disputed_scenario_ids", [])) | set(dx.scenario_ids))
    # TODO(NEEDS HUMAN): the Scenario contract has no `disputed` field. Disputed ids are kept in the
    # graph-internal `disputed_scenario_ids` and in this payload until the contract gains one.
    await emit(
        state,
        head,
        "finished",
        "You rejected the test change; those scenarios are marked as disputed.",
        {"decision": "rejected", "disputed_scenario_ids": disputed},
    )
    # A rejected gate uses up one iteration, so rejecting forever still hits the iteration cap.
    return {
        "gate_decision": "rejected",
        "disputed_scenario_ids": disputed,
        "iteration": state.get("iteration", 0) + 1,
        "status": "running",
    }


async def fixer(state: GraphState) -> dict:
    head = "fixer"
    # TODO(0.3): tracing.span(HEAD)
    await emit(state, head, "started", "Writing the smallest fix for the diagnosed cause…")
    budget.check(state)
    dx = state["failure_ledger"][-1]
    patches = list(state.get("patches", []))
    n = len(patches) + 1
    patch = Patch(
        id=f"patch-{n}",
        diagnosis_id=dx.id,
        commit_sha=f"stub{n:04d}",
        checkpoint_id=None,
        lines_changed=12,
        verdict="pending",
    )
    await emit(state, head, "finished", f"Fix {n} committed ({patch.lines_changed} lines, {dx.layer} layer).")
    return {"patches": patches + [patch], "cost_usd": _cost(state, head)}


async def verifier(state: GraphState) -> dict:
    """Code, no model. Stub: the first patch is rolled back, later ones are kept.

    Every Fixer -> Verifier cycle counts as one iteration (max 5), whatever the verdict.
    """
    head = "verifier"
    # TODO(0.3): tracing.span(HEAD)
    await emit(state, head, "started", "Re-running the scenarios to check the fix…")
    patches = list(state.get("patches", []))
    verdict = "kept" if len(patches) > 1 else "rolled_back"
    patches[-1] = patches[-1].model_copy(update={"verdict": verdict})
    iteration = state.get("iteration", 0) + 1
    metrics = list(state.get("metrics", []))
    last_f1 = metrics[-1].heldout_f1 if metrics else 0.5
    f1 = round(min(1.0, last_f1 + 0.3), 2) if verdict == "kept" else last_f1
    metrics.append(_metrics(iteration, f1, state.get("cost_usd", 0.0)))
    await emit(state, head, "metrics", f"Score after round {iteration}.", metrics[-1].model_dump())
    message = (
        f"The fix helped, so I kept it (round {iteration})."
        if verdict == "kept"
        else f"The fix did not help, so I rolled it back (round {iteration})."
    )
    await emit(state, head, "finished", message, {"verdict": verdict, "patch_id": patches[-1].id})
    return {"patches": patches, "iteration": iteration, "metrics": metrics}


async def done(state: GraphState) -> dict:
    head = "system"
    await emit(state, head, "started", "Wrapping up the run…")
    if state.get("stop_reason") == "budget":  # set by the budget guard in build.py
        reason = "budget"
    else:
        reason = "all_passed" if all_passed(state) else (stop_reason(state) or "unknown")
    disputed = len(state.get("disputed_scenario_ids", []))
    all_passed_text = (
        f"Every scenario passes except {disputed} you disputed. The agent is ready."
        if disputed
        else "Every scenario passes. The agent is ready."
    )
    budget_text = (
        "Stopped because the run reached its cost limit. Here is the best version so far."
        if state.get("metrics")
        else "Stopped because the run reached its cost limit before the first test finished, so there is no result yet."
    )
    text = {
        "all_passed": all_passed_text,
        "iteration_cap": f"Stopped after the {MAX_ITERATIONS}-round limit. Here is the best version so far.",
        "no_gain": "Stopped because two rounds in a row did not improve the score. Here is the best version so far.",
        "budget": budget_text,
        "unknown": "The run ended without a clear reason. Here is the best version so far.",
    }[reason]
    status = budget_stop_status(state) if reason == "budget" else "done"
    await emit(
        state,
        head,
        "finished",
        text,
        {"stop_reason": reason, "iterations": state.get("iteration", 0), "disputed": disputed},
    )
    return {"status": status, "stop_reason": reason}
