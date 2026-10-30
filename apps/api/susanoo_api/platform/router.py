"""Expanded private control API. Legacy routes remain independently available."""

import hashlib
import logging
import os
from collections.abc import Generator
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4
from urllib.parse import urlsplit

import psycopg
from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import Field
from susanoo_contracts import (
    ApprovalIn,
    ArtifactCreate,
    BudgetReservation,
    ChangeIn,
    DependencyIn,
    EventBatch,
    EvidenceBatch,
    ExportIn,
    Finalize,
    InvitationIn,
    JobCreate,
    LeaseRequest,
    MembershipChange,
    ProjectCreate,
    ProjectPatch,
    ReviewIn,
    RunCreate,
    RunnerClaim,
    RunnerRegistration,
    ScheduleIn,
    VersionCreate,
    WaiverIn,
    WireModel,
    WorkspaceCreate,
)
from susanoo_contracts import MeResponse, ProjectRecord, VersionRecord, RunRecord, ArtifactRecord
from susanoo_contracts import DataPolicy
from susanoo_contracts import RunnerClaimResponse, ControlResponse, EventAcknowledgement, FinalizeResponse

from . import artifacts, security, service, workflows
from .database import Context, PlatformError, transaction

log = logging.getLogger(__name__)


class LoginIn(WireModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=1024)


class TokenIn(WireModel):
    token: str = Field(min_length=32, max_length=200)
    password: str = Field(min_length=12, max_length=1024)


class EmailIn(WireModel):
    email: str = Field(min_length=3, max_length=254)


class ComparisonIn(WireModel):
    baseline_id: UUID
    candidate_id: UUID


class LeaseReconciliation(LeaseRequest):
    state: str = Field(pattern="^(resolved|unresolved)$")
    reason: str = Field(min_length=1, max_length=4000)


def context(request: Request) -> Generator[Context]:
    with transaction() as db:
        actor = security.authenticated(db, request)
        if actor["kind"] == "cli" and request.method not in {"GET", "HEAD", "OPTIONS"}:
            allowed = request.url.path.endswith(
                ("/runs", "/jobs", "/exports", "/reproductions", "/cancel")
            ) or request.url.path.endswith("/comparisons")
            if not allowed:
                raise PlatformError(
                    "FORBIDDEN", "This scoped CLI credential cannot perform administrative mutations.", 403
                )
        yield Context(db, actor, request.state.correlation_id)


def runner_context(request: Request) -> Generator[Context]:
    with transaction() as db:
        actor = security.authenticated(db, request, runner=True)
        yield Context(db, actor, request.state.correlation_id)


Ctx = Annotated[Context, Depends(context, scope="function")]
RunnerCtx = Annotated[Context, Depends(runner_context, scope="function")]
Idempotency = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)]


def _failure(request, code, message, status):
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": {},
                "correlation_id": getattr(request.state, "correlation_id", str(uuid4())),
            }
        },
    )


