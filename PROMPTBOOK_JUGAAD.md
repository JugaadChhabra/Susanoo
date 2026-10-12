# PROMPTBOOK — Jugaad (Frontend, platform backend, testing, infra)

One prompt per ticket. The tickets are GitHub issues #1–#21 on `JugaadChhabra/Susanoo`, broken out of `IMPLEMENTATION_JUGAAD.md`, which follows `PRD.md`, the single source of truth. Claude reads `CLAUDE.md` automatically; the prompts point it at the right skill and section.

**How to use**
- **Automatically:** `scripts/run-tickets.sh` picks the next unblocked `ready-for-agent` issue, starts a fresh Claude Code session, and runs `/mattpocock-skills:implement` with that issue's prompt from this file. The prompt is found by its heading, `### #<issue> — <title>`, so keep that heading format and keep exactly one fenced block under each heading.
- **By hand:** start a new session, paste the session-start prompt, then `/mattpocock-skills:implement` followed by the ticket's prompt.
- Every ticket ends the same way: typecheck and tests pass, `/code-review` is done, and work is committed in the `CLAUDE.md` §3 format. The script then merges to `main`, pushes and closes the issue.
- `ready-for-human` tickets (#2, #21) are never run by the script. Run them by hand.
- Your git identity must be `JugaadChhabra` (`make check-identity`).

---

## Session start (manual sessions only)

▶
```
I'm Jugaad (git user JugaadChhabra). Read CLAUDE.md, then IMPLEMENTATION_JUGAAD.md, and use the prd-lookup skill for any section you need from PRD.md.
Run `gh issue list --state open` and tell me in 5 lines or fewer: which tickets are done, which are unblocked now, and which one I should take next.
Don't write code yet.
```

## Before ticket #1 (manual, once)

▶ **Dev kit**
```
Run bash scripts/setup-dev.sh and show me the output. If it says my identity is wrong, stop and tell me the exact command to run myself. Don't set it yourself.
```

▶ **Schema review (done: schemas are on main; ask Muaaz to confirm)**
```
packages/core/susanoo/schemas/ was copied verbatim from [PRD › Contracts (locked)] and [PRD › LangGraph flow and shared state] (core.py, events.py, state.py). Review the models against the PRD line by line (use the prd-lookup skill) and list any mismatch. Don't change anything; report only.
```

---

## Foundation

### #1 — Monorepo skeleton runs locally
```
Use prd-lookup for [PRD › Repository structure] and [PRD › Tech stack]. Scaffold the monorepo exactly as the tree shows:
- uv workspace root pyproject.toml with members apps/api, apps/worker, packages/core (Python 3.12, ruff, pytest)
- apps/api/susanoo_api/main.py: /health that reads from Supabase (degrade with a clear status if SUPABASE_URL is unset), CORS for APP_DOMAIN, one consistent JSON error shape, and a clear place to mount both owners' routers
- apps/web: Next.js + TypeScript strict, Tailwind, shadcn/ui, React Flow; route groups (marketing), (auth) and (app) with placeholder pages; the placeholder home page calls /health and shows the result
- .env.example (names only) and an MIT LICENSE
- .github/workflows/ci.yml: ruff, pytest, tsc and eslint on every PR and push
Keep the existing Makefile targets working (dev, test, lint, types). Don't create anything the tree doesn't list.
Acceptance: make lint and make test pass; make dev serves the page and /health.
```

### #2 — Skeleton live on every tier (Vercel, EC2, Supabase, Cloudflare)
*ready-for-human: run by hand.*
```
Use the deploy-check skill and [PRD › Deployment]. Set up:
- docker-compose.yml (api, worker, caddy) and a Caddyfile routing api.<API_DOMAIN> and the wildcard run-*.<PREVIEW_DOMAIN> with automatic HTTPS
- .github/workflows/deploy-backend.yml (SSH to EC2: compose pull + up -d on push to main)
- Vercel for apps/web
Give me a checklist of the manual steps only I can do (EC2 instance, Cloudflare DNS records for app., api. and *., Supabase project, GitHub secrets, Vercel env vars), in order.
Done when https://app.<domain> calls https://api.<domain>/health, which reads from Supabase, and a push to main redeploys both.
Afterwards: draft the kickoff #neatHack post with the architecture diagram (docs/build-in-public.md), log it in EVIDENCE.md §6, and confirm Entire is recording checkpoints.
```

### #3 — Generated frontend types and contract tests in CI
```
The schemas are on main (packages/core/susanoo/schemas/: core.py, events.py, state.py) and scripts/export_types.py already writes apps/web/types/generated.ts from them. Check the export against the models (e.g. fields with defaults, like AnswerKey.must_not_flag and RunEvent.trace_url, are always present in API responses) and fix it if needed. Add a CI step that fails when generated.ts is stale.
Add tests/contracts/: sample RunEvent, Scenario, IterationMetrics and Diagnosis payloads validate against the Pydantic models. Put the samples where frontend screens can reuse them as fixtures.
Don't edit anything in packages/core/susanoo/schemas/ (frozen; see the contract-change skill).
Acceptance: make types is deterministic; CI fails on a stale generated.ts; contract tests pass.
```

### #4 — Generated apps run as sibling containers behind Caddy
```
Use the deploy-check skill and [PRD › Deployment] / [PRD › Deployment › Sandbox limits].
Give the worker container access to the Docker API so it can start sibling containers, and make Caddy route run-<id>.<PREVIEW_DOMAIN> to a container by label. Prove it with a hello-world container started from the worker. Containers get CPU and memory caps and no host mounts.
Use the label convention Muaaz's sandbox/docker.py expects; if docs/integration-points.md still says TBD for it, propose one there and report TICKET_BLOCKED so I can agree it with Muaaz.
List any EC2 steps I must do by hand in your final report.
```

## Data, auth, projects

### #5 — Full data model with row-level security
```
Use prd-lookup for [PRD › Data model]. Write supabase/migrations/001_init.sql creating every table and column in the ER diagram (users, connections, projects, runs, intake_items, run_events, scenarios, iterations, results, diagnoses, patches, approvals), with FKs, sensible NOT NULLs, an index on run_events(run_id, seq) and on runs(status, created_at).
Enable row-level security on every table so users only see their own rows (via projects.user_id). Store connection secrets in Supabase Vault; connections.vault_secret_id points to them.
Don't create LangGraph's checkpoint tables; Muaaz's PostgresSaver.setup() does that.
Test against a local Supabase (supabase start) only; never touch the hosted project. Write a test proving user B can't read or write user A's projects, runs or connections.
Put the full SQL summary in your final report so I can review it before applying it to the hosted project.
```

### #6 — emit_event writes ordered run events
```
Use the api-endpoint skill. In db.py add the session helper and emit_event(run_id, head, type, message, payload, trace_url=None), which writes a RunEvent row with seq increasing per run (safe under concurrency). Expose it so packages/core can import it without depending on apps/api, and record the import path in docs/integration-points.md for Muaaz.
Tests: rows match the RunEvent shape; seq is strictly increasing per run under concurrent calls.
```

### #7 — Sign in and out (magic link + GitHub OAuth)
```
Implement Supabase Auth per [PRD › Frontend (marketing site + app) and API › Auth flow]: email magic link + GitHub OAuth.
- Frontend: lib/supabase.ts, the (auth)/login page, sign out, and middleware that redirects logged-out users away from (app) routes to /login
- Backend: apps/api/susanoo_api/auth.py verifies the Supabase JWT and provides a current_user dependency; deps.py wires it
/health, /demo/* and the marketing pages stay public.
Tests: 401 without a token, 200 with one.
```

### #8 — Create and list projects
```
Use the api-endpoint and frontend-screen skills.
- routers/projects.py: POST /projects, project list, PUT /projects/{id}/rules (rules as sentences, [PRD › Non-technical users › Extra API]); all require auth
- lib/api.ts: typed fetch using only types/generated.ts, with the auth header
- (app)/projects: the user's projects and runs (status, held-out F1, cost)
- (app)/projects/new: spec box, rules as sentences, connector picker (stub for now), example specs with the code reviewer pre-filled, Start
Every screen: types only from generated.ts, loading/empty/error states, works at phone width, plain English by default.
Tests: projects happy path; another user's projects never appear.
```

### #9 — Daily run quota and global cap
```
Use the api-endpoint skill and prd-lookup for [PRD › Connectors and API budget › Anthropic budget].
quota.py: an enforce_run_quota dependency for Muaaz's POST /projects/{id}/runs: 3 runs per user per day (RUNS_PER_USER_PER_DAY) plus the global daily cap (GLOBAL_DAILY_CAP_USD); 429 with a plain-English message. Demo mode is never quota-limited.
Show "runs left today" on the (app)/projects screen.
Tests: 429 on the 4th run of the day; global cap refuses new runs; /demo/* unaffected.
```

## App screens (on fixture events)

If Muaaz's `tests/fixtures/run_events.json` and `iteration_metrics.json` aren't in the repo yet, use the contract samples from #3 and generate the rest from the schemas.

### #10 — Live run view: heads graph and event log (on fixtures)
```
Use frontend-screen and prd-lookup for [PRD › Frontend (marketing site + app) and API › Screens] and [PRD › LangGraph flow and shared state].
- lib/sse.ts: EventSource for GET /runs/{id}/events, resume from last seq on reconnect, dedupe by seq, plus a fixture mode that replays the fixture events on a timer (unit-test resume and dedupe)
- (app)/runs/[id] driven by an event array, so live, fixture and demo replay share the same components
- HeadsGraph (React Flow): Planner → Builder → Agent Smith → Simulator → Runner → Diagnostician → Fixer → Verifier, loop edge Verifier → Runner; map event.head to the node and started/finished/error/approval_needed to its state; the active head glows and edges animate
- EventLog: plain-English messages, links to neatlogs traces and commits, raw details behind a Details toggle
It must look great: this is what judges see in the video. Loading/empty/error states; phone width.
```

### #11 — Run view: scoreboard, failure ledger and approval card
```
Use frontend-screen. Add to (app)/runs/[id]: Scoreboard (line chart of recall, precision and held-out F1 per iteration; cost vs cap), FailureLedger (friendly layer badges, hypothesis, kept or rolled back) and ApprovalCard for test-layer changes.
Drive it from the fixtures; the run view must animate through a full fixture run including an approval card. Loading/empty/error states; phone width.
```

### #12 — Intake screen: questions and scenario approval (on fixtures)
```
Use frontend-screen and prd-lookup for [PRD › Intake: questions and approved scenarios]. Build (app)/runs/[id]/intake: up to 5 questions (multiple choice + suggested default + why), then scenario cards to approve, edit or reject; Build unlocks only after approval.
Wire to GET /runs/{id}/intake, POST …/intake/answers and POST …/intake/scenarios, mocked until Muaaz's router lands. Loading/empty/error states; phone width.
```

### #13 — Agent screen: card, report, preview and Try it (on fixtures)
```
Use frontend-screen and prd-lookup for [PRD › Non-technical users]. Build (app)/runs/[id]/agent: AgentCard, ReportCard, a preview iframe of run-<id>.<domain>, the repo link, and "Try it" (paste a diff or pick a PR) with thumbs up/down that posts to /runs/{id}/feedback (mocked until Muaaz's router lands). Loading/empty/error states; phone width.
```

### #14 — Public demo replay
```
Use the api-endpoint skill. Implement routers/demo.py: GET /demo/{run_id} returns the stored RUN_EVENTS of a saved run, with no auth and no quota. Muaaz saves runs with scripts/save_demo_run.py; until one exists, seed a fixture run for tests.
Build (marketing)/demo: the run view from #10/#11 fed by GET /demo/{run_id}, replayed on a timer, no login.
Tests: demo replay works with no auth.
```

## Connectors

### #15 — Connect a GitHub repo
```
Use prd-lookup for [PRD › Connectors and API budget] and [PRD › Connectors and API budget › Credential handling].
- packages/core/susanoo/connectors/: base.py (spec, guide, test_connection(), tools(), dry-run for writes), github.py (fine-grained token scoped to one repo: read pull requests + contents; comments optional and dry-run by default), guides/github.md
- routers/connectors.py: GET /connectors, POST /connections (save to Vault), POST /connections/{id}/test, DELETE /connections/{id}; all require auth
- (app)/connectors: available connectors, the setup guide beside each credential field, Test connection, revoke; the new-project connector picker lists real connections
Never return or log tokens; redact them in logs and neatlogs spans.
Tests: connector save/test/revoke, no secrets in any response.
List the click-paths in guides/github.md that I must verify by hand against the current GitHub UI.
```

### #16 — Anthropic connector: own key or platform key
```
Use prd-lookup for [PRD › Connectors and API budget › Anthropic budget]. Add anthropic.py (the user's own key or the platform key, each with its cap) and guides/anthropic.md, using the connector contract from #15. The key never appears in responses, logs or traces.
Tests: BYOK save/test/revoke; platform-key path applies the platform cap.
List the click-paths in guides/anthropic.md that I must verify by hand against the current Anthropic Console.
```

## Marketing

### #17 — Marketing landing page
```
Use frontend-screen (load the frontend-design skill too, if available) and prd-lookup for [PRD › Frontend (marketing site + app) and API › Surface 1: Marketing site].
Build (marketing)/page.tsx with the sections in the PRD's order: Hero ("AI agents are a many-headed mess. Susanoo tames them." with Start building and Watch the demo CTAs), How it works (eight heads animated in sequence), Use cases (code review live; support, ops and compliance coming soon), Trust, Connectors (GitHub and Anthropic live; others roadmap), Footer (repo, #neatHack, muaazshaikh.com, jugaadchhabra.dev). Leave a slot for Proof and the embedded demo (#19).
Enterprise-grade feel in the spirit of sierra.ai, but original design and copy: don't copy their layout, assets or text. Responsive at phone width; add OG image and meta tags.
```

### #18 — Pricing page
```
Build (marketing)/pricing: Free / Team / Enterprise per [PRD › Business model (inputs for cfo.ai)]; prices blank or "Contact us" until the cfo.ai model sets them. Responsive at phone width.
```

### #19 — Proof section and embedded demo on the landing page
```
Fill the landing page's Proof slot with the real before/after from the saved demo run (held-out F1, recall, false alarms), linking to its neatlogs traces, and embed the demo replay from #14.
Use the run_id in docs/integration-points.md. If it's still TBD, stop and report TICKET_BLOCKED; never ship placeholder numbers as proof.
```

## Going live

### #20 — End-to-end smoke test in CI
```
Use prd-lookup for [PRD › Team and ownership › Testing]. Add tests/e2e/ (Playwright): log in → new project → intake → run view receives events (against demo mode, so it's deterministic) → agent page loads. Run it in ci.yml on every PR and push, and make sure a failing smoke test fails CI.
```

### #21 — Hero spec end to end on the live site
*ready-for-human: run by hand, with Muaaz.*
```
Switch the app from fixtures to the real API and SSE (keep fixture mode behind a flag). With Muaaz, run the hero spec on the LIVE site using the deploy-check skill's checklist. Report anything broken with the exact screen and the response.
Then go through the "before going public" checklist in IMPLEMENTATION_JUGAAD.md §7 and report each item: auth on every (app) and API route except /health, /demo/* and marketing; RLS tested; quota and global cap enforced; Anthropic Console spend limit set; demo mode works with no login and no quota.
Done when a new user can sign up on the live site, connect GitHub, run the hero spec under quota and watch it improve, and CI is green.
```

---

## Handoffs from Muaaz's overnight build

### #23 — Mount the AI routers and match the platform to them
```
Read docs/integration-points.md rows "Mounting the AI-serving routers" and "Run queue schema + worker deployment" first; they are the spec. Use prd-lookup for [PRD › Frontend and API] and the public-safety rules in CLAUDE.md §5.
Mount Muaaz's routers (runs, events, intake, approvals, feedback, agent) by filling AI_ROUTERS in apps/api/susanoo_api/routers/__init__.py. Don't rewrite his router logic; if one needs a change, make the smallest fix and say so in the final report.
Then the platform side: the real POST /projects/{id}/runs replaces the stand-in in tests/api/test_quota.py; add queued and cancelled to RunSummary.status (migration 006); count queued runs in quota usage; export the routers' response models to generated.ts (make types) so the CI staleness check passes; answers and approvals require pause_token, a stale one returns 409 with a plain-English message.
Tests: auth required on every new route, RLS isolation between two users, quota counts queued runs, 409 on a stale pause_token.
```

### #24 — Signed-in app on the real API instead of fixtures
```
Use prd-lookup for [PRD › Frontend and API] and the screens it lists. Switch the signed-in app from fixtures to the real API mounted in #23: create run, the live run view on GET /runs/{id}/events (resume from the last seq after a dropped connection), intake questions and scenario approval, the test-change approval card, and the agent page's real cards. Answers and approvals send the pause_token; on 409 show "this step has moved on" in plain English and reload the step.
Keep fixtures only for tests and for development without an API. Only types from generated.ts. Keep every screen's loading, empty and error states and phone width (IMPLEMENTATION_JUGAAD.md §7). The e2e smoke test must still pass against demo mode.
List in the final report how to try it locally end to end (make dev + local worker + hosted Supabase).
```

### #25 — Connector tool code that generated agents can use
```
Read docs/integration-points.md row "Connector tool code in generated repos" and [PRD › Connectors and API budget › Connector contract] (prd-lookup). Add a credential-free way to ship connector tool code into generated repos, e.g. tool_sources() -> list[str]: self-contained functions, imports inside the body, credentials read only from the env vars env() sets, GitHub writes dry-run unless GITHUB_WRITES_ENABLED is on. env() and tools() must keep working with a placeholder credential.
Tests: each source compiles on its own with no susanoo import, contains no secret-shaped strings, and passes Agent Smith's secret scan (call its checker; don't edit heads/).
Don't edit Muaaz's heads. End the final report with exactly what Agent Smith needs to call, so I can pass it to Muaaz.
```

## Not ticketed: after freeze (Mon 13:00–18:00, with Muaaz)

▶ **README and evidence**
```
Feature freeze is on; no new features. Update README.md with the pitch, an architecture diagram (from [PRD › System architecture]), the live link, the demo link and how to run it. In EVIDENCE.md, fill §6 (all #neatHack links) and §7 (repo + first commit hash and time, which must be after Sat 10 Oct 2026 12:30 IST); help Muaaz with §3 (entire-graph output of this repo) using the evidence-capture skill. Real links only.
```

▶ **cfo.ai**
```
Using docs/business-plan/cost-inputs.md from Muaaz and [PRD › Business model (inputs for cfo.ai)], give me the exact numbers to enter in cfo.ai for base and growth scenarios (customers, ARPU per plan, inference and infra cost per run, fixed costs), with the reasoning in one line each. I'll enter them and export the plan to docs/business-plan/.
```

▶ **Video**
```
Using docs/demo-script.md, write my narration for 0:00–0:40 and 2:25–3:00 (problem, sign up → spec → intake, cfo.ai, call to action), under 150 words, plain spoken English. It must explicitly mention cfo.ai's role (pricing, cost per run from traces, runway).
```

▶ **Final check and submission**
```
Final check: CI green; deploy-check list passes on the live site; EVIDENCE.md has no TODOs; every commit on main is by MuaazSM or JugaadChhabra with no AI attribution (check git log authors, committers and bodies). Report anything off; don't rewrite history.
Then draft our X submission post quoting the official neatlogs post: team, project name (Susanoo), public repo https://github.com/JugaadChhabra/Susanoo, live link and demo video link, with #neatHack. The submission window is Mon 19:00–23:55 IST; submit early.
```

## Relief backlog (when you're ahead; tell Muaaz first)

▶
```
I'm ahead of schedule. From IMPLEMENTATION_JUGAAD.md §6 (relief backlog), check which items Muaaz hasn't started (look at the repo and recent commits), recommend the most valuable one, and wait for my go-ahead. Remember the api-endpoint skill's ownership table: tell me before you touch Muaaz's routers.
```

## If you're behind

▶
```
I'm behind schedule. Compare the open issues and the repo against IMPLEMENTATION_JUGAAD.md and list what's done, what's missing, and the smallest set of tickets that keeps the live demo working by 13:00 (run view + demo mode + auth + quota first; marketing polish last). Only propose cuts that keep the PRD's must-haves ([PRD › Deployment] public-safety items, [PRD › Goals, non-goals, success metrics]).
```
