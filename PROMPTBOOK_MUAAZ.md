# PROMPTBOOK — Muaaz (AI system + AI-serving backend)

Copy-paste prompts for Claude Code, phase by phase. Each phase follows `IMPLEMENTATION_MUAAZ.md`, which follows `PRD.md`, the single source of truth. Claude reads `CLAUDE.md` automatically; the prompts point it at the right skill and section.

**How to use**
- Run the prompts in order. Each `▶` block is one message to Claude Code.
- Paste the **session-start prompt** at the beginning of every new session.
- After each phase, run the **phase-close prompt** (verify → evidence → commit → post).
- Your git identity must be `MuaazSM` (`make check-identity`).

---

## Session start (every new session)

▶
```
I'm Muaaz (git user MuaazSM). Read CLAUDE.md, then IMPLEMENTATION_MUAAZ.md, and use the prd-lookup skill for any section you need from PRD.md.
Tell me in 5 lines or fewer: which phase I'm in (look at what exists in the repo and the last commits), what's done, and the next 3 tasks from IMPLEMENTATION_MUAAZ.md with the PRD sections they cite.
Don't write code yet.
```

---

## Phase 0 — Contracts, LLM client, tracing, Entire spike (Sun 18:00–20:00)

▶ **0.1 Schemas (do this with Jugaad on a call)**
```
Use the contract-change and prd-lookup skills. Create packages/core/susanoo/schemas/ with the Pydantic v2 models copied exactly from [PRD › Contracts (locked)] (Layer, Severity, Split, Rule, Plan, AgentBundle, ExpectedIssue, AnswerKey, Scenario, Finding, ScenarioResult, Diagnosis, Patch, IterationMetrics, RunEvent), one file per group as listed in [PRD › Repository structure].
Add state.py with SusanooState from [PRD › LangGraph flow and shared state].
Don't rename or "improve" any field. Add a tests/contracts/test_schemas.py that builds one valid example of each model.
Then show me the file list and stop. Jugaad will wire the TypeScript export.
```

▶ **0.2 LLM client and budget guard**
```
Implement packages/core/susanoo/integrations/llm.py and guards/budget.py per IMPLEMENTATION_MUAAZ.md P0 steps 2–3.
- HEAD_MODELS map: planner and diagnostician → claude-opus-5-5; builder, agent_smith and fixer → claude-sonnet-5-5; simulator and translate → claude-haiku-5-5.
- call(head, messages, **kw) returns the text/JSON plus input_tokens, output_tokens and cost_usd. Put pricing in one constants dict so I can update it.
- Retries with exponential backoff on transient API errors.
- budget.check(state) raises BudgetExceeded if state cost_usd ≥ RUN_COST_CAP_USD from env (default 2).
Unit-test both with a fake client; no real API calls in tests.
```

▶ **0.3 neatlogs tracing**
```
Implement packages/core/susanoo/integrations/tracing.py: neatlogs setup from NEATLOGS_API_KEY and a span(name, **attrs) context manager/decorator.
First check whether neatlogs instruments LangGraph or the Anthropic SDK natively; the PRD lists this as an open question in [PRD › Timeline, owners, risks]. Tell me what you found and which approach you chose.
Redact anything that looks like a credential from span attributes.
Then make one real traced Haiku call through llm.call("simulator", ...) and give me the trace link so I can confirm it in neatlogs.
```

▶ **0.4 Entire spike (time-box: 60 min)**
```
Time-box 60 minutes. Goal: decide the coding worker for Builder/Fixer per [PRD › Tech stack] and [PRD › Timeline, owners, risks › Risks and fallbacks].
1. In a scratch git repo, make one small code change via the Claude Agent SDK and check whether Entire records a checkpoint for it.
2. If not, do the same with Claude Code headless (claude -p) and check again.
Write integrations/coding_worker.py with a single function apply_change(repo_path, instruction, allowed_paths) -> commit_sha, using whichever works.
Report the result in one paragraph I can paste into the PRD.
```

▶ **Phase-close**
```
Phase 0 close-out:
1. Run make test and make lint; fix anything red.
2. Confirm: schemas merged, one traced call visible in neatlogs (give the link), Entire decision made.
3. Use the commit-and-push skill to commit in small logical commits (feat(contracts), feat(llm), feat(tracing), feat(worker)) and push.
4. Draft a "first traced run" #neatHack post from docs/build-in-public.md with the trace link, and add the link to EVIDENCE.md §2.
```

---

