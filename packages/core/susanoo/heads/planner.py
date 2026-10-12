"""Planner head: intake questions, scenario approval, and the build plan.
[PRD › The eight heads of Orochi], [PRD › Intake: questions and approved scenarios]

Four graph nodes live here; each is a drop-in replacement for the stub of the same name in `graph/stubs.py`
(same signature, same state keys), wired through `Heads(intake=..., user_questions=..., scenario_approval=...,
planner=...)`:

- `intake`            Opus lists assumptions and writes at most 5 questions (never ones the spec answers).
                      If there are no questions it also drafts the scenario cards (nothing to wait for).
- `user_questions`    `interrupt()` with a `question_needed` event; resume with `{"answers": {id: value}}`.
                      After the answers arrive it asks the Simulator to draft 6-10 scenario cards.
- `scenario_approval` `interrupt()` with an `approval_needed` event; resume with `{"approved_ids": [...]}` (required;
                      nothing is approved by default) and optionally `{"rejected_ids": [...]}` (wins over
                      approved) and `{"edits": {id: {"title": ...}}}` (applied to approved cards only).
                      Approved cards become `split="acceptance"`, `approved_by_user=True`.
- `planner` (`run`)   Opus turns spec + answers + approved cards into a validated `Plan`.

Unattended runs (env `SUSANOO_UNATTENDED=1`): no interrupts. Defaults are used for every question (and logged in
plain English); drafted cards are kept as the acceptance split but stay `approved_by_user=False`, because no
human approved them.

Two GraphState keys beyond the stubs' are needed (the orchestrator adds them to graph/state.py):
`answer_sources: dict[str, str]` (question id -> "user" | "default", written by `user_questions`) and
`scenario_feedback: list[dict]` (`{"title", "action": "edited" | "rejected", "note"}`, written by
`scenario_approval`). Both are read by `planner` and sent to the model.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from susanoo.emit import emit
from susanoo.graph.pause import pause
from susanoo.graph.resume import validate_resume
from susanoo.graph.state import GraphState, IntakeQuestion
from susanoo.guards import budget
from susanoo.heads import simulator
from susanoo.integrations import llm, tracing
from susanoo.schemas import Plan, Scenario

HEAD = "planner"
UNATTENDED_ENV = "SUSANOO_UNATTENDED"
# Questions answered by a person, but nobody is there to approve the cards: they are kept, marked not approved.
UNATTENDED_CARDS_ENV = "SUSANOO_UNATTENDED_CARDS"
MAX_QUESTIONS = 5

_PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
_REPO_ROOT = Path(__file__).resolve().parents[4]
_RULE_ID = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)+$")
_QUESTION_ID = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


class IntakeError(ValueError):
    """The intake output from the model could not be used."""


class PlanValidationError(ValueError):
    """The plan from the model broke a rule of the contract."""


# ---------------------------------------------------------------------------------------------
# small helpers


def unattended() -> bool:
    """True when the run must not wait for a human (defaults are used and logged)."""
    return os.environ.get(UNATTENDED_ENV, "").strip().lower() in ("1", "true", "yes")


def unattended_cards() -> bool:
    """True when nobody will review the example scenarios (fully unattended, or cards-only unattended)."""
    return unattended() or os.environ.get(UNATTENDED_CARDS_ENV, "").strip().lower() in ("1", "true", "yes")


def load_prompt(name: str) -> str:
    return (_PROMPTS / f"{name}.md").read_text(encoding="utf-8")


def template_listing(root: Path | None = None) -> str:
    """One line per file under backend/ and web/ of the app template, so the model knows what exists."""
    base = root or Path(os.environ.get("SUSANOO_TEMPLATE_DIR") or _REPO_ROOT / "templates" / "app-template")
    paths = sorted(
        p.relative_to(base).as_posix()
        for top in ("backend", "web")
        if (base / top).is_dir()
        for p in (base / top).rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    )
    if not paths:
        raise FileNotFoundError(f"No template files found under {base}")
    return "\n".join(paths)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _cost(state: Mapping[str, Any]) -> float:
    return float(state.get("cost_usd", 0.0))


# ---------------------------------------------------------------------------------------------
# intake: assumptions -> questions


def filter_questions(data: Any) -> tuple[list[IntakeQuestion], list[str]]:
    """Apply the code checks to the model's intake JSON.

    Drops questions with a spec_quote (the spec already answers them) and duplicate ids, cuts to 5, and checks
    that each default is one of the options. Returns (questions, kept_assumption_texts).
    """
    if not isinstance(data, dict) or not isinstance(data.get("questions"), list):
        raise IntakeError("The intake step did not return a list of questions.")
    questions: list[IntakeQuestion] = []
    seen: set[str] = set()
    for raw in data["questions"]:
        if not isinstance(raw, dict):
            raise IntakeError("An intake question was not an object.")
        quote = raw.get("spec_quote")
        if quote is not None and str(quote).strip():
            continue  # the spec already answers it
        qid, text, why = raw.get("id"), raw.get("text"), raw.get("why")
        options, default = raw.get("options") or [], raw.get("default")
        if not (isinstance(qid, str) and _QUESTION_ID.match(qid)):
            raise IntakeError(f"Question id {qid!r} is not kebab-case.")
        if not (isinstance(text, str) and text.strip() and isinstance(why, str) and why.strip()):
            raise IntakeError(f"Question {qid!r} is missing its text or its one-line reason.")
        if not (isinstance(options, list) and all(isinstance(o, str) for o in options)):
            raise IntakeError(f"Question {qid!r} has options that are not text.")
        if not (isinstance(default, str) and default.strip()):
            raise IntakeError(f"Question {qid!r} has no default.")
        if options and default not in options:
            snapped = next((o for o in options if o.strip().lower() == default.strip().lower()), None)
            if snapped is None:
                raise IntakeError(f"The default for question {qid!r} is not one of its options.")
            default = snapped
        if qid in seen:
            continue
        seen.add(qid)
        questions.append(
            {"id": qid, "text": text.strip(), "options": list(options), "default": default, "why": why.strip()}
        )
    assumptions = data.get("assumptions") if isinstance(data.get("assumptions"), list) else []
    kept = [a["text"] for a in assumptions if isinstance(a, dict) and a.get("kept") and isinstance(a.get("text"), str)]
    return questions[:MAX_QUESTIONS], kept


async def _draft_cards(
    state: Mapping[str, Any],
    questions: list[IntakeQuestion],
    answers: Mapping[str, str],
    sources: Mapping[str, str],
    costs: list[float],
) -> list[Scenario]:
    """Ask the Simulator for 6-10 cards using the answers so far. The call's cost lands in `costs` even on failure."""
    by_id = {q["id"]: q for q in questions}
    given = [
        {
            "id": qid,
            "question": by_id[qid]["text"] if qid in by_id else qid,
            "answer": a,
            "source": sources.get(qid, "default"),
        }
        for qid, a in answers.items()
    ]
    return await simulator.draft_cards(state, intake_answers=given, on_cost=costs.append)


