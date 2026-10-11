# Video narration: Muaaz, 0:40–2:25

The loop, neatlogs, Entire, and the before/after numbers. This fits the Muaaz rows of `docs/demo-script.md`
(0:40–1:30 live run view, 1:30–2:00 neatlogs and Entire, 2:00–2:25 scoreboard).
Every number below comes from the real run **hero-2** (`docs/evidence/hero-2-run.json`, `EVIDENCE.md` §1, §3),
recorded on Mon 12 Oct, 04:20–04:41 IST. `[[SLOT: …]]` marks footage that a later run may improve. Leave the slot
empty, or fill it with a real number only.

Spoken words: 248 with the *(cut)* lines, 229 without (about 105 s at 140–150 wpm). The Phase 6 prompt asks for
under 230 words; drop the *(cut)* lines to get there. No required beat is lost.

## Beats

| Time | On screen | Spoken |
|---|---|---|
| 0:40–0:52 | **Intake screen** for `specs/code-review.md`: the one-paragraph spec beside the 5 question cards, each with its suggested default. Highlight the spec's rule list, then the question "Besides names, emails and phone numbers, should logging internal user IDs or IP addresses also count as logging PII?" | "Susanoo has eight heads on one graph. The Planner sees the spec already lists five rules, so it doesn't re-ask them. It asks only what's left open, like: does an internal user ID count as personal data?" |
| 0:52–1:04 | **Live run view, heads graph:** Builder → Agent Smith → Simulator light up. Event: *"32 scenarios ready: 15 to practise on, 7 held back for the final check, and 10 …"* (crop the rest of that line; see NEEDS HUMAN 2). | "Builder, Agent Smith and Simulator generate the app *(cut: from a fixed template)*, the agent, and thirty-two scenarios. Seven are hidden checks the fixing heads never see." |
| 1:04–1:12 | **Runner** event: *"6 of 32 scenarios failed."* **Failure ledger** opens. | "Baseline: six of thirty-two fail. Every agent call is a neatlogs trace." |
| 1:12–1:32 | **Diagnostician** card for `diag-i0-1`, layer **test**, with its 4 evidence span ids. Then the **human gate** card: *"Approve or reject changing a test: …"* → *"You rejected the test change; those scenarios are marked as disputed."* | "The Diagnostician reads the failing spans and must name exactly one layer: code, tool, prompt, or test, with span evidence. First it blamed four answer keys. A test change could game the score, so it waits for a human. Rejected." |
| 1:32–1:50 | **neatlogs trace** for `sim-04` (span `54f78e9b81499340`) beside diagnosis `diag-i1-1`, layer **prompt_policy**. Then the Fixer event *"Fix 1 is saved (3 lines changed in the agent's prompt and policy). It is recorded in an Entire checkpoint."* and the **Entire checkpoint** `01M4PKQ6VKBEM41T77PB8CJE95` on commit `ae319a2`. | "Next round, a live access token slipped through. The trace points at the prompt: it mistook the token for a fake test value. The Fixer may only edit that layer: a three-line patch, a git commit, and an Entire checkpoint holding the failure, diagnosis and reason." |
| 1:50–2:02 | **Verifier** event *"The fix helped, so I kept it (round 2)."* `[[SLOT: rollback footage — a Verifier "rolled it back" event plus the revert commit from a real hero run. hero-2 had no rollback; do not use the stub-head crash demo as a rollback.]]` | "The Verifier re-runs every set, hidden ones included. A fix stays only if it fixes its target, breaks nothing, and the hidden score holds. Otherwise, git revert. *(cut: This one stayed.)*" |
| 2:02–2:25 | **Scoreboard**, before → after: training F1 0.833 → 1.000, acceptance 0.90 → 1.00, held-out F1 0.909 → 0.909, cost $1.75. Final event: *"Stopped: I couldn't find a cause I could fix for the remaining failures."* `[[SLOT: held-out F1 before → after from a run where it rises; leave 0.909 → 0.909 unless a real run shows more.]]` | "Training score: point eight three to one. Acceptance: ninety to a hundred percent. Hidden-check score: point nine one, before and after. It didn't rise, and we won't pretend it did. The last miss is hidden, so Susanoo stopped and said why, at a dollar seventy-five *(cut: , under the two-dollar cap)*. *(cut: Every claim links to a trace or checkpoint.)*" |

## What the segment covers for neatlogs and Entire (Phase 6 requirement)

| | neatlogs | Entire |
|---|---|---|
| Architecture | One trace per head and per generated-agent call (1:04) | One checkpoint per Fixer patch, on the generated repo (1:32) |
| Implementation | Diagnostician reads failing spans and cites span ids as evidence (1:12, 1:32) | Fixer commits, then checkpoints with failure, diagnosis and reason (1:32) |
| Runtime | 28 final-round traces in hero-2 (e.g. `5dbfa67df744d0103b14d1d0d195ec43`) | Checkpoint `01M4PKQ6VKBEM41T77PB8CJE95` on `ae319a2` |
| Reasoning | Span `54f78e9b81499340` led to the `prompt_policy` diagnosis | Checkpoint carries why the patch was made; rollback is a git revert |
| Result | Before/after numbers on the scoreboard (2:02) | Kept patch; history shows the whole story |

## Sources

- `docs/demo-script.md`, `PROMPTBOOK_MUAAZ.md` Phase 6 › Video segment
- `PRD.md` › Goals, non-goals, success metrics; The eight heads of Orochi; Intake: questions and approved scenarios;
  Improvement loop and guardrails; Sponsor integrations and evidence plan; Non-technical users
- `docs/evidence/hero-2-run.json` (events, metrics, failure_ledger, patches), `EVIDENCE.md` §1, §3, `specs/code-review.md`

## Before recording

1. Paste the neatlogs dashboard links for the `sim-04` trace and a baseline trace into `EVIDENCE.md` (still TODO
   there), so the 1:04 and 1:32 shots show a real dashboard and not just ids.
2. hero-2 ran before commits `93671c6` and `85cb24b`, so its Simulator and Runner events still say "you approved"
   about cards nobody approved (the run was unattended). Crop those words, or record from a newer run.
3. If a new hero run gives a real rollback or a held-out gain, fill the two `[[SLOT]]`s and update the numbers in the
   2:02 line from that run's metrics. Change nothing else.
