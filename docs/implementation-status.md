# Expanded Build Implementation Status

Updated 2026-11-02. Integration branch: `codex/aarav-runtime-integration`; preserved baseline:
`73224ee9133133663a7f4df6925b8afac209d88b`.

The hackathon product is preserved. The expanded platform has a working private
control plane and UI at `/evaluation`. This is **not complete product acceptance**:
The outcome/preflight runner now connects to the real platform and has passed synthetic
PASS/FAIL/UNKNOWN service integration. Advanced job dispatch, specialized report views and
provider qualification remain incomplete. Canonical wire major `1.0`, revision `1.1-proposed`,
is pending peer review, not an agreed freeze. See the current
[integration handoff](parallel/aarav/INTEGRATION_HANDOFF.md) and
[Muaz continuation prompt](parallel/aarav/MUAZ_NEXT_PROMPT.md) for the complete remaining split.

The matrix below preserves the original platform checkpoint. The integration handoff supersedes
its older statements that no runtime exists; it does not turn pending milestones into release acceptance.

| Milestone | Aarav platform implementation | Remaining shared/runtime exit gate |
|---|---|---|
| M0 audit/scope | Existing stack and baseline preserved; ownership documented | No new runtime acceptance inferred from old tests |
| M1 foundation/C0 | Auth/recovery/invitations, tenant/project roles, hidden grants, immutable revisions, seven additive migrations, private artifacts, generated contracts, native development and Compose tooling | Muaz's C0 review; hosted container checks and deployment qualification |
| M2 outcome slice | Pinned launch, runner registration/claims, leases, events, reservations, evidence/result ingestion, run UI | Actual agent invocation, independent observation and business checker |
| M3 reliable suites | Approval gates, required counts, budget/concurrency enforcement, cancellation, interrupted-attempt handling, resource quarantine and fencing | Real crash-point journal/reconciliation and fixture isolation at concurrency 1/3 |
| M4 source intake | Pinned source metadata, separate read-only GitHub App credential broker, encrypted grants/revocation, private source artifacts and job manifests | Local Git/archive acquisition, traversal protection, builds and SDK/container adapters |
| M5 evidence workflow | Findings/detail, evidence inspection, fresh reproduction tracking, compatibility comparisons, review/expiring waivers, shared-snapshot PDF/JSON/JUnit/ZIP exports | Reproduction against real fresh fixtures; runtime report conformance |
| M6 simulator | Versioned profiles/contracts, approved job dispatch and authorized result/artifact inspection | Stateful simulator/reference services, normalizers, paired replay; typed domain reports and final specialized UI review |
| M7 impact/repair | Dependency records, approved repair artifacts, exact validation/held-out/control envelope, separate approval, bounded authorized draft-PR publisher | Impact/reduction/grader-challenge algorithms; actual retest evidence; specialized 2x2/diff/reduction views |
| M8 remaining intake | I01-I14 configuration schemas/forms, capability/preflight boundaries and job orchestration | Each protocol adapter's conformance, real capability evidence and complete input workflows |
| M9 recurring/advanced | Schedules/deduplication, dependency invalidation, signed GitHub/CI callbacks, bounded jobs, probe budgets and grader/control references | Passive collectors, incident extraction, collision/probing/semantic engines and domain views |
| M10 media | Consent/policy-aware private upload/download/playback, voice/provider configuration and callback-verifier extension boundary | Concrete media/telephony clients, timestamp/audio metrics, language-review reports; live credentials separately required |
| M11 operations | Encrypted consistent backup/restore with current deletion journal, retention, default-deny RLS, capacity test, CI, operations/handoff | Hosted CI, target clean-install/container execution, real runtime restart and complete T01-T40 acceptance |

## Evidence Levels

- New platform tests use actual disposable PostgreSQL 17.6 databases, real local
  object storage and HTTP/browser services. Signed protocol clients are explicitly
  test doubles, not business runtimes.
- The 20-case concurrency tests exercise platform allocation at 1 and 3. The
  10,000-attempt benchmark records actual loopback HTTP latency and bounded resource
  measurements, not an SLA or customer-execution performance.
- See [Aarav status](parallel/aarav/STATUS.md), the
  [T01-T40 evidence matrix](parallel/aarav/ACCEPTANCE.md),
  [operations](parallel/aarav/OPERATIONS.md) and
  [Muaz handoff](parallel/aarav/HANDOFF.md) for exact qualifications.
- No production migration, deployment, customer-system call, merge or main push
  is authorized by this ledger. Provider mocks are not live-provider verification.
- The [GitHub failure audit](parallel/aarav/CI_AUDIT.md) distinguishes corrected
  historical runs from a test-only deterministic-clock fix for a legacy browser
  animation flake. It does not turn green CI into full product acceptance.

## Known Release Limits

The Windows Docker engine failed before container execution; do not reset user
Docker data to bypass it. Native PostgreSQL was used instead. Compose includes no
fake customer runner; a subsequent hosted checkpoint passed container startup
and backup. Full native Windows legacy testing still has 17 failures in unchanged
runtime/test paths, documented in
[Windows qualification](parallel/aarav/WINDOWS_LEGACY_TESTS.md).
The plan's named MinIO distribution was unavailable, so
protected filesystem storage plus the explicit S3 boundary are used pending review.
Cloud S3/TLS/SMTP/GitHub App execution and complex-script PDF typography require
their own target qualification. Three low-severity web CLI dependency advisories
remain; the critical Next.js advisory was addressed without forcing an unrelated
major CLI downgrade. Advanced domain-specific reports remain provisional until
Muaz supplies reviewed typed producers. No complete bot or live-provider claim is made.
