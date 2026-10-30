# Susanoo Parallel Build

The active product specification is `docs/spec/Susanoo_Architecture_and_Build_Plan.md`.
`PRD.md` and the original implementation files describe the preserved hackathon product.
Current source and Git state determine what actually works.

## Ownership

Aarav owns `apps/api`, `apps/web`, `packages/contracts`, database migrations,
deployment/root tooling, platform tests, and `docs/parallel/aarav`.
Muaz owns the runtime/SDK/evaluator/simulator/connectors, `apps/worker`, examples,
templates and legacy core except `db.py`/`emit.py`. Legacy core schemas remain frozen.
Do not edit the other lane to satisfy an integration test. Canonical contracts have
one writer (Aarav); runtime review is required before calling them agreed.

## Git and Safety

Use genuine contributor identities and keep hooks enabled. Work on feature branches,
never push directly to auto-deploying main. The contributor policy addition on this
branch is a proposal for maintainer review. Do not merge, deploy, run production
migrations or call customer systems without explicit authorization.
Preserve unrelated changes and existing evidence. Never use an expired runner lease
as permission to repeat a started mutation. No customer code executes in the API.

## Evidence

Keep unit, real-service integration, hosted CI and live-provider results separate.
The platform must remain honest about missing runtime features. A contract fake is
not an accepted customer runner. Update `docs/implementation-status.md` and the
owner handoff after meaningful work. Do not write ContextHub memory unless registered.
