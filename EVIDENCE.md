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

## 2. We used neatlogs
- First traced call (Haiku, via `scripts/traced_haiku_call.py`, 02:50 IST): trace id `5f0f1f3aabb3dfc3708cd3ea40e28e34`. TODO(Muaaz): paste the dashboard URL.
- Hero run hero-2: one trace per scenario run (e.g. `5dbfa67df744d0103b14d1d0d195ec43`); all 28 final-iteration trace ids are in `docs/evidence/hero-2-run.json`. TODO(Muaaz): dashboard links.
- Trace links:
- Screenshot: Diagnostician reading failing spans:

## 3. We used Entire
- Checkpoints on generated repo (patch kept): hero-2 `patch-1`, commit `ae319a2` "fix(prompt_policy): fix the cause of sim-04", Entire checkpoint `01M4PKQ6VKBEM41T77PB8CJE95`. The build commit `9e504ce` has checkpoint `01M4PKF5FY5R6BC2TVXCRR5233`. (The generated repo is local: publishing to the GitHub org failed with a 403 until the token gets repo-create permission.)
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
- First commit (after Sat 10 Oct 2026, 12:30 PM IST):
