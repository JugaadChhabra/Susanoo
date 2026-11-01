# Muaz Lane Handoff

Updated 2 Nov 2026.

## Starting point

- Branch `codex/muaz-runtime`, now checked out in the main Susanoo checkout (the separate worktree was
  retired on 2 Nov at the owner's request). Created from `origin/codex/aarav-platform` @ `301c39d`.
- Reviewed contract: protocol `1.0`, digest `c774ab3513ac8b629da1b8bfd5ed79b81bbcfcd1c6cc4289a975c2b50b1b32ac`.
  No newer contract commit exists on `codex/aarav-platform`.
- Not pushed. No PR, merge, deployment, paid call or live provider call has been made.

## Lane status in one paragraph

Groups A-D runtime deliverables are implemented and tested locally (see `STATUS.md`): durable runner and
journal, evaluator and gate, collections slice, stateful simulator, fidelity/impact/reduction/repair (simulator
and agent layers), every I01-I14 intake family at the honest level the machine allows, CLI/CI commands,
imports, passive evidence, validity tracking, collision, probing, semantic grading and voice/media. What is
**not** done is integration: the runner has no platform client because C0 is not agreed, so nothing is
*integrated* or *live-verified*.

## Required platform action (Aarav)

1. **C0 requests** (`contract-requests/C0-review.md`), blocking the platform runner client:
   CR-01 typed event payloads and runtime enums; CR-02 verifiable manifest; CR-03 late events and cleanup on
   `/reconciliations`; CR-04 cleanup budget and dispatch after cancel ack; CR-07 UTC `Z` normalization;
   CR-08 `ErrorCode` enum; CR-05/06/09 adopt the evaluator DSL, verdict/gate module and adapter models.
   Each landed request flips a strict-xfail test in `tests/contract/runtime/test_c0_review.py`.
2. **Workspace and dependencies:** `DEPENDENCIES.md` D-1 to D-5.
3. **Callback endpoints you own; helpers I supply:**
   - Async webhooks: `susanoo_connectors.webhook.handle_callback` (HMAC verification, correlation, dedupe).
     Implement persistent, tenant-scoped `TaskRegistry` and `RelayQueue`.
   - Twilio: `susanoo_connectors.media.CallbackVerifier` (signature version `twilio-request-signature/1`;
     validate against the configured public URL, never the Host header).
   - Passive OTLP: mount `susanoo_runner.collector.CollectorReceiver`; store project tokens as digests.
4. **Repair publication:** consume `agent_repair.PublicationRequest` / `StaleProposal`; implement
   `BaseResolver` and `PublicationMarkerLookup`; call `reconcile_publication` before any retry; you own reviewer
   approval and draft-PR creation. Nothing in the runner pushes or opens PRs.
5. **Persist typed reports** (canonical equivalents needed): `ComparisonResult`, `PairOutcome`, `TwoByTwoCell`,
   `ReductionResult`, `RepairValidation`, `ProbePlan`/probe runs, `CollisionReport`, `Conclusion`/`ValidityMark`,
   `ImportBatch`, incident `DraftScenario`, `CapacityReport`, media `AudioMetricsReport`.
6. **Exports:** render PDF from `susanoo-runner export --format json`; build the reproduction ZIP from
   `reproduction.json`, including only cases it lists as not withheld.
7. **Scheduling:** unique index on the trigger dedupe key (project, trigger, version, suite version); call
   `dependencies.check_automatic_run` before each scheduled/webhook run and show its reasons.
8. **CI:** run `node --test packages/sdk-typescript/test/` (Node >= 22.18) and `tests/runtime`; container tests
   need Docker and skip without it.

## Service startup and health contracts

- Simulator: `susanoo-simulator --root /data --host 0.0.0.0 --port 8765`; `SUSANOO_SIMULATOR_ADMIN_KEY`;
  `GET /health`. See D-3.
- Runner: `susanoo-runner local|preflight|run|wait|reproduce|compare|export|recover|fidelity-demo`.
  Exit codes: 0 ready, 1 observed required failure, 2 operational/unknown/incomplete/cancelled, 3 invalid
  configuration/auth.
- Passive collector: ASGI app from `CollectorReceiver`, OTLP/HTTP protobuf or JSON.

## Unsupported variants (honest labels)

- MCP: tools only, header auth (no OAuth), no remote cancel. A2A: JSON-RPC binding of 0.3.0 only, no push
  notifications. gRPC: unary only, grpcio transport never run. Browser: real Playwright driver never run.
- CrewAI/AutoGen: README only, not claimed. n8n: mapping checked against a fake, no live n8n.
- Twilio: REST and signatures match documented vectors; no live call. Hosted media relay not built.
- Containers on Linux need `--add-host` or a routed simulator for tool access; a bridge network does not
  restrict egress.
- Multilingual voice cases are drafts awaiting fluent review.

## Unresolved defects

- The simulator reconciles at namespace scope, not per operation.
- The shared-ledger collision domain is not registered in the HTTP simulator service.
- Probe budget reservations cannot be released, so unused retry/cleanup allowance counts as spent.
- A semantic-grader timeout leaves its worker thread running until the provider returns.

## Live credentials and approvals still needed

Twilio account plus an approved test destination; a real MCP/A2A/gRPC agent endpoint; Playwright with a
customer test identity; a GitHub App installation for I01 metadata. None are needed for the synthetic suite.

## Next step

Build the platform runner client as soon as CR-01-CR-09 land, then the integrated M2/M3 gates with Aarav.
