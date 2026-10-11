# PROMPTBOOK — Jugaad (Frontend, platform backend, testing, infra)

Copy-paste prompts for Claude Code, phase by phase. Each phase follows `IMPLEMENTATION_JUGAAD.md`, which follows `PRD.md`, the single source of truth. Claude reads `CLAUDE.md` automatically; the prompts point it at the right skill and section.

**How to use**
- Run the prompts in order. Each `▶` block is one message to Claude Code.
- Paste the **session-start prompt** at the beginning of every new session.
- After each phase, run the **phase-close prompt** (verify → evidence → commit → post).
- Your git identity must be `JugaadChhabra` (`make check-identity`).

---

## Session start (every new session)

▶
```
I'm Jugaad (git user JugaadChhabra). Read CLAUDE.md, then IMPLEMENTATION_JUGAAD.md, and use the prd-lookup skill for any section you need from PRD.md.
Tell me in 5 lines or fewer: which phase I'm in (look at what exists in the repo and the last commits), what's done, and the next 3 tasks from IMPLEMENTATION_JUGAAD.md with the PRD sections they cite.
Don't write code yet.
```

---

## Phase 0 — Contracts, scaffold, skeleton deploy (Sun 18:00–21:00)

▶ **0.0 Dev kit in the repo (first thing)**
```
This repo is https://github.com/JugaadChhabra/Susanoo. The dev kit files are already at the root (CLAUDE.md, .claude/, .githooks/, .devkit/, PRD.md/PRD.pdf, IMPLEMENTATION_*.md, PROMPTBOOK_*.md).
Run bash scripts/setup-dev.sh and show me the output. If it says my identity is wrong, stop and tell me the exact command to run myself. Don't set it yourself.
```

▶ **0.1 Schemas (with Muaaz on a call)**
```
Muaaz is creating packages/core/susanoo/schemas/ from [PRD › Contracts (locked)]. Review his models against the PRD line by line (use the prd-lookup skill) and list any mismatch. Don't change anything; report only.
```

▶ **0.2 Monorepo scaffold**
```
Use prd-lookup for [PRD › Repository structure] and [PRD › Tech stack]. Scaffold the monorepo exactly as the tree shows:
- uv workspace root pyproject.toml with members apps/api, apps/worker, packages/core (Python 3.12, ruff, pytest)
- apps/api/susanoo_api/main.py with /health only, CORS for APP_DOMAIN, and consistent JSON errors
- apps/web: Next.js + TypeScript strict, Tailwind, shadcn/ui, React Flow; route groups (marketing), (auth) and (app) with placeholder pages
- Keep the existing Makefile targets working (dev, test, lint, types)
Don't create anything the tree doesn't list.
```

▶ **0.3 Type export**
```
Write scripts/export_types.py: export every Pydantic model in packages/core/susanoo/schemas/ to apps/web/types/generated.ts (JSON Schema → TypeScript; pick a maintained tool and say why). Add a CI step that fails when generated.ts is stale. Run make types and show the output.
```

▶ **0.4 Skeleton deploy**
```
Use the deploy-check skill and [PRD › Deployment]. Set up:
- docker-compose.yml (api, worker, caddy) and a Caddyfile routing api.<API_DOMAIN> and the wildcard run-*.<PREVIEW_DOMAIN> with automatic HTTPS
- .github/workflows/ci.yml (ruff, pytest, tsc, eslint, type-staleness) and deploy-backend.yml (SSH to EC2: compose pull + up -d on push to main)
- Vercel for apps/web
- Give the worker container access to the Docker API so it can start sibling containers, with a label convention for Caddy routing (document it in docs/integration-points.md for Muaaz)
Give me a checklist of the manual steps only I can do (EC2 instance, Cloudflare DNS records, Supabase project, GitHub secrets, Vercel env vars), in order.
```

▶ **Phase-close**
```
Phase 0 close-out: verify https://app.<domain> calls https://api.<domain>/health, which reads from Supabase; CI and commit-guard are green; a push to main redeploys both. Commit with the commit-and-push skill (infra, chore, feat(web)) and push.
Draft the kickoff #neatHack post with the architecture diagram (docs/build-in-public.md) and log it in EVIDENCE.md §6. Confirm Entire is recording checkpoints on this repo.
```

---

## Phase 1 — Database, auth, quota, projects (Sun 21:00–Mon 00:00)

