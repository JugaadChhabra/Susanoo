# C0 Runtime Review: protocol 1.0

Reviewer: Muaz (runtime/evaluator lane), 31 Oct 2026.
Reviewed: `packages/contracts` at branch `codex/aarav-platform` head `301c39d`, digest
`c774ab3513ac8b629da1b8bfd5ed79b81bbcfcd1c6cc4289a975c2b50b1b32ac` (83 definitions), plus the
server behaviour that gives the contract its meaning (`apps/api/susanoo_api/platform/service.py`,
`router.py`, `runner_io.py`, `jobs.py`).

**Verdict: not agreed yet.** The runner sequence, lease timing and evidence separation are
sound and I accept them (section A). Nine requests block the first real vertical slice (M2)
because the runner cannot implement them safely against the current shapes (section B).
The rest can land in a compatible 1.x revision (section C).

Each request names the safety implication and a test in
`tests/contract/runtime/test_c0_review.py`. The tests for open requests are `xfail(strict=True)`:
they flip to XPASS (and fail the suite) once the canonical package changes, which tells us to
remove the marker and record agreement.

Reproducibility check: regenerating `contracts.json` from `susanoo_contracts.models` matches the
checked-in file byte-for-byte after parsing, and its RFC 8785 hash equals `VERSION.json`.

---

## A. Accepted as proposed

- Heartbeat 10 s, lease ≤ 30 s capped by the deadline, control poll 2 s; control polling does
  not renew (`service.py:669-687`). Matches Spec §11 and §29.
- Prepare → reserve → dispatch ordering, duplicate-dispatch rejection per operation ID, and no
  dispatch or reservation after `cancel_acknowledged` (`service.py:746-777`, `router.py:666-670`).
- Event ingestion dedupe on `(attempt_id, event_id)` and `(attempt_id, sequence)` with payload-hash
  conflict, and `highest_contiguous_sequence` in the acknowledgement (`service.py:736-803`).
- Lease expiry → `interrupted` / `UNKNOWN` / `reconciliation_state=pending`, never requeued
  (`jobs.py:28-33`); reconciliation appends evidence without restoring dispatch authority
  (`runner_io.py:197-224`).
- `agent_context` separated from `evaluator_context` and `execution_manifest` in the claim.
- Namespace binding of observations, `local_only` never uploads values, upload-field allowlist
  intersected with the current policy (`service.py:821-833`).
- Finalize idempotent by content hash; a required FAIL cannot be relabelled; PASS requires every
  required check to have passed (`service.py:910-936`).
- RFC 8785 JCS + SHA-256 as the canonical hash, safe-integer domain, NaN/Infinity rejected.

---

## B. Blocking for M2

### CR-01 Typed event payloads

**Problem.** `RunnerEvent.payload` is `dict[str, Any]` (`models.py:455`). The only per-type rule is a
key allowlist inside the API (`service.py:694-733`), which is not part of the canonical package.
Values are untyped: `dispatch_state`, `side_effect_state`, cleanup `state` and `action_kind` accept
any value, so `{"dispatch_state": "whatever", "side_effect_state": 42}` is valid.

**Safety.** The journal's whole purpose is to record whether a mutation may have happened. If
`dispatch_state` has no agreed values, the server cannot tell "acknowledged" from "uncertain", and
the gate cannot block on unresolved side effects with confidence. A runner and API that disagree
on spelling silently lose that distinction.

**Request.** Make `RunnerEvent` a discriminated union on `type` with one payload model per type,
exported from `susanoo_contracts`:

| type | payload |
|---|---|
| `step_prepared` | `operation_id`, `request_digest: Digest`, `action_kind: ActionKind` |
| `step_dispatching` | `operation_id` |
| `step_response_recorded` | `operation_id`, `dispatch_state: DispatchState`, `side_effect_state: SideEffectState`, `response_artifact_id: UUID \| None` |
| `observation_recorded` | `observation_id` |
| `check_recorded` | `check_version_id` |
| `cleanup_recorded` | `state: CleanupState`, `reason_code: str \| None` |
| `reconciliation_recorded` | `state: Literal["resolved","unresolved"]`, `evidence_id: UUID` |
| `cancel_acknowledged` | empty |
| `progress` | `code: str` |
| `job_result` | discriminated by job kind (see CR-12), not one flat bag of 25 optional keys |

