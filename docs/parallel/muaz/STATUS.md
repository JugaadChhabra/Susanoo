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
| Platform runner client (claims, heartbeat, events, evidence, finalize) | §11, §29 | Blocked on CR-01-CR-09 | — |
| Fidelity / differential replay, task impact, reduction, repair gate | §14-15; F01-F06 | Not started | — |
| Remaining intake families I01-I05, I07, I09-I14 | §4, §19 | Not started | — |
| Groups C-D, M8-M11 runtime work | §18-21 | Not started | — |

## Commands

```bash
uv sync --all-packages
uv run pytest tests/runtime tests/contract -q -rxX
export PYTHONPATH=packages/evaluator:packages/simulator:packages/sdk-python:packages/runner:examples/collections
uv run python -m susanoo_runner.cli local --suite examples/collections/suite.json --agent jsonl:correct
```

## Last results (1 Nov 2026)

- `uv run pytest tests/runtime tests/contract -q`: **99 passed, 13 xfailed** (open C0 requests).
- Full repository suite `uv run pytest tests -q`: **2108 passed, 163 skipped, 13 xfailed**.
- Local demo, real JSONL agent process: correct → 20/20 PASS, `READY_FOR_ACCEPTANCE`, ~5.5 s.
  Mode matrix in `examples/collections/README.md`.
