# Muaz Lane Status

Updated 1 Nov 2026. Branch `codex/muaz-runtime`, based on `codex/aarav-platform` @ `301c39d`.

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
| Platform runner client (claims, heartbeat, events, evidence, finalize) | §11, §29 | Blocked on CR-01-CR-09 | — |
| Agent-layer repair in isolated worktrees (reuse diagnosis/fixer/verifier) | §15, §17 | Not started | — |
| I07 TypeScript SDK, I08 container runtime, I09 MCP, I10 A2A/gRPC, I11 browser, I13 imports, I14 recipes | §4, §19 | Not started (Docker unavailable on this machine for I08 tests) | — |
| Group D: CLI wait/compare/export, passive OTLP, collision, probing, semantic grading; M10 voice/media | §18, §20-21 | Not started | — |

## Commands

```bash
uv sync --all-packages
uv run pytest tests/runtime tests/contract -q -rxX
export PYTHONPATH=packages/evaluator:packages/simulator:packages/sdk-python:packages/runner:examples/collections
uv run python -m susanoo_runner.cli local --suite examples/collections/suite.json --agent jsonl:correct
```

## Last results (1 Nov 2026)

- `uv run pytest tests/runtime tests/contract -q`: **142 passed, 13 xfailed** (open C0 requests).
- Full repository suite `uv run pytest tests -q`: **2108 passed, 163 skipped, 13 xfailed**.
- Local demo, real JSONL agent process: correct → 20/20 PASS, `READY_FOR_ACCEPTANCE`, ~5.5 s.
  Mode matrix in `examples/collections/README.md`.
- `susanoo-runner fidelity-demo`: 5 unresolved candidates (duplicate write, access control, state), both
  task impacts `discrepancy_induced` after intervention, 8 → 2 actions in 18 runs, repair `accepted`.
