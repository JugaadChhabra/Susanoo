# Local Sim / Susanoo Integration

This is a real local integration using **synthetic collections data**. It is not
a production connector, a new agent-building platform, a customer acceptance
decision or completion of Susanoo V3. No customer code executes in the API.

See [HANDOFF.md](HANDOFF.md) for architecture, continuation work and failure
recovery; [EVIDENCE.md](EVIDENCE.md) separates observed results from untested claims.

## Requirements And Boundaries

- A working Susanoo checkout with `uv sync --frozen --all-packages` completed.
- The existing local platform API, web and native PostgreSQL running, per
  [platform operations](../../docs/parallel/aarav/OPERATIONS.md).
- Synthetic owner/reviewer accounts from the explicit development bootstrap.
  `PLATFORM_BROWSER_CREDENTIALS` can select their private JSON file. The harness
  requires `owner@susanoo.local` and `reviewer@susanoo.local`; never supply real
  customer accounts. If multiple seed files exist, selection is mandatory.
- Docker with Compose supporting `!override`, sufficient memory/disk for the
  official images, and free loopback ports 3010/3012. No Docker socket is mounted
  into the Susanoo API. Node/Playwright are needed only for browser verification.
- PowerShell wrappers target Windows. Python, Node and Compose commands can be
  run directly elsewhere, but macOS/Linux execution is **not qualified here**.

Fixed local endpoints: Sim `http://localhost:3010`, realtime
`http://localhost:3012`, Susanoo API `http://127.0.0.1:8000/api/v1`, console
`http://localhost:3000/evaluation`. This package never calls customer endpoints.
Port changes require coordinated code/config changes, not just a Compose edit.

## Fresh Setup

From the Susanoo repository root, use a fresh sibling Sim clone (do not reset an
existing working checkout):

```powershell
git clone https://github.com/simstudioai/sim.git ../sim
git -C ../sim checkout 87fefdf8050e7a55c038a481675744d1f2e17dec
uv sync --frozen --all-packages
uv run python integrations/sim/init.py
./integrations/sim/sim.ps1 up
uv run python integrations/sim/provision.py
uv run python integrations/sim/check-failures.py
uv run python integrations/sim/smoke.py
uv run python integrations/sim/full-suite.py
uv run python integrations/sim/platform-integration.py
./integrations/sim/worker.ps1 up
```

Start the existing platform before the provisioning harness. Follow its documented
bootstrap procedure only on a new local development installation; **never
reinitialize an existing database**. No production migrations are authorized.

`init.py` creates secrets exactly once and refuses an existing `.env`. `sim.ps1`
uses the official upstream `docker-compose.prod.yml` plus this package's override.
The override pins all six image digests, keeps authentication enabled, binds app
and realtime to 127.0.0.1, and leaves database/Redis unpublished. The Docker project
is `susanoo-local-sim`; its `postgres_data` volume persists independently of Susanoo.
The executed images are identified by their digests, not assumed to match the
cloned source commit exactly.

State defaults to `.platform/sim-local/` in this checkout. Overrides:

| Variable | Purpose |
| --- | --- |
| `SUSANOO_SIM_STATE_DIR` | Absolute private state directory, including existing `.env` and `.private/` |
| `SIM_SOURCE_DIR` | Existing pinned upstream Sim checkout; default is sibling `../sim` |
| `PLATFORM_BROWSER_CREDENTIALS` | Explicit local synthetic account JSON |
| `PLAYWRIGHT_BROWSERS_PATH` | Existing browser cache, if not using Playwright's default |

The JSONL subprocess receives only the explicit state-directory override in
addition to the SDK's sanitized base environment. It does not inherit platform
tokens or the host environment wholesale.

## What Gets Created

`provision.py` creates a local Sim account, workspace, private key and Start ->
Function workflow. Authentication is enabled. The account/key is separate from
Sim Cloud. A personal key is necessary for the version-selected execution API;
the initially created workspace key is revoked. Only this synthetic local account
is involved. `workflow-code.js` contains the deterministic workflow logic.

The platform harness creates **Sim Local Integration - Synthetic** in the first
accessible seeded workspace, grants explicit project-scoped hidden access to the
local owner/engineer and separate reviewer accounts, and records requirements,
fixtures, checks, original case visibility, suites, environment and connections.
It registers separate calibration and outcome/preflight runners. Approvals are
automated synthetic approvals, never a human review or external acceptance.

All 76 critical definitions are challenged before suite approval. Calibration
uses actual prior passing observations from `full-suite.py` plus explicitly seeded
wrong-state observations. It uploads actual typed calibration reports through the
advanced job handler. Critical severity and the platform's approval guard remain
unchanged. This is why the full-suite command must precede platform provisioning.

## Run And Inspect

```powershell
./integrations/sim/sim.ps1 status
./integrations/sim/worker.ps1 status
./integrations/sim/worker.ps1 stop
./integrations/sim/worker.ps1 up
./integrations/sim/sim.ps1 logs
```

Worker `stop` requests a drain. Wait for `status` to report stopped before rerunning
the provisioning harness. Never run that harness concurrently with the persistent
worker: both claim from the same project queue. A refused/expired assignment is
not replayed. Preserve journals and use the platform's recovery procedures.

In the console select **Sim Local Integration - Synthetic**, then Runs. The full
positive run has 20/20 completed and READY_FOR_ACCEPTANCE. Three separate one-case
negative runs intentionally have BLOCKED/BLOCKED/NEEDS_REVIEW. Calibration runs
and earlier unsuccessful setup attempts remain visible; do not delete them to
make the run list look green. The persistent worker supports only the approved
preflight/outcome connections, not arbitrary Sim tools or advanced job types.

Browser checks, after `npm ci` in `tests/e2e` and installing its Chromium browser:

```powershell
node integrations/sim/check-ui.cjs
node integrations/sim/check-platform-ui.cjs
```

The second check deliberately launches one new synthetic one-case run, waits for
the persistent worker, and verifies its result in the actual console. It is not
read-only. Browser screenshots, reports, generated workflow exports and private
journals go to the state directory, never this source folder.

## Secrets And Publishing

`.private/account.json`, `.private/binding.json`, `.private/platform.json`, `.env`,
raw journals and reports are local-only. The state folder is protected using the
existing Windows ACL/POSIX helper before provisioning. Do not regenerate the
encryption key or discard the database volume. Neither a PR nor a clone transfers
the running services, accounts or data; each operator must provision their own.

Before changing the workflow, drain the worker, update/provision deliberately,
reapprove the new platform connection/profile, then restart. Old runs stay pinned
to old immutable Sim deployment versions. Never edit a binding/profile to resume
an expired lease or uncertain mutation.
