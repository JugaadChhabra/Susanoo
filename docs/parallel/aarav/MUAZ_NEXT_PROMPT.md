# Message for Muaz's Coding Agent

Fetch the current `codex/aarav-runtime-integration` branch from JugaadChhabra/Susanoo before editing.
It already contains all 27 commits from `codex/muaz-runtime` plus the new integration work.
Do not continue from stale `301c39d`, recreate the platform client, or overwrite either lane's work.
The integration review is draft PR #45, stacked on platform PR #44; neither is a release approval.
Preserve dirty files, use your genuine contributor identity, and leave all hooks enabled.

First read `AGENTS.md`, `docs/spec/Susanoo_Architecture_and_Build_Plan.md`,
`docs/implementation-status.md`, `docs/parallel/aarav/INTEGRATION_HANDOFF.md`,
and the final publication/CI record. Inspect actual source, branches and tests over historical claims.

Review the exact canonical contract digest and record agreement or concrete remaining change requests.
The SDK/evaluator now re-export the canonical definitions; do not restore competing schemas.
The former expected-failure checks now pass normally, but that does not replace your semantic review.

Prioritize the remaining runtime work: advanced platform job handlers and report upload; installed
connector/profile binding; conditional and non-text execution; true platform/runner crash, cancellation,
reconciliation and concurrency gates; and complete resource-limit enforcement. The outcome/preflight
platform client and the four previously documented runtime fixes already exist and have focused tests.

The Aarav-owned report persistence/views, callback registry/queue, OTLP/token handling, automatic-run
admission and exports/publication integration now exist. Read PLATFORM_COMPLETION.md and
RUNNER_OPERATIONS.md before proposing replacements. Integrate your actual advanced workers with them:

- Upload the matching discriminated DTO to POST /runner/attempts/{attempt_id}/reports under the live
  lease; retain the returned report ID in your durable journal before emitting job_result.
- Completed advanced jobs must reference assigned typed reports via report_ids, or repair report_id.
  Repair approval requires the exact typed evidence digest, canonical PatchBundle.validation_diff()
  digest, pinned base/finding/layer and every gate. Boolean summary flags alone cannot authorize it.
- Pinned connection.mapping.relay_integration_id enables live-lease task registration only after the
  correlated operation was dispatched. Journal the task registry ID. Poll using POST plus the existing
  lease token; an expired lease permits evidence polling only, never customer dispatch.
- Runtime export and reproduction helpers must emit a matching snapshot digest. Project reproduction
  bundles contain public cases/requirements only. The shared helper now withholds hidden-only requirements.
- Hosted held-out feedback has a monotonic per-project/split cap. Enforce the local pre-execution cap
  too; API upload refusal cannot erase a local exposure. Never rotate just the key to bypass the split cap.
- Keep unsupported capabilities unadvertised until actual handlers and end-to-end tests pass.

Do not silently edit canonical contracts in parallel; request explicit ownership/coordination when needed.

Re-run frozen-install Python, TypeScript, real PostgreSQL/browser and container checks. Use an isolated
Linux bytecode cache under WSL to avoid Windows .pyc filenames breaking inspect.getsource.
Keep unit tests, synthetic real-service integration, hosted CI and live-provider evidence separate.
Real endpoints/credentials and fluent Hindi/Marathi review remain external gates. Do not fabricate them.

Update the implementation ledger and your handoff, publish on a feature branch, and open a review PR.
Do not merge, deploy, run production migrations, call customer systems or make paid provider calls
without the owner's explicit authorization. Report remaining gates instead of claiming the entire PRD
is finished from local tests alone.
