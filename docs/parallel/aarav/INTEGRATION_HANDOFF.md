# Integration Checkpoint and Remaining Work

Updated 2026-11-02. Working branch: `codex/aarav-runtime-integration`.
Base: Muaz's `a8f7707`, which already contains Aarav's `301c39d` and all 27 runtime commits.
This is a review candidate, not full PRD acceptance or a production deployment.
Published review: https://github.com/JugaadChhabra/Susanoo/pull/45 (draft, stacked on PR #44).
Setup and recovery: [customer runner operations](RUNNER_OPERATIONS.md).

## Authorization and ownership

The owner explicitly authorized this integration pass to change both lanes, including the runner client
and the four defects recorded in Muaz's handoff. Canonical contracts still have one writer and their
agreement flag remains pending Muaz's review. Do not create a competing SDK schema.
No main push, merge, production migration, paid provider call or customer-system mutation was performed.
The ignored `promo/` project, source PRD and generated credentials are excluded from this publication.

## Implemented in this pass

- Canonical typed event payloads and discriminated event batches; per-kind job results; strict operands
  and operator validation using one canonical check DSL; observation-name binding; adapter-boundary models.
- Component hashes over the exact typed claim projection, including connection, suite, request, case,
  checks, fixture, policy, limits and evaluator material. The run's original immutable hash is separately
  `run_manifest_hash`; `manifest_hash` in the claim hashes `component_hashes`.
- Canonical UTC timestamp serialization, explicit error codes, shared evaluator verdict/gate/evidence rules,
  late journal evidence, idempotent reconciliation and separately reserved cleanup budgets.
- The HTTP/JSONL wire major remains 1.0. Contract revision is `1.1-proposed`; changing the actual wire version
  requires peer agreement. This is not a claim of backwards compatibility with every old producer.
- SDK and evaluator models re-export canonical definitions. The former strict-xfail assertions are now
  normal regression gates; their passing result is conformance evidence, not independent peer approval.
- Runtime packages and the collections example are real workspace members. Direct dependencies and optional
  gRPC/browser/Twilio dependencies are locked; the example no longer needs an ad hoc PYTHONPATH.
- A customer-side `PlatformClient`, locally approved connection hashes/profile, durable platform journal,
  lease/control monitoring, prepare/reserve/dispatch ordering, evidence upload and finalization.
- `platform-worker` handles outcome suites and real preflight cases only. It never executes a command
  received from the platform. Agent command, simulator destination and local authority are host configured.
- `platform-reconcile` uploads late evidence without executing or cleaning up. Lost authority does not
  authorize resuming an attempt. Local journals contain sensitive assignment material and are protected.
- Runtime fixes: operation-correlated simulator read-back, registered shared-ledger domain, idempotent
  refund of known-unused probe allowances, and terminating process isolation for timed-out semantic graders.
- Windows JSONL stdin support and owned process-tree cleanup; POSIX-only source builds fail closed on
  Windows instead of failing imports or silently dropping resource limits.
- Python and TypeScript tool clients propagate the current operation correlation ID. Unknown correlation
  is explicitly unresolved, not evidence that a different namespace write was the uncertain action.
- Follow-up hardening journals and reserves observation before calling the fixture adapter, because a
  simulator observer can advance its clock. The default housekeeping reserve is now three operations:
  prepare, observe, cleanup. Old pinned limits below three are refused before fixture execution; create
  an approved new manifest rather than mutating old evidence. The canonical digest changed accordingly.
- Lost event/reservation acknowledgements permanently fence the local attempt. Evidence-processing
  failures still attempt authorized cleanup; a one-shot worker cannot report success with failed cleanup
  or pending reconciliation. These changes have focused regression gates.
- Uncertain fixture creation remains unresolved with failed cleanup instead of falsely saying cleanup
  was unnecessary. The runner does not retry creation or guess a safe remote deletion.
- Runtime CI includes TypeScript tests, optional provider dependencies and real Chromium installation.
  The container build includes the new workspace member's metadata, not a customer execution service.
- Preserved hackathon Dockerfiles now include every workspace member's metadata and the API's evaluator/
  contract sources. Hosted container CI also builds and import-smokes both legacy images. Runner state
  under `.susanoo` is excluded from Git and all root Docker contexts; it contains assignment secrets.

## Verified before publication

- C0 regression assertions: 19 passed.
- Focused platform bridge plus real runtime cases: 8 passed against disposable PostgreSQL and actual TCP services.
- Real core path: API -> PostgreSQL -> durable journal -> separate JSONL agent -> HTTP simulator ->
  independent observer/evaluator -> persisted result. Correct PASS, false-completion FAIL, observer-outage UNKNOWN.
- Desktop/mobile platform browser checks: 4 passed. Their producer remains the explicitly labelled
  protocol test double; these are not claimed as a combined real-business-runner browser acceptance test.
- Web TypeScript and lint passed; 116 web unit tests and the production Next.js build passed.
- Latest complete disposable-PostgreSQL platform/contract bundle: 67 passed in 217.24 seconds.
- Added assignment-to-component consistency checks also passed all four focused claim/runtime cases.
- Final focused native Windows regression bundle: 57 passed in 89.18 seconds, including contracts,
  CLI export/cleanup, platform bridge checks and spawn-isolated semantic grading. Ruff passed.
- Follow-up complete PostgreSQL platform/contract bundle: 68 passed in 239.99 seconds, including actual
  lease expiry after the agent write, no observer-clock mutation or cleanup dispatch after expiry,
  preserved late evidence and idempotent reconciliation without renewed authority.
- Follow-up focused suites: 26 runner tests, 13 regression tests, 32 runner/journal tests and 31
  CLI/regression tests passed. These include insufficient-reserve refusal and uncertain fixture creation.
