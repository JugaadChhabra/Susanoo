# IMPLEMENTATION — Muaaz (AI system + AI-serving backend)

> **Single source of truth:** the Susanoo PRD ([live doc](https://claude.ai/code/artifact/a3bb4cb5-bbec-4d63-a641-a65d4a0d5124), exported to the repo root as `PRD.md` for agents and `PRD.pdf` for people).
> This file only turns the PRD into an ordered task list for Muaaz. It adds no new requirements.
> If this file and the PRD disagree, **the PRD wins**. To change scope, update the PRD first, then this file.
> References below look like **[PRD › Section name]** and match the PRD's headings exactly.

---

## 1. Your scope

From **[PRD › Team and ownership]**, you own:

| Area | What that means in code |
|---|---|
| AI system | All eight heads (`packages/core/susanoo/heads/`), intake, LangGraph graph + checkpointer (`graph/`), prompts (`prompts/`), scoring (`scoring/`), sandbox runner, mocks and simulated user (`sandbox/`), coding worker (`integrations/coding_worker.py`), LLM client (`integrations/llm.py`), neatlogs + Entire instrumentation of the heads (`integrations/tracing.py`, `integrations/github.py`), guards (`guards/`), translate modules (`translate/`) |
| AI-serving backend | Worker process (`apps/worker/`), run orchestration routers: `runs.py`, `events.py`, `intake.py`, `approvals.py`, `feedback.py`, `agent.py` in `apps/api/susanoo_api/routers/` |
| Generated product | `templates/app-template/`, `specs/code-review.md`, `specs/backup.md` |
| Tests | Unit tests for heads and scoring (`tests/heads/`, `tests/scoring/`) |
| Shared with Jugaad | `schemas/` (frozen, see **[PRD › Contracts (locked)]**), cfo.ai plan, demo video, #neatHack posts, `EVIDENCE.md` |

**Not yours** (Jugaad owns these; don't build them, just consume them): the FastAPI app shell, auth, quota, projects, connectors and connections, the demo replay endpoint, Supabase migrations / RLS / Vault, the frontend, CI, deployment and infra.

---

## 2. Ground rules (from the PRD)

- **Contracts are frozen.** Every head reads and writes only the shapes in **[PRD › Contracts (locked)]**. Changing one needs Jugaad's agreement.
- **Head interface:** `async def run(state: SusanooState) -> dict`. Return only the keys you changed. Emit `started` and `finished` (or `error`). Check the budget guard before every model call. Wrap the work in a neatlogs span named after the head. **[PRD › Contracts (locked) › Head interface]**
- **Model per head** follows the table in **[PRD › The eight heads of Orochi]**: Opus 5.5 for Planner and Diagnostician, Sonnet 5.5 for Builder, Agent Smith and Fixer, Haiku 5.5 for Simulator. The model strings are `claude-opus-5-5`, `claude-sonnet-5-5` and `claude-haiku-5-5`.
- **Fixed output shape.** Every generated product is the template in **[PRD › Repository structure › Generated product repo]**. Never invent an architecture per spec.
- **Guardrails are not optional:** held-out set, the human gate for `test`-layer changes, the layer allow-list, the patch size cap, budgets, and git rollback. **[PRD › Improvement loop and guardrails]**
- **Entire is on for this repo from the first commit**, and **neatlogs is on from the first run**, so early runs become the "before" baseline. **[PRD › Before development: first-hour checklist › Setup]**
- **Trunk-based.** Small commits to `main`, which auto-deploys. **[PRD › Before development › Working agreements]**

---

## 3. Timeline at a glance

Times are IST and come from the Gantt chart in **[PRD › Timeline, owners, risks]**.

| When | Phase | Exit criterion |
|---|---|---|
| Sun 18:00 – 20:00 | P0: Contracts, LLM client, tracing, Entire spike | Schemas merged; one traced Haiku call visible in neatlogs; Entire decision made |
| Sun 20:00 – 23:00 | P1: Template, hand-written reviewer, seed scenarios, scorer | Template runs by hand; 10 seed scenarios score correctly |
| Sun 23:00 – Mon 03:00 | P2: Intake, Planner, Builder, Agent Smith, graph skeleton | Spec goes in, a generated repo with an agent comes out |
| Mon 03:00 – 06:00 | P3: Simulator, Runner, scoring in the graph | Iteration 0 metrics for the hero spec |
| Mon 06:00 – 11:00 | P4: Diagnostician, Fixer, Verifier, worker, AI endpoints | Full loop runs with keep or rollback, streamed over SSE |
| Mon 10:00 – 13:00 | P5 (together): Hero spec end to end on the live site | Before/after traces saved; demo run saved |
| Mon 13:00 | **Feature freeze** | — |
| Mon 13:00 – 18:00 | P6 (together): Evidence, cfo.ai inputs, video | `EVIDENCE.md` complete |
| Mon 19:00 | Submit early in the 19:00–23:55 window | — |

Stagger sleep with Jugaad so one of you is always pushing.

---

## 4. Phase-by-phase tasks

### P0 — Contracts and foundations (Sun 18:00–20:00)

1. **Lock the schemas with Jugaad** (30 min, together). Copy the Pydantic models in **[PRD › Contracts (locked)]** verbatim into `packages/core/susanoo/schemas/`, one file per group as listed in **[PRD › Repository structure]**. Add `state.py` with `SusanooState`, copied from **[PRD › LangGraph flow and shared state]**. Jugaad wires `scripts/export_types.py` to export them to TypeScript.
2. **`integrations/llm.py`:**
   - A thin Anthropic client: `call(head, messages, **kw)` picks the model from a `HEAD_MODELS` map.
   - Records input/output tokens and computes `cost_usd` per call.
   - Adds the cost to `state.cost_usd`.
   - Retries with backoff on API errors (**[PRD › LangGraph flow and shared state › Guards on every node]**).
3. **`guards/budget.py`:** `check(state)` raises `BudgetExceeded` before a call that would cross the per-run cap. The initial cap is $2, from **[PRD › Connectors and API budget › Anthropic budget]**. The graph catches it, sets `status`, and ends cleanly with a partial report.
4. **`integrations/tracing.py`:** neatlogs setup plus a `span(head_name)` helper or decorator. First check whether neatlogs instruments LangGraph natively; this is an open question in **[PRD › Timeline, owners, risks › Open questions]**. Record the answer in the PRD.
5. **Entire spike (≤ 60 min).** Decide whether Entire checkpoints capture changes made through the Claude Agent SDK, or only through coding-agent CLIs. If only CLIs, switch `coding_worker.py` to Claude Code headless (`claude -p`), per **[PRD › Tech stack]** and **[PRD › Timeline, owners, risks › Risks and fallbacks]**. Update the PRD with the decision.

**Done when:** schemas are merged; one traced Haiku call shows in neatlogs with its cost; the Entire decision is written in the PRD.

### P1 — The template and ground truth, by hand (Sun 20:00–23:00)

The PRD is explicit: if the hand-made template doesn't run, nothing Susanoo generates will. **[PRD › Before development › Build first, by hand]**

1. **`templates/app-template/`**, matching **[PRD › Repository structure › Generated product repo]**:
   - `backend/main.py` with FastAPI and `POST /review`, `GET /reviews/{id}`, per **[PRD › Hero use case › What Susanoo generates]**.
   - `web/index.html`: paste a diff, see findings (file, line, rule, severity, message).
   - `agent/prompt.md`, `agent/policy.yaml` (rules with `id`, `text`, `default_severity`), `agent/tools.py` (parse diff, get file context, look up rule).
   - `scenarios/train/`, `scenarios/heldout/`, `tests/`, `Dockerfile`.
   - The reviewer must output `Finding` objects whose `rule_id` comes from the policy. **[PRD › Contracts (locked) › Scoring rule (1)]**
2. **`specs/code-review.md`:** the example input spec from **[PRD › Hero use case › Example input spec]**. Draft `specs/backup.md` too (open question in the PRD; pick something with clear rules).
3. **Ten seed scenarios** with answer keys, covering every row of the scenario table in **[PRD › Hero use case › Scenario design]**: planted (raw SQL, PII logs, hardcoded secret, missing auth), edge, adversarial and clean. Store them as `Scenario` JSON.
4. **`scoring/matcher.py` and `scoring/metrics.py`:** implement **[PRD › Contracts (locked) › Scoring rule (decided)]** exactly:
   - same file, same `rule_id`, line inside `line_start..line_end` widened by ±3;
   - one-to-one matching, closest finding wins, duplicates ignored;
   - false positives are unmatched `block`/`warn` findings, plus any finding whose rule is in `must_not_flag`; unmatched `nit`s are ignored;
   - severity accuracy only on matched pairs;
   - a scenario passes only when every expected issue is matched with the right severity and there are no false positives;
   - recall, precision, F1, held-out F1 and acceptance pass rate;
   - a runtime error or unparseable output fails the scenario.
5. **`tests/scoring/`:** unit tests for every numbered rule above. This is the contract the judges' before/after numbers rest on, so it must be airtight.

**Done when:** the template container runs locally; the hand-written reviewer scores against the 10 seeds; scoring tests pass.

### P2 — Intake and building the product (Sun 23:00–Mon 03:00)

1. **`graph/build.py` and `graph/checkpointer.py`:**
   - StateGraph wired exactly as in the state diagram in **[PRD › LangGraph flow and shared state]**: Intake → UserQuestions / ScenarioApproval → Planner → Builder → AgentSmith → Simulator → Runner → (Done | Diagnostician) → (Fixer | HumanGate) → Verifier → loop.
   - Use `PostgresSaver` on Supabase. Coordinate with Jugaad, who owns the DB, on calling `setup()` for its tables.
   - Write stub heads first, so the graph runs end to end with fakes.
2. **Intake** (`heads/planner.py`, intake part). Follow **[PRD › Intake: questions and approved scenarios]**:
   - Ask at most 5 questions, multiple choice where possible, each with a suggested default and one line on why it matters. Only ask questions that change what gets built.
   - Pause with LangGraph `interrupt()` and emit `question_needed`.
   - Hand the answers to the Simulator to draft 6–10 plain-English scenario cards. Pause again for approve, edit or reject.
   - Approved cards become the `acceptance` split, with `approved_by_user=True`, locked from the Fixer.
   - Unattended runs take the defaults and say so in the event log.
3. **Planner:** spec + intake answers → `Plan` (rules, files to generate, acceptance criteria). Model: Opus.
4. **Builder** (`heads/builder.py` + `integrations/coding_worker.py` + `integrations/github.py`):
   - Copy the template.
   - Have the coding worker fill only `backend/` and `web/` from the plan.
   - Create the repo in the GitHub org, commit, and record `head_commit`.
   - Model: Sonnet.
5. **Agent Smith:** generate `agent/prompt.md`, `agent/policy.yaml` (compiled from the plain-English rules) and `agent/tools.py`. Pull tool functions for the selected connectors from Jugaad's `connectors/*.tools()`, per **[PRD › Connectors and API budget › Connector contract]**. Model: Sonnet.

**Done when:** `specs/code-review.md` in → intake questions out → scenarios approved (by CLI is fine for now) → a generated repo in the org that boots in Docker.

### P3 — Simulation and running (Mon 03:00–06:00)

1. **Simulator** (Haiku):
   - Generate `planted`, `edge`, `adversarial` and `clean` scenarios with answer keys, seeded by your 10 hand-written ones.
   - Split about 30% into held-out, as in **[PRD › Improvement loop and guardrails › Guardrails]**.
   - Generate mocked world state per scenario (file context etc.), per **[PRD › Testing environments › Mocked tools]**.
   - Write held-out scenarios to `scenarios/heldout/`, which is never writable by the Fixer.
2. **`sandbox/`**, following **[PRD › Testing environments]**:
   - `mocks.py`: mocked tools return the scenario's world state (Level 1, in-process).
   - `simulated_user.py`: an LLM persona for multi-turn and adversarial scenarios.
   - `docker.py`: Level 2. Build and run the generated app container with CPU/memory caps, no host mounts, an idle timeout and a preview URL `run-<id>.<domain>`, per **[PRD › Deployment › Sandbox limits]**. Jugaad provides Docker on EC2 and Caddy wildcard routing; you call the Docker API.
3. **Runner:** run scenarios at Level 1 for training iterations and Level 2 for the Verifier's final check. Score them with `scoring/`, write `ScenarioResult` rows with `trace_id`, emit `metrics` events.

**Done when:** iteration 0 produces `IterationMetrics` for the hero spec, with every result linked to a neatlogs trace. Make sure some honest failures exist; if not, add harder adversarial and edge scenarios. **[PRD › Timeline, owners, risks › Risks and fallbacks]**

### P4 — The improvement loop and serving it (Mon 06:00–11:00)

1. **Diagnostician** (Opus):
   - Fetch the failing traces from neatlogs.
   - Attribute each failure to exactly one layer: `code | tool | prompt_policy | test`.
   - Output a `Diagnosis` with a plain-English `hypothesis`, `evidence_span_ids` and `target_files`.
   - **[PRD › The eight heads of Orochi › Root-cause layers]**
2. **Fixer** (Sonnet):
   - Make the smallest patch, only inside the layer's allow-listed folders (`guards/layer_allowlist.py`, mapping in **[PRD › Repository structure]**).
   - Enforce the diff-size cap (`guards/patch_size.py`).
   - Commit with an Entire checkpoint carrying the failure, diagnosis and rationale.
   - A `test`-layer diagnosis goes to the HumanGate (`interrupt()`, `approval_needed` event) instead of patching.
3. **Verifier** (code, no LLM):
   - Rerun the failing scenarios, the full training set, the held-out set and the acceptance set.
   - Keep the patch only if it fixes its targets, causes no regressions, and doesn't lower the held-out score. Otherwise `git revert` and mark the hypothesis rejected.
   - Stop after 5 iterations, after 2 iterations with no improvement, or at the cost cap.
   - **[PRD › Improvement loop and guardrails]**
4. **Worker** (`apps/worker/susanoo_worker/main.py`): poll the `runs` table for queued runs (no Redis), run the graph, resume from the checkpointer after a crash. **[PRD › Tech stack › Job queue]**
5. **Event emission:** every head writes `RunEvent` rows to `RUN_EVENTS` through Jugaad's `db.py` helper, with `seq` increasing per run and `message` in plain English.
6. **AI-serving routers.** Endpoints come from **[PRD › Frontend (marketing site + app) and API]**, **[PRD › Intake]** and **[PRD › Non-technical users › Extra API]**. Use Jugaad's `auth.py`, `quota.py` and `deps.py` dependencies; don't re-implement auth.

   | Router | Endpoints |
   |---|---|
   | `runs.py` | `POST /projects/{id}/runs` (quota dependency, enqueue), `GET /runs/{id}`, `POST /runs/{id}/cancel` |
   | `events.py` | `GET /runs/{id}/events` (SSE from `RUN_EVENTS`, resumable by `seq`) |
   | `intake.py` | `GET /runs/{id}/intake`, `POST /runs/{id}/intake/answers`, `POST /runs/{id}/intake/scenarios` |
   | `approvals.py` | `POST /runs/{id}/approvals/{patch_id}` |
   | `feedback.py` | `POST /runs/{id}/feedback` (thumbs down + expectation → new scenario) |
   | `agent.py` | `GET /runs/{id}/agent-card`, `GET /runs/{id}/report-card` |

7. **`translate/`** (Haiku), per **[PRD › Non-technical users]**:
   - `agent_card.py`: what the agent does, can do and won't do, in plain sentences.
   - `report_card.py`: e.g. "Caught 9 of 10 problems, 1 false alarm. Last week: 6 of 10."
   - `feedback_to_scenario.py`: turns a 👎 + "it should have…" into a `Scenario` with an answer key, which then flows through the normal loop. Users never edit the agent directly.

**Done when:** a run started from the dashboard streams events live, pauses for intake and for a test-layer approval, keeps one patch and rolls back another, and ends with better held-out F1.

### P5 — Hero end to end, together (Mon 10:00–13:00)

1. Run `specs/code-review.md` end to end on the **live** deployment, then the backup spec once to show generality. **[PRD › Goals, non-goals, success metrics]**
2. **Capture evidence as you go.** **[PRD › Sponsor integrations and evidence plan › Evidence checklist]** Your rows:
   - **The agent improved:** neatlogs links for iteration 0 vs the final iteration, plus the metrics table.
   - **Used neatlogs:** trace links, plus screenshots of the Diagnostician reading spans.
   - **Recovers from failures:** a trace of a failed action (a container boot failure or an API error) and the retry or rollback that followed.
   - **Used Entire:** checkpoints on the generated repo (shared with Jugaad).
3. Save the best completed run with `scripts/save_demo_run.py`, so Jugaad's demo mode and marketing proof section can replay it.
4. **Fallback:** if the hero spec isn't running end to end by Mon 10:00, cut generation down to agent + tools only and keep the loop. **[PRD › Timeline, owners, risks › Risks and fallbacks]**

### P6 — After freeze (Mon 13:00–18:00, together)

- **cfo.ai cost inputs:** tokens and cost per head per run, from neatlogs, plus sandbox minutes. **[PRD › Business model (inputs for cfo.ai) › Cost drivers]**
- **Video:** you narrate the loop segment (intake → fail → diagnose → fix → verify). It must explicitly cover the architecture, implementation, runtime, reasoning and result for neatlogs, Entire and cfo.ai.
- Help finish `EVIDENCE.md`.

---

## 5. Interfaces with Jugaad (agree these early)

| You need from Jugaad | Jugaad needs from you |
|---|---|
| `db.py` session helper and the `RUN_EVENTS` / `RESULTS` / … tables (**[PRD › Data model]**) | Events written in the `RunEvent` shape, `seq` increasing per run |
| `auth.py`, `quota.py`, `deps.py` FastAPI dependencies | AI routers mounted under the paths in the API tables |
| `connectors/*.tools()` and credential injection from Vault | Agent Smith using only `tools()`; never logs or writes tokens |
| Docker on EC2, Caddy wildcard `run-*` routing | `docker.py` labels containers so Caddy can route `run-<id>` |
| `export_types.py` and a CI job validating sample payloads | Sample `RunEvent` / `IterationMetrics` fixtures for the mock event stream (by Sun 23:00) |
| Demo replay endpoint (`GET /demo/{run_id}`) | A saved completed run via `save_demo_run.py` |

**Jugaad's "relief backlog":** Jugaad may take over API tests for your routers, `save_demo_run.py`, or the Docker host setup if you fall behind. Say so in chat early rather than late.

---

## 6. Checklists

**Before you write head code**
- [ ] Schemas merged and exported to TypeScript
- [ ] Entire is recording checkpoints on this repo
- [ ] neatlogs shows your first traced call
- [ ] Anthropic Console spend limit set; a separate key for generated apps

**Definition of done for every head**
- [ ] Returns only the keys it changes
- [ ] Emits `started` and `finished` (or `error`) with a plain-English `message`
- [ ] Budget guard checked before each model call
- [ ] Wrapped in a neatlogs span named after the head
- [ ] Has at least one unit test with a fake LLM

**Before feature freeze**
- [ ] Held-out F1 is higher after the loop than before, on the live site
- [ ] At least one patch kept and one rolled back, both with Entire checkpoints
- [ ] One recovery trace saved
- [ ] Demo run saved for replay
