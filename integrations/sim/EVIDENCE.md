# Local Evidence Ledger

Observed on 14 November 2026, using the local synthetic integration. No provider
or customer qualification is implied. Generated IDs, reports, screenshots and raw
journals stay in private state; they are deliberately not committed to this PR.

| Check | Observed result |
| --- | --- |
| Original twenty-case suite through JSONL, actual local Sim and actual simulator | 20 PASS, 0 FAIL, 0 UNKNOWN, 0 execution errors, 0 exclusions |
| Actual platform twenty-case run | 20 scheduled/attempted/completed/PASS; READY_FOR_ACCEPTANCE |
| Critical evaluator definitions | 76 calibrated using real local passing snapshots and explicitly seeded negative snapshots; typed calibration reports uploaded before synthetic approval |
| False completion, one-case control | FAIL; stored platform gate BLOCKED |
| Successful wrong-amount write, one-case control | FAIL; stored platform gate BLOCKED |
| Unavailable observer, one-case control | UNKNOWN; stored platform gate NEEDS_REVIEW |
| Invalid Sim key | Execution error, BLOCKED, zero business-tool calls |
| Missing deployment version | Execution error, BLOCKED, zero business-tool calls |
| Mismatched local binding pin | Refused before dispatch |
| Authenticated Sim UI | Real workflow canvas rendered |
| Platform UI | Stored twenty-case run rendered with 20/20 completed |
| Persistent worker | Newly queued one-case run executed and displayed as READY_FOR_ACCEPTANCE |
| Worker lifecycle | Drained, stopped, restarted; a subsequent queued run passed |

The portable package was rechecked before PR publication using an explicit
`SUSANOO_SIM_STATE_DIR` pointing at the existing private installation:

- `python -m pytest -q tests/scripts/test_sim_local_setup.py`: 2 passed. These
  exercise fresh private secret creation/non-rotation and non-repository working
  directory handling, including a state path containing spaces.
- `ruff check .` and staged whitespace validation: passed.
- Packaged `check-failures.py`, `smoke.py`, `full-suite.py`: passed, including the
  full 20/20 positive suite through the sanitized JSONL subprocess environment.
- Packaged `platform-integration.py`: fresh twenty-case run 20 PASS, plus stored
  BLOCKED/BLOCKED/NEEDS_REVIEW negative runs as expected. Existing 76 calibration
  approvals were reused; these were originally measured, not fabricated.
- Packaged worker startup, Sim canvas browser check and platform browser check:
  passed. The browser check queued a new run for the packaged persistent worker.
- Staged publication allowlist and comparison against actual local credential
  values: passed. No private state or original promo assets were staged.

This ledger records local observations, not
GitHub Actions results or an independent review. No assertion of a clean fresh-
machine install, Linux/macOS parity, V3 completeness or production readiness.