- Docker workspace/secret-exclusion static tests: 3 passed. Actual legacy-image builds are a separate
  hosted gate, not inferred from static assertions or the unavailable local Docker daemon.
- TypeScript SDK: 13 passed using `node --test "packages/sdk-typescript/test/*.test.ts"`.
  The old directory-only command is not portable to the installed Node release.
- Seven additional checks passed: actual local grpcio transport, actual Chromium browser adapter,
  installed Twilio SDK signature parity, budget settlement, unrelated-operation read-back rejection,
  domain registration and semantic worker termination. These are not live vendor calls.
- An initial WSL full run had 7 legacy source-inspection failures (2388 passed, 183 skipped). The root
  cause was Windows-generated .pyc code filenames reused by Linux on the shared NTFS checkout. With a
  separate PYTHONPYCACHEPREFIX, the unchanged affected legacy file passed all 67 tests. Do not weaken
  source/secret guards or delete the user's caches to hide this environment issue.
- Full rerun, latest platform bundle, hosted CI and exact publication SHA must be read from the final
  publication record. Do not substitute earlier successful checks for the final head.
- First publication `da1d896` passed every reported hosted PR check: full Python, expanded runtime
  (including optional dependencies and Chromium), PostgreSQL platform/browser, web, legacy browser,
  Caddy, container startup/backup and identity guards. Follow-up head results must be verified separately;
  earlier-head passes are not final-head evidence. Existing Vercel automation also created a preview;
  no manual or production deployment was requested or performed.
- The next full Linux run had 2399 passed, 184 skipped and two semantic worker wall-time assertions
  fail under WSL startup overhead. Provider timeout is still enforced independently; tests now cover
  the documented bounded startup plus termination allowance rather than assuming in-process latency.
  This run is not labelled green; final-head hosted results remain required.
- A subsequent WSL run had 2400 passed, 184 skipped and one reproduction refusal in 563.75 seconds.
  That run overlapped the housekeeping-default change: its parent generated a reserve-two manifest,
  then its subprocess loaded the new reserve-three code and correctly refused the stale manifest.
  It is mixed-revision evidence, not a green result or a reason to weaken immutable-manifest refusal.
  A fresh no-edit-during-execution run is required and final-head hosted CI remains authoritative.

## Remaining by lane

### Aarav / platform integration

1. Persist and expose the runtime's typed advanced reports with their correct authorization and lineage;
   complete specialized comparison/2x2/reduction/probe/media views rather than calling a generic JSON inspector finished.
2. Connect persistent tenant/attempt-scoped webhook TaskRegistry/RelayQueue, Twilio callback validation using
   the configured public URL, and project-token-digest OTLP collection. Generic callback boundaries are not these services.
3. Connect automatic-run budget decisions and semantic trigger deduplication to schedules/webhooks; retain
   blocked reasons. Existing idempotency keys do not by themselves prove the requested trigger policy.
4. Finish advanced report PDF/reproduction-bundle projection, repair publication reconciliation with the
   supplied runtime helpers, and the complete T01-T40 evidence matrix.
5. Qualify the actual target deployment, storage, SMTP, GitHub App and complex-script PDF shaping separately.

### Muaz / runtime integration

1. Review the exact generated canonical digest/revision. Test semantic compatibility, not just field presence.
2. Add platform handlers for build/source, fidelity, impact, reduction, repair, imports, incident drafting,
   collision, probing, calibration and voice. These functions exist locally but this worker does not claim them.
3. Bind every installed connector/configuration to locally approved connection hashes and capability evidence.
   Complete conditional-script and non-text platform execution support; the current worker refuses unsupported variants.
4. Complete real platform/runner process-kill, reconnect, cancel-during-dispatch, isolation-at-concurrency-3,
   source-grant revocation and original-evidence-preservation gates. Local crash tests alone are not this matrix.
5. Enforce the remaining resource/storage/output limits end to end. Declaring a limit field is not enforcement.
   Recheck untrusted build output capture and customer-code isolation before release qualification.
6. Finish or explicitly defer unsupported variants: MCP OAuth/remote cancellation; A2A push/non-JSON-RPC
   bindings; streaming gRPC; CrewAI/AutoGen recipes; hosted media relay. Preserve truthful capability labels.

### External / joint release gates

- Real authorized agent endpoints, browser test identity, Twilio credentials and approved destination,
  GitHub App installation and explicitly authorized provider tests. No live calls were fabricated.
- Fluent Hindi/Marathi review. Draft language cases and synthetic waveforms are not validated speech quality.
- Human contract/code review, all final-head CI, full product acceptance, and explicit authorization before
  production migration, merge or deployment. A green branch is not customer release approval.

## Reproducing checks

```powershell
uv sync --frozen --all-packages --group providers
uv run --no-sync pytest tests/contract/runtime -q
uv run --no-sync python scripts/test_platform.py -q
node --test "packages/sdk-typescript/test/*.test.ts"
```

For WSL sharing this Windows checkout, use a Linux-only environment and bytecode cache:

```bash
export UV_PROJECT_ENVIRONMENT="$HOME/.cache/susanoo-bridge-venv"
export PYTHONPYCACHEPREFIX="$HOME/.cache/susanoo-bridge-pycache"
uv sync --frozen --all-packages --group providers
uv run --no-sync pytest tests -q
```

For the browser adapter test, install Chromium and set `SUSANOO_BROWSER_TESTS=1`; an optional
`PLAYWRIGHT_BROWSERS_PATH` must point to that same installation. CI does this explicitly.
Semantic providers now need to be spawn-serializable and initialize network clients in their worker.
Do not serialize live client pools, locks or credentials into exported reports.
