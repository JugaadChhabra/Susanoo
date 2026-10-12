"""Projects: create from a spec, list with their runs, edit rules as sentences.

[PRD › Frontend (marketing site + app) and API], [PRD › Non-technical users › Extra API]
A project keeps the ids of its owner's picked connections, never secrets. [PRD › Credential handling]
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
FOREIGN_CONNECTION = (
    "One of the connections you picked isn't yours or has been removed. Refresh the page and pick again."
)
_NAME_LENGTH = 60
# The caller's ids among %(ids)s at write time, so a connection revoked after the check is never stored.
_OWNED_IDS = "array(select c.id from public.connections c where c.user_id = %(user)s and c.id = any(%(ids)s::uuid[]))"


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
    async def foreign_connections(self, user_id: str, connection_ids: list[str]) -> list[str]:
        """The ids that aren't one of the caller's saved connections."""
        ...

    async def create(
        self, user_id: str, name: str, spec: str, rules: list[str], connection_ids: list[str]
    ) -> ProjectSummary: ...
    async def set_rules(
        self, user_id: str, project_id: str, rules: list[str], connection_ids: list[str] | None
    ) -> ProjectSummary | None:
        """Save the rules, and the connections too unless connection_ids is None."""
        ...


_SELECT = """
    select p.id::text, p.name, p.spec, p.rules, p.repo_url,
           array(
               select c.id::text from public.connections c
               where c.id = any(p.connection_ids) and c.user_id = p.user_id
               order by c.connector
           ) as connection_ids,
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

    async def foreign_connections(self, user_id: str, connection_ids: list[str]) -> list[str]:
        if not connection_ids:
            return []
        async with session() as conn:
            cur = await conn.execute(
                "select id::text from public.connections where user_id = %s and id = any(%s::uuid[])",
                (user_id, connection_ids),
            )
            mine = {row["id"] for row in await cur.fetchall()}
        return [cid for cid in connection_ids if cid not in mine]

    async def create(
        self, user_id: str, name: str, spec: str, rules: list[str], connection_ids: list[str]
    ) -> ProjectSummary:
        async with session() as conn:
            cur = await conn.execute(
                f"""
                insert into public.projects (user_id, name, spec, rules, connection_ids)
                values (%(user)s, %(name)s, %(spec)s, %(rules)s, {_OWNED_IDS})
                returning id::text
                """,
                {"user": user_id, "name": name, "spec": spec, "rules": rules, "ids": connection_ids},
            )
            row = await cur.fetchone()
            assert row is not None
            cur = await conn.execute(
                _SELECT.format(extra="and p.id = %(project)s"), {"user": user_id, "project": row["id"]}
            )
            created = await cur.fetchone()
            assert created is not None
            return _project(created)

    async def set_rules(
        self, user_id: str, project_id: str, rules: list[str], connection_ids: list[str] | None
    ) -> ProjectSummary | None:
        async with session() as conn:
            keep = "connection_ids" if connection_ids is None else _OWNED_IDS
            cur = await conn.execute(
                f"update public.projects set rules = %(rules)s, connection_ids = {keep} "
                "where id = %(project)s and user_id = %(user)s",
                {"rules": rules, "ids": connection_ids, "project": project_id, "user": user_id},
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


async def _check_connections(store: ProjectStore, user_id: str, ids: list[UUID] | None) -> list[str] | None:
    """The picked ids as strings, or a plain-English 422 if any isn't one of the caller's connections."""
    if ids is None:
        return None
    picked = [str(i) for i in ids]
    try:
        foreign = await store.foreign_connections(user_id, picked)
    except Exception:
        raise _unavailable("check connections", user_id) from None
    if foreign:
        raise HTTPException(422, FOREIGN_CONNECTION)
    return picked


@router.post("", response_model=ProjectSummary, status_code=201)
async def create_project(body: NewProject, user: CurrentUserDep, store: StoreDep) -> ProjectSummary:
    connection_ids = await _check_connections(store, user.id, body.connection_ids) or []
    try:
        return await store.create(user.id, name_from_spec(body.spec), body.spec, body.rules, connection_ids)
    except Exception:
        raise _unavailable("create a project", user.id) from None


@router.put("/{project_id}/rules", response_model=ProjectSummary)
async def update_rules(project_id: str, body: ProjectRules, user: CurrentUserDep, store: StoreDep) -> ProjectSummary:
    try:
        canonical = str(UUID(project_id))
    except ValueError:
        raise HTTPException(404, _NOT_FOUND) from None
    connection_ids = await _check_connections(store, user.id, body.connection_ids)
    try:
        project = await store.set_rules(user.id, canonical, body.rules, connection_ids)
    except Exception:
        raise _unavailable("update rules", user.id) from None
    if project is None:
        raise HTTPException(404, _NOT_FOUND)
    return project
