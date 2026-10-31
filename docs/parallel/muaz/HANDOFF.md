# Muaz Lane Handoff

Updated 1 Nov 2026.

## Starting point

- Branch `codex/muaz-runtime` in its own worktree, created from `origin/codex/aarav-platform` @ `301c39d`.
- Reviewed contract: protocol `1.0`, digest `c774ab3513ac8b629da1b8bfd5ed79b81bbcfcd1c6cc4289a975c2b50b1b32ac`.
- Not pushed. No PR, merge, deployment or live call has been made.

## Required platform action (Aarav)

Read `contract-requests/C0-review.md`. In priority order, the requests that block the first vertical slice:

1. **CR-01** typed per-event payloads and the `ActionKind` / `DispatchState` / `SideEffectState` / `CleanupState` enums.
2. **CR-02** a manifest the runner can verify (component hashes; include the connection version).
3. **CR-03** late journal events and cleanup state on `/reconciliations` after lease expiry.
4. **CR-04** cleanup budget and cleanup/observe dispatch after cancel acknowledgement.
5. **CR-07** UTC `Z` datetime normalization before hashing.
6. **CR-08** an `ErrorCode` enum covering every code the API returns.
7. **CR-05 / CR-06 / CR-09** need models from me first (typed DSL, pure verdict/gate module,
   adapter-boundary models). I will send them as follow-up request files; please land them in
   `susanoo_contracts` and switch `project_gate` / `validate_evidence` / `finalize_attempt` to the
   evaluator functions.

When a request lands, the matching strict-xfail test in `tests/contract/runtime/test_c0_review.py`
will XPASS and fail; I remove the marker and record the agreed digest and commit here.

## Ready for the platform to consume

- **CR-06:** `susanoo_evaluator.run_gate`, `case_verdict`, `gate_blockers` and `evidence_supports`
  are the single verdict implementation. Please call them from `project_gate`,
  `validate_evidence` and `finalize_attempt` once the package is in the workspace (D-1).
- **CR-05:** the typed DSL is `susanoo_evaluator.dsl.Check` (`CHECK_ADAPTER.json_schema()` gives
  the JSON Schema). Proposed as the `CheckSpec` replacement.
- **CR-09:** draft adapter models in `susanoo_sdk.models`; see `contract-requests/CR-09-adapter-models.md`.

## Dependency requests

See `DEPENDENCIES.md`: D-1 (add four packages to the uv workspace; no new third-party deps),
D-2 (make targets), D-3 (simulator container, later).

## Unresolved defects

- The simulator reconciles at namespace scope: an uncertain step is resolved against all committed
  changes in the attempt's namespace, not one specific operation. Fine for one-mutation cases; a
  per-operation correlation needs agent-supplied operation IDs on tool calls.
- Local mode only: the runner has no platform client yet (blocked on C0).

## Typed reports for the platform to persist (request)

Group B produces these pydantic models; please add canonical equivalents so the platform can store
and show them instead of the generic inspector: `ComparisonResult` (+ `Candidate`,
`StepComparison`, `StateDifference`), `PairOutcome` (+ `Intervention`), `TwoByTwoCell`,
`ReductionResult` (+ `ReductionAttempt`) and `RepairValidation` (+ `GateCheck`). Owner
classification of candidates (`confirmed_defect`, `intentional_simplification`,
`reference_limitation`, `unresolved`) stays on the platform side.

## Next step

Agent-layer repair in isolated worktrees (reusing the legacy diagnosis/fixer/verifier), then the
remaining intake families (I07 TypeScript SDK, I08 container, I09 MCP, I10 A2A/gRPC, I11 browser,
I13 imports, I14 recipes) and Group D. The platform client starts as soon as CR-01-CR-09 land.
