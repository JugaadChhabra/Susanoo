# Platform Publication and Runtime Handoff

Prepared 2026-10-30 for Aarav and Muaz. This is a platform integration checkpoint,
**not a fully working/accepted business bot**. No new runtime/evaluator/simulator
has been substituted for Muaz's work.

## Exact Starting Point

- Repository: `https://github.com/JugaadChhabra/Susanoo` (public).
- Feature branch: `codex/aarav-platform`; no main merge or production deployment.
  The existing Vercel integration automatically creates frontend branch previews;
  those previews do not qualify the private backend or complete product.
- Preserved main baseline: `73224ee9133133663a7f4df6925b8afac209d88b`.
- Platform implementation commit: `a7b5fd67f789814e232e44fc30623661ab015ce2`.
  Later handoff/CI fixes are visible in branch history; start from its latest
  published head, not an older main checkout.
- Canonical protocol: `1.0`, 83 definitions, **pending Muaz review**.
- Canonical schema digest:
  `c774ab3513ac8b629da1b8bfd5ed79b81bbcfcd1c6cc4289a975c2b50b1b32ac`.
- Original supplied build-plan SHA-256:
  `E5818C33110752B75149A1FE7D9DD8E00BE1530076BD0472403B14E258AFDF64`.
  Publication was explicitly approved; the source file is preserved, including
  its trailing blank line. Original attached PDF and local secrets are excluded.
- Genuine repository-local author identity is AaravMehta-07 with his GitHub
  noreply address. The allowlist addition is a proposal for maintainer review;
  all identity/commit/push guards remained enabled. No impersonated coauthor.

In a separate clone/worktree for Muaz:

```bash
git fetch origin codex/aarav-platform
git switch -c codex/muaz-runtime origin/codex/aarav-platform
```

Use your own approved contributor identity. Read root `AGENTS.md`, the active
specification, [contract proposal](CONTRACT_PROPOSAL.md),
[evidence matrix](ACCEPTANCE.md) and [agent prompt](MUAZ_START_PROMPT.md).

## What Is Available

The `/evaluation` console and additive `/api/v1` API support private auth,
workspace/project grants, approved immutable criteria/configuration, pinned
launches, trusted-runner ingestion, evidence/findings, review/reproduction,
four matching exports, schedules/integrations and repair boundaries. The old
application is preserved. Seven private-schema migrations, generated contract
packet, private artifact storage, internal workers, native development, Compose,
backup/recovery and hosted CI are included.

No customer code runs in the API. The trusted runner receives the evaluator and
execution manifest; the agent receives only `agent_context`. Do not leak hidden
checks into prompts/fixer context. Uncertain writes are never automatically
redispatched; expired/exclusive resources remain quarantined until read-back.

## Required Work With Muaz

1. Review the exact shared schema/digest and examples. Agree lease/event/fencing,
   cancellation, evidence, source credentials, job-result and artifact semantics.
   Aarav remains the single writer for canonical contracts and shared root locks.
2. Implement the real journaled runner, HTTP/Python adapters, independent
   observer/checker, reference/simulator services and 20-case collections example.
3. Integrate the remaining I01-I14 adapters, acquisition/builds, fidelity, impact,
   reduction, grader/repair, passive ingestion, collision/probing and media clients.
4. Propose concrete typed advanced reports before calling the generic authorized
   inspector a finished simulator/2x2/reduction/probe/language-review experience.
   Aarav owns those web/API changes once producer contracts are reviewed.
5. Request exact package/lockfile, container image, health, entry-point and network
   integration changes. Do not edit the other lane to satisfy tests.
6. Run the matching T01-T40 matrix against actual services, including crash-point,
   hidden-data, tenant, secret, uncertain-write and controlled-clock lifecycle
   fixtures. Record live-provider evidence separately; missing credentials do not
   excuse fake success.

Muaz not starting yet does not invalidate independent platform work. Integration
changes are expected, so contracts remain a proposal. Keep separate branches and
coordinate the shared root/contract requests instead of concurrently editing them.

## Local Evidence

All runs below used the preserved code plus the implementation checkpoint:

