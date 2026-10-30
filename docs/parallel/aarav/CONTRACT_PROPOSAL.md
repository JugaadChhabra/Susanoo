# C0 Protocol Proposal

Status: implemented and platform-tested; **not agreed with Muaz**. Protocol `1.0`
is a proposal until the runtime consumer review and shared conformance checks pass.
The canonical digest is in `packages/contracts/schema/VERSION.json`; the publication
commit is recorded in `HANDOFF.md`. Do not independently fork these schemas.

## Canonical Package

- Python: `from susanoo_contracts import ...`, workspace package `packages/contracts`.
- JSON Schema: `packages/contracts/schema/contracts.json`.
- OpenAPI: `packages/contracts/schema/openapi.json`, mounted beneath `/api/v1`.
- Generated web types: `apps/web/types/platform-generated.ts`.
- Generation: `uv run python scripts/export_platform_contracts.py`.
- Hashing: SHA-256 of RFC 8785 JCS UTF-8 bytes via `content_hash` / `canonical_json`.
  JSON integers must fit the interoperable safe-integer domain; non-finite numbers
  and lone surrogates are rejected. Hash the normalized canonical model, not a
  pretty-printed wire string. Decimal money uses integer minor units.

## Runner Sequence

1. An owner issues a one-use registration token. Register with protocol/version and
   actual supported job kinds; receive a 24-hour scoped runner credential. All
   runner calls use `Authorization: Bearer ...`, never browser cookies.
2. Poll `/runner/claims`. HTTP 204 means no eligible work. A successful response is
   `RunnerClaimResponse`: attempt/run IDs, fresh namespace, opaque lease token,
   monotonic fencing generation, deadline, lease expiry and the pinned contexts.
3. The heartbeat interval is 10 seconds, lease lifetime at most 30 seconds and
   control polling target 2 seconds. Renew via the heartbeat endpoint; polling
   control alone does not renew. Deadlines are hard upper bounds. Idle claims
   update runner liveness even if there is no work.
4. Pass **only** `agent_context` to the agent-facing driver. The evaluator context
   and execution manifest can contain hidden expectations/control cases. Keep them
   in the trusted runner/evaluator process, never model prompts or fixer context.
5. Reserve each agent/reference operation against the shared run allowance. Store
   a stable step/operation ID and request digest in the local journal, append
   `step_prepared`, then `step_dispatching`. The server checks prepare ordering,
   reservation ownership and duplicate dispatches. Its receipt is not proof that
   the customer operation completed.
6. Record observations and evaluator-produced `CheckResult`s. Hosted values must
   match their digest and approved field allowlist. Local-only observations upload
   only availability/completeness/provenance/digests, not raw values. Definitive
   checks need independent, complete, elapsed-window evidence where required.
7. Upload artifacts through attempt-bound reserve / verified content / download
   routes. Uploads use `X-Lease-Token`, not URL query credentials. Media requires a
   pinned approved voice revision with recording consent and hosted permission;
   source/patch bytes need the separate source-upload data-policy grant.
8. Finalize only after the event sequence is contiguous. Finalization is
   idempotent by exact content hash. Business verdict, execution state, cleanup,
   reconciliation and run gate remain separate. The API projects declared evidence;
   it does not implement the runtime's business checker.

Expired or settled attempts cannot regain dispatch authority. An interrupted write
is never requeued automatically. Append late read-back observations through
`/runner/attempts/{id}/reconciliations`; original results remain unchanged. Exclusive
resource leases stay quarantined until a separate reviewer verifies complete
read-back evidence and explicitly releases the resource. A fresh reproduction is
still required. Namespaced fixtures must enforce the namespace locally as well.

## Jobs and Provider Boundaries

Supported control-plane job kinds are the `JobCreate.kind` enum. The manifest pins
root revisions and their bounded transitive dependencies, approved policy, limits,
input artifact records and any exact repair context. Browser responses never expose
the private execution manifest. Input artifacts are downloaded only by the assigned
attempt. Source acquisition/build/normalization/evaluation algorithms remain Muaz's.

`job_result` is a bounded metadata envelope, not a transcript upload channel.
Preflight must report separate boolean invocation/observer/cleanup results. Parallel
launches additionally need reported isolation verification and tested concurrency.
Calibration reports typed control counts and the exact control-set hash. Repair
validation identifies the exact patch artifact/base and reports reproduction,
controls, held-out checks and unchanged criteria. Put richer policy-approved reports
in verified private artifacts; request canonical typed report additions before
building incompatible producers or UI consumers.

GitHub source credentials, when configured, are read-only App tokens scoped to the
approved repository and assigned source revision. They are encrypted at rest and
queued for revocation on completion, expiry, explicit release or installation
revocation. They never go to the browser. GitHub's own expiry bounds uncertainty if
revocation is unavailable. Source fetching still belongs on the authorized runner.

GitHub App webhooks use `/api/v1/callbacks/github/app` and
`PLATFORM_GITHUB_WEBHOOK_SECRET`. Per-integration signed relay routes remain
`/api/v1/callbacks/{integration_id}`. The App route verifies before routing and
handles installation deletion without requiring a repository field. Media signature
helpers are supplied by the runtime as the `susanoo.callback_verifiers` entry-point
group, entry name `media`; the helper must be pure bounded verification/normalization,
not a provider call or customer-agent invocation.

## Review Requests for Muaz

- Consume the same commit and canonical digest before implementing network clients.
- Confirm local journal ordering, cancellation acknowledgement, lease/fencing and
  late reconciliation semantics against real crash points and side effects.
- Supply actual adapter capability/preflight evidence, not self-declared success.
- Supply the local runner, evaluator, 20-case collections fixtures, simulator,
  reference backend and example agent; these are deliberately not replaced here.
- Add deterministic and semantic evaluator/control conformance, I01-I14 protocol
  conformance, source acquisition/build and advanced/media service tests.
- Propose concrete typed simulator diff, impact, reduction, probe, incident and
  media result/report additions against this package. The current generic inspector
  can inspect authorized records/artifacts, but it is not final acceptance of those
  domain-specific runtime views.
- Provide package dependency and container image/health/entry-point requests. Aarav
  updates root locks and Compose integration; do not edit shared locks concurrently.

Use a separate clone/worktree and your own branch. Do not edit Aarav-owned contracts,
migrations, API/web, root locks or CI. Record requested changes in your own handoff
and coordinate them through Aarav. No agreement is inferred from a passing test double.
