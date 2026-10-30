"""Short PostgreSQL transactions; never hold one across a provider call."""

import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


class PlatformError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


@contextmanager
def transaction():
    url = os.environ.get("PLATFORM_DATABASE_URL", "")
    if not url:
        raise PlatformError("UNAVAILABLE", "The evaluation platform database is not configured.", 503)
    with psycopg.connect(url, row_factory=dict_row, connect_timeout=5) as connection:
        connection.execute("set local statement_timeout='10s'")
        connection.execute("set local lock_timeout='3s'")
        yield connection


@dataclass
class Context:
    db: Any
    actor: dict[str, Any]
    correlation_id: str

    def audit(self, action: str, subject_id=None, project=None, details=None):
        self.db.execute(
            """insert into platform.audit(workspace_id,project_id,actor_id,action,subject_id,correlation_id,details)
            values(%s,%s,%s,%s,%s,%s,%s)""",
            (
                project["workspace_id"] if project else None,
                project["id"] if project else None,
                self.actor.get("user_id"),
                action,
                subject_id,
                self.correlation_id,
                Jsonb(details or {}),
            ),
        )

    def project(self, project_id, roles=None):
        if self.actor.get("project_id") and str(self.actor["project_id"]) != str(project_id):
            raise PlatformError("NOT_FOUND", "Project not found.", 404)
        row = self.db.execute(
            """select p.*,m.roles as workspace_roles,g.roles as project_roles,
            coalesce(g.hidden_access,false) hidden_access
            from platform.projects p join platform.workspaces w on w.id=p.workspace_id
            join platform.memberships m on m.workspace_id=w.id and m.user_id=%s
            left join platform.project_grants g on g.project_id=p.id and g.user_id=m.user_id
            where p.id=%s and p.deleted_at is null and w.deleted_at is null""",
            (self.actor.get("user_id"), project_id),
        ).fetchone()
        if row is None:
            raise PlatformError("NOT_FOUND", "Project not found.", 404)
        effective = set(row["project_roles"] if row["project_roles"] is not None else row["workspace_roles"])
        if "owner" in row["workspace_roles"]:
            effective.add("owner")
        row["roles"] = sorted(effective)
        if roles and not set(roles).intersection(row["roles"]):
            raise PlatformError("FORBIDDEN", "Your project role does not allow this action.", 403)
        return row

    def version(self, version_id, project, kind=None, approved=False):
        row = self.db.execute(
            """select v.*,exists(select 1 from platform.approvals a where a.version_id=v.id
            and a.content_hash=v.content_hash) approved from platform.versions v
            where v.id=%s and v.workspace_id=%s and v.project_id=%s""",
            (version_id, project["workspace_id"], project["id"]),
        ).fetchone()
        if row is None or (row["hidden"] and not project.get("hidden_access")):
            raise PlatformError("NOT_FOUND", "Version not found or not available to this role.", 404)
        if kind and row["kind"] != kind:
            raise PlatformError("INVALID_INPUT", f"Expected a {kind} version.", 422)
        if approved and not row["approved"]:
            raise PlatformError("APPROVAL_REQUIRED", "This exact version requires reviewer approval.", 409)
        return row


def new_context(db, actor):
    return Context(db, actor, str(uuid4()))
