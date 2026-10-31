# Muaz Lane Dependency Requests

Root `pyproject.toml`, `uv.lock`, Compose, Makefile and CI belong to Aarav. These are the exact
changes the runtime lane needs; until they land, `tests/runtime/conftest.py` puts the packages on
`sys.path` so the tests run without touching the lockfile.

## D-1 Add the runtime packages to the uv workspace (needed now)

`pyproject.toml`:

```toml
[tool.uv.workspace]
members = [
    "apps/api", "apps/worker", "packages/core", "packages/contracts",
    "packages/evaluator", "packages/simulator", "packages/sdk-python", "packages/runner",
    "packages/connectors",
]

[tool.uv.sources]
# existing entries, plus:
susanoo-evaluator = { workspace = true }
susanoo-simulator = { workspace = true }
susanoo-sdk = { workspace = true }
susanoo-runner = { workspace = true }
susanoo-connectors = { workspace = true }

[dependency-groups]
dev = [
    # existing entries, plus:
    "susanoo-evaluator",
    "susanoo-simulator",
    "susanoo-sdk",
    "susanoo-runner",
    "susanoo-connectors",
]
```

New third-party dependencies: none. The five packages use `pydantic`, `httpx`, `fastapi`, `uvicorn`,
`websockets` and `rfc8785` (via `susanoo-contracts`), all already resolved in `uv.lock`. Source
builds also need the `git` CLI on the runner host.

After this lands, `examples/collections` still needs `PYTHONPATH=examples/collections` (it is an
example, not a package) or a tiny `pyproject.toml` I can add under `examples/`.

## D-2 Make targets (needed for the demo gate)

```make
runtime-test:      ## runtime unit, crash-window and local-slice tests
	uv run pytest tests/runtime tests/contract/runtime -q
runtime-demo:      ## 20-case synthetic suite, real JSONL agent process, local simulator
	PYTHONPATH=examples/collections uv run susanoo-runner local \
	  --suite examples/collections/suite.json --agent jsonl:correct --report .susanoo/report.json
```

`make test` should include `tests/runtime`; it needs no database, network or model key.

## D-3 Simulator service container (needed for Compose, not yet urgent)

- Entry point: `susanoo-simulator --root /data --host 0.0.0.0 --port 8765`
- Required env: `SUSANOO_SIMULATOR_ADMIN_KEY` (secret, never logged)
- Health: `GET /health` returns `{"status": "ok", ...}`
- Volume: `/data` (per-fixture SQLite files; disposable)
- Network: reachable only from the runner, never from the internet or the control plane
- Image: `python:3.12-slim`, non-root user, read-only root filesystem plus a writable `/data`