▶ **1.1 Migration**
```
Use prd-lookup for [PRD › Data model]. Write supabase/migrations/001_init.sql creating every table and column in the ER diagram (users, connections, projects, runs, intake_items, run_events, scenarios, iterations, results, diagnoses, patches, approvals), with FKs, sensible NOT NULLs, an index on run_events(run_id, seq) and on runs(status, created_at).
Enable row-level security on every table so users only see their own rows (via projects.user_id). Store connection secrets in Supabase Vault; connections.vault_secret_id points to them.
Don't create LangGraph's checkpoint tables; Muaaz's PostgresSaver.setup() does that.
Show me the SQL before applying it.
```

▶ **1.2 Auth**
```
Implement Supabase Auth per [PRD › Frontend (marketing site + app) and API › Auth flow]: email magic link + GitHub OAuth.
- Frontend: lib/supabase.ts, the (auth)/login page, and middleware that redirects logged-out users away from (app) routes
- Backend: apps/api/susanoo_api/auth.py verifies the Supabase JWT and provides a current_user dependency; deps.py and db.py with a db_session dependency
Test: 401 without a token, 200 with one.
```

▶ **1.3 Events helper, quota, projects**
```
Use the api-endpoint skill.
1. In db.py add emit_event(run_id, head, type, message, payload, trace_url=None) that writes a RunEvent row with seq increasing per run (safe under concurrency). Expose it so packages/core can import it without depending on apps/api; agree the import path with Muaaz and document it in docs/integration-points.md.
2. quota.py: enforce_run_quota dependency: 3 runs per user per day (RUNS_PER_USER_PER_DAY) plus the global daily cap (GLOBAL_DAILY_CAP_USD); 429 with a plain-English message.
3. routers/projects.py: POST /projects, project list, PUT /projects/{id}/rules (rules as sentences).
Tests: RLS isolation between two users, quota 429 on the 4th run, projects happy path.
```

▶ **Phase-close**
```
Phase 1 close-out: run the tests; apply the migration to Supabase; log in on the live site and create a project via the API. Commit (feat(db), feat(api)) with the commit-and-push skill and push. Tell Muaaz the emit_event import path and the label convention.
```

---

## Phase 2 — All app screens on mock events (Mon 00:00–05:00)

Muaaz commits `tests/fixtures/run_events.json` and `iteration_metrics.json` by Sun 23:00. If they're late, generate your own from the schemas and swap later.

▶ **2.1 Plumbing**
```
Use the frontend-screen skill. Build lib/api.ts (typed fetch using only types/generated.ts, with auth header), and lib/sse.ts (EventSource for GET /runs/{id}/events, resume from last seq, dedupe by seq, plus a fixture mode that replays tests/fixtures/run_events.json on a timer).
Make the run view's data source an event array, so live, fixture and demo replay share the same components.
```

▶ **2.2 Run view (the demo centrepiece)**
```
Use frontend-screen and prd-lookup for [PRD › Frontend (marketing site + app) and API › Screens] and [PRD › LangGraph flow and shared state].
Build (app)/runs/[id] with HeadsGraph (React Flow: Planner → Builder → Agent Smith → Simulator → Runner → Diagnostician → Fixer → Verifier, loop edge Verifier → Runner; states started/finished/error/approval_needed), EventLog (plain-English messages, links to traces and commits, a Details toggle), Scoreboard (line chart of recall, precision and held-out F1 per iteration; cost vs cap), FailureLedger (friendly layer badges, hypothesis, kept or rolled back) and ApprovalCard.
Drive it from the fixtures. It must look great: this is what judges see in the video.
```

▶ **2.3 Intake screen**
```
Use frontend-screen and prd-lookup for [PRD › Intake: questions and approved scenarios]. Build (app)/runs/[id]/intake: up to 5 questions (multiple choice + suggested default + why), then scenario cards to approve, edit or reject; Build unlocks only after approval. Wire to GET /runs/{id}/intake, POST …/intake/answers, POST …/intake/scenarios (mock them until Muaaz's router lands).
```

▶ **2.4 Remaining app screens**
```
Use frontend-screen. Build the remaining screens from the skill's table: (app)/projects, (app)/projects/new (code-reviewer example pre-filled from specs/code-review.md), (app)/connectors (UI only for now) and (app)/runs/[id]/agent (AgentCard, ReportCard, preview iframe, repo link, "Try it" with thumbs up/down that posts to /runs/{id}/feedback).
Every screen needs loading, empty and error states and must work at phone width.
```

▶ **Phase-close**
```
Phase 2 close-out: click through all screens on fixtures; the run view animates a full run including an approval card. Run make lint. Commit (feat(web)) and push. Record a 20-second screen capture of the run view for a #neatHack post.
```

