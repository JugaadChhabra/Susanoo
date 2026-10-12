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

### P6 — Platform gaps found after going live (Mon 18:00 onwards, post-freeze)

Found on Mon 12 Oct once the backend was live on EC2 and Muaaz's loop was wired in. Each gap is a feature the PRD describes and the UI already shows, but which doesn't work end to end yet. They are post-freeze work, taken on by Jugaad's explicit decision; if time runs out, document whatever's unfinished as a limitation in `README.md` rather than shipping it half-done. Tickets: **#26** (connections reach the run), **#4** (live previews, rewritten), **#27** (Try it).

#### Problem Statement

A user who signs up on the live site, connects their GitHub repo and starts the hero spec gets an agent that:
- **can't use the repo they connected.** The new-project screen lets them pick connections, but the choice is thrown away: it's never sent to the API, never stored, never reaches the run. The generated agent's GitHub tools exist (#25, plus Agent Smith copying them) but run with no `GITHUB_TOKEN` or `GITHUB_REPO`, because nothing reads the user's credential from Vault and gives it to the agent's container.
- **can't be opened.** The agent page shows a "live preview" of `run-<id>.susanoo.jugaadchhabra.dev`, but nothing records a preview URL, the finished app isn't kept running for people to visit, and Caddy has no route or certificate for `run-*` hosts.
- **can't be tried from its own page.** "Try it" (paste a diff or pick a PR) calls an endpoint that doesn't exist, so it says reviewing from the page isn't on yet.

So the product promise on the marketing site ("connect GitHub", "you get a code reviewer with its own web page") stops short of what a judge can actually do.

#### Solution

