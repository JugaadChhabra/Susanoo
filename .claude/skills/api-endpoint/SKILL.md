---
name: api-endpoint
description: Add or change a FastAPI route in apps/api for Susanoo with the right owner, auth and quota dependencies, locked schemas, RLS-safe database access and tests. Use for any work in apps/api/susanoo_api/.
---

# FastAPI endpoint

Sources: **[PRD › Frontend (marketing site + app) and API]** (API table), **[PRD › Intake]**, **[PRD › Non-technical users › Extra API]**, **[PRD › Connectors and API budget]**, **[PRD › Data model]**, **[PRD › Team and ownership]**.

## 1. Check it's in the PRD and whose it is
| Router | Owner | Paths |
|---|---|---|
| `projects.py` | Jugaad | `POST /projects`, project list, `PUT /projects/{id}/rules` |
| `connectors.py` | Jugaad | `GET /connectors`, `POST /connections`, `POST /connections/{id}/test`, `DELETE /connections/{id}` |
| `demo.py` | Jugaad | `GET /demo/{run_id}` (no auth) |
| `runs.py` | Muaaz | `POST /projects/{id}/runs`, `GET /runs/{id}`, `POST /runs/{id}/cancel` |
| `events.py` | Muaaz | `GET /runs/{id}/events` (SSE) |
| `intake.py` | Muaaz | `GET /runs/{id}/intake`, `POST …/intake/answers`, `POST …/intake/scenarios` |
| `approvals.py` | Muaaz | `POST /runs/{id}/approvals/{patch_id}` |
| `feedback.py` | Muaaz | `POST /runs/{id}/feedback` |
| `agent.py` | Muaaz | `GET /runs/{id}/agent-card`, `GET /runs/{id}/report-card` |

A path that isn't in this table isn't in the PRD, so ask before adding it. Don't edit the other owner's router unless the human asks.

## 2. Shape
```python
from fastapi import APIRouter, Depends
from susanoo_api.deps import current_user, db_session          # Jugaad
from susanoo_api.quota import enforce_run_quota                 # Jugaad
from susanoo.schemas.result import IterationMetrics             # locked contracts

router = APIRouter(prefix="/runs", tags=["runs"])

@router.get("/{run_id}", response_model=RunStatus)
async def get_run(run_id: str, user=Depends(current_user), db=Depends(db_session)):
    run = await db.get_run_for_user(run_id, user.id)   # always scope to the user
    if not run: raise HTTPException(404, "Run not found")
    return run
```
- **Auth:** every route depends on `current_user`, except `/health` and `/demo/*`.
- **Quota:** `POST /projects/{id}/runs` must depend on `enforce_run_quota` (3 runs per user per day plus the global daily cap) and return 429 with a plain-English message.
- **Models:** request and response models come from `packages/core/susanoo/schemas/` or are thin wrappers around them. Never return raw dicts to the frontend.
- **Data access:** scope every query to the user. RLS is a backstop, not the only guard.
- **Secrets:** never return or log credentials. Connection responses show status only.
- **Errors:** a consistent JSON error with a plain-English `message`.
- **SSE** (`events.py`): stream `RunEvent` rows in `seq` order. Support resuming from `Last-Event-ID` or `?after_seq=`, send keep-alive pings, and keep Caddy from buffering.

## 3. Tests (`tests/api/`)
- 401 without auth (except the public routes)
- 404 or 403 when touching another user's resource
- the happy path, validated against the response model
- 429 at quota, for run creation
- no secrets in any response body

## 4. Finish
Run `make test`, then commit with the `commit-and-push` skill: `feat(api): …`, citing the PRD section.
