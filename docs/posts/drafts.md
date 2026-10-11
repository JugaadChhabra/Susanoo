# #neatHack post drafts (not posted)

These are drafts for Muaaz to edit and post himself. Nothing here has been published. Do not add rows to `EVIDENCE.md` section 6 until a post is actually live (then add its real URL, time and what it showed).

Rules followed: numbers and ids come only from `EVIDENCE.md`, `docs/evidence/hero-2-run.json` and `docs/evidence/worker-crash-resume.txt`. Where a link or screenshot does not exist yet it says TODO(Muaaz). Check each sponsor's actual X handle before tagging (the kit says so; the handles below are the ones in `docs/build-in-public.md`, unverified).

Honesty note that applies to posts 3 and 4: in hero-2 the held-out F1 did **not** rise (0.909 before and after). What improved was the practice-set precision and the acceptance pass rate. The run was unattended: intake took the suggested defaults, no person approved the example scenarios, and the one test change was rejected by the script. Do not word any post as "held-out F1 went up".

---

## 1. Phase 0: first traced run

**Template:** "First traced run" in `docs/build-in-public.md`.

**Post text**

> First traced run of Susanoo, in @neatlogs. One real Claude Haiku call through our own LLM client, traced from the first commit. This is the baseline everything else gets measured against. #neatHack
> [screenshot: TODO(Muaaz)]

**Relies on**
- `EVIDENCE.md` section 2: first traced call (Haiku, `scripts/traced_haiku_call.py`, 02:50 IST on Mon 12 Oct), trace id `5f0f1f3aabb3dfc3708cd3ea40e28e34`.
- TODO(Muaaz): the neatlogs dashboard URL and a screenshot of that trace. They do not exist in the repo yet (`EVIDENCE.md` §2 still has this as a TODO). Do not post until you have the screenshot.
- Do not add a cost figure for this call: none is recorded in the repo.

---

## 2. Phase 2: the intake questions Susanoo asked

**Template:** adapted from "First traced run" / "Non-technical feedback" (there is no intake template in the kit; this one is original).

**Post text**

> Before building anything, Susanoo reads your spec and asks what it would otherwise have to guess. For our code-review spec it came back with 5 questions, each with a suggested default:
> - Should missing error handling on an external call block a PR, or only warn? (default: warn)
> - Should logging internal user IDs or IP addresses count as logging PII? (default: yes)
> - Can health checks and API docs go without auth? (default: yes)
> - Does a shared error handler or retry wrapper count as handling? (default: yes)
> - Where are your PRs hosted? (default: GitHub)
> It also wrote down 7 assumptions it was making, so you can see what it would have guessed. Questions first, code second. #neatHack
> [screenshot of the intake screen or event log: TODO(Muaaz)]

**Relies on**
- `docs/evidence/hero-2-run.json`, events seq 2 (5 questions, 7 assumptions in `payload.assumptions`) and seq 4 (the five question texts and their defaults).
- True caveat to keep in mind: in hero-2 nobody answered; Susanoo said "No one is available to answer, so I used the suggested defaults" (seq 4) and then drafted 10 example scenarios (seq 5). If you post this as "what Susanoo asked" that is accurate; do not imply a person answered.
- TODO(Muaaz): a screenshot of the intake screen (frontend, Jugaad) or of the event log; neither is in the repo.

---

## 3. Phase 3: the iteration-0 "before" row

**Template:** "First traced run" with the "before" baseline wording.

**Post text**

> Our "before". Susanoo built a Python code reviewer from a plain-English spec, wrote 32 test scenarios (15 to practise on, 7 held back it never sees, 10 acceptance checks), and ran them all.
> 6 of 32 failed. Recall 1.00, precision 0.71, hidden-check F1 0.91, acceptance 0.90. Cost of the whole setup plus this first scoring pass: $0.82.
> Every scenario run has its own @neatlogs trace. Next: can it work out why it failed? #neatHack
> [screenshot of the before table / a trace: TODO(Muaaz)]

