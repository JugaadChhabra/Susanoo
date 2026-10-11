---
name: contract-change
description: Change or extend Susanoo's frozen contracts (Pydantic schemas in packages/core/susanoo/schemas/, the RunEvent shape, the head interface, the scoring rule). Use for any edit under schemas/ or anything that changes data crossing the AI/platform/frontend boundary.
---

# Contract change

The contracts in **[PRD › Contracts (locked)]** are where Muaaz's AI half and Jugaad's frontend and platform half meet. They're frozen: change them only when both owners agree.

## Before touching `schemas/`
1. Read **[PRD › Contracts (locked)]** with the `prd-lookup` skill.
2. If your task can be done **without** changing a shape, do that instead. Add optional fields rather than renaming or removing.
3. **Ask the human to confirm with the other owner** before a breaking change. Breaking means renaming or removing a field, changing a type, or changing an enum value. An additive optional field still needs a heads-up.

## Making the change
1. Edit the model in `packages/core/susanoo/schemas/`. Keep Pydantic v2, type hints, and comments that match the PRD.
2. Regenerate the frontend types: `make types`, which rewrites `apps/web/types/generated.ts`. Commit the generated file in the **same commit**.
3. Update the contract tests (`tests/contracts/`) and the fixture payloads that the frontend mock stream uses.
4. Search for every producer and consumer: `grep -rn "<ModelName>" apps packages`. Update them in the same change or say clearly what's left.
5. Run `make test` and `make lint` (the TypeScript check catches frontend breakage).

## The PRD must change too
A contract change is a scope change. Tell the human exactly what to update in the live PRD's Contracts section, so they can edit it and re-export `PRD.md` and `PRD.pdf`. Never edit `PRD.md` yourself.

## Commit
`refactor(contracts): …` or `feat(contracts): …`, with the reason and both owners' agreement noted in the body (the `commit-and-push` skill).

## Special cases
- **Scoring rule:** never change the matching tolerance, false-positive rules or metrics to make results look better. Only change it if the human says the PRD changed.
- **`RunEvent`:** new `type` values need the frontend's `HeadsGraph` and `EventLog` to handle them, so tell Jugaad.
