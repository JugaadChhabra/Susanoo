# Final demo video script (Muaaz + Jugaad), neatHack 2026

Target length **2:55**. Spoken words about 360 (at 140–150 wpm). Every number comes from `EVIDENCE.md`. Fill the `[[SLOT]]`s only with real numbers, or leave them out.

## 0. How this wins (judging weights)

| Category | Weight | What the video must show |
|---|---|---|
| The agent | 30% | A substantial task start to finish: spec in, built agent, attack, diagnosis by layer, fix, verified result. Guardrails (hidden checks, human gate) prove it isn't gaming. |
| neatlogs + Entire + cfo.ai | 25% | Each tool shown doing real work, not logos: traces drive the Diagnostician, checkpoints record each patch, cfo.ai prices it from the measured cost. |
| Demo video | 20% | Clear in 3 minutes, a person on screen for the first beat, the numbers on screen while spoken, plain English. |
| Building in public | 15% | The #neatHack posts (log them in `EVIDENCE.md` §6) and the public repo. One end-card line. |
| Usefulness + originality | 10% | "Agents are easy to build and hard to trust." Susanoo fixes the right layer and proves it. |

**Rule for the whole video: only claim what is on screen.** Three honest lines that win trust:
1. "Hero-7's hidden score was already perfect, so it couldn't rise."
2. "We switched Entire on late, and we say so."
3. "A rolled-back fix: not in these runs, it's covered by tests." (say it only if asked; don't narrate a rollback you haven't shown)

## 1. Roles and screens

- **Jugaad** drives the live site (`app.susanoo.jugaadchhabra.dev`): hero, new project, intake, cfo.ai.
- **Muaaz** drives the replay and evidence: live run view (demo replay of hero-2), neatlogs, the hero-7 GitHub repo, Entire, scoreboard.
- Record **voice and screen separately** if you can: screen capture of each segment first, then one voice take each, then cut. It saves an hour of retakes.

## 2. The script

| Time | Who | On screen | Spoken |
|---|---|---|---|
| 0:00–0:14 | Jugaad (face cam, 3 s, then marketing hero) | Landing page headline "Many-headed mess. Tamed." | "Building an AI agent takes an afternoon. Knowing whether it actually works takes forever. Susanoo does the second part: you describe an agent in plain English, and it builds it, attacks it, and fixes it, with proof." |
| 0:14–0:38 | Jugaad | New project → paste the code-review spec → intake question cards with suggested defaults → scenario approval list | "I paste one paragraph: a code-review agent that flags secrets and unhandled errors in pull requests. Susanoo doesn't guess. Its Planner asks only what the spec left open, then drafts example scenarios for me to approve. Nothing gets built on assumptions." |
| 0:38–0:55 | Muaaz | Live run view: heads graph lights up Builder → Agent Smith → Simulator; event "39 scenarios ready" | "Behind that are eight heads in one LangGraph. The Builder makes the app from a fixed template, Agent Smith gives it its agent, and the Simulator writes thirty-nine scenarios. Nine are hidden checks the fixing heads never see." |
| 0:55–1:15 | Muaaz | Runner event with a failing scenario; failure ledger; the `sim-17` card: a cleanup job that passes an API key typed into code and the reviewer approves it | "The Runner attacks the agent. One scenario fails: a cleanup job passes an API key typed into the code, and the reviewer says it's fine. Every agent call is a neatlogs trace, so the failure isn't a guess." |
| 1:15–1:40 | Muaaz | **neatlogs dashboard**: the failing trace's span tree; then the Diagnostician card: layer **prompt_policy**, with its span ids | "The Diagnostician reads the failing spans and must name exactly one layer: code, tool, prompt, or test, and cite the spans. Here it's the prompt. The Fixer may only edit that layer, a small patch under a size cap. If it blames a test, a human must approve. Susanoo can't mark its own homework." |
| 1:40–2:00 | Muaaz | **GitHub**: `susanoo-apps/python-code-review-hero7`, the commit `60edf30`, then terminal `entire checkpoint list` (or the text file) with checkpoint `01M4RB1D8JPSNQYGPBW8V7DCMT`; flash the Susanoo repo with `entire graph` output | "Every patch is a commit with an Entire checkpoint: the failure, the diagnosis, the reason. The whole history is public. We used Entire on Susanoo itself too, switched on late, and entire-graph maps the codebase." |
| 2:00–2:22 | Muaaz | Scoreboard for hero-7: recall 93% → 100%, F1 0.96 → 1.00, acceptance 89% → 100%, cost $1.75; stop reason "all passed" | "Then the Verifier re-runs everything, hidden checks included. A fix stays only if its target passes, nothing regresses, and the hidden score holds. Otherwise it's a git revert. Result: problems caught, ninety-three to a hundred percent. Acceptance, eighty-nine to a hundred. A dollar seventy-five. The hidden score was already perfect, so it couldn't rise." |
| 2:22–2:44 | Jugaad | **cfo.ai** plan: unit-economics tab (cost per run), pricing table, base and growth scenario, runway `[[SLOT: runway months from the cfo.ai export]]` | "cfo.ai turned those traces into a business. One hardened agent costs about a dollar fifty in model spend. Team is seventy-nine dollars for twenty runs: about sixty percent margin at full use. Free runs are modelled as pure cost, and that drives the runway." |
| 2:44–2:55 | Both (split-screen or alternate) | Live URL + repo URL + `EVIDENCE.md`; #neatHack | Jugaad: "Try it at the link." Muaaz: "Every claim links to a trace or a checkpoint. Built in public, hashtag neatHack." |