async def intake(state: GraphState) -> dict:
    """Planner (intake part): list assumptions, keep the ones that change behaviour, ask <= 5 questions."""
    spent: list[float] = []  # cost of every model call this node made, kept even if the node fails
    with tracing.span(HEAD, run_id=state["run_id"], step="intake"):
        await emit(state, HEAD, "started", "Reading your spec and listing what I would have to assume…")
        try:
            budget.check(state)
            message = (
                f"<spec>{simulator.neutralise_tags(str(state['spec']))}</spec>\n"
                f"<template>{simulator.neutralise_tags(template_listing())}</template>"
            )
            result = await llm.call(HEAD, messages=[{"role": "user", "content": message}], system=load_prompt("intake"))
            spent.append(result.cost_usd)
            questions, assumptions = filter_questions(result.json())
            cost = _cost(state) + result.cost_usd
            update: dict[str, Any] = {"intake_questions": questions}
            if questions:
                done = f"I have {_plural(len(questions), 'question')} before I build."
            else:
                # Nothing to wait for, so draft the example scenarios now (the graph goes straight to approval).
                card_costs: list[float] = []
                try:
                    cards = await _draft_cards({**state, "cost_usd": cost}, questions, {}, {}, card_costs)
                finally:
                    spent.extend(card_costs)
                cost += sum(card_costs)
                update["scenarios"] = cards
                done = (
                    "The spec already answers everything I need, so I have no questions. "
                    f"I drafted {len(cards)} example scenarios."
                )
            await emit(
                state,
                HEAD,
                "finished",
                done,
                {"question_count": len(questions), "assumptions": assumptions},
            )
        except budget.BudgetExceeded:
            raise
        except Exception as e:
            await emit(
                state, HEAD, "error", f"I could not work out what to ask: {e}", {"cost_usd": round(sum(spent), 6)}
            )
            raise
    # Both next steps may pause for a human, so mark the run as waiting.
    return {**update, "cost_usd": cost, "status": "awaiting_approval"}