---

## Phase 3 — Marketing site (Mon 06:00–10:00)

▶ **3.1 Landing page**
```
Use frontend-screen (load the frontend-design skill too, if available) and prd-lookup for [PRD › Frontend (marketing site + app) and API › Surface 1: Marketing site].
Build (marketing)/page.tsx with the sections in the PRD's order: Hero ("AI agents are a many-headed mess. Susanoo tames them." with Start building and Watch the demo CTAs), How it works (eight heads animated in sequence), Proof (before/after numbers, placeholders until Muaaz's saved run), Use cases, Trust, Connectors, Pricing teaser, embedded Demo, Footer (repo, #neatHack, muaazshaikh.com, jugaadchhabra.dev).
Enterprise-grade feel in the spirit of sierra.ai, but original design and copy: don't copy their layout, assets or text. Responsive at phone width; add OG image and meta tags.
```

▶ **3.2 Pricing and demo pages**
```
Build (marketing)/pricing (Free / Team / Enterprise per [PRD › Business model (inputs for cfo.ai)]; prices blank or "Contact us" until the cfo.ai model sets them) and (marketing)/demo (the run view fed by GET /demo/{run_id}, no login).
```

▶ **Phase-close**
```
Phase 3 close-out: check the marketing pages at desktop and phone width, run make lint, commit (feat(marketing)) and push. Draft a #neatHack post with a screenshot of the landing page.
```

---

## Phase 4 — Connectors, tests, going live (Mon 10:00–13:00, with Muaaz)

▶ **4.1 Connectors**
```
Use prd-lookup for [PRD › Connectors and API budget]. Implement packages/core/susanoo/connectors/: base.py (spec, guide, test_connection(), tools(), dry-run for writes), github.py (fine-grained token scoped to one repo: read pull requests + contents; comments optional and dry-run by default), anthropic.py (BYOK or platform key with caps), and guides/github.md and guides/anthropic.md.
Then routers/connectors.py: GET /connectors, POST /connections (save to Vault), POST /connections/{id}/test, DELETE /connections/{id}. Never return or log tokens; redact them in spans.
List the click-paths in the guides that I must verify manually against the current GitHub and Anthropic Console UIs.
```

▶ **4.2 Demo endpoint**
```
Use the api-endpoint skill. Implement routers/demo.py: GET /demo/{run_id} returns the stored RUN_EVENTS of a saved run, with no auth. Muaaz saves runs with scripts/save_demo_run.py. Point (marketing)/demo and the landing-page demo at the run_id he gives me.
```

▶ **4.3 Tests and CI**
```
Use prd-lookup for [PRD › Team and ownership › Testing]. Add:
- tests/api/: auth required, RLS isolation, quota 429, connector save/test/revoke (no secrets in responses), demo replay
- tests/contracts/: fixture payloads validate against the Pydantic models; generated.ts is current
- tests/e2e/ (Playwright): log in → new project → intake → run view receives events (against demo mode, so it's deterministic) → agent page loads
All of it runs in ci.yml on every push. Show me a green run.
```

▶ **4.4 Swap to real APIs and run the hero**
```
Switch the app from fixtures to the real API and SSE (keep fixture mode behind a flag). With Muaaz, run the hero spec on the LIVE site using the deploy-check skill's checklist. Report anything broken with the exact screen and the response.
```

▶ **Phase-close**
```
Phase 4 close-out: a new user can sign up on the live site, connect GitHub, run the hero spec under quota, and watch it improve; CI is green. Commit and push. Go through the deploy-check skill's "Safety checks before going public" list and report each item.
```

---

## Phase 5 — After freeze (Mon 13:00–18:00, with Muaaz)

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

---

## Relief backlog (when you're ahead; tell Muaaz first)

▶
```
I'm ahead of schedule. From IMPLEMENTATION_JUGAAD.md §6 (relief backlog), check which items Muaaz hasn't started (look at the repo and recent commits), recommend the most valuable one, and wait for my go-ahead. Remember the api-endpoint skill's ownership table: tell me before you touch Muaaz's routers.
```

## If you're behind

▶
```
I'm behind schedule. Compare the repo against IMPLEMENTATION_JUGAAD.md and list what's done, what's missing, and the smallest set of tasks that keeps the live demo working by 13:00 (run view + demo mode + auth + quota first; marketing polish last). Only propose cuts that keep the PRD's must-haves ([PRD › Deployment] public-safety items, [PRD › Goals, non-goals, success metrics]).
```