Enums:
- `ActionKind = agent_invoke | environment_action | fixture_prepare | observe | cleanup | reference_call`
- `DispatchState = acknowledged | rejected_before_effect | uncertain | not_dispatched`
- `SideEffectState = none_expected | requires_observation | confirmed_by_readback | unresolved`
- `CleanupState = succeeded | failed | not_required` (same as `Finalize.cleanup_state`)

**Test.** `test_cr01_step_response_payload_is_typed`.

### CR-02 The runner cannot verify what it was asked to run

**Problem.** The claim returns `manifest_hash` = hash of the full stored manifest
(`service.py:385`), but for `outcome_suite` the runner receives only the pinned case plus
`environment`, `limits`, `policy` and `timezone` (`service.py:639-642`). The connection version,
suite and request are dropped from `execution_manifest` (the connection reaches the runner only
inside `agent_context`). The runner cannot recompute `manifest_hash` from what it receives.

**Safety.** Spec §11 step 2 has the runner persist and check the manifest against its locally
approved profile before dispatch, and Spec §29 makes manifest hashes the basis for comparison and
reproduction. Today a wrong or tampered payload under a correct-looking hash is undetectable on
the runner, and the runner's journal cannot prove which approved versions it executed.

**Request.** Either (a) send a runner manifest that is exactly what was hashed, with hidden
material in a separately hashed evaluator section, or (b) add
`component_hashes: dict[Literal["suite","case","checks","fixture","environment","connection","policy","limits"], Digest]`
to `RunnerClaimResponse`, define `manifest_hash = content_hash(component_hashes)`, and include the
`connection` version record in `execution_manifest`. I prefer (b): the runner can verify every
component it receives without seeing other cases.

**Test.** `test_cr02_claim_carries_verifiable_component_hashes`.

### CR-03 Journal evidence is lost after lease expiry

**Problem.** Once a lease expires, every lease-bearing write is refused (`service.py:658-665`) and
the sweeper marks the attempt `interrupted` (`jobs.py:28-33`). The only remaining write is
`/reconciliations`, and `ReconciliationIn` carries observations only (`models.py:507-510`).

**Safety.** The case that matters most, a partition during an in-flight mutation, is exactly the
case where the runner holds unuploaded `step_prepared` / `step_dispatching` / `step_response_recorded`
events and a cleanup outcome. Those can never reach the server, so the stored record cannot say
which operations were potentially dispatched or whether cleanup ran. Spec §11 requires late
evidence from the original runner to be stored as reconciliation evidence.

**Request.** Extend `ReconciliationIn` with `late_events: list[RunnerEvent]` (stored as
reconciliation evidence, never as live events, and never re-enabling dispatch) and
`cleanup_state: CleanupState | None`. Keep `dispatch_authority: false` in the response.

**Test.** `test_cr03_reconciliation_carries_late_journal_and_cleanup`.

### CR-04 Cleanup is blocked by cancellation and has no budget

**Problem.** After `cancel_acknowledged`, both `step_dispatching` and budget reservations are
refused (`service.py:746-747`, `router.py:669-670`). `BudgetReservation.budget_kind` is
`agent | reference` only (`models.py:680`).

**Safety.** Spec §12 requires prepare/cleanup operations to be journaled with the same
idempotency and reconciliation semantics as agent writes, and Spec §21 requires budgets that
include cleanup. As specified, a cancelled attempt can only clean up off the journal, which is
the unsafe path, and leaves resources quarantined.

**Request.** Add `budget_kind: cleanup` with a reserve fixed at claim time (e.g. `limits.cleanup_reserve`),
and allow `step_dispatching` after cancel acknowledgement only for `action_kind in {cleanup, observe}`.

**Test.** `test_cr04_cleanup_budget_kind_exists`.

### CR-05 The check DSL is untyped

**Problem.** `CheckSpec.expected` is `Any` (`models.py:216`); there is no `filter` / fixture-alias
binding, no typed money or date, and no operand structure for `range`, `relationship`,
`transition` or `forbidden_change`. `expected=1999.99` is accepted for a money check.
`Observation` has no field naming which declared observation it satisfies, so the server cannot
check that a check result cites the right observation (`CheckSpec.observation` vs `Observation.source`).