async def user_questions(state: GraphState) -> dict:
    """Pause for the user's answers (at most 5 questions, each with a default), then draft the cards."""
    questions = list(state.get("intake_questions", []))
    defaults = {q["id"]: q["default"] for q in questions}
    if unattended():
        await emit(state, HEAD, "started", "Running without anyone watching, so I will use the suggested answers…")
        given: dict[str, Any] = {}
        used = "; ".join(f"{q['text']} → {q['default']}" for q in questions)
        await emit(state, HEAD, "progress", f"No one is available to answer, so I used the suggested defaults: {used}.")
    else:
        payload = {"kind": "questions", "questions": questions}
        reply = await pause(
            state,
            HEAD,
            "Waiting for your answers to a few questions…",
            "question_needed",
            f"Please answer {_plural(len(questions), 'quick question')} (each has a suggested default).",
            payload,
        )
        validate_resume("questions", reply, payload)
        given = reply["answers"]

    def answered(qid: str) -> bool:
        return qid in given and str(given[qid]).strip() != ""

    answers = {qid: str(given[qid]).strip() if answered(qid) else d for qid, d in defaults.items()}
    sources = {qid: "user" if answered(qid) else "default" for qid in defaults}
    n_default = sum(1 for s in sources.values() if s == "default")

    card_costs: list[float] = []
    with tracing.span(HEAD, run_id=state["run_id"], step="user_questions"):
        try:
            cards = await _draft_cards(state, questions, answers, sources, card_costs)
            await emit(
                state,
                HEAD,
                "finished",
                f"Got your answers ({len(questions) - n_default} answered, {n_default} using the suggested default). "
                f"I drafted {len(cards)} example scenarios for you to review.",
                {"answers": answers},
            )
        except budget.BudgetExceeded:
            raise
        except Exception as e:
            await emit(
                state,
                HEAD,
                "error",
                f"I could not draft the example scenarios: {e}",
                {"cost_usd": round(sum(card_costs), 6)},
            )
            raise
    return {
        "intake_answers": answers,
        "answer_sources": sources,
        "scenarios": cards,
        "cost_usd": _cost(state) + sum(card_costs),
        "status": "awaiting_approval",  # next node pauses again
    }


# ---------------------------------------------------------------------------------------------
# scenario approval


def apply_review(cards: list[Scenario], reply: Any) -> tuple[list[Scenario], list[str], list[dict[str, str]]]:
    """Apply the user's approve / edit / reject decisions.

    Returns (kept cards as acceptance + approved, rejected ids, feedback for the Planner). `reply` is
    `{"approved_ids": [...], "rejected_ids": [...]?, "edits": {id: {"title": ...}}?}`, already checked by
    `validate_resume`. `approved_ids` is required (nothing is approved by default), a rejected id wins over an
    approved one, and edits apply to approved cards only.
    """
    all_ids = [c.id for c in cards]
    keep = set(reply["approved_ids"]) - set(reply.get("rejected_ids", []))
    edits = reply.get("edits", {})
    kept: list[Scenario] = []
    feedback: list[dict[str, str]] = []
    for card in cards:
        if card.id not in keep:
            feedback.append({"title": card.title, "action": "rejected", "note": "The user rejected this scenario."})
            continue
        edit = edits.get(card.id)
        new_title = edit.get("title") if isinstance(edit, dict) else None
        title = new_title.strip() if isinstance(new_title, str) and new_title.strip() else card.title
        if title != card.title:
            feedback.append({"title": card.title, "action": "edited", "note": f"The user changed it to: {title}"})
        kept.append(card.model_copy(update={"title": title, "split": "acceptance", "approved_by_user": True}))
    rejected = [i for i in all_ids if i not in keep]
    return kept, rejected, feedback


async def scenario_approval(state: GraphState) -> dict:
    """Pause so the user can approve, edit or reject each example scenario. [PRD › Intake › Scenario approval]"""
    cards = list(state.get("scenarios", []))
    if unattended_cards():
        await emit(state, HEAD, "started", "Running without anyone watching, so I will keep the example scenarios…")
        kept = [c.model_copy(update={"split": "acceptance", "approved_by_user": False}) for c in cards]
        rejected: list[str] = []
        feedback: list[dict[str, str]] = []
        message = (
            f"No one is available to review them, so I kept all {len(kept)} example scenarios as the acceptance set "
            "without anyone's approval."
        )
    else:
        payload = {"kind": "scenarios", "scenarios": [{"id": s.id, "title": s.title} for s in cards]}
        reply = await pause(
            state,
            HEAD,
            "Waiting for you to review the example scenarios…",
            "approval_needed",
            f"Please approve, edit or reject {_plural(len(cards), 'example scenario')}.",
            payload,
        )
        validate_resume("scenarios", reply, payload)  # never approve by default: approved_ids is required
        kept, rejected, feedback = apply_review(cards, reply)
        message = f"{_plural(len(kept), 'scenario')} approved and locked as the acceptance set."
        if rejected:
            message += f" {len(rejected)} rejected."
    await emit(
        state,
        HEAD,
        "finished",
        message,
        {"approved": [s.id for s in kept], "rejected": rejected},
    )
    return {"scenarios": kept, "status": "running", "scenario_feedback": feedback}


# ---------------------------------------------------------------------------------------------
# planner


