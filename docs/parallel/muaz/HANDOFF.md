# Muaz Lane Handoff

Updated 31 Oct 2026.

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

## Dependency requests

None yet.

## Unresolved defects

None in this lane yet (no runtime code written).

## Next step

Independent groundwork that doesn't need the contested shapes: evaluator DSL draft and pure
verdict/gate functions, the collections simulator and 20 fixtures, and the runner journal state
machine with crash-window tests.