**Relies on**
- `EVIDENCE.md` section 1, row "hero-2 (before)": held-out F1 0.909, recall 1.000, precision 0.714, acceptance pass rate 0.900, cost $0.82.
- `docs/evidence/hero-2-run.json`: event seq 20 ("32 scenarios ready: 15 to practise on, 7 held back for the final check, and 10 you approved"), seq 22 (baseline metrics, `cost_usd` 0.821175), seq 23 ("6 of 32 scenarios failed").
- Wording check: the Simulator message says "10 you approved", but in hero-2 no person approved them (the unattended script kept all 10). The draft says "10 acceptance checks" to stay accurate.
- "Every scenario run has its own trace": true for the final iteration's 28 results (`results[].trace_id`, 28 distinct ids). The iteration-0 per-scenario trace ids are not in the saved JSON. TODO(Muaaz): link at least one iteration-0 trace, or soften to "each run is traced" before posting.

---

## 4. Phase 4: the loop works

**Template:** "The loop works" and "Honest failure" from the kit, merged. The kit's "one patch kept, one rolled back" line does **not** apply: nothing was rolled back in hero-2.

**Post text**

> We pointed Susanoo at its own code reviewer and let it fix itself. Result of one unattended run:
> - Round 1: it diagnosed 4 failures as a problem with the tests, not the agent. Changing a test needs a human, so the script said no. Susanoo marked those 4 as disputed and moved on.
> - Round 2: it found the reviewer had missed a live access token typed into code, most likely because its prompt exempted anything that looked like a fake test fixture (Susanoo's own words: "this is a guess"). It wrote a 3-line prompt fix. Verified: target fixed, no regressions, kept. The fix has an @EntireHQ checkpoint with the reasoning.
> Practice-set precision 0.71 -> 1.00, acceptance pass rate 0.90 -> 1.00. Hidden-check F1 stayed at 0.91; it did not go up this time, and one hidden check still fails. Susanoo stopped because it could not find a cause it could fix. Cost: $1.75. #neatHack
> [before/after table + Diagnostician trace: TODO(Muaaz)]

**Relies on**
- `EVIDENCE.md` section 1 ("hero-2 (before)" and "(after)" rows, and the "What changed" notes) and section 3 (patch-1, commit `ae319a2`, Entire checkpoint `01M4PKQ6VKBEM41T77PB8CJE95`).
- `docs/evidence/hero-2-run.json`: `metrics` (iteration 0 to 2: precision 0.714 to 1.0, acceptance 0.9 to 1.0, `heldout_f1` 0.909 throughout), `patches[0]` (3 lines, verdict kept), `failure_ledger` (diag-i0-1 test layer; diag-i1-1 prompt_policy, scenario sim-04), events seq 28 (test change rejected, 4 disputed), seq 38 (verifier checks all true), seq 44 (`stop_reason` no_diagnosis), `cost_usd` 1.750983.
- Check before posting: the "Practice-set precision" comparison uses the training-set precision column (`EVIDENCE.md` §1 explains recall and precision are on the training set). Recall also moved 1.00 -> 0.875 -> 1.00 along the way; leaving it out is fine, but do not claim recall "improved".
- TODO(Muaaz): neatlogs links for the before and after traces and a Diagnostician screenshot (`EVIDENCE.md` §1 and §2 still have these as TODO). The Entire checkpoint id is real, but a public link to it does not exist yet. The generated repo is local only (GitHub publish got a 403), so there is no repo link to give.

### 4b. Optional second post: it survives a crash

**Post text**

> Killed the worker mid-run (exit 137, inside the Builder). A second worker picked the run up after the stale window and resumed from the last saved checkpoint. The steps that had already finished did not run again; only the interrupted Builder ran twice, on worker A then worker B. Final status: done. #neatHack
> [paste of the output or a screenshot: TODO(Muaaz)]

**Relies on**
- `docs/evidence/worker-crash-resume.txt`: "Builder ran on ['worker-A', 'worker-B']; Planner ran 1 time(s); exit codes A=137 B=0", "RESULT: OK, the run resumed from its checkpoint on worker-B".
- Say in the post (or reply) that this demo used a local Postgres and stub heads (per `EVIDENCE.md` §5), not real model calls, so it shows the recovery mechanism rather than a full real run. The run shown there cost $0.6500 of stub data; do not quote that as a real cost.
