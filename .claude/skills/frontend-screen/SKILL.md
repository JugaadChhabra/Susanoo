---
name: frontend-screen
description: Build a Susanoo page or component in apps/web — marketing site sections or authenticated app screens (projects, connectors, new project, intake, live run view, agent page, demo replay) — using generated types, mock event fixtures and plain-English UI. Use for any work in apps/web/.
---

# Frontend screen

Owner: Jugaad. Sources: **[PRD › Frontend (marketing site + app) and API]**, **[PRD › Non-technical users]**, **[PRD › Intake: questions and approved scenarios]**, **[PRD › LangGraph flow and shared state]**.

## Where things go
- `app/(marketing)/`: public pages (landing, pricing, demo). No auth.
- `app/(auth)/login`: Supabase Auth (magic link + GitHub OAuth).
- `app/(app)/`: authenticated screens. Middleware redirects logged-out users to `/login`.
- `components/marketing/*`, `components/app/*`
- `lib/api.ts` (typed fetch), `lib/sse.ts` (events), `lib/supabase.ts`
- `types/generated.ts`: **the only source of API types.** Never hand-write a type that mirrors a Pydantic model; run `make types` instead.

## The screen list (don't add screens the PRD doesn't list)
| Route | Must show |
|---|---|
| `(app)/projects` | projects and runs: status, held-out F1, cost, quota left today |
| `(app)/connectors` | connector cards, setup guide beside the credential field, Test connection, revoke |
| `(app)/projects/new` | spec, rules as sentences, connector picker, example specs (code reviewer pre-filled), Start |
| `(app)/runs/[id]/intake` | ≤ 5 questions with suggested defaults, then approve, edit or reject scenario cards; Build unlocks after approval |
| `(app)/runs/[id]` | HeadsGraph, EventLog, Scoreboard (line chart per iteration, cost vs cap), FailureLedger, ApprovalCard |
| `(app)/runs/[id]/agent` | AgentCard, ReportCard, live preview iframe of `run-<id>`, repo link, "Try it" + 👍/👎 feedback |
| `(marketing)/demo` | the run view fed by `GET /demo/{run_id}` |

## Building against fixtures first
- Build every screen against **fixture events** (`RunEvent[]`, `IterationMetrics[]`) so it doesn't wait on the AI half. Swap in the real API later behind the same `lib/api.ts` and `lib/sse.ts` calls.
- Make the run view **driven by an event array**. Live mode appends from SSE; demo mode replays stored events on a timer. Same components.

## HeadsGraph
- Eight nodes in this order: Planner → Builder → Agent Smith → Simulator → Runner → Diagnostician → Fixer → Verifier, plus a loop edge Verifier → Runner.
- Map `event.head` to the node: `started` = active (glow), `finished` = done, `error` = red, `approval_needed` = amber with the ApprovalCard open, `question_needed` = route to intake.

## Plain-English UI ([PRD › Non-technical users])
- Show `event.message` and translated text by default. Raw JSON, span IDs and diffs go behind a "Details" toggle.
- Layer badges get friendly names: code → "App code", tool → "Agent's tools", prompt_policy → "Agent's rules", test → "The test itself".

## Every screen must have
- [ ] loading, empty and error states
- [ ] phone-width layout (16px side gutter, no horizontal scroll)
- [ ] keyboard access and visible focus
- [ ] no secrets rendered; connection screens show status only
- [ ] types only from `generated.ts`

## Marketing site
Section order and copy points come from the PRD's "Surface 1: Marketing site". The look should be enterprise-grade in the spirit of sierra.ai, but **original design and copy**: don't copy their layout, assets or text. Load the `frontend-design` skill if it's available for aesthetic direction. Leave prices blank or "Contact us" until the cfo.ai model sets them.

## Finish
`make lint` (tsc + eslint), check it at phone width, then commit with the `commit-and-push` skill: `feat(web): …` or `feat(marketing): …`.
