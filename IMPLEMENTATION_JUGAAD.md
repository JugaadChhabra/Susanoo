# IMPLEMENTATION — Jugaad (Frontend, platform backend, testing, infra)

> **Single source of truth:** the Susanoo PRD ([live doc](https://claude.ai/code/artifact/a3bb4cb5-bbec-4d63-a641-a65d4a0d5124), exported to the repo root as `PRD.md` for agents and `PRD.pdf` for people).
> This file only turns the PRD into an ordered task list for Jugaad. It adds no new requirements.
> If this file and the PRD disagree, **the PRD wins**. To change scope, update the PRD first, then this file.
> References below look like **[PRD › Section name]** and match the PRD's headings exactly.

---

## 1. Your scope

From **[PRD › Team and ownership]**, you own:

| Area | What that means in code |
|---|---|
| Frontend | `apps/web/`: the marketing site `app/(marketing)/`, auth `app/(auth)/`, and the authenticated app `app/(app)/`, including the React Flow heads graph and every screen |
| Platform backend | The FastAPI app shell (`main.py`), `auth.py`, `quota.py`, `deps.py`, `db.py`, and routers `projects.py`, `connectors.py`, `demo.py`; `packages/core/susanoo/connectors/` (GitHub, Anthropic, guides) |
| Database | `supabase/migrations/`: all tables in **[PRD › Data model]**, row-level security, Vault for credentials |
| Testing | CI, API tests, contract tests for schemas, frontend end-to-end smoke tests |
| Infra | EC2, Docker Compose, Caddy, Vercel, Cloudflare DNS, GitHub Actions, secrets |
| Repo hygiene | `README.md`, public repo, commit history dated during the hackathon |
| Shared with Muaaz | `schemas/` (frozen, see **[PRD › Contracts (locked)]**), cfo.ai plan, demo video, #neatHack posts, `EVIDENCE.md` |

**Not yours** (Muaaz owns these; consume them, don't build them): the eight heads, the LangGraph graph, scoring, the sandbox runner, the worker, and the AI-serving routers (`runs.py`, `events.py`, `intake.py`, `approvals.py`, `feedback.py`, `agent.py`).

Your second job is to **ease Muaaz's backend load**. See the relief backlog in §6.

---

## 2. Ground rules (from the PRD)

- **Contracts are frozen.** The frontend and the API speak only the shapes in **[PRD › Contracts (locked)]**, exported to `apps/web/types/generated.ts`. Changing one needs Muaaz's agreement.
- **Deploy tonight, then on every push.** An empty skeleton (Vercel → FastAPI on EC2 → Supabase) goes live first. **[PRD › Deployment]**
- **Public-safety must-haves before going public:** Supabase Auth, 3 runs per user per day, a per-run cost cap, and demo mode as the safety net. **[PRD › Deployment]**, **[PRD › Connectors and API budget › Anthropic budget]**
- **Credentials never leak.** They're stored encrypted in Supabase Vault, never written to repos, logs or traces, and injected into containers as env vars at start. **[PRD › Connectors and API budget › Credential handling]**
- **Entire is on for this repo from the first commit.** Use it while you build with your coding agent; judges want Entire used to *build* Susanoo. **[PRD › Before development: first-hour checklist › Setup]**
- **Trunk-based.** Small commits to `main`, which auto-deploys. **[PRD › Before development › Working agreements]**

---

## 3. Timeline at a glance

Times are IST and come from the Gantt chart in **[PRD › Timeline, owners, risks]**.

| When | Phase | Exit criterion |
|---|---|---|
| Sun 18:00 – 21:00 | P0: Contracts, monorepo scaffold, skeleton deploy on all tiers | `app.` / `api.` live over HTTPS; CI green |
| Sun 21:00 – Mon 00:00 | P1: Supabase schema, RLS, auth, quota, projects | Log in, create a project, quota enforced |
| Mon 00:00 – 05:00 | P2: All app screens on mock events | Full app flow clickable against fixture events |
| Mon 06:00 – 10:00 | P3: Marketing site | Landing, pricing and demo pages live |
| Mon 10:00 – 13:00 | P4: Connectors, CI tests, e2e smoke; hero end to end with Muaaz | Hero spec runs on the live site |
| Mon 13:00 | **Feature freeze** | — |
| Mon 13:00 – 18:00 | P5 (together): README, evidence, cfo.ai, video | `EVIDENCE.md` complete |
| Mon 19:00 | Submit early in the 19:00–23:55 window | — |

Stagger sleep with Muaaz so one of you is always pushing.

---

## 4. Phase-by-phase tasks

### P0 — Contracts, scaffold, skeleton deploy (Sun 18:00–21:00)

1. **Lock the schemas with Muaaz** (30 min, together), from **[PRD › Contracts (locked)]**. *Done: copied verbatim into `packages/core/susanoo/schemas/` (`core.py`, `events.py`, `state.py`); Muaaz to confirm.*
2. **Monorepo scaffold**, exactly as in **[PRD › Repository structure › Susanoo monorepo]**:
   - A uv workspace root, with `apps/api`, `apps/worker` and `packages/core`.
   - `apps/web`: Next.js + TypeScript, Tailwind, shadcn/ui and React Flow (**[PRD › Tech stack]**), with route groups `(marketing)`, `(auth)` and `(app)`.
   - `.env.example`, `Makefile`, `LICENSE` (MIT), and `PRD.pdf` plus both IMPLEMENTATION files in the repo root.
3. **`scripts/export_types.py`:** Pydantic → `apps/web/types/generated.ts`. Run it in CI and fail the build if the generated file is stale.
4. **Skeleton deploy on every tier.** Topology from **[PRD › Deployment]**:
   - **Vercel:** the Next.js app.
   - **EC2** (hackathon AWS credit, us-east-1; EC2 is the allowed compute): Docker Compose running `api`, `worker` and `caddy`. Caddy terminates HTTPS and routes `api.<domain>` and the wildcard `run-*.<domain>`.
   - **Supabase:** a new project; put its URL and keys in env vars.
   - **Cloudflare DNS:** `app.`, `api.` and `*.` (for `run-<id>`).
   - **GitHub Actions:** `ci.yml` (lint + tests on PR) and `deploy-backend.yml` (SSH + `compose pull && up -d` on push to `main`).
   - Secrets live only in Vercel and EC2 env vars, never committed.
5. **Docker on EC2 for generated apps.** Make sure the worker container can reach the Docker API to start sibling containers, and that Caddy can route to containers by label. Coordinate the label convention with Muaaz's `sandbox/docker.py`.
6. **Day-one hygiene:** turn on Entire for the repo; post the first #neatHack update with the architecture diagram from **[PRD › System architecture]**.

**Done when:** `https://app.<domain>` renders a placeholder that calls `https://api.<domain>/health`, which reads from Supabase; CI is green; a push to `main` redeploys both.

### P1 — Database, auth, quota, projects (Sun 21:00–Mon 00:00)

1. **`supabase/migrations/001_init.sql`.** Create every table and column in the ER diagram in **[PRD › Data model]**: `users`, `connections`, `projects`, `runs`, `intake_items`, `run_events`, `scenarios`, `iterations`, `results`, `diagnoses`, `patches`, `approvals`.
   - Indexes on `run_events (run_id, seq)` and on `runs` status (the worker polls queued runs).
   - **Row-level security:** users see only their own projects, runs and connections. **[PRD › Data model]**
   - **Vault** for connection secrets; `connections.vault_secret_id` points to the secret.
   - Leave room for LangGraph's `PostgresSaver` tables, which Muaaz creates via `setup()`.
2. **Auth** (**[PRD › Frontend (marketing site + app) and API › Auth flow]**):
   - Supabase Auth with an email magic link and GitHub OAuth.
   - `apps/api/susanoo_api/auth.py` verifies the Supabase JWT and exposes a `current_user` dependency.
   - The frontend redirects logged-out users from `(app)` routes to `/login`.
3. **`quota.py`:** a dependency used by Muaaz's `POST /projects/{id}/runs`.
   - 3 runs per user per day on the platform key.
   - Also enforce the global daily cap. Past it, new runs are refused but demo mode stays available.
   - **[PRD › Connectors and API budget › Anthropic budget]**
4. **`db.py`:** a session helper plus `emit_event(run_id, head, type, message, payload, trace_url)`, which writes `RunEvent` rows with `seq` increasing per run. Muaaz's heads call this.
5. **`routers/projects.py`:** `POST /projects`, the projects list, and `PUT /projects/{id}/rules` (rules as sentences). **[PRD › Non-technical users › Extra API]**
6. **App shell** (`main.py`): CORS for `app.<domain>`, consistent error JSON, `/health`, and mounting both your routers and Muaaz's.

**Done when:** a logged-in user can create a project via the API; RLS blocks other users' rows (write a test for it); quota returns 429 on the 4th run of the day.

### P2 — The app, on mock events (Mon 00:00–05:00)

Build every screen against **fixture events** (the `RunEvent` / `IterationMetrics` samples from Muaaz, or ones you generate from the schemas), so you never wait on the AI. Screens come from **[PRD › Frontend (marketing site + app) and API › Screens]**.

| # | Route | What it shows |
|---|---|---|
| 1 | `(app)/projects` | The user's projects and runs: status, held-out F1, cost, quota remaining today |
| 2 | `(app)/connectors` | Available connectors, the setup guide beside each credential field, Test connection, revoke |
| 3 | `(app)/projects/new` | Spec box, rules as sentences, connector picker, example specs (code reviewer pre-filled), Start |
| 4 | `(app)/runs/[id]/intake` | Up to 5 questions with suggested defaults, then scenario cards to approve, edit or reject. Build unlocks after approval. **[PRD › Intake: questions and approved scenarios]** |
| 5 | `(app)/runs/[id]` | Live run view: React Flow graph of the eight heads (active head glows, edges animate), SSE event log with neatlogs and commit links, scoreboard (recall, precision, held-out F1 per iteration as a line chart, cost vs cap), failure ledger (layer badge, hypothesis, kept / rolled back), and an approval card for `test`-layer changes |
| 6 | `(app)/runs/[id]/agent` | Agent card, report card, live preview of `run-<id>.<domain>`, repo link, and "Try it" (paste a diff or pick a PR) with 👍 / 👎 feedback. **[PRD › Non-technical users]** |
| 7 | `(marketing)/demo` | Replay of a saved run, no login. Same components as screen 5, fed from `GET /demo/{run_id}` |

Implementation notes:

- **`lib/sse.ts`:** an EventSource client for `GET /runs/{id}/events`. Resume from the last `seq` on reconnect. Dedupe by `seq`.
- **`components/app/HeadsGraph.tsx`:** eight nodes in the order Planner → Builder → Agent Smith → Simulator → Runner → Diagnostician → Fixer → Verifier, with the loop edge Verifier → Runner. Map `event.head` to the node and `started` / `finished` / `error` to its state. **[PRD › LangGraph flow and shared state]**
- **Reusing screen 5 for replay:** make it driven by an event array, so the demo replay just feeds stored events on a timer.
- **Plain language everywhere.** Show the event's `message` text, never raw JSON, by default. **[PRD › Non-technical users]**

**Done when:** you can click through all seven screens on fixtures, and screen 5 animates through a full fixture run, including an approval card.

### P3 — Marketing site (Mon 06:00–10:00)

Content and section order come from **[PRD › Frontend (marketing site + app) and API › Surface 1: Marketing site]**. Aim for an enterprise-grade look in the spirit of sierra.ai, but with **original design and copy**. Don't copy their layout, assets or text.

- [ ] **Hero:** "AI agents are a many-headed mess. Susanoo tames them." CTAs: Start building and Watch the demo.
- [ ] **How it works:** the eight heads of Orochi, animated in sequence.
- [ ] **Proof:** a real before/after from the saved demo run (held-out F1, recall, false alarms), linking to the neatlogs traces.
- [ ] **Use cases:** code review (live); support, ops and compliance (coming soon).
- [ ] **Trust:** held-out evaluation, human approval for test changes, sandboxed execution, dry-run connectors.
- [ ] **Connectors:** GitHub and Anthropic live; others marked roadmap.
- [ ] **Pricing** (`/pricing`): Free, Team and Enterprise per **[PRD › Business model (inputs for cfo.ai)]**. Leave prices blank or "Contact us" until the cfo.ai model sets them.
- [ ] **Demo:** an embedded replay (screen 7).
- [ ] **Footer:** GitHub repo, #neatHack, muaazshaikh.com, jugaadchhabra.dev.
- [ ] Responsive at phone width, plus OG image and meta tags (they help with the X submission post).

### P4 — Connectors, tests, going live (Mon 10:00–13:00)

1. **Connectors** (`packages/core/susanoo/connectors/`), per **[PRD › Connectors and API budget]**:
   - `base.py`, the connector contract: `spec` (fields, scopes, read vs write actions), `guide.md`, `test_connection()`, `tools()`, and dry-run behaviour for writes.
   - `github.py`: a fine-grained token scoped to one repo. Read pull requests and contents; posting comments is optional and dry-run by default.
   - `anthropic.py`: the user's own key (BYOK) or the platform key, each with its cap.
   - `guides/github.md` and `guides/anthropic.md`: the steps drafted in the PRD. **Verify every click-path against the current GitHub and Anthropic Console UIs before shipping**, as the PRD flags.
   - `routers/connectors.py`: `GET /connectors`, `POST /connections` (save to Vault), `POST /connections/{id}/test`, `DELETE /connections/{id}`.
   - Tokens are redacted in logs and neatlogs spans, and injected into generated-app containers as env vars at start.
2. **`routers/demo.py`:** `GET /demo/{run_id}` returns the stored `RUN_EVENTS` of a saved run (Muaaz saves it with `save_demo_run.py`). No auth.
3. **Testing** (**[PRD › Team and ownership › Testing]**):
   - **API tests** (pytest + httpx): auth required, RLS isolation, quota 429, connector save, test and revoke, demo replay.
   - **Contract tests:** sample `RunEvent`, `Scenario`, `IterationMetrics` and `Diagnosis` payloads validate against the Pydantic models, and `generated.ts` is up to date.
   - **E2E smoke** (Playwright): log in → new project → intake → run view receives events (against demo mode, so it's deterministic) → agent page loads.
   - All of the above run in `ci.yml` on every PR.
4. **Swap fixtures for the real APIs**, then run the hero spec end to end with Muaaz on the live site. **[PRD › Goals, non-goals, success metrics]**

**Done when:** a new user can sign up on the live site, connect GitHub, run the hero spec under quota, and watch it improve. CI is green.

### P5 — After freeze (Mon 13:00–18:00, together)

- **`README.md`:** the pitch, the architecture diagram, how to run it, the live link and the demo link.
- **`EVIDENCE.md`:** your rows from **[PRD › Sponsor integrations and evidence plan › Evidence checklist]**:
  - **Code written during the hackathon:** a public repo with commits after 10 Oct, 12:30 PM IST. Check that nothing was pre-written.
  - **Used Entire:** entire-graph output of the Susanoo repo plus your checkpoints (shared with Muaaz).
  - **Built in public:** links to every #neatHack post.
- **cfo.ai** (both): sign up at cfo.ai/neathacks and cancel the subscription right after signing up. Build base and growth scenarios from Muaaz's cost-per-run numbers. Export the plan to `docs/business-plan/`. **[PRD › Business model (inputs for cfo.ai)]**
- **Video** (≤ 3 minutes): you record the product walkthrough (marketing site → sign up → spec → intake → live run view); Muaaz narrates the loop. It must explicitly cover the architecture, implementation, runtime, reasoning and result for neatlogs, Entire and cfo.ai.
- **Submit early in the 19:00 window:** quote the official neatlogs post on X with team, project name, public repo and demo link.

---

## 5. Interfaces with Muaaz (agree these early)

| You provide to Muaaz | You need from Muaaz |
|---|---|
| `db.py` with `emit_event()`, and all tables from **[PRD › Data model]** | Events in the exact `RunEvent` shape, `seq` increasing per run |
| `auth.py`, `quota.py`, `deps.py` dependencies; app shell that mounts his routers | AI routers at the paths in the PRD's API tables |
| `connectors/*.tools()` and Vault credential injection | Agent Smith uses only `tools()`; never logs tokens |
| Docker host on EC2, Caddy wildcard `run-*` routing | Container label convention from `sandbox/docker.py` |
| `export_types.py`, contract tests in CI | Sample event and metrics fixtures by Sun 23:00 |
| `GET /demo/{run_id}` | A saved completed run (`save_demo_run.py`) |

---

## 6. Relief backlog (take these when you're ahead)

These ease Muaaz's backend load without touching the AI logic. Tell Muaaz before you pick one up, so you don't both build it.

1. API tests for Muaaz's routers (runs, events, intake, approvals, feedback, agent).
2. `GET /runs/{id}/events` SSE plumbing: resumable by `seq`, keep-alive pings, Caddy buffering turned off.
3. `POST /runs/{id}/cancel`, and the worker-side check for a cancel flag.
4. `scripts/save_demo_run.py`: copy a finished run's events into a demo record.
5. Container cleanup: an idle-timeout reaper for generated-app containers. **[PRD › Deployment › Sandbox limits]**
6. A cost and usage panel reading `runs.cost_usd`, which feeds the cfo.ai cost drivers.

---

## 7. Checklists

**Before going public**
- [ ] Auth on every `(app)` route and every API route except `/health`, `/demo/*` and the marketing pages
- [ ] RLS tested
- [ ] Quota and global daily cap enforced
- [ ] Anthropic Console spend limit set (backstop)
- [ ] Demo mode works with no login and no quota

**Definition of done for every screen**
- [ ] Uses only types from `generated.ts`
- [ ] Loading, empty and error states
- [ ] Works at phone width
- [ ] Plain-English text by default; raw details behind a toggle

**Before feature freeze**
- [ ] The hero spec runs end to end on the live site
- [ ] CI green: API, contract and e2e tests
- [ ] Marketing site live with the real proof numbers
- [ ] Commit history clean and public
