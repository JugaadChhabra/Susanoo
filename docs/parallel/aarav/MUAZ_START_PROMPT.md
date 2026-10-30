# Muaz Runtime Agent Prompt

Implement only Muaz's Susanoo lane against the attached/published
`docs/spec/Susanoo_Architecture_and_Build_Plan.md`. Read root `AGENTS.md`,
`docs/implementation-status.md`, `docs/parallel/aarav/HANDOFF.md`,
`CONTRACT_PROPOSAL.md` and `ACCEPTANCE.md` before coding. Current files/Git state
outrank older prompts.

Start in a separate clone/worktree on a feature branch from the exact platform
implementation commit in `HANDOFF.md`. Never share Aarav's dirty worktree. Preserve
the hackathon product. Do not push to or merge main, deploy, run production
migrations or call customer systems. Keep genuine identity hooks enabled.

You own runtime/SDK/evaluator/simulator/connectors, `apps/worker`, examples,
templates and legacy core except `db.py`/`emit.py`; frozen legacy schemas remain
unchanged. Aarav owns `apps/api`, `apps/web`, `packages/contracts`, migrations,
root lockfiles/tooling/deployment/CI and platform tests. Do not modify his lane to
make a test pass. Write proposed shared changes in your handoff and coordinate
them. No broad refactoring of preserved runtime code merely for style.

First review canonical protocol `1.0` and the digest in
`packages/contracts/schema/VERSION.json`. It is **not yet agreed**. Validate the
Python models, JSON/OpenAPI, generated examples and RFC 8785 hash vectors. Report
concrete incompatibilities to Aarav before creating divergent clients or schemas.
Keep hidden evaluator/manifests out of agent/fixer contexts. Propose typed reports
for simulator diff, impact/2x2, reduction, probing, incidents and media; the generic
inspector does not mean specialized views are accepted.

Implement the true local runner with persistent journal, bounded leases,
monotonic fencing, heartbeat/control, stable operation IDs, atomic budget
reservations and evidence/artifact uploads. Expired leases never authorize a
repeat write. Reconcile uncertain started actions through read-back and preserve
the original result. Enforce namespace/exclusive-resource isolation locally;
failed cleanup stays quarantined. No customer code executes in the API.

Complete the real HTTP/Python adapter, independent observer/checker and 20-case
collections example before advancing through simulator/fidelity/impact/reduction/
grader/repair, all I01-I14 families, source builds, passive ingestion, schedules,
collision/probing, semantic grading and media/telephony. Use established libraries
for protocol engines and respect approved data, secret and external-model grants.
Never claim correctness from agent text or fabricated observations.

Use actual platform API/PostgreSQL/object storage and shared contracts for
integration. The protocol clients in platform tests are deliberately test doubles;
do not reuse them as the shipped runtime or count them as acceptance. Request root
dependency/Compose changes from Aarav with exact package, image, entry-point,
health and test requirements. No executable provider helper may invoke customer
code in the API; media callbacks require pure bounded verification only.

Complete and record T01-T40 with matching services and crash/security fixtures.
Keep unit, real-service, hosted CI and live-provider evidence separate; missing
live credentials remain unverified. Update `docs/parallel/muaz` with status,
commands, evidence, failed approaches and unresolved Aarav requests. Leave an exact
commit handoff and never label the product ready while a required gate is missing.
ContextHub memory is allowed only after a registered bootstrap supplies a
project_id. Continue eligible owned work autonomously until completed or genuinely
blocked. Publish only the finished feature branch, never main.
