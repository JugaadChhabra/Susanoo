---
name: evidence-capture
description: Record judge-checkable evidence for neatHack in EVIDENCE.md — neatlogs before/after traces, recovery traces, Entire checkpoints, cfo.ai plan, #neatHack posts, commit history. Use whenever a run improves, recovers from a failure, produces a notable diagnosis, or a phase finishes.
---

# Evidence capture

Judging rewards claims that judges can verify. Source: **[PRD › Sponsor integrations and evidence plan › Evidence checklist]**. Template: `EVIDENCE.md` at the repo root.

## When to capture
- **A run's held-out F1 went up:** §1, "The agent improved".
- **An action failed and Susanoo retried, rolled back or recovered:** §5, "Recovers from failures".
- **A patch was kept or rolled back:** §3, Entire checkpoints for both.
- **A phase finished:** suggest a #neatHack post (`docs/build-in-public.md`) and log it in §6.
- **The cfo.ai plan was exported:** §4, with files in `docs/business-plan/`.

## How
1. **Get the real links.** Use the neatlogs trace URL for the exact run or iteration and the Entire checkpoint ID or URL for the commit. Never write a link or number you haven't seen. If you can't reach it, leave a `TODO(<owner>)` and tell the human.
2. **Numbers come from the database or the trace, not memory:** held-out F1, recall, precision, acceptance pass rate and cost per iteration (`iterations` and `runs` tables).
3. **Fill the §1 table** for iteration 0 vs the final iteration of the **same** run, then add one line per diagnosis → patch → verdict between them.
4. **Recovery (§5):** link the trace where the action failed (e.g. a container boot failure or an Anthropic API error) **and** the span showing what happened next (retry, rollback, clean stop at the budget cap).
5. **Screenshots** go in `docs/evidence/` with descriptive names; link them from `EVIDENCE.md`.
6. Commit with `docs(evidence): …` (the `commit-and-push` skill).

## Honesty rules
- Report **held-out** numbers as the headline, never training numbers alone.
- Keep failed and rolled-back attempts. They are evidence that the loop works.
- Never edit scenarios, answer keys or scoring to improve a number (see the `scenario-authoring` skill).
