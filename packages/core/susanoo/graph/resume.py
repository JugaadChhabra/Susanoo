"""Validated resume of a paused run. [PRD › LangGraph flow and shared state › Human gate]

Resume shapes, by the `kind` in the pending interrupt's payload (graph-internal, pending contract decision):

    questions : {"answers": {question_id: value}}
    scenarios : {"approved_ids": [...], "rejected_ids": [...]?, "edits": {id: {"title": ...}}?}
                (an id in both lists counts as rejected; edits apply to approved scenarios only)
    gate      : {"approved": bool}        (the gate approves ONLY on {"approved": True})

LangGraph silently ignores `Command(resume={})`/`None` and pauses again, so the API and worker must resume
through `resume_run`, which rejects a bad value before the graph is touched.
"""

from __future__ import annotations

from typing import Any

from langgraph.types import Command

from susanoo.graph.config import run_config


def _ids(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(i, str) for i in value):
        raise ValueError(f"'{name}' must be a list of scenario ids")
    return value


def validate_resume(kind: str, value: Any, payload: dict | None = None) -> None:
    """Raise ValueError (plain English) unless `value` is a well-formed answer for a `kind` pause.

    With `payload` (the interrupt payload), ids are also checked against what was actually asked.
    """
    if value is None or value == {}:
        raise ValueError(f"An empty answer cannot resume a '{kind}' pause; send the human's actual answer")
    if not isinstance(value, dict):
        raise ValueError(f"The answer to a '{kind}' pause must be an object, got {type(value).__name__}")
    if kind == "questions":
        answers = value.get("answers")
        if not isinstance(answers, dict):
            raise ValueError("Answers to questions must look like {'answers': {question_id: value}}")
        if payload is not None:
            known = {q["id"] for q in payload.get("questions", [])}
            unknown = sorted(set(answers) - known)
            if unknown:
                raise ValueError(f"Answers given for unknown questions: {', '.join(unknown)}")
    elif kind == "scenarios":
        if "approved_ids" not in value:
            raise ValueError(
                "Scenario approval needs 'approved_ids' (a list, possibly empty); nothing is approved by default"
            )
        approved = _ids(value["approved_ids"], "approved_ids")
        rejected = _ids(value.get("rejected_ids", []), "rejected_ids")
        edits = value.get("edits", {})
        if not isinstance(edits, dict) or not all(isinstance(e, dict) for e in edits.values()):
            raise ValueError("'edits' must look like {scenario_id: {'title': ...}}")
        if payload is not None:
            known = {s["id"] for s in payload.get("scenarios", [])}
            unknown = sorted((set(approved) | set(rejected) | set(edits)) - known)
            if unknown:
                raise ValueError(f"Unknown scenarios: {', '.join(unknown)}")
    elif kind == "gate":
        if not isinstance(value.get("approved"), bool):
            raise ValueError("A gate answer must look like {'approved': true} or {'approved': false}")
    else:
        raise ValueError(f"Unknown pause kind: {kind!r}")


async def pending_interrupt(graph: Any, config: dict) -> dict:
    """Payload of the interrupt this thread is waiting on; ValueError if it is not waiting."""
    snapshot = await graph.aget_state(config)
    for task in snapshot.tasks:
        for pending in task.interrupts:
            if isinstance(pending.value, dict) and "kind" in pending.value:
                return pending.value
    raise ValueError("This run is not waiting for an answer, so there is nothing to resume")


async def resume_run(graph: Any, run_id: str, value: Any, *, config: dict | None = None) -> dict:
    """Validate `value` against the run's pending interrupt, then resume it. Returns the graph output."""
    config = config or run_config(run_id)
    payload = await pending_interrupt(graph, {"configurable": config["configurable"]})
    validate_resume(payload["kind"], value, payload)
    return await graph.ainvoke(Command(resume=value), config)
