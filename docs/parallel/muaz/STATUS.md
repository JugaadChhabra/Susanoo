# Muaz Lane Status

Updated 2 Nov 2026. Branch `codex/muaz-runtime` (main checkout), based on `codex/aarav-platform` @ `301c39d`.

States: **implemented** (code exists) · **contract-tested** (tests against canonical contracts or
fakes) · **integrated** (real UI/API/DB/runner flow) · **live-verified** (real provider).
Nothing in this lane is *integrated* yet: the platform HTTP boundary waits on C0 (CR-01 to CR-09).

| Item | Spec / IDs | State | Evidence |
|---|---|---|---|
| C0 runtime review | §7-13, §29 | Review written; **not agreed** | `contract-requests/C0-review.md`, `CR-09-adapter-models.md`; `tests/contract/runtime/test_c0_review.py` (6 pass, 13 strict xfail) |
| Evaluator: typed DSL, PASS/FAIL/UNKNOWN, partial-view proof rules | §13; R13-R17, U01 | implemented, unit + property tested | `packages/evaluator`; `tests/runtime/test_evaluator.py` |
| Single case-verdict / run-gate implementation | §13; CR-06 | implemented; platform still uses its own copy | `susanoo_evaluator.verdict` |
| Grader challenge (verified negative controls, held-out) | §13; U06 | implemented (deterministic checks) | `susanoo_evaluator.calibration` |
| Stateful simulator: collections + issue tracker, 8 fault kinds, clock, seed/snapshot/clone/reset, tokens | §14; F01 groundwork, U02 | implemented, tested | `packages/simulator`; `tests/runtime/test_stateful_simulator.py` |
| Durable journal: WAL + fsync, two-phase intents, crash → uncertain, budgets | §11; R07-R12 | implemented; real-kill crash tests | `susanoo_runner.journal`; `tests/runtime/test_journal.py` |
| Attempt executor: deadlines, cancellation, read-back reconciliation, cleanup | §11-12; T05-T10 runtime side | implemented, tested | `susanoo_runner.executor` |
| Python SDK + bounded JSONL bridge (I06/I08 transport) | §10 | implemented; contract violations tested with real subprocesses | `packages/sdk-python`; `tests/runtime/test_jsonl_bridge.py` |
| Synthetic collections agent (9 modes) + 20-case suite | §2, §12, §25 | implemented | `examples/collections` |
| **M2 runtime slice** (correct PASS / false completion FAIL / observer outage UNKNOWN) | §26 M2 | **implemented locally, not integrated** | `tests/runtime/test_runner_local.py` |
| Fidelity contract + preflight, fixed-action comparison, reviewed normalizers, unresolved candidates | §14-15; F01-F03, U02, U05 | implemented, tested | `susanoo_runner.fidelity`, `susanoo_evaluator.normalize`; `tests/runtime/test_fidelity.py` |
| Fresh agent pairs with controlled single-delta intervention; 2x2 cells | §15; F04, U03 | implemented, tested | `susanoo_runner.impact` |
| Budgeted delta-debugging reduction + fixture simplification | §15; F05, U04 | implemented, tested | `susanoo_runner.reduce` |
| Simulator repair gate (reduced repro, valid controls, held-out, unchanged criteria) | §15; F06 | implemented, tested | `susanoo_runner.repair`; `susanoo-runner fidelity-demo` |
| I03 sync HTTP, I04 async submit/poll, I05 SSE + WebSocket | §4, §19 | implemented; contract-tested against local servers (no live vendor) | `packages/connectors`; `tests/runtime/test_connectors.py` |
| I01/I02 pinned git, archive and directory snapshots, runtime suggestions, approved builds | §9 | implemented, tested locally (no GitHub App yet; that metadata is Aarav's) | `susanoo_connectors.source`; `tests/runtime/test_source_intake.py` |
| Agent-layer repair in isolated worktrees | §15, §17; F06, T31, T32 | implemented, tested (real runner + simulator flips C01 FAIL→PASS) | `susanoo_runner.agent_repair`; `tests/runtime/test_agent_repair.py` |
| Approved custom environments; unvalidated OpenAPI scaffolding | §14 | implemented, tested | `susanoo_simulator.custom`, `.openapi_scaffold` |
| I03 GraphQL, I04 signed webhook completion | §19; T22 | contract-tested against local fakes | `susanoo_connectors.graphql`, `.webhook` |
| I09 MCP (protocol 2025-11-25, mcp SDK 2.3) | §19; T23 | contract-tested (stdio subprocess + Streamable HTTP fake) | `susanoo_connectors.mcp` |
| I10 A2A 0.3.0 (JSON-RPC) and unary gRPC | §19; T23 | contract-tested; gRPC wire transport needs `grpcio` (D-4), tested on a fake channel | `susanoo_connectors.a2a`, `.grpc_connector` |
| I07 TypeScript SDK + TS collections agent | §10, §19 | implemented; 13 node tests; runs through the real runner | `packages/sdk-typescript`, `examples/collections/ts-agent` |
| I06/I08 CLI argv and digest-pinned containers | §9, §19 | implemented; tested against the local Docker daemon | `susanoo_connectors.container`; `examples/collections/container` |
| I11 browser | §19; T24 | contract-tested with a fake driver; Playwright not installed (D-4) | `susanoo_connectors.browser` |
| I13 historical imports (CSV/JSONL/trace schema/OTLP) | §18; T21 | implemented, tested; evidence-only by construction | `susanoo_runner.imports` |
| I14 recipes | §4, §19 | LangGraph 1.2.14 and HTTP webhook recipes tested; CrewAI/AutoGen documented as untested | `examples/recipes` |
| CLI preflight/run/wait/reproduce/compare/export, exit codes 0-3, waivers | §16, §18; T15-T17 | implemented, tested via real subprocesses | `susanoo_runner.ci`, `.compare`, `.export` |
| Passive SDK (fail-open, sampling off) + OTLP collector | §18; T34 | implemented, tested | `susanoo_sdk.passive`, `susanoo_runner.collector` |
| Incident-to-draft; dependency graph / evidence validity | §18; T33, T34 | implemented, tested (scheduling/persistence is Aarav's) | `susanoo_runner.incidents`, `.dependencies` |
| Collision runs | §21; T35 | implemented, tested | `susanoo_runner.collision`, `domains/shared_ledger.py` |
| Reference probing with atomic budgets | §21; T36 | implemented, property-tested | `susanoo_runner.probing` |
| Semantic grading (calibrated, permissioned, error = UNKNOWN) | §13; T30, T38 | implemented with a fake provider; no model calls | `susanoo_evaluator.semantic` |
| M10 voice/media: audio import, media WebSocket, Twilio client, WER/CER/latency | §20; I12, T37 | contract-tested against fakes; **not live-verified**; language cases are drafts | `susanoo_connectors.media`, `examples/voice` |
| Runner restart (T39 runtime half) and capacity benchmark (T40) | §23, §25 | implemented, tested | `susanoo_runner.capacity`; `tests/runtime/test_restart_capacity.py` |
| Platform runner client (claims, heartbeat, events, evidence, finalize) | §11, §29 | **Blocked on CR-01-CR-09**; no commits on `codex/aarav-platform` since `301c39d` | — |
| Integrated UI/API/DB acceptance, clean install, backup/restore (M1, M3, M5, M11) | §25-27 | Platform gates (Aarav); runtime commands supplied | — |

## Commands

```bash
uv sync --all-packages
uv run pytest tests/runtime tests/contract -q -rxX
export PYTHONPATH=packages/evaluator:packages/simulator:packages/sdk-python:packages/runner:examples/collections
uv run python -m susanoo_runner.cli local --suite examples/collections/suite.json --agent jsonl:correct
```

## Last results (2 Nov 2026)

- `uv run pytest tests/runtime tests/contract -q`: **385 passed, 13 xfailed** (open C0 requests).
- Full repository suite `uv run pytest tests -q`: **2467 passed, 90 skipped, 13 xfailed**.
- `node --test packages/sdk-typescript/test/`: 13 passed.
- Container tests ran against the local Docker 28 daemon (they skip without Docker).
- Local demo, real JSONL agent process: correct → 20/20 PASS, `READY_FOR_ACCEPTANCE`.
- `susanoo-runner fidelity-demo`: 5 unresolved candidates, both task impacts `discrepancy_induced`,
  8 → 2 actions, repair `accepted`.

Nothing here is *live-verified*: every vendor connector (MCP, A2A, gRPC, Twilio, browser) is tested against
local fakes only, and no real call, payment or message was made.
