# CR-09 Adapter-boundary models (proposal)

Follows `C0-review.md` CR-09. Spec §10 asks for concrete models in `packages/contracts` for every
type in the adapter protocols. The draft lives in `packages/sdk-python/susanoo_sdk/models.py` and
is exercised end to end by `tests/runtime/test_runner_local.py` and `test_jsonl_bridge.py`.

**Request:** land these classes in `susanoo_contracts` as protocol 1.1 additions (same field names
and constraints). Once landed, `susanoo_sdk.models` re-exports the canonical classes and the strict
xfail `test_cr09_adapter_boundary_models_exist` flips.

| Model | Purpose | Safety property it carries |
|---|---|---|
| `InvocationContext` | What the agent may know: attempt/run/case IDs, namespace, deadline, turn, `tool_route` | No expected outcomes, checks or fixture truth |
| `ToolRoute` | Approved environment route: base URL, namespace, token, operations | Token is a `SecretStr`; binding is enforced server-side, a prompt cannot widen it |
| `Message`, `SessionHandle` | One user turn; a fresh session per case | — |
| `AgentReply` | Text/structured reply, `usage`, `tool_events`, `truncated` | `usage` and `tool_events` are `Unavailable` unless reported, never fabricated |
| `Usage`, `Unavailable`, `ToolEvent` | Supporting types | — |
| `CancelResult` | Cancellation acknowledgement | A request, not proof that a remote action stopped |
| `CapabilityReport` | Adapter preflight result | Reuses canonical `Capabilities` |
| `FixtureContext` | Attempt ID, namespace, **fencing generation**, deadline | Fencing reaches the fixture/environment side |
| `FixtureHandle` | Namespace, seed digest, aliases, prepared time | Aliases stay on the evaluator side |
| `ObservationQuery` | Declared observation name, entity kind, window, baseline flag | Name must match `CheckSpec.observation` (CR-05) |
| `StepIntent` | Attempt/step/operation IDs, `ActionKind`, request digest, prepared time | Written before any possible mutation |
| `Reconciliation` | Read-back result: `resolved`/`unresolved`, `effect_present`/`effect_absent`/`indeterminate` | Late evidence never restores dispatch authority |
| `CleanupResult` | `succeeded`/`failed`/`not_required` + reason | Failed cleanup blocks the gate |
| `ApprovedAction`, `ActionContext`, `ActionResult` | Environment action, its context (with fencing), its result | `ActionResult.status` includes `uncertain` |
| `SnapshotScope`, `StateSnapshot` | Environment snapshot request and result | Snapshot carries a digest |
| `JsonlFrame` | `{protocol_version, request_id, type, payload}` | Closed `type` set; anything else is an adapter error |

Enums proposed alongside (also in CR-01): `ActionKind`, `DispatchState`.

The observation payload handed to the evaluator is `susanoo_evaluator.ObservedState`; its hosted,
redacted projection is the canonical `Observation` (see CR-05/CR-10 for the missing `name` and
`entity` fields).
