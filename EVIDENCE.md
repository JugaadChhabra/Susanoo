# EVIDENCE — Susanoo (neatHack 2026)

Every claim in our submission links to something a judge can check. Rows mirror
[PRD › Sponsor integrations and evidence plan › Evidence checklist]. Add links the moment they exist.

## 1. The agent improved
| Run | Iteration | Held-out F1 | Recall | Precision | Acceptance pass rate | Cost | neatlogs trace |
|---|---|---|---|---|---|---|---|
| hero-2 (before) | 0 | 0.909 | 1.000 | 0.714 | 0.900 | $0.82 | TODO(Muaaz): link; trace ids are in `docs/evidence/hero-2-run.json` |
| hero-2 (after) | 2 | 0.909 | 1.000 | 1.000 | 1.000 | $1.52 (run total $1.75) | TODO(Muaaz): link |

Recall and precision are on the training set (the scorer's headline set for those two columns); held-out F1 and the acceptance pass rate are the headline. Run on Mon 12 Oct, 04:20–04:41 IST, locally with `scripts/run_local.py` (real heads, unattended: intake took the suggested defaults, cards not approved by a person, any test change rejected). Full event log, metrics, results and plan: `docs/evidence/hero-2-run.json`.

What changed between them (diagnosis → patch → verdict):
- Iteration 0: 6 of 32 scenarios failed. Diagnosis `diag-i0-1` (layer **test**): four scenarios failed only because the reviewer also warned about unhandled database calls, which the intake default ("web services plus databases and message queues") asks for, but the answer keys didn't expect. The test change went to the human gate and was **rejected** (the unattended script never approves test changes), so those scenarios were marked disputed.
- Iteration 1: 3 of 28 failed. Diagnosis `diag-i1-1` (layer **prompt_policy**): the agent missed a live access token typed into code (`sim-04`). Patch `patch-1`, 3 lines in the agent's prompt and policy, commit `ae319a2`, Entire checkpoint `01M4PKQ6VKBEM41T77PB8CJE95`: **kept** (target fixed, no regressions, held-out F1 not lower).
- Iteration 2: 1 of 28 failed (one hidden check). Hidden checks are never shown to the Diagnostician, so the run stopped with "I couldn't find a cause I could fix" (`stop_reason=no_diagnosis`). Held-out F1 did not rise in this run: honest result, not yet the "after > before" headline.

Second hero run, **hero-3** (05:00 IST, same script, 20 generated scenarios; `docs/evidence/hero-3-run.json`): 39 scenarios (21 practice, 9 hidden, 9 from intake). Iteration 0: held-out F1 0.857, training F1 0.848, acceptance 0.889, $1.02. The Diagnostician again blamed the **test** layer (5 scenarios: the reviewer's extra "database call has no error handling" warning matches the intake default, but the hand-written seed answer keys only expected HTTP calls). Rejected at the gate by the unattended script, so they were disputed. Iteration 1: training F1 1.000, held-out 0.857, acceptance 0.889; no practice failures left, and the remaining 2 hidden checks and 1 intake example are never shown to the Diagnostician, so it stopped (`no_diagnosis`). Total $1.43.

Takeaway for the human: in both runs the first diagnosis is a real answer-key conflict caused by the intake default for "which calls need error handling". Answering that question with "HTTP only" at intake, or approving the test change at the gate, would let the loop move on to the remaining failures.

Backup spec, **backup-2** (05:20 IST; `docs/evidence/backup-2-run.json`): Susanoo built "Python Test Review" from `specs/backup.md` with its own 5 rules (`assertion-required`, `no-sleep`, `skip-reason-required`, `no-network-calls`, `no-swallowed-exceptions`): 32 scenarios (15 practice, 7 hidden, 10 from intake). Iteration 0: training F1 1.000, held-out F1 1.000, acceptance 0.90 (one intake example failed; acceptance examples are never shown to the Diagnostician, so it stopped). $0.89. This shows the same pipeline working on a second product.

Third hero run, **hero-7** (18:20 IST, published: https://github.com/susanoo-apps/python-code-review-hero7; `docs/evidence/hero-7-run.json`): intake didn't ask the "which calls need error handling" question this time, and the answer-key conflict didn't appear. 39 scenarios (21 practice, 9 hidden, 9 from intake). Iteration 0: training recall 0.929, F1 0.963, **held-out F1 1.000**, acceptance 0.889. Diagnosis `diag-i0-1` (**prompt_policy**): the reviewer approved a cleanup job that passes an API key typed into the code (`sim-17`). Patch `patch-1`, 3 lines, commit `60edf30`, Entire checkpoint `01M4RB1D8JPSNQYGPBW8V7DCMT`: **kept**. Iteration 1: every scenario passes (training F1 1.000, held-out 1.000, acceptance 1.000); `stop_reason=all_passed`, $1.75. Held-out F1 was already perfect, so it couldn't rise; no rollback happened. GitHub's push protection blocked pushing the later commits because the Simulator planted a realistic-format fake Stripe key in `sim-17` (fixed: planted keys now use made-up formats).

## 2. We used neatlogs
- First traced call (Haiku, via `scripts/traced_haiku_call.py`, 02:50 IST): trace id `5f0f1f3aabb3dfc3708cd3ea40e28e34`. TODO(Muaaz): paste the dashboard URL.
- Hero run hero-2: one trace per scenario run (e.g. `5dbfa67df744d0103b14d1d0d195ec43`); all 28 final-iteration trace ids are in `docs/evidence/hero-2-run.json`. TODO(Muaaz): dashboard links.
- Trace links:
- Screenshot: Diagnostician reading failing spans:

## 3. We used Entire
- Checkpoints on generated repo (patch kept): hero-7 `patch-1`, commit `60edf30`, Entire checkpoint `01M4RB1D8JPSNQYGPBW8V7DCMT` (repo https://github.com/susanoo-apps/python-code-review-hero7). hero-2 `patch-1`, commit `ae319a2` "fix(prompt_policy): fix the cause of sim-04", Entire checkpoint `01M4PKQ6VKBEM41T77PB8CJE95`. The build commit `9e504ce` has checkpoint `01M4PKF5FY5R6BC2TVXCRR5233`. (The generated repo is local: publishing to the GitHub org failed with a 403 until the token gets repo-create permission.)
- The generated hero-2 app boots in the sandbox (05:00 IST): built from its repo with `sandbox.docker`, started with 512 MB / 1 CPU, read-only root, all capabilities dropped, no mounts and no published ports; `/health` answered `{"status":"ok"}`.
- Checkpoints on generated repo (patch rolled back): TODO: no rollback happened in hero-2 yet.
- entire-graph output of this repo:
- Checkpoints from building Susanoo itself:

## 4. We used cfo.ai
- Business plan (pricing, costs, revenue, runway): `docs/business-plan/`

## 5. The agent recovers from failures
- Worker crash and resume (`scripts/worker_crash_demo.py`, local Postgres, stub heads): worker A is killed with exit 137 inside the Builder; worker B reclaims the run after the stale window and resumes from the last PostgresSaver checkpoint; nodes that had finished never ran again. Output: `docs/evidence/worker-crash-resume.txt`.
- Clean failure: hero-1 stopped at the Builder with a plain-English error when GitHub refused to create the repo (403), instead of continuing without the repo.
- Trace of a failed action and what Susanoo did next:

## 6. We built in public (#neatHack)
| When (IST) | Who | Link | What |
|---|---|---|---|

## 7. We wrote the code during the hackathon
- Public repo: https://github.com/JugaadChhabra/Susanoo
- First commit (after Sat 10 Oct 2026, 12:30 PM IST): `46709f3` "Initial commit", Sun 11 Oct 2026 17:40 IST (repo created in GitHub's web UI). All 142 commits on main are dated after the start.
