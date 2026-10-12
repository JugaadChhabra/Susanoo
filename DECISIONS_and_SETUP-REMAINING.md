# Decisions and setup remaining

State at Mon 12 Oct, 05:00 IST. All of Muaaz's promptbook code is on `main`, and every task passed an Opus review. The final audit: 1,911 tests pass, lint is clean, and no commit carries AI attribution or a secret. What's left needs a person or Jugaad.

Deadlines: fallback check **10:00** (hero end to end or cut to agent + tools), feature freeze **13:00**, submit **19:00–23:55**.

## 1. Phase status

| Phase | Code | Close | Missing |
|---|---|---|---|
| 0 Contracts, LLM, tracing, Entire | Done | Partial | neatlogs trace URL; post (draft in `docs/posts/drafts.md`); Entire paragraph in the PRD |
| 1 Template, seeds, scorer | Done | Done | — |
| 2 Intake, Planner, Builder, Agent Smith, graph | Done | Partial | Repo in the GitHub org (token gets a 403, so repos are local); post |
| 3 Simulator, sandbox, Runner | Done | Partial | Trace links for the iteration-0 row |
| 4 Diagnostician, Fixer, Verifier, worker, API, translate | Done | **Not done** | Deployed dashboard (Jugaad); a rollback; a held-out F1 gain |
| 5 Hero end to end, live | Tools done | **Not done** | Live site (Jugaad); demo run saved to Supabase |
| 6 Evidence, costs, video | Done | Partial | `EVIDENCE.md` links; posts published; neatlogs token counts for costs |

Real runs so far (all local, real models, unattended): see `EVIDENCE.md` and `docs/evidence/`.
- hero-2: one fix kept, training F1 0.833 → 1.000, acceptance 0.90 → 1.00, held-out F1 0.909 flat.
- hero-3: held-out F1 0.857 flat.
- backup-2: held-out F1 1.000.
- The generated app boots in the capped sandbox.

## 2. Setup remaining: Muaaz

Do these first; they unblock the Phase 4 headline.

1. **GitHub token:** add **Administration: Read and write** for the `susanoo-apps` org (repo creation currently fails with 403).
2. **`ANTHROPIC_APP_API_KEY`** in `.env`, from a separate workspace with a lower limit. Without it, code and tool fixes can't be verified at Level 2 and always roll back.
3. **Rerun the hero spec** and either answer intake question 4 with **"HTTP only"** or **approve** the test change at the gate. In both hero runs, the default answer made the first diagnosis a real answer-key conflict, which the unattended script rejected. Example command:
   `uv run python scripts/run_local.py specs/code-review.md --run-id hero-4 --out runs/hero-4.json`
   (that script never approves test changes; for a run you drive yourself, use the worker and API once they're live, or ask me to add an answering mode).
4. **neatlogs:** open a trace, paste its dashboard URL (we'll derive `NEATLOGS_TRACE_URL_TEMPLATE` from it), and add the dashboard links and screenshots to `EVIDENCE.md` §1, §2 and §5. Optional: `NEATLOGS_TOKEN` (`observability:read`) so the Diagnostician can read traces from neatlogs, not just the in-process recorder.
5. **Save the demo run:** `DATABASE_URL=<supabase> uv run python scripts/save_demo_run.py docs/evidence/hero-2-run.json --user-id <demo account> --dry-run`, then without `--dry-run`. Give Jugaad the `DEMO_RUN_IDS=` line it prints.
6. **Publish the posts** in `docs/posts/drafts.md` and log each link in `EVIDENCE.md` §6.
7. **Commit your `.env.example` edit:** `GITHUB_ORG=` already exists in the file, so drop the duplicate line.
8. **Root commit `46709f3`** (created in GitHub's web UI) uses an identity outside the allowed list. It can't be fixed without rewriting history (forbidden). Decide whether to mention it or leave it.

## 3. Setup remaining: Jugaad

Full details in `docs/integration-points.md`.

- [ ] Apply `docs/proposals/006_run_queue.sql` as `supabase/migrations/006_run_queue.sql`. Until then the deployed worker logs errors. The API inserts runs as `queued`; answers carry the pause token.
- [ ] Mount the AI routers in `apps/api/susanoo_api/routers/__init__.py` (`AI_ROUTERS` is `[]`). Update `tests/api/test_quota.py`, add `queued` and `cancelled` to `RunSummary.status`, count `queued` runs in `usage_since`, and export the routers' response models to `generated.ts`.
- [ ] Caddy: `reverse_proxy api:8000 { flush_interval -1 }` for SSE, plus the `run-*` preview routing on the `susanoo_sandbox` network (proposed labels in integration-points). Restrict egress and block 169.254.169.254.
- [ ] Worker in compose:
  - Docker socket.
  - `stop_grace_period: 60s`.
  - `entire` binary in the image.
  - A direct or session `DATABASE_URL`, not port 6543.
  - Keys on EC2.
- [ ] Frontend: stream events with `fetch` (Bearer header), and send `pause_token` back with answers and approvals (expect 409 when stale).
- [ ] Connectors: a credential-free `tool_sources()` so generated agents get GitHub tools.
- [ ] OK running `PostgresSaver.setup()` on Supabase (tables listed in integration-points).
- [ ] Fix `apps/web/lib/fixtures/run-events.json`: `stop_reason` is `all_passed`.

## 4. Decisions only you can make (current default in brackets)

- **PR links in the hero spec** [cut: answer "Pasted diffs only"; the template reviews diffs].
- **Rule id `external-call-errors`, severity `warn`** [keep].
- **Scorer conventions** [headline recall, precision and F1 on the train split; zero-division values per the `scoring/metrics.py` docstring].
- **Patch-size cap** [60 changed lines; `PATCH_MAX_LINES`].
- **Feedback scenarios** [train split, not acceptance].
- **Unattended mode** [keep: defaults, cards marked not human-approved, test gate still needs a person].
- **Haiku 5.5 price** [placeholder $1 / $5 per M tokens in `llm.py`].
- **Video narration length** [draft is 248 words; trimmed version 229 to meet the promptbook's limit].
- **With Jugaad** (`contract-change` skill):
  - a `disputed` field on Scenario;
  - where a run's connector list lives;
  - whether to freeze the intake-question and pause-payload shapes.

## 5. Decisions already taken in code (write these into the PRD)

Full text in `docs/prd-edits-for-muaaz.md`:
- Entire works with the Agent SDK.
- The backup spec.
- Stop-only edges to Done (`budget`, `no_diagnosis`).
- "No improvement" means no kept patch two rounds running.
- Iteration counting.
- Prompt/policy and test fixes are verified at Level 1; code and tool fixes at Level 2.
- Acceptance scenarios are hidden from the Diagnostician and the Fixer.
- Per-run cost cap.
- Generated repos are public.

## 6. Known limitations (not blocking)

- The Verifier rerun and the next Runner pass score the same agent version (about $0.23 per round duplicated).
- A cancel during verification can leave a patch "pending" until the run resumes from its checkpoint.
- Translate costs from the API can be overwritten by the worker's cost write while a run is active.
- This repo has no Entire checkpoints of its own (the "entire-graph of Susanoo" evidence row).