## Phase 1 — Template, hand-written reviewer, seed scenarios, scorer (Sun 20:00–23:00)

▶ **1.1 App template**
```
Use prd-lookup for [PRD › Hero use case: the code review agent] and [PRD › Repository structure › Generated product repo].
Build templates/app-template/ by hand:
- backend/main.py (FastAPI) with POST /review (diff in → list[Finding]) and GET /reviews/{id}
- web/index.html: paste a diff, see findings (file, line, rule, severity, message)
- agent/prompt.md, agent/policy.yaml (Rule list: id, text, default_severity) and agent/tools.py (parse_diff, get_file_context, lookup_rule)
- scenarios/train/, scenarios/heldout/, tests/, Dockerfile
The reviewer calls Claude through env ANTHROPIC_APP_API_KEY and must only emit rule_ids that exist in policy.yaml.
Write a hand-written reviewer prompt for the hero rules in specs/code-review.md.
Make it build and run with docker locally; show me a sample review of a diff containing an f-string SQL query.
```

▶ **1.2 Specs**
```
Create specs/code-review.md with the example input spec from [PRD › Hero use case › Example input spec], word for word.
Then propose 2 options for specs/backup.md (a second product with clear, checkable rules, per the open question in the PRD). Wait for my pick before writing it.
```

▶ **1.3 Seed scenarios**
```
Use the scenario-authoring skill. Write 10 seed scenarios as Scenario JSON in templates/app-template/scenarios/seeds/, covering every row of the scenario-design table: raw SQL, PII in logs, hardcoded secret, missing auth, missing error handling, one edge case with must_not_flag, one adversarial prompt-injection comment, and clean diffs.
Verify every line number with a script that parses each diff. Give titles in plain English.
```

▶ **1.4 Scorer**
```
Use the scenario-authoring skill. Implement packages/core/susanoo/scoring/matcher.py and metrics.py exactly per [PRD › Contracts (locked) › Scoring rule (decided)], all 8 rules.
Write tests/scoring/ with at least one test per rule, plus: duplicate findings, an unknown rule_id, nits on a clean diff, a must_not_flag hit, and an unparseable output.
Then run the hand-written reviewer from 1.1 against the 10 seeds and print a per-scenario table plus recall, precision, F1 and severity accuracy.
```

▶ **Phase-close**
```
Phase 1 close-out: run the tests; confirm the template container runs and the scorer reproduces the table. Commit with the commit-and-push skill (feat(template), feat(scoring), test(scoring)) and push.
Then send Jugaad the fixture files: generate tests/fixtures/run_events.json (a realistic full run: intake → build → 2 iterations → done, using the RunEvent shape, including one approval_needed and one error) and tests/fixtures/iteration_metrics.json. Commit them as test(fixtures) so his screens can use them. This is due by Sun 23:00.
```

---

## Phase 2 — Intake, Planner, Builder, Agent Smith, graph skeleton (Sun 23:00–Mon 03:00)

▶ **2.1 Graph skeleton with stubs**
```
Use the new-head skill. Build packages/core/susanoo/graph/build.py wiring the StateGraph exactly as the state diagram in [PRD › LangGraph flow and shared state]: Intake, UserQuestions, ScenarioApproval, Planner, Builder, AgentSmith, Simulator, Runner, Diagnostician, HumanGate, Fixer, Verifier, Done.
Use stub heads that return canned data and emit events. Add graph/checkpointer.py with PostgresSaver on DATABASE_URL; call setup() (tell Jugaad which tables it creates).
Run the stub graph end to end with interrupts auto-answered, and show the event sequence.
```

▶ **2.2 Intake**
```
Use new-head and prd-lookup for [PRD › Intake: questions and approved scenarios]. Implement the intake part of heads/planner.py:
- From the spec, list assumptions; keep only those that change behaviour. Ask at most 5 questions, multiple choice where possible, each with a suggested default and a one-line why.
- interrupt() with a question_needed event; resume with answers (or defaults for unattended runs, logged in plain English).
- Have the Simulator draft 6–10 plain-English scenario cards with expected outcomes; interrupt again for approve, edit or reject.
- Approved cards become split=acceptance, approved_by_user=True.
Prompt in prompts/intake.md. Test it with specs/code-review.md and show me the questions it asks. They must not ask anything the spec already answers.
```

▶ **2.3 Planner**
```
Use new-head. Implement the Planner (Opus): spec + intake answers → Plan (product_name, summary, rules as Rule objects with stable kebab-case ids, intake_answers, files_to_generate limited to backend/ and web/, acceptance_criteria). Prompt in prompts/planner.md. Show me the Plan for the hero spec.
```

