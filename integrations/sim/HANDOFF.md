# Technical Handoff For Muaz And Coding Agents

## Scope And Authority

This PR packages the local integration previously built outside the repository.
It adds scripts and documentation, not modifications to existing runtime modules,
canonical contracts, migrations, deployment workflows or the upstream Sim source.
The active build spec remains authoritative. The proposed V3 PRD and its audit
were read for context but are not adopted by this PR. No supplied PDFs, user
screenshots, secrets, model keys, local databases or private execution logs are
published. Main is a review baseline only; do not merge/deploy automatically.

At publication preparation, Git fetch showed remote main and the integration
branch at `b5cfe8b`, Muaz's runtime branch at `a8f7707`, and no newer V3 branch.
Retarget or cherry-pick this standalone integration folder into the actual V3
branch once it exists. Re-run against its contract/runtime changes before merging.

## End-To-End Flow

1. Platform stores immutable versions and separate synthetic reviewer approvals.
   Local profile pins the exact connection content hashes and tool operations.
2. Registered local runner claims a preflight/outcome job. Platform client verifies
   manifest hashes; local binding checks workflow ID, graph hash and immutable Sim
   deployment version. The subprocess also checks the binding pin.
3. SDK starts `serve.py` as a local JSONL process with bounded protocol frames and
   sanitized environment. The API never imports or runs this agent.
4. `SimAgent` sends only the current message, conversation state and previous tool
   feedback to the version-selected local Sim execution API. It does not send
   hidden checks, expected values, observer credentials or platform credentials.
5. The real Sim Function block returns state, response text and at most one typed
   action per planning round. At most three rounds are permitted per invocation.
   It supports account/amount/date parsing, multi-turn state, confirmation and
   policy handoffs. It is deterministic, not an LLM or customer agent.
6. The local bridge permits only `create_promise` and `request_handoff`; the
   fixture-scoped SDK ToolClient executes the selected action. If a promise write
   loses acknowledgement, it reads back once using `list_promises`; it never
   repeats the mutation. No model-generated executable code is accepted.
7. Susanoo's fixture adapter independently reads backend state. Pure evaluator
   computes PASS/FAIL/UNKNOWN. PlatformJournal uploads real events, observations,
   checks and finalization under the lease. Platform stores the actual run gate.
8. Browser reads the real API-backed console. The persistent worker can process
   later queued runs without the one-off harness remaining open.

## File Map

| File | Responsibility |
| --- | --- |
| `paths.py`, `paths.cjs`, `paths.ps1` | Repo/state discovery; no hardcoded laptop path |
| `init.py`, `compose.local.yml`, `sim.ps1` | Private secrets; digest-pinned loopback Docker deployment |
| `workflow-code.js`, `provision.py` | Sim graph, local account/key, immutable deployment and binding |
| `sim_agent.py`, `serve.py` | Bounded Sim planning/tool bridge and JSONL process entry point |
| `smoke.py`, `smoke.ps1` | Four one-case business/observer scenarios |
| `check-failures.py` | Invalid key/version/binding must not dispatch business tools |
| `full-suite.py` | Original twenty cases through real JSONL/Sim/simulator/evaluator |
| `calibrate_platform.py` | Actual per-critical-check control challenges and typed platform reports |
| `platform-integration.py` | Synthetic revisions/approvals, runners, preflight, claims and four platform scenarios |
| `platform_worker.py`, `worker.ps1` | Continuous approved claim handling and safe drain/restart |
| `check-ui.cjs`, `check-platform-ui.cjs` | Sim canvas, platform console and new queued-run verification |

## Evidence And Reproduction

See EVIDENCE.md. Keep these layers distinct: local pure calibration, actual local
TCP/JSONL integration, browser verification, hosted CI and external-provider
qualification. This PR does not turn one into another. FAIL/BLOCKED negative
controls are expected successful tests. Never change those expected results to
make every scenario display PASS.

The full-suite SQLite journal is an input to calibration: passing observation
snapshots become positive synthetic controls; explicit wrong counts, amounts,
dates or fees become labelled negative controls. These challenge each individual
critical definition, but are not a customer expert-labelled corpus or a fresh
held-out evaluator benchmark. A separate local reviewer identity records clearly
labelled automated synthetic approval. Do not describe this as human approval.

## Failed Approaches Preserved In Local Evidence

- Workspace Sim keys could import graphs but manual/deployment execution required
  a personal key; the first workspace key was revoked, not used to bypass auth.
- The internal state API did not accept the API-key path. Use the public v2 state
  API, then execute a specific immutable deployment version.
- Native Windows default text encoding could not serialize the rupee symbol in a
  raw report. The packaged full report explicitly uses UTF-8.
- Creating a grader from a normalized response instead of its original input
  produced a different revision with no matching calibration. Approval correctly
  refused it. Grader approval now targets the exact calibrated original input.
- Preflight's agent context can omit the connection; read the hashed pinned
  connection version from its execution manifest, as the existing runner does.
- Initial browser selectors used full IDs/title case while the console displays
  eight-character IDs/uppercase gates. Checks now use actual UI semantics and
  wait for Run Evidence before taking screenshots.

Do not replay the failed/expired assignments; final validation used new attempts.

## Continue Here

1. Review the local bridge under runtime ownership, and retarget to V3 without
   overwriting either lane. No canonical schema change is requested here.
2. Reconcile V3's approved profile, connection-status, no-default-agent, quarantine,
   cancellation, hidden-data and full-suite gate changes with these callers.
   Existing audit defects are not fixed merely because this synthetic scenario
   avoids them. Do not remove those V3 requirements.
3. If making this a supported connector, add a canonical reviewed intake recipe,
   connection/version status evidence, cancellation semantics, resource isolation,
   per-action durable authority/budgets, typed planner validation and adversarial
   plan tests. Currently it is trusted local synthetic code, not a sandbox.
4. Bind an actual intended model-backed workflow only after provider credentials,
   data permissions, budgets and tool access are explicitly configured. Preserve
   immutable version selection and reapproval on every binding change.
5. Qualify one authorized external workflow/reference, obtain real human review,
   then complete the product's release gate. No live provider, phone carrier,
   customer endpoint, production deploy or customer acceptance was exercised here.

## Operational Limits

- Host/ports are deliberately fixed to loopback; project identity and credentials
  are generated locally. This is single-worker, text collections scope.
- Do not run the harness concurrently with the persistent worker; both use the
  same project queue. Stop requests drain the current assignment, not the whole
  platform. Uncertain/lost-authority assignments remain reconciliation-only.
- State checkpoints prevent most duplicate setup, but create/registration calls
  are not an exactly-once provisioning transaction. On lost acknowledgement,
  inspect/revoke/reconcile before rerunning; never assume no resource was created.
- Controls are generated per check, not a comprehensive independent benchmark;
  complete hostile/malformed plans, cancellation, concurrency and customer-network
  qualification remain work for a production connector.
- Windows was exercised. A brand-new machine and other operating systems were not.
- Source and official published images can differ. Use recorded image digests to
  identify executed software; do not claim the clone commit built those images.
