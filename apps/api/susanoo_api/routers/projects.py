"""Projects: create from a spec, list with their runs, edit rules as sentences.

[PRD › Frontend (marketing site + app) and API], [PRD › Non-technical users › Extra API]
The API connects as the service role, so every query here is scoped to the caller; RLS is the backstop.
"""

import logging
from typing import Annotated, Any, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from susanoo.db import session
from susanoo_api.deps import CurrentUserDep
from susanoo_api.models import NewProject, ProjectRules, ProjectSummary, RunSummary

router = APIRouter(prefix="/projects", tags=["projects"])

logger = logging.getLogger("susanoo_api.projects")

_NOT_FOUND = "We couldn't find that project."
_UNAVAILABLE = "Your projects are unavailable right now. Please try again in a minute."
_NAME_LENGTH = 60


def name_from_spec(spec: str) -> str:
    """A short project name: the spec's first line, cut at its first sentence, at most 60 characters."""
    first_line = next((line.strip() for line in spec.splitlines() if line.strip()), "Untitled project")
    end = first_line.find(". ")
    sentence = first_line[: end + 1] if end != -1 else first_line
    if len(sentence) <= _NAME_LENGTH:
        return sentence
    return sentence[:_NAME_LENGTH].rstrip() + "…"


class ProjectStore(Protocol):
    async def list_for_user(self, user_id: str) -> list[ProjectSummary]: ...
    async def create(self, user_id: str, name: str, spec: str, rules: list[str]) -> ProjectSummary: ...
    async def set_rules(self, user_id: str, project_id: str, rules: list[str]) -> ProjectSummary | None: ...


_SELECT = """
    select p.id::text, p.name, p.spec, p.rules, p.repo_url,
           coalesce(
               json_agg(
                   json_build_object(
                       'id', r.id::text, 'status', r.status, 'heldout_f1', it.heldout_f1,
                       'cost_usd', r.cost_usd, 'cost_cap_usd', r.cost_cap_usd, 'created_at', r.created_at::text
                   ) order by r.created_at desc
               ) filter (where r.id is not null),
               '[]'
           ) as runs
    from public.projects p
    left join public.runs r on r.project_id = p.id
    left join lateral (
        select i.heldout_f1 from public.iterations i where i.run_id = r.id order by i.n desc limit 1
    ) it on true
    where p.user_id = %(user)s {extra}
    group by p.id
    order by p.created_at desc
"""


def _project(row: dict[str, Any]) -> ProjectSummary:
    return ProjectSummary(**{**row, "runs": [RunSummary(**run) for run in row["runs"]]})


class PostgresProjectStore:
    async def list_for_user(self, user_id: str) -> list[ProjectSummary]:
        async with session() as conn:
            cur = await conn.execute(_SELECT.format(extra=""), {"user": user_id})
            return [_project(row) for row in await cur.fetchall()]

    async def create(self, user_id: str, name: str, spec: str, rules: list[str]) -> ProjectSummary:
        async with session() as conn:
            cur = await conn.execute(
                """
                insert into public.projects (user_id, name, spec, rules) values (%s, %s, %s, %s)
                returning id::text, name, spec, rules, repo_url
                """,
                (user_id, name, spec, rules),
            )
            row = await cur.fetchone()
            assert row is not None
            return ProjectSummary(**row, runs=[])

    async def set_rules(self, user_id: str, project_id: str, rules: list[str]) -> ProjectSummary | None:
        async with session() as conn:
            cur = await conn.execute(
                "update public.projects set rules = %s where id = %s and user_id = %s",
                (rules, project_id, user_id),
            )
            if cur.rowcount == 0:
                return None
            cur = await conn.execute(
                _SELECT.format(extra="and p.id = %(project)s"), {"user": user_id, "project": project_id}
            )
            row = await cur.fetchone()
            return _project(row) if row else None


def project_store() -> ProjectStore:
    """Tests override this dependency with an in-memory store."""
    return PostgresProjectStore()


StoreDep = Annotated[ProjectStore, Depends(project_store)]


def _unavailable(action: str, user_id: str) -> HTTPException:
    logger.exception("Could not %s for user %s", action, user_id)
    return HTTPException(503, _UNAVAILABLE)


@router.get("", response_model=list[ProjectSummary])
async def list_projects(user: CurrentUserDep, store: StoreDep) -> list[ProjectSummary]:
    try:
        return await store.list_for_user(user.id)
    except Exception:
        raise _unavailable("list projects", user.id) from None


@router.post("", response_model=ProjectSummary, status_code=201)
async def create_project(body: NewProject, user: CurrentUserDep, store: StoreDep) -> ProjectSummary:
    try:
        return await store.create(user.id, name_from_spec(body.spec), body.spec, body.rules)
    except Exception:
        raise _unavailable("create a project", user.id) from None


@router.put("/{project_id}/rules", response_model=ProjectSummary)
async def update_rules(project_id: str, body: ProjectRules, user: CurrentUserDep, store: StoreDep) -> ProjectSummary:
    try:
        canonical = str(UUID(project_id))
    except ValueError:
        raise HTTPException(404, _NOT_FOUND) from None
    try:
        project = await store.set_rules(user.id, canonical, body.rules)
    except Exception:
        raise _unavailable("update rules", user.id) from None
    if project is None:
        raise HTTPException(404, _NOT_FOUND)
    return project