▶ **2.4 Builder**
```
Use new-head. Implement heads/builder.py (Sonnet) with integrations/coding_worker.py and integrations/github.py:
copy templates/app-template → create a repo in GITHUB_ORG → coding worker fills only backend/ and web/ from the Plan → commit → record head_commit and repo_path.
The coding worker may only touch allowed paths. Show me the generated repo and confirm it builds with docker.
```

▶ **2.5 Agent Smith**
```
Use new-head. Implement heads/agent_smith.py (Sonnet): generate agent/prompt.md, compile the plain-English rules into agent/policy.yaml, and write agent/tools.py.
Tools for connectors come only from Jugaad's packages/core/susanoo/connectors/<name>.tools(). If those don't exist yet, code against the interface in [PRD › Connectors and API budget › Connector contract] and leave a clear TODO.
Never put credentials in files. Return an AgentBundle.
```

▶ **Phase-close**
```
Phase 2 close-out: end to end from specs/code-review.md → intake (answer via CLI) → approved scenarios → generated repo in the org that boots in docker. Run the tests, commit per head with the commit-and-push skill, and push.
Draft a #neatHack post showing the intake questions Susanoo asked, and log it in EVIDENCE.md §6.
```

---

## Phase 3 — Simulator, Runner, sandbox (Mon 03:00–06:00)

▶ **3.1 Simulator**
```
Use new-head and scenario-authoring. Implement heads/simulator.py (Haiku): generate planted, edge, adversarial and clean scenarios with answer keys, seeded from the 10 seeds; about 30% go to held-out; also generate per-scenario mocked world state (file context) for Level 1 runs, per [PRD › Testing environments].
Validate every scenario with the Pydantic model and a line-number check; drop invalid ones and log them. Held-out goes to scenarios/heldout/.
```

▶ **3.2 Sandbox**
```
Implement packages/core/susanoo/sandbox/:
- mocks.py: tools return the scenario's mocked world state (Level 1, in-process).
- simulated_user.py: an LLM persona for multi-turn and adversarial scenarios.
- docker.py (Level 2): build and run the generated app with CPU/memory caps, no host mounts, an idle timeout, and labels so Caddy can route run-<id>.<PREVIEW_DOMAIN>. Use the label convention Jugaad documented in docs/integration-points.md (IMPLEMENTATION_MUAAZ.md §5).
Inject connector credentials only as container env vars at start. Never exec generated code on the host.
```

▶ **3.3 Runner**
```
Use new-head. Implement heads/runner.py: run training scenarios at Level 1 (and the Verifier's final check at Level 2), score with scoring/, write ScenarioResult with trace_id (the neatlogs trace of that scenario run), and emit a metrics event with IterationMetrics.
Run iteration 0 on the hero spec and show me the metrics table. If nothing fails, add harder edge and adversarial scenarios. Don't sabotage the agent.
```

▶ **Phase-close**
```
Phase 3 close-out: iteration-0 metrics with every result linked to a trace. Put the iteration-0 trace link in EVIDENCE.md §1 as the "before" row (evidence-capture skill). Run the tests, commit, push.
```

---

## Phase 4 — Diagnostician, Fixer, Verifier, worker, AI endpoints (Mon 06:00–11:00)

▶ **4.1 Diagnostician**
```
Use new-head. Implement heads/diagnostician.py (Opus): fetch failing traces from neatlogs; for each failure cluster output a Diagnosis with exactly one layer (code | tool | prompt_policy | test), a plain-English hypothesis, evidence_span_ids and target_files that pass guards/layer_allowlist.py.
Never read scenarios/heldout/. Prompt in prompts/diagnostician.md, with 1 worked example per layer. Show me its diagnoses for iteration 0.
```

▶ **4.2 Guards and Fixer**
```
Implement guards/layer_allowlist.py (code → backend/, web/; tool → agent/tools.py; prompt_policy → agent/prompt.md, agent/policy.yaml; test → scenarios/train/ only; scenarios/heldout/ never) and guards/patch_size.py.
Then heads/fixer.py (Sonnet) via coding_worker: smallest patch to the diagnosed layer only, one commit per patch, Entire checkpoint with failure + diagnosis + rationale. A test-layer diagnosis goes to the HumanGate: interrupt() + approval_needed event; never auto-edit.
```