| Check | Result |
|---|---|
| New platform/contract tests, real disposable PostgreSQL 17.6 | Latest hardening rerun: 59 passed, 203.50s |
| Preserved focused Python suites | 752 passed, 26 skipped, 130.88s |
| Web unit tests | 116 passed across 13 files |
| Root Ruff, web TypeScript/lint, browser TypeScript | Passed |
| Production Next.js 16.4.3 build | Passed; 16 routes |
| Real local API/DB/storage browser checks | 4 passed, 31.6s; desktop/mobile |
| Synthetic protocol allocation | 20 cases at concurrency 1 and 3 passed |
| 10,000-attempt real loopback HTTP benchmark | Latest p95 run status 159.95ms, case list 151.59ms |
| Encrypted native backup/empty-database restore | Passed; newer deletions/revocations, controlled-clock expiry and expired-restore refusal |
| English PDF and desktop/mobile screenshots | Rendered and visually inspected |

See [platform report](platform-acceptance-report.json) and
[capacity report](capacity-report.json). API-test process peak RSS was 135,036,928
bytes; database size 14,601,363 bytes. PostgreSQL process RSS was not measured.
The benchmark is not an SLA or a real customer-execution throughput result.

The browser producer is an **explicit protocol test double**. Passing local
checks does not prove business execution, runtime isolation or live providers.
The full acceptance command intentionally returns nonzero while that evidence is
absent. Unit, real-service platform, hosted CI and live results are separate.

Hosted checkpoint `22f90f5c03797415dc14ed9c3e33ecdda19fe238` passed
[expanded platform CI](https://github.com/JugaadChhabra/Susanoo/actions/runs/41055813937)
and [preserved CI](https://github.com/JugaadChhabra/Susanoo/actions/runs/41055814030).
That run included 59 platform tests, 116 web unit tests, four browser tests,
container startup/backup, and the full Linux Python suite (2086 passed, 96 skipped).
Subsequent backup-temp security and stronger lifecycle/fencing tests require the
latest exact-head checks on PR #44; the earlier run is not proof of a later head.
The new workflow runs real PostgreSQL,
browser checks, canonical drift and container build/start/backup checks. It does
not run a replacement customer runtime or deploy production.

The follow-up [GitHub failure audit](CI_AUDIT.md) records older red notifications
and a deterministic-clock correction for a legacy browser counter test. This is
a test-only hardening change; passing a rerun is not called a production fix or
full customer-runtime acceptance. Verify the follow-up head's checks separately.

## Running This Workspace

Current local URL: `http://localhost:3000/evaluation`.
Native PostgreSQL is isolated on loopback port 55432. Generated credentials are
in the ignored local `.platform/demo-credentials-*.json`; never publish them.
There is no registered customer runner or seeded business PASS in the demo.

```powershell
powershell -NoProfile -File scripts/platform-dev.ps1 status
powershell -NoProfile -File scripts/platform-dev.ps1 down
powershell -NoProfile -File scripts/platform-dev.ps1 up
```

The script handles only recorded owned development processes, not database/data.
For clean setup, recovery, credential rotation, backups and equivalent commands,
follow [OPERATIONS.md](OPERATIONS.md). Do not reinitialize an existing database.

## Release Limits

- Local Docker Linux engine initialization failed; native services were used.
  Do not reset or delete the user's Docker data. Hosted container startup/backup
  passed at the checkpoint above; actual deployment remains separately qualified.
- Named MinIO image retrieval was unavailable. Protected filesystem and explicit
  S3-compatible boundaries are supplied; no unreviewed mirror was substituted.
- Cloud storage policies, real SMTP delivery, target TLS/encryption, GitHub App
  publication, telephony/model providers and complete runtime are not live verified.
- Complex-script PDF shaping is unqualified. Three low-severity CLI dependency
  advisories remain; no forced major CLI downgrade was made.
- Advanced typed reports/specialized views, complete runtime restart and full
  T01-T40 acceptance remain open, explicitly mapped in `ACCEPTANCE.md`.
- Full native Windows legacy suite: 17 failed, 2001 passed, 164 skipped (469.66s).
  The failing files and runtime source are unchanged by this branch. See
  [Windows qualification](WINDOWS_LEGACY_TESTS.md); native legacy execution is not
  qualified merely because Linux CI passes.
- No production migrations, merge, production deployment or customer-system mutations were
  performed. `production-check` only checks configuration and never certifies the
  product. ContextHub workspace was unregistered, so no persistent entries were written.
