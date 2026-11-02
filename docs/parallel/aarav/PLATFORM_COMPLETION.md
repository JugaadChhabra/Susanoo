# Aarav Platform Completion Ledger

Authority: `docs/spec/Susanoo_Architecture_and_Build_Plan.md`; owner requested every remaining Aarav item.
Branch: `codex/aarav-runtime-integration`, draft PR #45. No merge or production authorization is implied.

| Platform item | Implementation | Verification / next gate |
|---|---|---|
| Advanced typed reports | 19 discriminated report variants reuse Muaz's DTOs; immutable scoped persistence, job-kind binding, classification audit, hidden/audience/policy enforcement | PostgreSQL protocol tests pass; final UI/browser gates pending |
| Specialized screens | Comparison responses/state discrepancies, impact attribution, all 2x2 repetitions, reduction history, repair gates, probe accounting, collision observations, calibration, media/import/incident/validity/capacity views | TypeScript and lint pass; browser screenshot gates pending |
| Callback persistence | Pinned integration/attempt/operation registry; atomic durable receipt and queue; duplicates/conflicts/order/terminal handling; outbound evidence-only polling | Real PostgreSQL tests with signed helper vectors pass |
| Twilio verifier | Supplied CallbackVerifier against configured HTTPS public origin; form and body-digest-bound JSON; call lifecycle never establishes task outcome | Signature/correlation tests pass; no live calls |
| Passive collector | Supplied OTLP receiver, digest-only project tokens, revocation, bounded gzip, persistence and restart dedupe, current-policy minimization, retention | JSON/gzip PostgreSQL tests pass; protobuf/browser gates pending |
| Automatic runs | Supplied budget decision and dedupe helpers; project lock; semantic unique key; retained blocked reasons; schedules and triggers use admission | Rate/case/call/cost-unknown/scope tests and schedule test pass |
| Advanced exports | Authorized reports in one snapshot; domain PDF projection; matched runtime export/reproduction ingredients; project-only reproduction ZIP withholding held-out cases | Basic report PDF/ZIP test passes; runtime-ingredient/complex-script gates pending |
| Repair publication | Existing scoped draft-only publisher preserved | Finish typed validation/evidence approval and supplied reconciliation/base adapters |
| Operations / evidence | New controls for collector credentials, callback tasks and automatic decisions; schema generation and additive migration | Final frozen install, full suites, backup/restore/retention and hosted CI pending |

The first expanded pass passed 77 disposable-PostgreSQL platform/contract tests in 293.03 seconds.
These include protocol-producer tests, not Muaz's future advanced platform worker handlers. Existing
core synthetic real-runner PASS/FAIL/UNKNOWN and lease-loss integration remain separate evidence.
Tests are overlapping evidence, never summed into a fictional independent acceptance total.

Remaining external/joint gates: Muaz's semantic contract review and advanced report-producing worker
handlers, actual authorized provider identities/endpoints, fluent voice review, and authorized target
deployment/SMTP/storage/GitHub App qualification. Missing live credentials do not excuse missing code.
