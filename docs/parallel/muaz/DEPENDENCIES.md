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

Core third-party dependencies are already resolved in `uv.lock`: `pydantic`, `httpx`, `fastapi`, `uvicorn`,
`websockets`, `rfc8785` (via `susanoo-contracts`). Source builds also need the `git` CLI on the runner host.
Package manifests still need these declared (all already in the lock transitively; see D-4 for new ones):

| Package | Add to its `pyproject.toml` | Why |
|---|---|---|
| `susanoo-runner` | `susanoo-core`, `susanoo-connectors`, `opentelemetry-proto>=1.45,<2`, `protobuf>=5.29` | agent repair reuses `susanoo.guards`; repair uses source intake; OTLP imports and collector |
| `susanoo-connectors` | `mcp>=2.3,<2.4`, `httpx2>=2.13,<3`, `jsonschema>=4.26`, `protobuf>=5.29`, `pyyaml>=6` | MCP client and schema checks; gRPC descriptors |
| `susanoo-simulator` | `pyyaml>=6` | OpenAPI scaffold YAML input (`safe_load`, aliases refused) |
| runtime tests | `langgraph>=1.2,<2` (test group) | LangGraph recipe test (skips without it) |

After this lands, `examples/collections` still needs `PYTHONPATH=examples/collections` (it is an
example, not a package) or a tiny `pyproject.toml` I can add under `examples/`.

## D-2 Make targets (needed for the demo gate)

```make
runtime-test:      ## runtime unit, crash-window and local-slice tests, TypeScript SDK
	uv run pytest tests/runtime tests/contract/runtime -q
	node --test packages/sdk-typescript/test/
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

## D-4 Optional provider dependencies (only for real, non-fake runs)

None of these is needed by the tests; each connector loads it lazily and fails with a clear error when absent.
Suggest optional extras on `susanoo-connectors`.

| Extra | Package | Used by | Why |
|---|---|---|---|
| `grpc` | `grpcio>=1.66` | `grpc_connector.GrpcioTransport` | real gRPC wire calls |
| `browser` | `playwright>=1.48` (+ `playwright install chromium` on the runner host) | `browser.PlaywrightDriver` | real browser runs (I11) |
| `twilio` | `twilio>=9.3,<10` | callback endpoint cross-check | spec §20 asks for the provider SDK's validator; our helper matches the documented vectors |

## D-5 CI wiring

- Node >= 22.18 on CI for `node --test packages/sdk-typescript/test/` (no `npm install` needed).
- Docker on the runner image if container tests (I08) should run rather than skip; the test image builds from
  `python:3.12-slim` and needs PyPI access during the build.
- Rename note: my transport test is `tests/runtime/test_agent_transports.py` (it collided with
  `tests/api/test_connectors.py` under pytest's rootdir import mode).