def create_platform_app():
    app = FastAPI(title="Susanoo Evaluation Platform", version="0.1.0")
    origin = urlsplit(os.environ.get("PLATFORM_ORIGIN", "http://localhost:3000"))
    if os.environ.get("PLATFORM_ENV") == "production" and (origin.scheme != "https" or not origin.hostname):
        raise RuntimeError("Production evaluation API requires an explicit HTTPS PLATFORM_ORIGIN")
    hosts = {origin.hostname or "localhost", "localhost", "127.0.0.1", "api"}
    if os.environ.get("PLATFORM_ENV") == "development":
        hosts.add("testserver")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=sorted(hosts))

    @app.middleware("http")
    async def boundary(request, call_next):
        request.state.correlation_id = str(uuid4())
        limit = 1048576 if request.url.path.endswith("/events") else 10485760
        if request.method in {"POST", "PUT", "PATCH"}:
            content = bytearray()
            async for chunk in request.stream():
                content.extend(chunk)
                if len(content) > limit:
                    return _failure(request, "PAYLOAD_TOO_LARGE", "Request exceeds the allowed byte limit.", 413)
            request._body = bytes(content)
        try:
            response = await call_next(request)
        except Exception as exc:
            log.error(
                "Platform request failed correlation=%s type=%s", request.state.correlation_id, type(exc).__name__
            )
            return _failure(request, "UNAVAILABLE", "The action could not be completed. Retry safe reads only.", 503)
        response.headers["X-Correlation-ID"] = request.state.correlation_id
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(PlatformError)
    async def platform_error(request, exc):
        return _failure(request, exc.code, exc.message, exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Validation details can contain input secrets. Publish field locations, never input values.
        field = ".".join(str(value) for value in exc.errors()[0]["loc"])
        return _failure(request, "INVALID_INPUT", f"Check the value for {field}.", 422)

    @app.exception_handler(psycopg.IntegrityError)
    async def integrity_error(request, exc):
        return _failure(
            request, "INTEGRITY_CONFLICT", "This change violates a version, ownership or uniqueness constraint.", 409
        )

    @app.get("/health")
    def health():
        with transaction() as db:
            db.execute("select version from platform.schema_versions limit 1").fetchone()
        return {"status": "ok", "protocol_version": "1.0"}

    @app.get("/auth/csrf")
    def csrf(response: Response):
        token = security.csrf_value()
        response.set_cookie(
            security.CSRF_COOKIE, token, secure=security.secure_cookies(), samesite="strict", path="/api/v1"
        )
        return {"csrf_token": token}

    @app.post("/auth/login")
    def login(body: LoginIn, request: Request, response: Response):
        security.check_csrf(request)
        with transaction() as db:
            key = hashlib.sha256(body.email.lower().encode()).hexdigest()
            db.execute("insert into platform.login_attempts(key_hash) values(%s) on conflict do nothing", (key,))
            attempts = db.execute(
                "select * from platform.login_attempts where key_hash=%s for update", (key,)
            ).fetchone()
            if (
                attempts["window_start"].timestamp() > datetime.now(UTC).timestamp() - 900
                and attempts["failures"] >= 10
            ):
                return _failure(request, "RATE_LIMITED", "Try signing in again after fifteen minutes.", 429)
            if attempts["window_start"].timestamp() <= datetime.now(UTC).timestamp() - 900:
                db.execute("update platform.login_attempts set failures=0,window_start=now() where key_hash=%s", (key,))
            user = db.execute(
                "select * from platform.identities where email=%s and not disabled", (body.email.strip().lower(),)
            ).fetchone()
            if not user or not security.password_matches(body.password, user["password_hash"]):
                db.execute("update platform.login_attempts set failures=failures+1 where key_hash=%s", (key,))
                return _failure(request, "UNAUTHENTICATED", "Email or password was not accepted.", 401)
            db.execute("update platform.login_attempts set failures=0 where key_hash=%s", (key,))
            token = security.issue_token(db, "session", user_id=user["id"], hours=8)
            response.set_cookie(
                security.COOKIE,
                token["token"],
                httponly=True,
                secure=security.secure_cookies(),
                samesite="strict",
                max_age=28800,
                path="/api/v1",
            )
            return {"user_id": user["id"], "email": user["email"]}

    @app.post("/auth/logout")
    def logout(ctx: Ctx, response: Response):
        ctx.db.execute("update platform.credentials set revoked_at=now() where id=%s", (ctx.actor["id"],))
        response.delete_cookie(security.COOKIE, path="/api/v1")
        ctx.audit("session.revoked", ctx.actor["id"])
        return {"signed_out": True}

    @app.get("/me", response_model=MeResponse)
    def me(ctx: Ctx):
        user = ctx.db.execute(
            "select id,email from platform.identities where id=%s", (ctx.actor["user_id"],)
        ).fetchone()
        workspaces = ctx.db.execute(
            "select w.id,w.name,m.roles from platform.workspaces w join platform.memberships m on m.workspace_id=w.id "
            "where m.user_id=%s and w.deleted_at is null order by w.created_at,w.id",
            (ctx.actor["user_id"],),
        ).fetchall()
        return {"user": user, "workspaces": workspaces}

    @app.post("/auth/password-reset")
    def request_reset(body: EmailIn, request: Request):
        security.check_csrf(request)
        from .mail import request_password_reset

        with transaction() as db:
            return request_password_reset(db, body.email)

    @app.post("/auth/password-reset/confirm")
    def reset(body: TokenIn, request: Request):
        security.check_csrf(request)
        from .mail import complete_password_reset

        with transaction() as db:
            return complete_password_reset(db, body.token, body.password)

    @app.post("/invitations/accept")
    def accept_invitation(body: TokenIn, request: Request):
        security.check_csrf(request)
        from .mail import accept_invitation as accept

        with transaction() as db:
            return accept(db, body.token, body.password)

    @app.post("/workspaces", status_code=201)
    def new_workspace(body: WorkspaceCreate, ctx: Ctx):
        return service.create_workspace(ctx, body.name)

    @app.get("/workspaces/{workspace_id}/members")
    def members(workspace_id: UUID, ctx: Ctx):
        service.workspace_member(ctx, workspace_id, {"owner"})
        return {
            "items": ctx.db.execute(
                "select i.id,i.email,m.roles from platform.memberships m join platform.identities i on i.id=m.user_id "
                "where m.workspace_id=%s order by i.email",
                (workspace_id,),
            ).fetchall()
        }

    @app.post("/workspaces/{workspace_id}/members")
    def member(workspace_id: UUID, body: MembershipChange, ctx: Ctx):
        return service.change_membership(ctx, workspace_id, body)

    @app.post("/workspaces/{workspace_id}/invitations", status_code=201)
    def invite(workspace_id: UUID, body: InvitationIn, ctx: Ctx):
        from .mail import invite as send

        return send(ctx, workspace_id, body)

    @app.delete("/workspaces/{workspace_id}/members/{user_id}")
    def remove_member(workspace_id: UUID, user_id: UUID, ctx: Ctx):
        return service.remove_member(ctx, workspace_id, user_id)

    @app.post("/projects", status_code=201, response_model=ProjectRecord)
    def new_project(body: ProjectCreate, ctx: Ctx):
        return service.create_project(ctx, body)

    @app.get("/projects")
    def projects(ctx: Ctx, after: UUID | None = None, limit: int = Query(50, ge=1, le=200)):
        rows = ctx.db.execute(
            """select p.id,p.workspace_id,p.name,p.workflow,p.timezone,p.created_at from platform.projects p
            join platform.workspaces w on w.id=p.workspace_id join platform.memberships m on m.workspace_id=w.id
            where m.user_id=%s and p.deleted_at is null and w.deleted_at is null and (%s::uuid is null or p.id>%s)
            and (%s::uuid is null or p.id=%s) order by p.id limit %s""",
            (ctx.actor["user_id"], after, after, ctx.actor["project_id"], ctx.actor["project_id"], limit + 1),
        ).fetchall()
        return {"items": rows[:limit], "next_cursor": str(rows[limit - 1]["id"]) if len(rows) > limit else None}

    @app.get("/projects/{project_id}", response_model=ProjectRecord)
    def project(project_id: UUID, ctx: Ctx):
        return ctx.project(project_id)

    @app.post("/projects/{project_id}/policy")
    def project_policy(project_id: UUID, body: DataPolicy, ctx: Ctx):
        return service.apply_policy(ctx, project_id, body)

    @app.patch("/projects/{project_id}", response_model=ProjectRecord)
    def update_project(project_id: UUID, body: ProjectPatch, ctx: Ctx):
        project = ctx.project(project_id, service.OWNERS)
        ctx.db.execute(
            "update platform.projects set name=coalesce(%s,name),workflow=coalesce(%s,workflow),"
            "timezone=coalesce(%s,timezone) where id=%s and deleted_at is null",
            (body.name, body.workflow, body.timezone, project_id),
        )
        ctx.audit("project.updated", project_id, project, {"fields": list(body.model_fields_set)})
        return ctx.project(project_id)

    @app.delete("/projects/{project_id}")
    def delete(project_id: UUID, ctx: Ctx):
        return workflows.delete_project(ctx, project_id)

    @app.post("/projects/{project_id}/versions", status_code=201, response_model=VersionRecord)
    def version(project_id: UUID, body: VersionCreate, ctx: Ctx):
        return service.create_version(ctx, project_id, body)

    @app.get("/projects/{project_id}/versions")
    def versions(
        project_id: UUID,
        ctx: Ctx,
        kind: str | None = None,
        after: UUID | None = None,
        limit: int = Query(50, ge=1, le=200),
    ):
        project = ctx.project(project_id)
        rows = ctx.db.execute(
            """select v.*,exists(select 1 from platform.approvals a
            where a.version_id=v.id and a.content_hash=v.content_hash) approved
            from platform.versions v where v.project_id=%s and (not v.hidden or %s)
            and (%s::text is null or v.kind=%s) and (%s::uuid is null or v.id>%s) order by v.id limit %s""",
            (project_id, project["hidden_access"], kind, kind, after, after, limit + 1),
        ).fetchall()
        return {"items": rows[:limit], "next_cursor": str(rows[limit - 1]["id"]) if len(rows) > limit else None}

    @app.get("/versions/{version_id}", response_model=VersionRecord)
    def version_detail(version_id: UUID, ctx: Ctx):
        row = ctx.db.execute("select project_id from platform.versions where id=%s", (version_id,)).fetchone()
        if not row:
            raise PlatformError("NOT_FOUND", "Version not found.", 404)
        return ctx.version(version_id, ctx.project(row["project_id"]))

    @app.post("/versions/{version_id}/approve")
    def approve(version_id: UUID, body: ApprovalIn, ctx: Ctx):
        return service.approve_version(ctx, version_id, body)

    @app.post("/projects/{project_id}/runs", status_code=201, response_model=RunRecord)
    def launch(project_id: UUID, body: RunCreate, key: Idempotency, ctx: Ctx):
        return service.browser_run(ctx, service.create_run(ctx, project_id, body, key), ctx.project(project_id))

    @app.get("/projects/{project_id}/runs")
    def runs(project_id: UUID, ctx: Ctx, after: UUID | None = None, limit: int = Query(50, ge=1, le=200)):
        project = ctx.project(project_id)
        rows = ctx.db.execute(
            "select * from platform.runs where project_id=%s and (%s::uuid is null or id>%s) order by id limit %s",
            (project_id, after, after, limit + 1),
        ).fetchall()
        return {
            "items": [service.browser_run(ctx, row, project) for row in rows[:limit]],
            "next_cursor": str(rows[limit - 1]["id"]) if len(rows) > limit else None,
        }

    @app.get("/runs/{run_id}", response_model=RunRecord)
    def run(run_id: UUID, ctx: Ctx):
        row, project = service.get_run(ctx, run_id)
        return service.browser_run(ctx, row, project)

    @app.post("/runs/{run_id}/cancel")
    def cancel(run_id: UUID, ctx: Ctx):
        return service.cancel_run(ctx, run_id)

    @app.get("/runs/{run_id}/results")
    def results(run_id: UUID, ctx: Ctx, after: UUID | None = None, limit: int = Query(50, ge=1, le=200)):
        row, project = service.get_run(ctx, run_id)
        items = ctx.db.execute(
            """select e.* from platform.evidence e join platform.attempts a on a.id=e.attempt_id
            where a.run_id=%s and (not e.hidden or %s) and (%s::uuid is null or e.id>%s) order by e.id limit %s""",
            (run_id, project["hidden_access"], after, after, limit + 1),
        ).fetchall()
        return {
            "items": artifacts.scoped_evidence(items[:limit], project),
            "next_cursor": str(items[limit - 1]["id"]) if len(items) > limit else None,
            "retention": ctx.db.execute(
                "select purged_at,reason from platform.retention_receipts where run_id=%s", (run_id,)
            ).fetchone(),
        }

    @app.get("/runs/{run_id}/events")
    def events(run_id: UUID, ctx: Ctx, after: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
        row, project = service.get_run(ctx, run_id)
        items = ctx.db.execute(
            """select e.ingestion_seq,e.event_id,e.sequence,e.event_type,e.received_at,e.payload,a.id attempt_id
            from platform.runner_events e join platform.attempts a on a.id=e.attempt_id
            where a.run_id=%s and (not a.hidden or %s) and e.ingestion_seq>%s
            order by e.ingestion_seq limit %s""",
            (run_id, project["hidden_access"], after, limit),
        ).fetchall()
        return {"items": items, "next_after": items[-1]["ingestion_seq"] if items else after}

    @app.get("/runs/{run_id}/attempts")
    def attempts(run_id: UUID, ctx: Ctx, after: UUID | None = None, limit: int = Query(50, ge=1, le=200)):
        _, project = service.get_run(ctx, run_id)
        rows = ctx.db.execute(
            """select id,case_version_id,repetition,arm,required,execution_state,business_verdict,
            cleanup_state,reconciliation_state,hidden from platform.attempts where run_id=%s
            and (not hidden or %s) and (%s::uuid is null or id>%s) order by id limit %s""",
            (run_id, project["hidden_access"], after, after, limit + 1),
        ).fetchall()
        return {"items": rows[:limit], "next_cursor": str(rows[limit - 1]["id"]) if len(rows) > limit else None}

    @app.post("/projects/{project_id}/jobs", status_code=201)
    def job(project_id: UUID, body: JobCreate, key: Idempotency, ctx: Ctx):
        row = workflows.create_job(ctx, project_id, body, key)
        return service.browser_run(ctx, row, ctx.project(project_id))

    @app.get("/projects/{project_id}/findings")
    def findings(project_id: UUID, ctx: Ctx, after: UUID | None = None, limit: int = Query(50, ge=1, le=200)):
        project = ctx.project(project_id)
        rows = ctx.db.execute(
            "select * from platform.findings where project_id=%s and (not hidden or %s) "
            "and (%s::uuid is null or id>%s) order by id limit %s",
            (project_id, project["hidden_access"], after, after, limit + 1),
        ).fetchall()
        return {"items": rows[:limit], "next_cursor": str(rows[limit - 1]["id"]) if len(rows) > limit else None}

    @app.get("/findings/{finding_id}")
    def finding(finding_id: UUID, ctx: Ctx):
        row, project = workflows.finding(ctx, finding_id)
        row["evidence"] = ctx.db.execute(
            "select * from platform.evidence where attempt_id=%s order by id", (row["attempt_id"],)
        ).fetchall()
        row["evidence"] = artifacts.scoped_evidence(row["evidence"], project)
        row["reviews"] = ctx.db.execute(
            "select * from platform.review_events where subject_id=%s order by created_at,id", (finding_id,)
        ).fetchall()
        row["acceptance"] = workflows.acceptance_projection(ctx, row)
        row["check"] = ctx.version(row["check_version_id"], project, "check")
        row["requirement"] = ctx.version(row["check"]["data"]["requirement_version_id"], project, "requirement")
        attempt = ctx.db.execute(
            "select execution_state,cleanup_state,reconciliation_state,namespace,reason "
            "from platform.attempts where id=%s",
            (row["attempt_id"],),
        ).fetchone()
        row["operational_state"] = attempt
        return row

    @app.post("/findings/{finding_id}/reviews")
    def review(finding_id: UUID, body: ReviewIn, ctx: Ctx):
        return workflows.review(ctx, finding_id, body)

    @app.post("/findings/{finding_id}/waivers")
    def waiver(finding_id: UUID, body: WaiverIn, ctx: Ctx):
        return workflows.waiver(ctx, finding_id, body)

    @app.post("/findings/{finding_id}/reproductions", status_code=201, response_model=RunRecord)
    def reproduce(finding_id: UUID, key: Idempotency, ctx: Ctx):
        row = workflows.reproduce(ctx, finding_id, key)
        return service.browser_run(ctx, row, ctx.project(row["project_id"]))

    @app.post("/comparisons")
    def compare(body: ComparisonIn, ctx: Ctx):
        return workflows.comparison(ctx, body.baseline_id, body.candidate_id)

    @app.post("/runs/{run_id}/exports", status_code=202)
    def export(run_id: UUID, body: ExportIn, ctx: Ctx):
        return artifacts.request_export(ctx, run_id, body)

    @app.post("/projects/{project_id}/artifacts", status_code=201)
    def artifact(project_id: UUID, body: ArtifactCreate, ctx: Ctx):
        return artifacts.reserve_upload(ctx, project_id, body)

    @app.put("/artifacts/{artifact_id}/content")
    async def upload(artifact_id: UUID, request: Request):
        from .uploads import verified_upload

        def authorize(db):
            actor = security.authenticated(db, request)
            if actor["kind"] != "session":
                raise PlatformError("FORBIDDEN", "Artifact upload requires the reserving browser session.", 403)
            ctx = Context(db, actor, request.state.correlation_id)
            row, project = artifacts.authorized_artifact(ctx, artifact_id)
            if row["created_by"] != actor["user_id"]:
                raise PlatformError("FORBIDDEN", "Only the reserving principal may upload these bytes.", 403)
            ctx.audit("artifact.upload_authorized", artifact_id, project)
            return row, project["id"]

        return verified_upload(artifact_id, await request.body(), authorize)

    @app.get("/artifacts/{artifact_id}", response_model=ArtifactRecord)
    def artifact_status(artifact_id: UUID, ctx: Ctx):
        row, _ = artifacts.authorized_artifact(ctx, artifact_id)
        return {
            key: row[key] for key in ("id", "status", "digest", "size", "content_type", "export_format", "expires_at")
        }

    @app.get("/artifacts/{artifact_id}/download")
    def download(artifact_id: UUID, request: Request):
        def authorize(db):
            ctx = Context(db, security.authenticated(db, request), request.state.correlation_id)
            row, project = artifacts.authorized_artifact(ctx, artifact_id)
            return row, project, ctx

        with transaction() as db:
            row, _, _ = authorize(db)
        if row["status"] != "ready":
            raise PlatformError("INVALID_STATE", "Artifact is not ready.", 409)
        content = artifacts.ObjectStore().get(row["object_key"])
        if hashlib.sha256(content).hexdigest() != row["digest"]:
            raise PlatformError("UNAVAILABLE", "Artifact integrity check failed.", 503)
        with transaction() as db:
            _, project, ctx = authorize(db)
            ctx.audit("artifact.downloaded", artifact_id, project)
        return Response(
            content,
            media_type=row["content_type"],
            headers={
                "Content-Disposition": f'attachment; filename="susanoo-{artifact_id}.{row["export_format"] or "bin"}"'
            },
        )

    @app.post("/projects/{project_id}/schedules", status_code=201)
    def schedule(project_id: UUID, body: ScheduleIn, ctx: Ctx):
        return workflows.create_schedule(ctx, project_id, body)

    @app.get("/projects/{project_id}/schedules")
    def schedules(project_id: UUID, ctx: Ctx):
        ctx.project(project_id)
        return {
            "items": ctx.db.execute(
                "select * from platform.schedules where project_id=%s order by id limit 200", (project_id,)
            ).fetchall()
        }

    @app.delete("/schedules/{schedule_id}")
    def disable_schedule(schedule_id: UUID, ctx: Ctx):
        row = ctx.db.execute("select project_id from platform.schedules where id=%s", (schedule_id,)).fetchone()
        if not row:
            raise PlatformError("NOT_FOUND", "Schedule not found.", 404)
        project = ctx.project(row["project_id"], service.ENGINEERS)
        ctx.db.execute("update platform.schedules set enabled=false where id=%s", (schedule_id,))
        ctx.audit("schedule.disabled", schedule_id, project)
        return {"id": schedule_id, "enabled": False}

    @app.post("/projects/{project_id}/dependencies")
    def dependency(project_id: UUID, body: DependencyIn, ctx: Ctx):
        return workflows.add_dependency(ctx, project_id, body)

    @app.get("/projects/{project_id}/dependencies")
    def dependencies(project_id: UUID, ctx: Ctx):
        project = ctx.project(project_id)
        return {
            "items": ctx.db.execute(
                """select e.* from platform.version_links e join platform.versions s on s.id=e.source_id
            join platform.versions t on t.id=e.target_id where e.project_id=%s
            and (not s.hidden or %s) and (not t.hidden or %s) order by e.source_id,e.target_id limit 200""",
                (project_id, project["hidden_access"], project["hidden_access"]),
            ).fetchall(),
            "dependency_coverage": "declared_and_received_only",
        }

    @app.post("/projects/{project_id}/changes")
    def change(project_id: UUID, body: ChangeIn, ctx: Ctx):
        return workflows.invalidate(ctx, project_id, body)

    @app.get("/projects/{project_id}/invalidations")
    def invalidations(project_id: UUID, ctx: Ctx):
        project = ctx.project(project_id)
        return {
            "items": ctx.db.execute(
                "select i.* from platform.invalidations i join platform.versions v on v.id=i.version_id "
                "where i.project_id=%s and (not v.hidden or %s) order by i.created_at desc limit 200",
                (project_id, project["hidden_access"]),
            ).fetchall(),
            "dependency_coverage": "declared_and_received_only",
        }

    @app.get("/projects/{project_id}/runners")
    def runners(project_id: UUID, ctx: Ctx):
        ctx.project(project_id, service.OWNERS)
        return {
            "items": ctx.db.execute(
                "select id,name,version,capabilities,last_heartbeat,revoked_at from platform.runners "
                "where project_id=%s order by id",
                (project_id,),
            ).fetchall()
        }

    @app.post("/projects/{project_id}/runner-registration")
    def registration(project_id: UUID, ctx: Ctx):
        return service.registration_token(ctx, project_id)

    @app.post("/runners/{runner_id}/revoke")
    def revoke_runner(runner_id: UUID, ctx: Ctx):
        row = ctx.db.execute("select project_id from platform.runners where id=%s", (runner_id,)).fetchone()
        if not row:
            raise PlatformError("NOT_FOUND", "Runner not found.", 404)
        project = ctx.project(row["project_id"], service.OWNERS)
        ctx.db.execute("update platform.runners set revoked_at=now() where id=%s", (runner_id,))
        ctx.db.execute("update platform.credentials set revoked_at=now() where runner_id=%s", (runner_id,))
        ctx.audit("runner.revoked", runner_id, project)
        return {"runner_id": runner_id, "revoked": True}

    @app.post("/projects/{project_id}/cli-credentials")
    def cli_credential(project_id: UUID, ctx: Ctx):
        project = ctx.project(project_id, service.ENGINEERS)
        result = security.issue_token(ctx.db, "cli", project=project, user_id=ctx.actor["user_id"], hours=1)
        ctx.audit("cli.credential_issued", UUID(result["credential_id"]), project)
        return result

    @app.post("/credentials/{credential_id}/revoke")
    def revoke_credential(credential_id: UUID, ctx: Ctx):
        row = ctx.db.execute(
            "select user_id,project_id from platform.credentials where id=%s", (credential_id,)
        ).fetchone()
        if not row:
            raise PlatformError("NOT_FOUND", "Credential not found.", 404)
        if row["user_id"] != ctx.actor["user_id"]:
            if not row["project_id"]:
                raise PlatformError("NOT_FOUND", "Credential not found.", 404)
            ctx.project(row["project_id"], service.OWNERS)
        ctx.db.execute("update platform.credentials set revoked_at=now() where id=%s", (credential_id,))
        ctx.audit("credential.revoked", credential_id)
        return {"revoked": True}

    @app.post("/runner/register", status_code=201)
    def register(body: RunnerRegistration):
        with transaction() as db:
            return service.register_runner(db, body)

    @app.post("/runner/claims", response_model=RunnerClaimResponse)
    def claim(body: RunnerClaim, ctx: RunnerCtx):
        result = service.runner_claim(ctx, body)
        return result if result else Response(status_code=204)

    @app.post("/runner/attempts/{attempt_id}/heartbeat", response_model=ControlResponse)
    def heartbeat(attempt_id: UUID, body: LeaseRequest, ctx: RunnerCtx):
        return service.runner_control(ctx, attempt_id, body, renew=True)

    @app.post("/runner/attempts/{attempt_id}/control", response_model=ControlResponse)
    def control(attempt_id: UUID, body: LeaseRequest, ctx: RunnerCtx):
        return service.runner_control(ctx, attempt_id, body)

    @app.post("/runner/attempts/{attempt_id}/events", response_model=EventAcknowledgement)
    def record_events(attempt_id: UUID, body: EventBatch, ctx: RunnerCtx):
        return service.runner_events(ctx, attempt_id, body)

    @app.post("/runner/attempts/{attempt_id}/evidence")
    def evidence(attempt_id: UUID, body: EvidenceBatch, ctx: RunnerCtx):
        return service.runner_evidence(ctx, attempt_id, body)

    @app.post("/runner/attempts/{attempt_id}/finalize", response_model=FinalizeResponse)
    def finalize(attempt_id: UUID, body: Finalize, ctx: RunnerCtx):
        return service.finalize_attempt(ctx, attempt_id, body)

    @app.post("/runner/attempts/{attempt_id}/reservations")
    def reservation(attempt_id: UUID, body: BudgetReservation, ctx: RunnerCtx):
        row = service.owned_attempt(ctx, attempt_id, body.lease_token)
        if row["cancel_acknowledged_at"]:
            raise PlatformError("INVALID_STATE", "No new budget reservation after cancellation acknowledgement.", 409)
        ctx.db.execute("select id from platform.runs where id=%s for update", (row["run_id"],))
        prior = ctx.db.execute(
            "select * from platform.budget_reservations where run_id=%s and operation_id=%s",
            (row["run_id"], body.operation_id),
        ).fetchone()
        if prior:
            if (
                prior["attempt_id"] != attempt_id
                or prior["budget_kind"] != body.budget_kind
                or prior["amount"] != body.amount
            ):
                raise PlatformError(
                    "IDEMPOTENCY_CONFLICT", "Budget operation identity reused with different input.", 409
                )
            return {"reserved": True, "duplicate": True}
        used = ctx.db.execute(
            "select coalesce(sum(amount),0) n from platform.budget_reservations where run_id=%s and budget_kind=%s",
            (row["run_id"], body.budget_kind),
        ).fetchone()["n"]
        cap = row["manifest"]["limits"][
            "max_agent_invocations" if body.budget_kind == "agent" else "max_reference_calls"
        ]
        if used + body.amount > cap:
            raise PlatformError("BUDGET_EXCEEDED", "The reserved call allowance would be exceeded.", 409)
        ctx.db.execute(
            "insert into platform.budget_reservations(workspace_id,project_id,run_id,operation_id,"
            "budget_kind,amount,attempt_id) "
            "values(%s,%s,%s,%s,%s,%s,%s)",
            (
                row["workspace_id"],
                row["project_id"],
                row["run_id"],
                body.operation_id,
                body.budget_kind,
                body.amount,
                attempt_id,
            ),
        )
        return {"reserved": True, "remaining": cap - used - body.amount, "duplicate": False}

    @app.get("/projects/{project_id}/audit")
    def audit(project_id: UUID, ctx: Ctx, after: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
        ctx.project(project_id, service.OWNERS)
        return {
            "items": ctx.db.execute(
                "select * from platform.audit where project_id=%s and id>%s order by id limit %s",
                (project_id, after, limit),
            ).fetchall()
        }

    @app.get("/projects/{project_id}/internal-jobs")
    def internal_jobs(project_id: UUID, ctx: Ctx):
        ctx.project(project_id, service.OWNERS)
        return {
            "items": ctx.db.execute(
                "select id,kind,status,attempts,error_code,available_at from platform.jobs "
                "where project_id=%s order by available_at desc limit 200",
                (project_id,),
            ).fetchall()
        }

    @app.get("/projects/{project_id}/health")
    def project_health(project_id: UUID, ctx: Ctx):
        ctx.project(project_id, service.OWNERS)
        return {
            "database": "ready",
            "source_grants": ctx.db.execute(
                "select state,count(*) count from platform.source_grants where project_id=%s group by state",
                (project_id,),
            ).fetchall(),
            "jobs": ctx.db.execute(
                "select status,count(*) count from platform.jobs where project_id=%s group by status", (project_id,)
            ).fetchall(),
            "runners": ctx.db.execute(
                """select id,name,last_heartbeat,capabilities from platform.runners where project_id=%s
                and revoked_at is null order by id""",
                (project_id,),
            ).fetchall(),
            "quarantined_attempts": ctx.db.execute(
                """select id,execution_state,cleanup_state,reconciliation_state from platform.attempts
                where project_id=%s and (cleanup_state='failed' or reconciliation_state in ('pending','unresolved'))
                and not hidden order by id limit 200""",
                (project_id,),
            ).fetchall(),
        }

    @app.get("/contracts")
    def contracts(ctx: Ctx):
        return {
            "protocol_version": "1.0",
            "agreement": "pending_runtime_review",
            "versions": VersionCreate.model_json_schema(),
        }

    from .integration_routes import mount_integration_routes
    from .runner_io import mount_runner_io
    from .source_access import mount_source_access

    mount_integration_routes(app)
    mount_runner_io(app)
    mount_source_access(app)
    return app