def _safe_path(f: str) -> bool:
    """A normalised relative path under backend/ or web/ with no hidden, dot or control-character parts."""
    if not f.startswith(("backend/", "web/")) or f != posixpath.normpath(f):
        return False
    if "\\" in f or any(ord(c) < 32 or ord(c) == 127 for c in f):
        return False
    # '.', '..', '.git', '.env', '.claude' and every other dot-part (also catches '' from '//').
    return all(part and not part.startswith(".") for part in f.split("/"))


def validate_plan(data: Any, question_ids: set[str]) -> Plan:
    """Check the model's plan against the contract and the Planner's rules; raise PlanValidationError."""
    try:
        plan = Plan.model_validate(data)
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()[:5])
        raise PlanValidationError(f"The plan did not match the expected shape ({problems}).") from e
    if not plan.rules:
        raise PlanValidationError("The plan has no rules.")
    ids = [r.id for r in plan.rules]
    for rule_id in ids:
        if not _RULE_ID.match(rule_id):
            raise PlanValidationError(f"Rule id {rule_id!r} is not kebab-case (for example no-raw-sql).")
    if len(set(ids)) != len(ids):
        raise PlanValidationError("Two rules share the same id.")
    files = plan.files_to_generate
    if not files:
        raise PlanValidationError("The plan lists no files to generate.")
    if len(set(files)) != len(files):
        raise PlanValidationError("The plan lists the same file twice.")
    for f in files:
        if not _safe_path(f):
            raise PlanValidationError(f"File {f!r} is outside backend/ and web/, so it cannot be generated.")
    if set(plan.intake_answers) != question_ids:
        raise PlanValidationError("The plan's intake answers do not match the questions that were asked.")
    return plan


def _plan_message(state: Mapping[str, Any]) -> tuple[str, dict[str, str]]:
    answers = {k: str(v) for k, v in (state.get("intake_answers") or {}).items()}
    questions = {q["id"]: q for q in state.get("intake_questions", [])}
    sources = state.get("answer_sources") or {}
    listed = []
    for qid, answer in answers.items():
        q = questions.get(qid)
        source = sources.get(qid) or ("default" if q and q["default"] == answer else "user")
        listed.append({"id": qid, "question": q["text"] if q else qid, "answer": answer, "source": source})
    titles = [s.title for s in state.get("scenarios", []) if s.split == "acceptance"]
    safe = simulator.neutralise_tags
    message = (
        f"<spec>{safe(str(state['spec']))}</spec>\n"
        f"<intake_answers>{safe(json.dumps(listed))}</intake_answers>\n"
        f"<approved_scenarios>{safe(json.dumps(titles))}</approved_scenarios>\n"
        f"<scenario_feedback>{safe(json.dumps(state.get('scenario_feedback') or []))}</scenario_feedback>\n"
        f"<template>{safe(template_listing())}</template>"
    )
    return message, answers


async def planner(state: GraphState) -> dict:
    """Planner (Opus): spec + intake answers + approved scenarios -> Plan. [PRD › Contracts (locked) › Plan]"""
    spent = 0.0  # what this node's model call cost, reported on failure since the node then returns nothing
    with tracing.span(HEAD, run_id=state["run_id"], step="plan"):
        await emit(state, HEAD, "started", "Turning your answers into a build plan…")
        try:
            budget.check(state)
            message, answers = _plan_message(state)
            result = await llm.call(
                HEAD, messages=[{"role": "user", "content": message}], system=load_prompt("planner")
            )
            spent = result.cost_usd
            plan = validate_plan(result.json(), set(answers))
            plan = plan.model_copy(update={"intake_answers": answers})  # exact strings, whatever the model wrote
            unknown = sorted(
                {
                    issue.rule_id
                    for s in state.get("scenarios", [])
                    if s.split == "acceptance"
                    for issue in s.answer_key.expected
                }
                - {r.id for r in plan.rules}
            )
            if unknown:
                await emit(
                    state,
                    HEAD,
                    "progress",
                    "Some approved example scenarios name rules that are not in the plan "
                    f"({', '.join(unknown)}), so they cannot be scored until that is fixed.",
                    {"unknown_rule_ids": unknown},
                )
            await emit(
                state,
                HEAD,
                "finished",
                f"Plan ready: {_plural(len(plan.rules), 'rule')} to enforce, "
                f"{_plural(len(plan.files_to_generate), 'file')} to generate.",
                {"product_name": plan.product_name, "rule_ids": [r.id for r in plan.rules]},
            )
            return {"plan": plan, "cost_usd": _cost(state) + result.cost_usd}
        except budget.BudgetExceeded:
            raise
        except Exception as e:
            await emit(state, HEAD, "error", f"I could not make a build plan: {e}", {"cost_usd": round(spent, 6)})
            raise


run = planner  # head interface name: `async def run(state: SusanooState) -> dict`