**Safety.** Spec §13 requires a typed DSL, integer minor units with currency, explicit timezones and
validated alias binding (`"account_alias": "fixture.primary_account"`). An untyped `expected` lets a
float amount or a naive date through approval, and approval hashes are then computed over
something the evaluator cannot interpret deterministically.

**Request.** The DSL belongs to the evaluator lane (two-agent plan, "Evaluation" row). I will author
the typed operator models in `packages/evaluator` and send them as a schema proposal; Aarav lands
them in `CheckSpec` as a discriminated union on `operator` (`exists`, `equals`, `range`, `count`,
`relationship`, `transition`, `forbidden_change`, `unchanged`, `deadline`, `manual`, `semantic`),
with `filter: dict[str, AliasRef]`, `Money{amount_minor:int, currency}` and
`LocalDate{date, timezone}` operands. Also add `Observation.name` that must equal the
`CheckSpec.observation` of every check citing it.

**Test.** `test_cr05_check_spec_has_typed_filter`, `test_cr05_observation_names_its_declared_query`.

### CR-06 One verdict implementation, not two

**Problem.** The API re-implements evaluator semantics: the definitive-evidence rule in
`validate_evidence` (`service.py:860-875`), the case-verdict consistency checks in `finalize_attempt`
(`service.py:924-936`) and the run gate in `project_gate` (`service.py:504-521`). The two-agent plan
says there is one business-verdict implementation, the evaluator's pure module, and the platform
projects it.

**Safety.** Two copies drift. One concrete divergence already exists: with
`requires_complete_observation=true`, the server refuses a FAIL backed by a *partial* observation.
For monotone violations (a duplicate record exists, a forbidden change is present) a partial view
already proves the failure; refusing it pushes a real FAIL to UNKNOWN.

**Request.** I will ship `packages/evaluator` with pure functions `case_verdict(...)`,
`run_gate(...)` and `evidence_supports(check, observation, verdict)`, with property tests. The API
then calls them instead of its own copies. Until then the API's checks stay as a safety net, but
neither side should extend them independently.

**Test.** None at the contract level; covered by evaluator property tests once the module exists.

### CR-07 Same instant, different hash

**Problem.** Datetimes are hashed as Pydantic serializes them, preserving the offset:
`2026-10-30T00:00:00Z` and `2026-10-30T05:30:00+05:30` are the same instant but give different
event, observation and finalize hashes.

**Safety.** Event and finalize idempotency compare content hashes. A runner that reloads an event
from its SQLite journal after a restart and re-serializes it with a different offset or precision
gets `IDEMPOTENCY_CONFLICT` on a legitimate retry and may be forced to abandon the attempt.

**Request.** Normalize every wire datetime to UTC with a `Z` suffix and fixed microsecond precision
in a `WireModel` serializer, and add hash vectors for it.

**Test.** `test_cr07_same_instant_hashes_equal`.

### CR-08 Error codes are free text

**Problem.** `ErrorDetail.code` is `str` (`models.py:627`). The server uses codes outside the spec
list (`BUDGET_REQUIRED`, `INCOMPLETE_EVIDENCE`, `INVALID_STATE`, `PREFLIGHT_REQUIRED`, `NOT_FOUND`,
`UNAUTHENTICATED`).

**Safety.** The runner's recovery logic branches on them: `LEASE_EXPIRED` → stop dispatching and
reconcile; `IDEMPOTENCY_CONFLICT` → stop and audit; `BUDGET_EXCEEDED` → stop scheduling. An
unrecognized code must be treated as fatal, so an unlisted code makes the runner over-cautious, and
a renamed one makes it unsafe.

**Request.** Export an `ErrorCode` StrEnum covering every code the API returns and type
`ErrorDetail.code` with it.

**Test.** `test_cr08_error_codes_are_enumerated`.

### CR-09 Adapter-boundary models are missing

**Problem.** Spec §10 requires concrete models in `packages/contracts` for every type in the adapter
protocols. None exist: `CapabilityReport`, `SessionHandle`, `Message`, `AgentReply`, `CancelResult`,
`FixtureContext`, `FixtureHandle`, `ObservationQuery`, `StepIntent`, `Reconciliation`, `CleanupResult`,
`ApprovedAction`, `ActionContext`, `ActionResult`, `SnapshotScope`, `StateSnapshot`, and the JSONL frame
`{protocol_version, request_id, type, payload}`.

