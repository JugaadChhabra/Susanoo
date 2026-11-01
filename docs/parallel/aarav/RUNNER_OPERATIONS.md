# Customer-Side Runner Operations

This connects the implemented outcome/preflight workflow, not every job in the build plan.
Use only synthetic data and locally trusted agent code until isolation and release qualification
are complete. Native JSONL processes are not a sandbox for untrusted customer code.

## Install and Approve

1. Fetch `codex/aarav-runtime-integration` and use a clean feature branch. Install with
   `uv sync --frozen --all-packages`. Optional wire tests additionally use `--group providers`;
   their browser installation is a separate step.
2. Start the private control plane as described in `OPERATIONS.md`. In `/evaluation`, create
   and approve the required requirement/check/fixture/case/suite/environment revisions and
   the connection configuration. The simulator fixture adapter must be `susanoo_simulator`.
3. Obtain the exact approved connection version from the authorized
   `GET /api/v1/versions/{version_id}` endpoint as `connection-version.json`. Compute the
   local approval hash over its `data`, not its title, ID, whole version or a reformatted string:

```powershell
uv run python -c "import json; from susanoo_contracts import content_hash; print(content_hash(json.load(open('connection-version.json', encoding='utf-8'))['data']))"
```

Create a local `PlatformProfile` JSON with that hash and the exact connection adapter name.
The following is a template, not authority to execute an arbitrary connection:

```json
{
  "allowed_connection_adapters": ["REPLACE_WITH_APPROVED_ADAPTER"],
  "allowed_connection_hashes": ["REPLACE_WITH_COMPUTED_HASH"],
  "allowed_fixture_adapters": ["susanoo_simulator"],
  "allowed_operations": ["REPLACE_WITH_REVIEWED_TOOL_OPERATIONS"],
  "data_mode": "synthetic",
  "cleanup_reserve": 3,
  "max_concurrency": 1
}
```

Supply explicit tool operations approved for this host. Set `observation_kinds` when check
definitions do not supply an `entity`. A connection change requires new local approval;
the worker will not silently trust a different connection hash. Old pinned manifests with
housekeeping reserve below three are refused before fixture creation.

## Register Without Printing Secrets

The project owner issues a one-time Runner Registration credential in Settings. Inject it
as `SUSANOO_REGISTRATION_TOKEN` through a local secret manager, not a command argument or a
committed file. Set `SUSANOO_PLATFORM_URL` to the trusted control-plane URL ending in `/api/v1`.
HTTPS is required except explicit loopback HTTP. Never disable certificate verification.

```powershell
$body = @{
  registration_token = $env:SUSANOO_REGISTRATION_TOKEN
  name = "Approved local outcome runner"
  version = "integration-review"
  protocol_version = "1.0"
  capabilities = @("outcome_suite", "preflight")
} | ConvertTo-Json
$registered = Invoke-RestMethod -Method Post -Uri "$env:SUSANOO_PLATFORM_URL/runner/register" -ContentType "application/json" -Body $body
$env:SUSANOO_RUNNER_TOKEN = $registered.token
$registered = $null
$body = $null
Remove-Item Env:\SUSANOO_REGISTRATION_TOKEN
```

Keep the runner token in the local secret manager for subsequent sessions. Never print
`$registered`, record a PowerShell transcript containing secrets, or paste tokens into an issue.
If registration acknowledgement is lost, inspect/revoke that registration through the owner;
do not assume a retry created a safe replacement credential.

## Start Synthetic Services

Inject a fresh local-only `SUSANOO_SIMULATOR_ADMIN_KEY` into both terminals through your
secret manager. Do not reuse a platform credential as the simulator key. In one terminal:

```powershell
uv run susanoo-simulator --host 127.0.0.1 --port 8765 --root .susanoo/simulator
```

In the runner terminal, with the profile saved outside source control:

```powershell
uv run susanoo-runner platform-worker --url $env:SUSANOO_PLATFORM_URL --profile .susanoo/approved-profile.json --simulator-url http://127.0.0.1:8765 --state-dir .susanoo/platform --agent jsonl:correct
```

The state directory must be empty or already marked runner-owned. It contains protected
assignment credentials and raw local evidence; preserve it privately and never attach it to
a public issue. For a trusted custom JSONL agent, use `--agent-cmd` followed by its command
as the final option. The platform cannot supply that command. The current worker uses only
its locally installed simulator fixture adapter and does not claim remote provider adapters.

Queue a preflight job for the approved connection, environment and suite. It invokes a real
case and checks invocation/observer/cleanup health, not the correctness of all business cases.
After successful preflight, launch the approved outcome run in the UI. Required-check results
produce the stored gate. Unsupported job kinds remain queued for a matching qualified worker.
Conditional scripts and non-text cases are refused rather than silently reinterpreted.

For one claim, add `--once` before `--agent`/`--agent-cmd`. Exit 0 requires a completed PASS
with clean cleanup and resolved/no-needed reconciliation. Exit 1 is a completed observed FAIL;
exit 2 is operational/unknown/incomplete; exit 3 is invalid configuration. One claim's exit
status is not the entire suite's acceptance decision; inspect the stored run gate.

## Lost Authority and Recovery

An expired lease or lost dispatch acknowledgement permanently fences that attempt locally.
Keep the journal. Do not delete it, reuse the attempt ID, replay a mutation or manually
advance the simulator clock to make an outcome pass. A normal worker restart claims only
new assignments; it does not resume a stored attempt.

Upload a known interrupted journal with its exact attempt UUID:

```powershell
uv run susanoo-runner platform-reconcile --url $env:SUSANOO_PLATFORM_URL --journal .susanoo/platform/ATTEMPT_UUID.sqlite --attempt ATTEMPT_UUID
```

This is evidence-only, does not renew authority and deliberately does not exit as a successful
execution. It does not perform operation read-back or cleanup. Reviewer-authorized read-back
and quarantine resolution remain separate; reproduce with a fresh namespace after resolving
uncertainty. Never retry an uncertain fixture creation or business write automatically.

## Evidence and Limits

`tests/platform/test_runtime_integration.py` exercises real TCP API/PostgreSQL/journal/JSONL
agent/simulator paths for PASS/FAIL/UNKNOWN and lease loss. Browser workflow tests still use
an explicitly labelled protocol producer. Neither proves live provider or full T01-T40 acceptance.
Advanced handlers, persistent callbacks/OTLP, complete resource-limit enforcement and target
deployment qualification remain in `INTEGRATION_HANDOFF.md`. No production action is authorized
by these instructions.