1. **Connections reach the run (#26).** The connections a user picks for a project are saved with the project. When a run starts, their connector names go into the run's state (Agent Smith already reads them), and when the worker starts the generated app's container, it reads each picked connection's secret from Vault and passes it in as the env vars that connector declares. The secret never touches logs, traces, events, the repo or the API's responses.
2. **Live previews (#4).** When a run finishes, the worker keeps the final version of the generated app running in its capped container and records its preview URL on the run. Caddy serves `run-<id>.susanoo.jugaadchhabra.dev` over HTTPS, getting a certificate on demand only for runs the API confirms exist, and proxies to that run's container on the sandbox network. Idle previews are stopped by the existing reaper; the agent page says so plainly.
3. **Try it (#27).** The agent page's "Try it" sends a pasted diff (or a PR from the connected repo) to the API, which forwards it to that run's live preview and returns the agent's summary and findings. Thumbs up / down keeps feeding the existing feedback route.

#### User Stories

**Connections reach the run**
1. As a user, I want the connections I tick on the new-project screen to be saved with my project, so that I don't have to pick them again for every run.
2. As a user, I want to see which connections a project uses on the projects list, so that I know what my agent can reach.
3. As a user, I want to change a project's connections later, so that I can add GitHub after I've created the project.
4. As a user, I want my generated agent to read pull requests from the repo I connected, so that it reviews my real code, not only pasted diffs.
5. As a user, I want write actions (posting PR comments) to stay dry-run unless I switched writes on for that connection, so that the agent never posts to my repo by surprise.
6. As a user, I want a clear message if a connection I picked was revoked or fails its test, so that I know why the agent can't reach it.
7. As a user, I want only my own connections to ever be usable in my runs, so that nobody can borrow my GitHub token.
8. As a security-minded user, I want my token never to appear in run events, traces, logs, the generated repo or any API response, so that connecting a repo is safe.
9. As Muaaz (Agent Smith), I want the run's state to list the connector names the user picked, so that the agent gets the right tools.
10. As Muaaz (Runner), I want Level 2 containers to receive the picked connections' env vars from one function, so that I don't have to know where secrets live.
11. As an operator, I want a run with no picked connections to behave exactly as today, so that the hero spec on pasted diffs keeps working.

**Live previews**
12. As a user, I want to open my finished agent at its own web address, so that I can use it and share it.
13. As a user, I want that address to work over HTTPS with no browser warning, so that it feels like a real product.
14. As a user, I want the agent page to show the preview inline and as a link, so that I can try it without leaving Susanoo.
15. As a user, I want a plain-English message when my preview has gone to sleep (idle-stopped), so that a blank frame doesn't look like a bug.
16. As a judge, I want the preview link from the demo or the agent page to open a working app, so that I can see the generated product for myself.
17. As an operator, I want certificates issued only for hosts belonging to real runs, so that nobody can make Caddy request certificates for random names.
18. As an operator, I want previews to keep their CPU, memory and no-mounts limits and to stop after an idle timeout, so that the one EC2 instance stays healthy.
19. As an operator, I want generated apps reachable only through Caddy on the sandbox network, never from the API or the database, so that a hostile generated app can't reach our internals.
20. As Muaaz, I want the container naming and labels he proposed to be the ones Caddy routes by, so that the sandbox code doesn't change.

**Try it**
21. As a user, I want to paste a diff on the agent page and see what my agent finds, so that I can judge it myself.
22. As a user with GitHub connected, I want to pick a PR from my repo and have the agent review it, so that I can try it on real work.
23. As a user, I want findings shown in plain English with their severity and line, so that I don't need to read JSON.
24. As a user, I want a clear message when the preview is asleep or the agent fails, so that I know whether to retry.
25. As a user, I want my thumbs down on a result to become a new test scenario (existing feedback route), so that the next round fixes it.
26. As an operator, I want Try it to need sign-in, count against nothing but the generated app's own key cap, and be limited in size (20,000 characters of diff), so that it can't be abused.
27. As a user, I want only my own runs to be tryable by me, so that other people can't use my agent through Susanoo.

#### Implementation Decisions

**Connections reach the run (#26)**
- **Where a run's connectors live:** on the **project**, as the list of the user's connection ids. A new migration adds the column (default empty), with RLS unchanged (projects are already owner-only). The platform's new-project body and the project-rules update gain an optional connection-ids field; ids that aren't the caller's are refused with a plain-English 422. This is a platform model change, not a frozen-contract change.
- **Run start:** creating a run copies the project's connector **names** (e.g. `github`) into the run's initial state under the `connectors` key, which Agent Smith and the Runner already read. This resolves the "Open" note in `docs/integration-points.md` (row "Connector tools() interface"); update that row. Tell Muaaz, since `SusanooState` doesn't list the key (it's read as optional today).
- **Secret resolution (the one new seam):** a platform function in `packages/core/susanoo/connectors/`, owned by Jugaad, resolves `(user id, connection ids)` into `{env var name: value}` by reading each connection's secret through the existing service-role Vault read and the connector's own `env()` mapping. It skips revoked connections and emits nothing containing a value. Write-enable settings map to the connector's write flag (e.g. `GITHUB_WRITES_ENABLED`).
- **Injection:** the Runner's Level 2 env builder takes its values from that function instead of the worker's own environment (the smallest change in Muaaz's Runner; flag it in the final report). The app's own Anthropic key still comes from the platform key or the user's BYOK, as #16 decided.
- **Redaction:** values are added to the tracing redaction list for the run before the container starts.

**Live previews (#4)**
- **Naming (Muaaz's proposal, accepted):** container `susanoo-run-<run_id>` on the `susanoo_sandbox` network, port 8000, labels as in `docs/integration-points.md`. Caddy derives the upstream from the host name, so there's no per-run config. Decision-rich sketch (to be validated with `caddy validate`):
  ```
  *.susanoo.jugaadchhabra.dev {
      tls { on_demand }
      @run header_regexp host Host ^run-([a-z0-9-]{1,40})\.
      handle @run { reverse_proxy susanoo-run-{re.host.1}:8000 }
      handle { respond "Not found" 404 }
  }
  # global: on_demand_tls { ask http://api:8000/internal/preview-allowed }
  ```
- **On-demand certificates instead of a wildcard:** no Cloudflare DNS plugin or API token needed. Caddy asks the API before each new certificate; the ask endpoint is reachable only on the compose network (Caddy and API are already there; it's not routed publicly) and answers 200 only for a host of the form `run-<id>.<PREVIEW_DOMAIN>` whose run exists and has a preview recorded.
- **Keeping the final app running:** when a run finishes, the worker starts (or keeps) the generated app at its final kept version via the existing sandbox start, and writes the run's preview URL. The existing idle reaper stops it later; the agent page shows "This preview is asleep" when the URL doesn't answer. Restarting a sleeping preview is out of scope.
- **Ownership:** Caddy, compose, the ask endpoint and the preview-URL write are Jugaad's (host). Keeping the container alive after the run touches Muaaz's worker/sandbox: do the smallest change and flag it.

**Try it (#27)**
- **Route:** `POST /runs/{run_id}/try`, auth required, owner-only (404 otherwise, same as the other run routes). Body: `{"kind": "diff", "diff": "…"}` or `{"kind": "pr", "pr": "owner/repo#123"}` (what the frontend already sends). Response: `{summary, findings}` using the frozen `Finding` model.
- **Path to the agent:** the API forwards to the run's **public preview URL** over HTTPS (so the API never joins the sandbox network), calling the generated app's existing review endpoint. Timeout 60 s; a sleeping or failing preview returns 503 with a plain-English message the page already knows how to show.
- **PRs:** a PR reference is resolved by the API using the run's GitHub connection (from #26) to fetch the diff, then sent as a diff. Without a GitHub connection, a PR returns 422 "Connect GitHub to try a PR".
- **Limits:** diff ≤ 20,000 characters (the frontend's existing cap); no quota cost, because the generated app pays from its own capped key.
- **Ownership:** the agent router is Muaaz's. Build the route as a separate small router in the platform list to avoid editing his file, and tell him.

#### Testing Decisions

- **Good tests here** go through the highest seam available and check behaviour a user or operator would notice: what an API call returns, what a container is started with, what Caddy accepts. Never assert on internal helper calls.
- **#26:**
  - API tests (prior art: `tests/api/test_projects.py`, `tests/api/test_connectors.py`): projects save and return connection ids; another user's connection id is refused; a new run's state carries the connector names.
  - Secret-resolution tests against Postgres with Vault (prior art: the Postgres-backed tests in `tests/api/test_ai_mount.py` and `tests/api/test_connectors.py`): returns the connector's env names with values; skips revoked connections; refuses other users' connections.
  - Runner Level 2 (prior art: `tests/heads/test_runner.py`, with the fake sandbox): the container is started with the resolved env, and no value appears in any emitted event or log line.
- **#4:**
  - `caddy validate` on the Caddyfile in CI (Caddy's Docker image).
  - API tests for the ask endpoint: 200 for an existing run with a preview, 404 for unknown run ids, malformed hosts and other domains.
  - A test that the worker records a preview URL when a run finishes (fake sandbox).
  - A manual live check, listed in the final report: open a finished run's preview over HTTPS.
- **#27:** API tests (prior art: `tests/api/test_feedback.py`, `tests/api/test_agent.py`) with the preview faked by an httpx mock transport: diff returns findings; PR without GitHub returns 422; preview down returns 503; other users get 404; diff over 20,000 characters returns 422.

#### Out of Scope

- Restarting a sleeping preview on demand (it's reported as asleep instead).
- Connectors other than GitHub and Anthropic.
- Letting generated agents post real PR comments by default (writes stay dry-run unless the user enabled them).
- Custom domains for previews.
- Changing the frozen contracts in `packages/core/susanoo/schemas/`.

#### Further Notes

- These are post-freeze. Order: **#26 → #4 → #27** (#27 is blocked by #4, and its PR path uses #26).
- If any of them can't land before submission, add it to the README's limitations with one honest sentence, and keep the agent page's existing fallback messages.
- Each ticket's final report must say what in Muaaz's code it touched, so he can review it.

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