**Safety.** These are the boundary between the runner and customer agents in the Python and
TypeScript SDKs. Without canonical shapes I would have to invent them in my lane, which is the
competing schema the plan forbids. `ActionContext` also has to carry the attempt's
`fencing_generation`, which the claim issues but nothing forwards to the environment today.

**Request.** I will draft these models (fields per Spec §10 and §11, with `AgentReply` using
explicit `unavailable` markers instead of fabricated usage) in a follow-up request file. Aarav lands
them in `susanoo_contracts` as protocol 1.1 additions.

**Test.** `test_cr09_adapter_boundary_models_exist`.

---

## C. Non-blocking (compatible 1.x additions)

- **CR-10 Observation scope and digests.** Spec §29 has `scope: {namespace, entity}` and
  `consistency: {...}`; the model has only `namespace`, so an ambiguous scope (→ UNKNOWN) cannot be
  expressed. Add `entity` and `schema`. Define `digest` for `local_only` as `content_hash` of the raw
  local values so a later reproduction can verify them. Test: `test_cr10_observation_scope_has_entity`.
- **CR-11 Tool route in `AgentContext`.** Spec §10/§14: the agent context carries the approved tool
  route (router URL plus a run/arm token bound to namespace and operations). Add
  `tool_route: ToolRoute | None`. Test: `test_cr11_agent_context_has_tool_route`.
- **CR-12 Job kinds and capabilities.** `RunnerRegistration.capabilities` is used as a job-kind list
  (`service.py:575`) while `Capabilities` means adapter capabilities. Rename to
  `supported_job_kinds: list[JobKind]` and export `JobKind` (including `outcome_suite`, which is
  missing from `JobCreate.kind`). Add `observer_consistency` and `retry_semantics` to `Capabilities`
  (Spec §4). Make `job_result` a per-kind union. Test: `test_cr12_capabilities_record_consistency_and_retry`.
- **CR-13 Fixture digest binding.** `FixtureSpec.digest` is not checked against the recipe (any 64-hex
  string passes). Validate `digest == content_hash({adapter, recipe})` and declare fixture aliases
  explicitly. Test: `test_cr13_fixture_digest_is_bound_to_recipe`.
- **CR-14 Finalize references.** Spec §29 lists observation/check references and missing-evidence
  reasons in finalization. Add `check_version_ids`, `observation_ids` and
  `missing_evidence: list[{kind, reason_code}]`.
- **CR-15 Limits.** Add storage bytes and cleanup reserve to `RunLimits` (Spec §12), and enforce the
  1 MiB event-batch cap in the model or at the route (Spec §29).
- **CR-16 Hash vectors for the TypeScript SDK.** Two vectors are not enough for a second
  implementation. Add vectors for non-ASCII text, control-character escapes, `-0`, `1e21`, `0.1`,
  nested arrays/objects, and a datetime after CR-07.
- **CR-17 Structured script branches.** `ScriptStep.when` only matches reply text; Spec §12 allows
  typed branches on allowed observable reply fields. Add an optional `field` path.
- **CR-18 Manual checks.** `Verdict` has no pending state; document that a pending `manual` check is
  reported as `UNKNOWN` with `reason_code=manual_pending`, and add `reason_code` to `CheckResult`.

---

## D. What I am doing meanwhile

Per the plan, I will not build HTTP clients against the shapes above until CR-01 to CR-09 are
resolved. Independent work that does not depend on them:

1. `packages/evaluator`: typed DSL draft (feeds CR-05) and pure verdict/gate functions with
   property tests (feeds CR-06).
2. `packages/simulator` + `examples/collections`: deterministic collections backend with the
   correct, false-completion, wrong-amount, duplicate, forbidden-change, handoff and
   timeout-after-write modes, plus the 20-case fixture set.
3. `packages/runner`: SQLite WAL journal and the dispatch state machine behind an interface, with
   crash-window tests, not yet wired to HTTP.
4. CR-09 draft models for the adapter boundary.
