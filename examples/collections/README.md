# Synthetic collections example

Everything here is synthetic: accounts, holders, amounts and approvals are invented, and the
suite's approval is a synthetic demo approval, not a human or customer approval. No model key,
payment, real message or call is involved.

- `collections_demo/agent.py`: deterministic sample agent with switchable behaviours:
  `correct`, `false_completion`, `wrong_amount`, `wrong_account`, `duplicate`, `forbidden_change`,
  `handoff`, `timeout_after_write`, `lost_ack`.
- `collections_demo/suite.py` → `suite.json`: the 20-case suite (10 single-turn promises, 2
  multi-turn, repeated confirmation, an injected lost acknowledgement, 4 out-of-policy requests, a
  fee-waiver request and a dispute; C07, C12 and C18 are held out).
- `collections_demo/serve.py`: serves the agent over the JSONL bridge.

## Run it

Until the packages join the uv workspace (`docs/parallel/muaz/DEPENDENCIES.md`):

```bash
export PYTHONPATH=packages/evaluator:packages/simulator:packages/sdk-python:packages/runner:examples/collections
uv run python -m susanoo_runner.cli local --suite examples/collections/suite.json --agent jsonl:correct
uv run python -m susanoo_runner.cli local --suite examples/collections/suite.json --agent jsonl:false_completion
uv run python -m susanoo_runner.cli local --suite examples/collections/suite.json --agent jsonl:correct --observer-outage
```

Expected (recorded 1 Nov 2026):

| Agent / switch | Gate | Verdicts |
|---|---|---|
| `correct` | READY_FOR_ACCEPTANCE | 20 PASS |
| `false_completion` | BLOCKED | 19 FAIL, 1 PASS (the dispute is still handed off) |
| `correct` + `--observer-outage` | NEEDS_REVIEW | 20 UNKNOWN |
| `wrong_amount` / `wrong_account` | BLOCKED | 15 FAIL |
| `duplicate` | BLOCKED | 14 FAIL |
| `handoff` | BLOCKED | 14 FAIL |
| `lost_ack` | BLOCKED | 2 FAIL (C13, C14) |
| `forbidden_change` | BLOCKED | 1 FAIL (C19, the only case where the environment permits waivers) |
| `timeout_after_write`, `--invocation-timeout 1` | BLOCKED | write found by read-back; execution `timed_out` |

Regenerate the suite after editing `suite.py`:

```bash
uv run python -m collections_demo.suite > examples/collections/suite.json
```
