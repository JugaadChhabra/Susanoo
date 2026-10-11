# PRD edits to make (Muaaz), from the overnight build

CLAUDE.md says scope changes go into the live PRD first, then the IMPLEMENTATION file. These are the decisions taken while building that the PRD doesn't state literally. Each one is already in the code; the PRD should say so (or you overrule it and we change the code).

## Decisions to write into the PRD

1. **Coding worker (Entire, [PRD › Timeline, owners, risks › Risks and fallbacks] and the "Verify in hour one" note).** Paste: "The Claude Agent SDK works as the coding worker: Entire records a checkpoint for each commit when the target repo has Entire enabled (`entire enable --agent claude-code`), so the `claude -p` fallback is not needed." The Builder enables Entire in every generated repo.
2. **Backup spec (open question).** `specs/backup.md`: a review app for test files in Python services (assertions, no `time.sleep`, skip reasons, no real network calls in unit tests, no swallowed exceptions). It ran end to end (backup-2).
3. **Stop-only edges to Done ([PRD › LangGraph flow and shared state]).** Besides the diagram's edges, any node routes to Done when a head sets `stop_reason` to `budget` (cost cap hit mid-node) or `no_diagnosis` (nothing the Diagnostician can see is left to explain). Add these to the diagram.
4. **"Stop after 2 iterations without improvement" ([PRD › Improvement loop and guardrails › Budgets]).** Implemented as: two rounds in a row without a kept patch (a rolled-back, refused or gate-rejected round counts as no improvement). A kept patch already guarantees its targets are fixed, nothing regressed and held-out F1 didn't drop.
5. **Iterations.** One Fixer→Verifier cycle, or one rejected HumanGate, is one iteration; iteration 0 is the baseline.
6. **Where fixes are verified ([PRD › Testing environments]).** Prompt/policy and test fixes are verified at Level 1; code and tool fixes at Level 2 (the real app in its container, before vs after). Level 1 can't see code/tool changes because it runs Susanoo's own copy of the app's tools and filter. A fix that can't be verified is rolled back. `metrics` is a Level 1 curve.
7. **Patch-size cap.** 60 changed lines (added + removed), overridable with `PATCH_MAX_LINES`. The PRD names no number.
8. **Acceptance scenarios are hidden from the Diagnostician and the Fixer.** Follows from "part of the held-out set" in [PRD › Intake › Scenario approval]; enforced in code (reads, searches and writes).
9. **Unattended runs.** `SUSANOO_UNATTENDED=1` takes the suggested defaults for intake questions (logged in plain English) and keeps the drafted cards as the acceptance set **marked not approved by a user**. A test-layer gate still pauses for a person.
10. **Feedback scenarios** go into the training split (not acceptance), `approved_by_user=false`, from the next Runner pass.
11. **Per-run cost cap.** Each run uses its own `runs.cost_cap_usd`; `RUN_COST_CAP_USD` is the default.
12. **Generated repos are public** (judges open them from the agent page).

## Decisions only you can make

- **PR links in the hero spec.** The template reviews pasted diffs only; intake asks where PR links come from. Recommend cutting PR links from the hero spec (answer "Pasted diffs only") unless Jugaad's GitHub connector tools land.
- **Rule id `external-call-errors`, severity `warn`.** Used everywhere; confirm.
- **Scorer conventions** (`packages/core/susanoo/scoring/metrics.py` docstring): headline recall/precision/F1 on the train split; zero-division values. Confirm.
- **Intake question 4 default.** In both hero runs, the default ("web services plus databases and message queues") made the reviewer's database warnings contradict the hand-written seed answer keys, so the first diagnosis was a test-layer change. Either answer "HTTP only" at intake, or approve that test change at the gate.
- **Contract fields (with Jugaad, `contract-change` skill):** a `disputed` field on Scenario; where a run's connector list lives; whether to freeze the intake-question and pause-payload shapes.
- **Haiku 5.5 price** in `packages/core/susanoo/integrations/llm.py` is a placeholder ($1/$5 per M tokens).
