# Integration Checkpoint and Remaining Work

Updated 2026-11-03. Working branch: `codex/aarav-runtime-integration`.
Base: Muaz's `a8f7707`, which already contains Aarav's `301c39d` and all 27 runtime commits.
This is a review candidate, not full PRD acceptance or a production deployment.
Published review: https://github.com/JugaadChhabra/Susanoo/pull/45 (draft, stacked on PR #44).
Setup and recovery: [customer runner operations](RUNNER_OPERATIONS.md).

## Latest continuation

The owner explicitly authorized publishing the current implementation to auto-deploying main after
being told the remaining gaps. This authorization does not certify full product acceptance or authorize
manual production migrations/customer/provider calls. All reported checks at `b51e0c7` passed before
promotion. The follow-up private source credential consumer has focused unit/Git-configuration and
API/PostgreSQL/worker tests; real GitHub transport remains externally unverified. It keeps tokens out
of argv, Git config files, build environments and local journals, and queues revocation after acquisition.

All advanced worker kinds now have concrete producers; activation still requires exact local inputs.
Approval keys now hash the entire typed execution manifest, including limits, policy and repair/artifact
pins. Old graph-only approvals are deliberately invalidated. Canonical schema revision remains proposed.

New evidence includes real four-cell impact, seeded discrepancy/reduction, equal-budget probing,
shared-state collision detection, deterministic grader calibration, actual synthetic WebSocket audio,
directory/archive intake and a passing Linux API-to-worktree repair/retest/reviewer-approval workflow.
The nine binding checks use real loopback services, including installed MCP, grpcio and Chromium paths;
combined provider/binding qualification passed 12 tests. The TypeScript SDK passed 13 tests, web unit
tests passed 121, production web build/type/lint passed, and six desktop/mobile platform browser checks
passed. Browser protocol fixtures are still distinct from actual runtime execution tests.

Linux-only qualification can be run from Windows with:

```powershell
uv run --no-sync python scripts/wsl_platform_tests.py --full -q --junitxml=.platform/qualification-linux.xml
```

This creates a random temporary WSL PostgreSQL role and UUID database; cleanup is restricted to databases
owned by that exact temporary role. It does not migrate or delete any existing application database.
The local UI is `http://localhost:3000/evaluation`; credentials remain in the ignored bootstrap file.

The first complete WSL/PostgreSQL 18 run recorded **2,534 passed, 128 skipped, 5 failed**. This is not a
green full-suite claim. One failure exposed a missing CREATEROLE privilege in the new disposable-role
wrapper; its RLS/migration retest now passes without granting superuser. Four semantic correctness tests
hit their shared 0.5-second IPC fixture budget. Correctness fixtures now allow three seconds; the explicit
timeout test still enforces 0.5 seconds and UNKNOWN on timeout. All 13 semantic tests pass in the focused
Linux retest. The original failing XML is preserved separately from retests. Hosted PostgreSQL 17 and
provider/browser evidence must be checked against the new PR head, not a previous green revision.

Remaining boundaries are explicitly listed in the latest `docs/implementation-status.md` section. In
particular, do not describe native framework placeholders, live Twilio execution, live private GitHub
qualification, a rootless source/image sandbox, or simulator-source PR publication as completed by these
tests. No vendor/customer call, merge, main push, production migration or manual deployment is authorized.

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

## Platform completion follow-up

The expanded platform checkpoint `9998b62` passed every reported hosted check. The subsequent
completion pass adds typed validation approval, provider-marker reconciliation, stale-base race
handling, patch byte integrity, hosted held-out exposure caps, embedded offline Noto fonts with
HarfBuzz shaping, public reproduction materials from runtime helpers, and expanded browser tests.
See `PLATFORM_COMPLETION.md` and the PR #45 exact-head publication record for final verification.
Earlier test totals above are historical, overlapping evidence, not the final-head count.
The final local platform/contract bundle passed 90 tests; preserved focused Python passed 759 with
26 skipped. Web passed 121 tests, six desktop/mobile platform browser checks and production build.
Native semantic tests passed all 13. The interrupted WSL timing run is documented, not called green.

## Remaining by lane

### 2026-11-03 worker continuation

The worker no longer supports only outcome/preflight: locally approved advanced handlers now execute
simulator inspect/reset, comparison, reduction, fresh task-impact pairs, trace import and incident drafts.
Seven actual API/PostgreSQL/HTTP simulator integration cases passed; the fresh pair independently passed
both arms. Conditional branching passed complete/truncated reply checks, and 47 local-runtime/contract
regressions passed. These are implementation-progress results, not completion of the full build.

Still absent from the platform worker: source/build, repair validation, 2x2, collision, probe, calibration
and voice execution. Connector/profile integration and the complete acceptance command remain open.
Keep the existing failure-closed full acceptance result until these actual workflows are qualified.

### Aarav / platform integration

The previously listed platform code gaps are implemented: 20 typed report variants and scoped domain
views; append-only classification; persistent callback registry/queue and configured-URL Twilio verification;
digest-token JSON/protobuf/gzip OTLP collection; schedule/webhook admission with retained blocked reasons;
advanced PDF/ZIP snapshots; exact-evidence publication reconciliation and held-out exposure accounting.
Implementation is not advanced worker acceptance. Qualified producers still need to call these endpoints.

English/Hindi/Marathi PDF engineering proof embeds licensed fonts and uses HarfBuzz; unsupported glyphs
or unspaced mixed-script words fail explicitly instead of producing silent replacement boxes. This is
not fluent-language or speech-quality validation. JSON remains the lossless fallback.

Target storage/SMTP/GitHub App and production deployment qualification remain external/joint gates,
not missing independent platform code. No release approval is implied by finishing this lane.

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
