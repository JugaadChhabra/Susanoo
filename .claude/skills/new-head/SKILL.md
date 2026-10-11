---
name: new-head
description: Build or modify one of Susanoo's eight LangGraph heads (Planner/intake, Builder, Agent Smith, Simulator, Runner, Diagnostician, Fixer, Verifier) following the locked head interface, model mapping, budget guard, tracing and guardrails. Use for any work in packages/core/susanoo/heads/ or graph/.
---

# Build a head

Owner: Muaaz. Sources: **[PRD › The eight heads of Orochi]**, **[PRD › Contracts (locked) › Head interface]**, **[PRD › LangGraph flow and shared state]**, **[PRD › Improvement loop and guardrails]**.

## The interface (non-negotiable)
```python
# packages/core/susanoo/heads/<head>.py
from susanoo.schemas.state import SusanooState
from susanoo.integrations import llm, tracing
from susanoo.guards import budget
# emit(): thin wrapper over Jugaad's db.emit_event(). Agree its import path in Phase 0
# (it must be importable from packages/core without depending on apps/api).

HEAD = "diagnostician"

async def run(state: SusanooState) -> dict:
    with tracing.span(HEAD, run_id=state["run_id"]):
        await emit(state, HEAD, "started", "Reading failing traces…")
        try:
            budget.check(state)                      # before EVERY model call
            out = await llm.call(HEAD, messages=[...])  # model picked from HEAD_MODELS
            ...
            await emit(state, HEAD, "finished", "Found 2 causes: policy (1), tool (1).")
            return {"failure_ledger": [...], "cost_usd": state["cost_usd"] + out.cost_usd}
        except budget.BudgetExceeded:
            raise                                     # graph ends the run cleanly
        except Exception as e:
            await emit(state, HEAD, "error", f"Diagnostician failed: {e}")
            raise
```
- **Return only the keys you changed.** Never mutate `state` in place.
- **Inputs and outputs** use the models in `schemas/` only (`Plan`, `Scenario`, `Diagnosis`, `Patch`, …).
- **`message` strings** are plain English; the UI shows them directly.
- **Prompts** live in `packages/core/susanoo/prompts/<head>.md`, not inline strings.

## Model per head
| Head | Model |
|---|---|
| Planner (incl. intake), Diagnostician | `claude-opus-5-5` |
| Builder, Agent Smith, Fixer | `claude-sonnet-5-5` |
| Simulator, translate modules | `claude-haiku-5-5` |
| Runner, Verifier | none (code); the generated agent uses its own model |

## Guardrails each head must respect
- **Diagnostician:** pick exactly one layer from `code | tool | prompt_policy | test`. Cite `evidence_span_ids` from neatlogs. Never read `scenarios/heldout/`.
- **Fixer:**
  - Edit only the files allowed for the layer (`guards/layer_allowlist.py`, mapping in **[PRD › Repository structure]**) and respect `guards/patch_size.py`.
  - Each patch gets its own commit plus an Entire checkpoint carrying the failure, diagnosis and rationale.
  - A `test` layer goes to the HumanGate via `interrupt()` plus an `approval_needed` event, never an automatic edit.
- **Verifier:** keep a patch only if it fixes its targets, causes no regressions, and the held-out score doesn't drop; otherwise `git revert`. Stop at 5 iterations, after 2 with no gain, or at the cost cap.
- **Intake:** at most 5 questions, each with a default; `interrupt()` with `question_needed`; approved cards become the `acceptance` split and are locked.
- **Sandbox:** generated code runs only through `sandbox/docker.py` containers, never on the host.

## Wiring into the graph
Edges must match the state diagram in **[PRD › LangGraph flow and shared state]**. The checkpointer is `PostgresSaver`. Interrupts resume via the intake and approvals endpoints.

## Definition of done
- [ ] Unit test in `tests/heads/test_<head>.py` with a fake `llm.call` (no real API in CI)
- [ ] Emits `started` and `finished` (or `error`) with plain-English messages
- [ ] Budget guard before each call; cost added to `cost_usd`
- [ ] Shows as its own span in neatlogs on a real run
- [ ] Commit via the `commit-and-push` skill, citing the PRD section
