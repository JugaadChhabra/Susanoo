# Video narration: Muaaz, 0:40–2:25

The loop, neatlogs, Entire, and the before/after numbers. This fits the Muaaz rows of `docs/demo-script.md`
(0:40–1:30 live run view, 1:30–2:00 neatlogs and Entire, 2:00–2:25 scoreboard).
Headline numbers use the same framing as the landing page's proof section (`apps/web/lib/proof.ts`, commit `b2a2188`):
the round before the kept fix (round 1) against the round after it (round 2), which scored the same 28 scenarios, plus the
run's total cost. The iteration-0 baseline (32 scenarios, a different set) is secondary context only and is labelled so.
hero-2 was **unattended**: no person approved the example scenarios, and the one test change was rejected by the script.
Every number below comes from the real run **hero-2** (`docs/evidence/hero-2-run.json`, `EVIDENCE.md` §1, §3),
recorded on Mon 12 Oct, 04:20–04:41 IST. `[[SLOT: …]]` marks footage that a later run may improve. Leave the slot
empty, or fill it with a real number only.

Spoken words: 223 without the *(cut)* lines (about 95 s at 140–150 wpm), 242 with them. The Phase 6 prompt asks for
under 230 words, so record without the *(cut)* lines. No required beat is lost.

## Beats

| Time | On screen | Spoken |
|---|---|---|
| 0:40–0:52 | **Intake screen** for `specs/code-review.md`: the one-paragraph spec beside the 5 question cards, each with its suggested default. Highlight the spec's rule list, then the question "Besides names, emails and phone numbers, should logging internal user IDs or IP addresses also count as logging PII?" | "Susanoo has eight heads. The Planner skips the five rules the spec already lists. It asks only what's left open, like: does an internal user ID count as personal data?" |
| 0:52–1:04 | **Live run view, heads graph:** Builder → Agent Smith → Simulator light up. Event: *"32 scenarios ready: 15 to practise on, 7 held back for the final check, and 10 …"* (crop before "you approved": nobody approved them in hero-2; see Before recording 2). | "Builder, Agent Smith and Simulator generate the app *(cut: from a fixed template)*, the agent, and thirty-two scenarios. Seven are hidden checks the fixing heads never see." |
| 1:04–1:12 | **Runner** event: *"6 of 32 scenarios failed."* **Failure ledger** opens. | "Baseline: six of thirty-two fail. Every agent call is a neatlogs trace." |
| 1:12–1:32 | **Diagnostician** card for `diag-i0-1`, layer **test**, with its 4 evidence span ids. Then the **human gate** card: *"Approve or reject changing a test: …"* → *"…those scenarios are marked as disputed."* (crop the "You rejected" words: the unattended script rejected it, not a person). | "The Diagnostician reads the failing spans and names exactly one layer, with span evidence. First it blamed four answer keys. A test change could game the score, so it needs a person. This run was unattended: no person approved anything, so the script said no." |
| 1:32–1:50 | **neatlogs trace** for `sim-04` (span `54f78e9b81499340`) beside diagnosis `diag-i1-1`, layer **prompt_policy**. Then the Fixer event *"Fix 1 is saved (3 lines changed in the agent's prompt and policy). It is recorded in an Entire checkpoint."* and the **Entire checkpoint** `01M4PKQ6VKBEM41T77PB8CJE95` on commit `ae319a2`. | "Next round, a live access token slipped through. The trace points at the prompt: it mistook the token for a fake test value. The Fixer may only edit that layer: a three-line patch and an Entire checkpoint with the failure, diagnosis and reason." |
| 1:50–2:02 | **Verifier** event *"The fix helped, so I kept it (round 2)."* `[[SLOT: rollback footage — a Verifier "rolled it back" event plus the revert commit from a real hero run. hero-2 had no rollback; do not use the stub-head crash demo as a rollback.]]` | "The Verifier re-runs every set, hidden ones included. A fix stays only if it fixes its target, breaks nothing, and the hidden score holds. Otherwise, git revert. *(cut: This one stayed.)*" |
| 2:02–2:25 | **Scoreboard**, round before the kept fix → round after (same 28 scenarios), as on the landing page: problems caught (recall) 88% → 100%, held-out F1 0.91 → 0.91, total run cost $1.75. Secondary, label it "baseline, round 0, different scenario set": training F1 0.833, acceptance 0.90. Final event: *"Stopped: I couldn't find a cause I could fix for the remaining failures."* `[[SLOT: held-out F1 before → after from a run where it rises; leave 0.91 → 0.91 unless a real run shows more.]]` | "Before and after the kept fix: problems caught, eighty-eight to a hundred percent. The hidden checks didn't change: point nine one both times, and we won't pretend otherwise. The last miss is hidden, so Susanoo stopped and said why. Whole run: a dollar seventy-five *(cut: , under the two-dollar cap)*. *(cut: Every claim links to a trace or checkpoint.)*" |

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
2. hero-2 ran before commits `93671c6` and `85cb24b`, so its Simulator, Runner and Fixer events still say "you approved"
   and "You rejected" about cards no person decided (the run was unattended). Crop those words, or record from a newer run.
3. If a new hero run gives a real rollback or a held-out gain, fill the two `[[SLOT]]`s and update the numbers in the
   2:02 line from that run's metrics (round before vs after the kept fix, plus total cost). Change nothing else.