Word counts: about 360 spoken (Jugaad about 130, Muaaz about 230), roughly 145 wpm. Muaaz carries the technical middle, Jugaad opens and closes the product and business story. Read each line aloud against a timer. If a line runs over, cut the second sentence, never speed up.

## 3. Sponsor checklist (the video must cover each)

| | neatlogs | Entire | cfo.ai |
|---|---|---|---|
| Architecture | One trace per head and per generated-agent call (0:55) | A checkpoint per Fixer patch on the generated repo (1:40) | Plan built from measured cost per run (2:22) |
| Implementation | Diagnostician reads failing spans, cites span ids (1:15) | Fixer commits, then checkpoints with failure, diagnosis, reason (1:40) | Pricing, cost, scenarios entered in cfo.ai (2:22) |
| Runtime | Real run traces on screen (1:15) | Real checkpoint id on screen (1:40) | Export on screen (2:22) |
| Reasoning | Span evidence leads to one layer (1:15) | The checkpoint carries why the patch was made (1:40) | Margin and runway follow from cost per run (2:22) |
| Result | Before/after numbers (2:00) | Kept patch with full history (1:40) | Base vs growth runway (2:22) |

## 4. Before you record (do these in this order)

1. **neatlogs:** open a hero-7 trace and note the exact view you'll show. Send me the URL so the EVIDENCE links get filled.
2. **Tabs ready, in order:** landing page → new-project screen → demo run replay → neatlogs trace → GitHub hero-7 repo (commit `60edf30`) → terminal in the Susanoo repo (`entire checkpoint list | head`, `entire graph stats`) → cfo.ai plan.
3. **Hide:** `.env`, API keys, personal email in any window, the browser bookmarks bar. Terminal font large.
4. **Footage to crop or avoid:** the demo replay is hero-2 (its events say "you approved" about cards a script handled). Show the live run view only for the heads graph and the failure ledger; take the numbers and the `sim-17` story from hero-7. Say "two real runs" if both appear.
5. **Dry-run the live link** before recording (Jugaad): `/demo/<id>` loads and the run view shows events.
6. Record at 1080p, one segment at a time, 3 takes max each.

## 5. X length warning

The submission needs the video **in a public quote-tweet**. X has limited video length for free accounts (it has been 2:20); a longer video needs X Premium or has to be posted another way. **Check this now.** If neither of you can post 2:55:
- cut to about 2:15 by removing the intake sentence (0:14–0:38 → one line), the second sentence of 1:15, and the Entire graph flash; or
- post the 2:15 cut natively and link the full version on YouTube in the reply.

## 6. Quote-tweet text (edit and post)

> Susanoo: describe an AI agent in plain English. It builds it, attacks it, finds why it fails, fixes the right layer, and proves the fix.
> Traced in @neatlogs, every patch checkpointed in @EntireHQ, business planned in @arithecfo.
> Live: <link> · Code: https://github.com/JugaadChhabra/Susanoo
> #neatHack

Check each handle before posting. Add the real tweet URL to `EVIDENCE.md` §6.

## 7. Submission form (neatlogs.com/submission), answers to paste

- **Project:** Susanoo turns a plain-English spec into a running web app with its own AI agent, then attacks it, diagnoses why it fails by layer (code, tool, prompt, test), fixes the right layer, and proves the fix with traces and hidden checks.
- **Repo:** https://github.com/JugaadChhabra/Susanoo (public; all commits made during the hackathon)
- **Team:** Muaaz Shaikh (MuaazSM), Jugaad Chhabra (JugaadChhabra)
- **neatlogs:** every head and every generated-agent call is traced; the Diagnostician reads failing spans and cites span ids. Links: `EVIDENCE.md` §1, §2, §5.
- **Entire:** every Fixer patch is a commit with a checkpoint (hero-7 `60edf30`, checkpoint `01M4RB1D8JPSNQYGPBW8V7DCMT`); Entire enabled late on the Susanoo repo (21:14 IST Mon 12 Oct), history imported; entire-graph output in `docs/evidence/`. `EVIDENCE.md` §3.
- **cfo.ai:** pricing, cost per run from measured traces, base and growth scenarios, runway: `docs/business-plan/`. `EVIDENCE.md` §4.
- **Evidence:** `EVIDENCE.md` (real runs hero-2, hero-3, hero-7, backup-2).

## 8. Questions judges may ask (short answers)

- *"Did held-out improve?"* In hero-7 it was already 1.00; in hero-2 it stayed 0.91. What improved was recall on practice scenarios and the acceptance pass rate. We don't claim more.
- *"Did a rollback happen?"* Not in these runs. The Verifier's revert path is covered by tests and the loop keeps a failure ledger.
- *"Can it game its own score?"* No: hidden checks and acceptance scenarios are unreadable to the Diagnostician and Fixer, the Fixer is limited to the diagnosed layer, and test-layer changes need a person.
- *"Was a person in the loop?"* hero-2 and hero-3 were unattended runs; cards were not human-approved and test changes were rejected by the script. The product asks a person.
- *"What does a run cost?"* About $1.50 on average ($0.89 to $1.75) under a $2 cap.

## 9. Timeline to the deadline (written at 22:10 IST)

| By (IST) | Done |
|---|---|
| 22:20 | cfo.ai export in `docs/business-plan/`; neatlogs URL sent; X video length checked |
| 22:30 | Tabs ready, live link checked, script read aloud once |
| 22:30–23:05 | Record and cut the video (2–3 takes per segment, no more) |
| 23:05 | 2–3 build-in-public posts live, links in `EVIDENCE.md` §6 |
| 23:20 | Quote-tweet posted with the video |
| 23:30 | Submission form submitted |
| 23:55 | Window closes (don't plan to use this) |

If you are behind at 22:45: record the 2:15 cut from section 5 and skip the face-cam opening.
