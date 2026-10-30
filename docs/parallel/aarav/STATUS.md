# Aarav Platform Status

State: implementation ready for platform review and Muaz integration, not full
product acceptance. Updated 2026-10-30.
Baseline: 73224ee9133133663a7f4df6925b8afac209d88b.
Branch: codex/aarav-platform.

## Decisions

- Preserve Next.js/FastAPI/Postgres and the legacy application. Add the expanded
  control plane under `/api/v1`, without changing frozen legacy core contracts.
- Customer execution belongs to Muaz's local runner. No replacement runtime will
  be implemented in the API. Initial contracts are proposals pending his review.
- Use additive private-schema PostgreSQL migrations and explicit application-side
  authorization. Test against a disposable local database, never production.
- Publish only after the owned implementation and available tests are finished;
  do not merge or deploy. Runtime-dependent acceptance remains separately pending.

## Current Work

Implemented locally: canonical strict Python/JSON/OpenAPI/TypeScript contracts;
additive private-schema migrations; local auth/recovery and scoped credentials;
roles and hidden grants; immutable revisions/approvals; pinned launches; runner
claims, leases, events, budget reservations, evidence and finalization; findings,
review/waivers/comparison/reproduction; asynchronous exports; schedules, signed
callbacks, repair proposal/validation/publication boundaries; encrypted backup,
deletion-journal restore and retention. The evaluation UI is at `/evaluation`.

Resource quarantine/fencing, source grants/revocation, policy-aware uploads,
browser workflows, deployment tooling, capacity, CI and operator/peer handoff are
implemented. Existence of an endpoint is not runtime-dependent acceptance. Advanced
simulator/impact/reduction/probe/media reports and specialized views remain
provisional until Muaz supplies reviewed typed producers.

## External/Peer Dependencies

Muaz has not started yet. His runner/evaluator/connectors/services and C0 review
are pending. This does not block independent platform implementation.

## Verification

- Preserved baseline focused tests: 752 passed, 26 skipped; web unit: 116 passed.
- Expanded PostgreSQL-backed tests: 59 passed in 165.65 seconds in the final
  combined platform bundle; every configured check returned zero. The sanitized
  report is `platform-acceptance-report.json` (platform-only, not full acceptance).
- Real API/PostgreSQL/storage browser navigation, signin/signout/recovery and
  approval, launch, evidence, export, fresh reproduction and cancellation:
  **4 passed in 31.6 seconds**, desktop and mobile. Screenshots were inspected
  without overlap. The producer is an explicit protocol test double, not a
  business runtime.
- Root Ruff and web TypeScript passed. Latest production build passed with Next.js
  16.4.3 and all 16 routes, including evaluation/invitation/recovery.
- Actual loopback HTTP benchmark at 10,000 attempts and 20-case allocation at
  concurrency 1/3 passed. See the sanitized capacity report for p95/RSS/CPU/database
  size and limitations; PostgreSQL process RSS is not measured. No SLA is claimed.
- Native encrypted consistent backup/empty-database restore, deletion-journal and
  credential-revocation checks passed. English PDF export was rendered/inspected;
  complex-script typography is not qualified.
- Docker Linux engine is unavailable on this host. Compose/container execution
  remains unverified locally; native PostgreSQL 17.6 is used, not a database fake.
- No new customer runner/evaluator/provider execution has been accepted. The
  explicit protocol test client cannot satisfy Muaz's integration gates.
- Publication and exact commit/hosted CI state are recorded in `HANDOFF.md`.
  No main push, merge, production migration or deployment is authorized here.

## Investigation Notes

- A raw reproduction response initially exposed its private run manifest. It now
  uses the typed authorized projection; a held-out canary regression checks
  browser lists, results, events, exports and reproduction.
- Windows `chmod(0600)` does not protect a DACL. Generated secrets/objects now use
  explicit current-user/SYSTEM grants. An initial manual recursive ACL command
  removed inherited file permissions, interrupting the development database;
  only this workspace's generated `.platform` tree was repaired from its protected
  parent ACL and PostgreSQL recovered. All platform tests then passed. Never apply
  directory-only inheritance flags recursively to existing files.
- Official named MinIO image retrieval was unavailable; no unreviewed mirror was
  substituted. Filesystem/S3 behavior and the distribution deviation are explicit.
