# Aarav Platform Completion Ledger

Authority: `docs/spec/Susanoo_Architecture_and_Build_Plan.md`; owner requested every remaining Aarav item.
Branch: `codex/aarav-runtime-integration`, draft PR #45. No merge or production authorization is implied.

| Platform item | Implementation | Verification / next gate |
|---|---|---|
| Advanced typed reports | 20 discriminated report variants reuse Muaz's DTOs; immutable scoped persistence, job-kind binding, classification audit, hidden/audience/policy enforcement | PostgreSQL protocol tests; final combined rerun recorded below |
| Specialized screens | Comparison responses/state discrepancies, impact attribution, all 2x2 repetitions, reduction history, repair gates/exposures, probe accounting, collision observations, calibration, media/import/incident/validity/capacity and simulator-state views | TypeScript/lint, 121 web tests, 6 real-platform desktop/mobile browser tests pass; screenshots inspected |
| Callback persistence | Pinned integration/attempt/operation registry; atomic durable receipt and queue; duplicates/conflicts/order/terminal handling; outbound evidence-only polling | Real PostgreSQL tests with signed helper vectors pass |
| Twilio verifier | Supplied CallbackVerifier against configured HTTPS public origin; form and body-digest-bound JSON; call lifecycle never establishes task outcome | Signature/correlation tests pass; no live calls |
| Passive collector | Supplied OTLP receiver, digest-only project tokens, revocation, bounded gzip, persistence and restart dedupe, current-policy minimization, retention | JSON/gzip/protobuf, scoped denial, policy tightening and browser issuance/hide/revoke tests pass |
| Automatic runs | Supplied budget decision and dedupe helpers; project lock; semantic unique key; retained blocked reasons; schedules and triggers use admission | Rate/case/call/cost-unknown/scope tests and schedule test pass |
| Advanced exports | Authorized reports in one snapshot; domain PDF projections; matching runtime export/reproduction ingredients; project-only reproduction ZIP withholding held-out cases/requirements | Runtime helpers -> API -> validated LocalSuite ZIP test passes; offline embedded-font English/Hindi/Marathi PDF rendered and inspected |
| Repair publication | Exact typed gate/patch/base/evidence digest approval; supplied marker-first reconciliation/base adapters; moved-base race clears approval; artifact integrity; monotonic hosted held-out caps | Focused exact digest/gate/diff, capped feedback, provider-double recovery/race and corrupt-byte tests pass; live GitHub App not exercised |
| Operations / evidence | New controls for collector credentials, callback tasks and automatic decisions; schema generation and additive migrations; retention invalidates expired repair approvals | Frozen install, backup/restore/retention regression and exact-head hosted results recorded in final publication record |

The first expanded pass passed 77 disposable-PostgreSQL platform/contract tests in 293.03 seconds.
These include protocol-producer tests, not Muaz's future advanced platform worker handlers. Existing
core synthetic real-runner PASS/FAIL/UNKNOWN and lease-loss integration remain separate evidence.
Tests are overlapping evidence, never summed into a fictional independent acceptance total.

## Completion verification

- Frozen all-workspace/provider install passed on native Windows and Linux/WSL.
- Canonical generator emits 234 definitions, digest `5f457dd16ab1e335d0f0d0b56861e89bc18f28146e33447d3accbca2b06a5d73`.
  Wire major remains 1.0 and revision 1.1-proposed until Muaz records semantic agreement.
- Preserved focused Python bundle: 759 passed, 26 skipped; root Ruff passed.
- Final complete native disposable-PostgreSQL/platform-contract bundle: 90 passed in 384.29 seconds,
  including consistent backup/restore, deletion journal/retention, runtime bridge and new publication gates.
- Native spawn-isolated semantic suite: 13 passed; actual local grpcio transport: 1 passed;
  real runtime CLI export/hidden-material regression: 1 passed. These are not paid/live provider tests.
- Web TypeScript/lint, 121 unit tests, browser TypeScript, 13 TypeScript SDK tests and production build passed.
- Six real-platform browser tests passed on desktop/mobile, including comparison classification with
  immutable machine result and owner collector controls. Their producer is an explicit protocol double.
- Three PDF font/shaping/coverage tests passed; synthetic PDF and desktop/mobile screenshots inspected.
  Unspaced mixed-script tokens/unsupported scripts fail explicitly with JSON fallback. Fluent speech review
  and a separately authorized fresh target reproduction remain outside this engineering proof.
- Full Linux and hosted results belong to the exact-head PR #45 publication record. One initial native
  run had a PDF export job failure while the host also reported ENOSPC; focused and final full reruns
  passed. The job exception is now surfaced by the regression test rather than hidden by worker retries.
  An interrupted broad WSL run had 1946 passed, 154 skipped and six deadline/semantic timing failures;
  it is not green full-suite evidence. Test invocation deadlines now refresh per test and the test proxy
  manager uses spawn rather than forking active service threads. Slow WSL semantic timing remained
  unqualified; native tests and exact-head hosted Linux checks are separate evidence.

Remaining external/joint gates: Muaz's semantic contract review and advanced report-producing worker
handlers, actual authorized provider identities/endpoints, fluent voice review, and authorized target
deployment/SMTP/storage/GitHub App qualification. Missing live credentials do not excuse missing code.
