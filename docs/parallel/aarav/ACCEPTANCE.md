# T01-T40 Evidence Matrix

Updated 2026-10-30. **None of these rows is declared complete shared product
acceptance.** Platform evidence uses actual PostgreSQL/storage/HTTP, but declared
observations and reports come from explicit protocol test doubles. Muaz's real
runtime, consumer conformance and matching end-to-end evidence are pending.

Test references are under `tests/platform`, `tests/contract/platform` and
`tests/e2e/platform`. Provider calls in focused boundary tests are mocked, not live.

| Gate | Current platform evidence | Required remaining evidence |
|---|---|---|
| T01 | Pinned lineage, evidence digest and complete-scope result ingestion | Independent real observer/checker verifies correct business outcome |
| T02 | Reported failure cannot become ready; browser displays BLOCKED | Actual false-completion agent and healthy empty backend after full window |
| T03 | Typed checks/results, separate findings and required counts | Wrong account/amount/date, duplicate and forbidden-change checker fixtures |
| T04 | Incomplete/unavailable evidence cannot justify definitive PASS | Observer outage and partial-scope runtime integration |
| T05 | Expired attempts never redispatched; reconciliation is append-only | Timeout-after-write read-back with no duplicate real write |
| T06 | Started-lease expiry interrupts, preserves events and quarantine | Real journal crashes at each send/upload boundary and restart |
| T07 | Launch/event/finalization idempotency and conflicting-payload tests | Matching SDK retries against real runner |
| T08 | 20 protocol cases allocated at concurrency 1 and 3 with distinct namespaces | Persistent multi-turn sessions and locally enforced business fixture isolation |
| T09 | Cancel control/ack rejects later dispatch; desktop/mobile cancellation | Runtime acknowledgement timing and in-flight real outcome retention |
| T10 | Expired exclusive resource stays quarantined; evidence-free release rejected; fencing persisted | Failed cleanup and successful independent read-back/release against actual fixture |
| T11 | Separate approvals, draft and zero-required launch rejection | Runtime consuming exactly the approved pinned manifest |
| T12 | Held-out canary absent from engineer browser/API/result/event/export/reproduction; agent_context separated | Hidden data absent from actual model/fixer prompts, local traces and SDK uploads |
| T13 | Cross-tenant object/list/artifact/claim/export/callback authorization tests | Full connector/provider tenant routing |
| T14 | Safe API errors, bounded metadata, encrypted credentials, policy redaction | Seeded-secret scan across real runner/provider logs, media and patch context |
| T15 | Conservative exact compatibility, explicit incomparable and transition tests | Runtime datasets and complete regression fixture table |
| T16 | Waiver expiry changes acceptance only; original machine gate unchanged | Joint UI/runtime acceptance record review |
| T17 | PDF/JSON/JUnit/ZIP use one authorized snapshot; hash checks and English PDF render QA | Real runtime reproduction bundle in a new fixture; complex-script typography if required |
| T18 | Immediate revocation/deletion, retention receipts, staged sweep, encrypted restore reapplies current journal | Controlled-clock backup expiration qualification and target storage lifecycle/restore drill |
| T19 | Pinned source approval, encrypted read-only App grants and queued revocation | Real fetch/build, archive traversal, missing runtime/secret, local source adapters |
| T20 | Signed callbacks, replay/fork/installation/repository boundaries and uninstall routing | Authorized live GitHub App installation/webhook execution |
| T21 | I01-I14 schemas and conservative capability/preflight boundary | Conformance for every adapter family, including evidence-only sources |
| T22 | Deduplicated callback/event ingestion and no lease redispatch | Actual async/stream disconnect, late callback and partial completion fixtures |
| T23 | Typed protocol/version/capability configuration | MCP tool-only refusal and real A2A/gRPC version mismatch |
| T24 | Private artifact/policy boundary and unknown states | Isolated browser adapter, cookie/screenshot redaction and backend outage |
| T25 | Simulator profile/revision/job orchestration | Stateful reset/snapshot/clock/permission/idempotency/fault services |
| T26 | Pinned environment/fidelity profiles | Initial-state/permission mismatch rejection and normalization fixtures |
| T27 | Authorized report inspection and discrepancy workflow boundary | Seeded simulator defect versus approved simplification classification |
| T28 | Pinned task-impact job envelope and budgets | Fresh task-impact pairs versus replay; unavailable routing disables claims |
| T29 | Reproduction/reduction dispatch and limits | Prerequisite-preserving reduction with identical violation |
| T30 | Calibration control/version hashes and typed report counts | Actual grader controls and valid-alternative/error cases |
| T31 | Exact patch/base, separate reviewer, controls/held-out validation envelope | Real retest of original failure/control/held-out outcomes and weakening detection |
| T32 | Publish grant, patch bounds, protected paths, stale-base and duplicate-publication tests | Authorized live draft PR with matching real validation; never auto-merge |
| T33 | Dependency invalidation, pinned schedules, deduplication, budgets and signed triggers | Actual CI/schedule runtime execution and result-validity fixtures |
| T34 | Bounded artifact/import envelope; draft revisions stay unapproved | Passive nonblocking redacted collector and incident-to-draft implementation |
| T35 | Collision profile and bounded job orchestration | Shared-state duplicate/lost-update engine versus isolated suites |
| T36 | Shared atomic reservation/caps and probe profile | Equal-budget baseline/priority search with concurrent real engine requests |
| T37 | Media consent, private artifacts/playback and signed-verifier extension boundary | Partial audio/timestamps/cancel/call uncertainty and transcript-only metric refusal |
| T38 | Calibration, external-data grant and pinned semantic grader/control references | Calibrated judge/error thresholds and provider behavior |
| T39 | Repeat migrations, default-deny RLS, native encrypted backup/restore and revocations | Target clean container install, complete stack and real runner restart |
| T40 | Actual HTTP p95/RSS/CPU/database size at 10,000 attempts; 20-case allocation at 1/3 | Runtime concurrency resource measurements and target deployment repetition |

## Commands and Artifacts

`uv run python scripts/platform_acceptance.py --platform-only` runs owned checks
and requires configured real browser execution. Without browser credentials it
returns 2, not success. Default/full mode also returns 2 while Muaz acceptance is
unavailable, even if every platform check passes. Missing gates are not silently
promoted to product acceptance.

Local reports are `.platform/acceptance-report.json` and
`.platform/capacity-report.json`; sanitized copies accompany this handoff.
Hosted CI is separate and must be checked at the exact published head. The
configuration-only `production-check` never claims product qualification.