▶ **4.3 Verifier**
```
Use new-head. Implement heads/verifier.py (no LLM): rerun failing + full train + held-out + acceptance. Keep the patch only if it fixes its targets, causes no regressions, and held-out F1 doesn't drop; otherwise git revert and mark the hypothesis rejected. Stop at 5 iterations, after 2 with no improvement, or on BudgetExceeded (clean stop with a partial report).
Run the full loop on the hero spec and show the per-iteration metrics, plus which patches were kept or rolled back.
```

▶ **4.4 Worker**
```
Implement apps/worker/susanoo_worker/main.py: poll the runs table for queued runs (SELECT … FOR UPDATE SKIP LOCKED), run the graph with the PostgresSaver checkpointer, resume interrupted runs, respect cancel flags, and handle crashes by resuming from the last checkpoint.
Simulate a crash mid-run and show it resuming. Save that trace for EVIDENCE.md §5 (recovery).
```

▶ **4.5 AI-serving routers**
```
Use the api-endpoint skill. Implement my routers in apps/api/susanoo_api/routers/: runs.py, events.py (SSE from RUN_EVENTS, resumable by seq, keep-alive), intake.py, approvals.py, feedback.py, agent.py, exactly the paths in the skill's table.
Use Jugaad's current_user, db_session and enforce_run_quota dependencies. Don't re-implement auth.
Write tests/api tests for 401, other-user 404, happy path and quota 429.
```

▶ **4.6 Translate modules**
```
Implement packages/core/susanoo/translate/ (Haiku) per [PRD › Non-technical users]:
- agent_card.py: what it does, can do and won't do, in plain sentences, no prompts or YAML
- report_card.py: e.g. "Caught 9 of 10 problems, 1 false alarm. Last week: 6 of 10."
- feedback_to_scenario.py: thumbs down + "it should have…" → a Scenario with an answer key that enters the normal loop; never a direct agent edit
Wire them to agent.py and feedback.py.
```

▶ **Phase-close**
```
Phase 4 close-out: from the deployed dashboard, a run streams live, pauses for intake and for a test-layer approval, keeps one patch and rolls back another, and ends with higher held-out F1.
Add the "after" row to EVIDENCE.md §1 and the kept and rolled-back Entire checkpoints to §3 (evidence-capture skill). Commit, push, and draft "the loop works" #neatHack post with the real numbers.
```

---

## Phase 5 — Hero end to end on the live site (Mon 10:00–13:00, with Jugaad)

▶
```
We're in Phase 5 (IMPLEMENTATION_MUAAZ.md P5). On the LIVE deployment:
1. Run specs/code-review.md end to end; then specs/backup.md once.
2. Fill EVIDENCE.md §1 (before/after, same run), §2 (trace links + Diagnostician screenshot), §3 (checkpoints) and §5 (recovery) with real links only (evidence-capture skill).
3. Save the best completed run with scripts/save_demo_run.py for demo mode and the marketing proof section; tell Jugaad the run_id.
If the hero isn't end to end by 10:00, apply the fallback from [PRD › Timeline, owners, risks]: cut generation to agent + tools only, keep the loop. Tell me before you cut anything.
```

▶ **If you're behind**
```
I'm behind schedule. Compare the repo against IMPLEMENTATION_MUAAZ.md and list what's done, what's missing, and the smallest set of tasks that still delivers the PRD's success metrics ([PRD › Goals, non-goals, success metrics]) by 13:00. Propose cuts that the PRD itself allows (fallbacks in [PRD › Timeline, owners, risks]); don't invent new scope.
```

---

## Phase 6 — After freeze (Mon 13:00–18:00, with Jugaad)

▶ **cfo.ai cost inputs**
```
Feature freeze is on; no new features. From the runs and iterations tables and neatlogs, compute per full hero run: tokens and cost per head, total cost, sandbox minutes, iterations. Give me a small table for cfo.ai inputs ([PRD › Business model (inputs for cfo.ai) › Cost drivers]) and save it as docs/business-plan/cost-inputs.md.
```

▶ **Video segment**
```
Using docs/demo-script.md, write my narration for 0:40–2:25 (the loop, neatlogs, Entire, before/after), under 230 words, plain spoken English. It must explicitly cover architecture, implementation, runtime, reasoning and result for neatlogs and Entire.
```

▶ **Final check**
```
Final check before submission: run make test and make lint; confirm EVIDENCE.md has no TODOs left in my sections; confirm every commit on main is by MuaazSM or JugaadChhabra with no AI attribution (git log --format='%an %ae %s' plus a check of message bodies). Report anything off. Don't fix history yourself.
```
