"""Attempt-bound storage and read-only reconciliation. Never renews dispatch authority."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import Header, Request, Response
from psycopg.types.json import Jsonb
from susanoo_contracts import (
    EvidenceBatch,
    LeaseRequest,
    ReconciliationIn,
    ResourceReleaseIn,
    RunnerArtifactCreate,
    content_hash,
    canonical_json,
)

from . import artifacts, service
from .database import PlatformError


def artifact_for_attempt(ctx, attempt, artifact_id, *, upload=False):
    row = ctx.db.execute(
        "select * from platform.artifacts where id=%s and project_id=%s",
        (artifact_id, attempt["project_id"]),
    ).fetchone()
    repair = attempt["manifest"].get("repair") or {}
    is_input = str(artifact_id) == repair.get("patch_artifact_id")
    is_input = is_input or str(artifact_id) in {item["id"] for item in attempt["manifest"].get("input_artifacts", [])}
    if not row or (row["attempt_id"] != attempt["id"] and (upload or not is_input)):
        raise PlatformError("NOT_FOUND", "Artifact is not assigned to this attempt.", 404)
    if upload and row["uploaded_by_runner"] != ctx.actor["runner_id"]:
        raise PlatformError("NOT_FOUND", "Artifact uploader does not match this runner.", 404)
    project = ctx.db.execute("select policy from platform.projects where id=%s", (attempt["project_id"],)).fetchone()
    if (
        row["expires_at"] <= datetime.now(UTC)
        or row["status"] == "purged"
        or row["policy_hash"] != content_hash(project["policy"])
    ):
        raise PlatformError("POLICY_CHANGED", "Artifact is expired or requires current policy approval.", 409)
    return row


def mount_runner_io(app):
    from .router import Ctx, RunnerCtx

    @app.get("/projects/{project_id}/resource-leases")
    def resources(project_id: UUID, ctx: Ctx):
        project = ctx.project(project_id, service.REVIEWERS)
        return {
            "items": ctx.db.execute(
                """select l.* from platform.resource_leases l join platform.attempts a on a.id=l.attempt_id
            where l.project_id=%s and (not a.hidden or %s) order by l.created_at desc limit 200""",
                (project_id, project["hidden_access"]),
            ).fetchall()
        }

    @app.post("/resource-leases/{lease_id}/release")
    def release(lease_id: UUID, body: ResourceReleaseIn, ctx: Ctx):
        row = ctx.db.execute("select * from platform.resource_leases where id=%s for update", (lease_id,)).fetchone()
        if not row:
            raise PlatformError("NOT_FOUND", "Resource lease not found.", 404)
        project = ctx.project(row["project_id"], service.REVIEWERS)
        reconciliation = ctx.db.execute(
            "select * from platform.reconciliations where id=%s and attempt_id=%s and state='resolved'",
            (body.reconciliation_id, row["attempt_id"]),
        ).fetchone()
        if row["state"] != "quarantined" or not reconciliation or not reconciliation["evidence_ids"]:
            raise PlatformError("RECONCILIATION_REQUIRED", "Quarantine release needs resolved read-back evidence.", 409)
        evidence = ctx.db.execute(
            """select count(*) n from platform.evidence where id=any(%s::uuid[]) and attempt_id=%s
            and kind='observation' and data->>'availability'='available'
            and data->>'completeness'='complete_for_scope' and (not hidden or %s)""",
            (reconciliation["evidence_ids"], row["attempt_id"], project["hidden_access"]),
        ).fetchone()["n"]
        if evidence != len(reconciliation["evidence_ids"]):
            raise PlatformError("RECONCILIATION_REQUIRED", "Complete authorized evidence is required.", 409)
        ctx.db.execute(
            "update platform.resource_leases set state='released',released_at=now() where id=%s", (lease_id,)
        )
        ctx.audit(
            "resource.quarantine_released",
            lease_id,
            project,
            {"rationale": body.rationale, "reconciliation_id": str(body.reconciliation_id)},
        )
        return {"released": True, "original_verdict_unchanged": True, "fresh_reproduction_required": True}

    @app.get("/attempts/{attempt_id}/reconciliations")
    def reconciliations(attempt_id: UUID, ctx: Ctx):
        row = ctx.db.execute("select project_id,hidden from platform.attempts where id=%s", (attempt_id,)).fetchone()
        if not row:
            raise PlatformError("NOT_FOUND", "Attempt not found.", 404)
        project = ctx.project(row["project_id"], service.REVIEWERS)
        if row["hidden"] and not project["hidden_access"]:
            raise PlatformError("NOT_FOUND", "Attempt not available to this role.", 404)
        records = ctx.db.execute(
            "select * from platform.reconciliations where attempt_id=%s order by created_at", (attempt_id,)
        ).fetchall()
        evidence = ctx.db.execute(
            "select * from platform.evidence where attempt_id=%s and kind='observation' order by received_at",
            (attempt_id,),
        ).fetchall()
        late = ctx.db.execute(
            "select reconciliation_id,event_id,sequence,payload,received_at from platform.late_runner_events "
            "where attempt_id=%s order by sequence",
            (attempt_id,),
        ).fetchall()
        return {"items": records, "evidence": artifacts.scoped_evidence(evidence, project), "late_events": late}

    @app.post("/runner/attempts/{attempt_id}/artifacts", status_code=201)
    def reserve(attempt_id: UUID, body: RunnerArtifactCreate, ctx: RunnerCtx):
        attempt = service.owned_attempt(ctx, attempt_id, body.lease_token)
        policy = attempt["manifest"]["policy"]

        def voice_lookup(value):
            voice = next(
                (
                    version
                    for version in attempt["manifest"].get("versions", [])
                    if version["kind"] == "voice" and version["id"] == str(value)
                ),
                None,
            )
            if not voice or not voice["approved"]:
                raise PlatformError("FORBIDDEN", "Voice authorization is not pinned to this attempt.", 403)
            return voice

        artifacts.validate_upload_policy(policy, body.artifact, voice_lookup)
        if policy["mode"] == "local_only" or (
            body.artifact.content_type == "audio/wav" and not policy["media_upload_allowed"]
        ):
            raise PlatformError("FORBIDDEN", "The attempt does not authorize hosted artifact bytes.", 403)
        artifact_id = uuid4()
        return ctx.db.execute(
            """insert into platform.artifacts(id,workspace_id,project_id,run_id,attempt_id,uploaded_by_runner,
            content_type,digest,size,object_key,audience,hidden,created_by,expires_at,policy_hash,
            purpose,authorization_version_id)
            select %s,a.workspace_id,a.project_id,a.run_id,a.id,%s,%s,%s,%s,%s,%s,a.hidden,r.created_by,%s,%s,%s,%s
            from platform.attempts a join platform.runs r on r.id=a.run_id where a.id=%s
            returning id,status,digest,size,content_type""",
            (
                artifact_id,
                ctx.actor["runner_id"],
                body.artifact.content_type,
                body.artifact.digest,
                body.artifact.size,
                f"{attempt['workspace_id']}/{attempt['project_id']}/{artifact_id}",
                "reviewers" if attempt["hidden"] else body.artifact.audience,
                datetime.now(UTC) + timedelta(days=policy["hosted_retention_days"]),
                content_hash(policy),
                body.artifact.purpose,
                body.artifact.authorization_version_id,
                attempt_id,
            ),
        ).fetchone()

    @app.put("/runner/attempts/{attempt_id}/artifacts/{artifact_id}/content")
    async def upload(
        attempt_id: UUID,
        artifact_id: UUID,
        request: Request,
        x_lease_token: str = Header(min_length=32, max_length=200),
    ):
        from .database import Context
        from .security import authenticated
        from .uploads import verified_upload

        def authorize(db):
            ctx = Context(db, authenticated(db, request, runner=True), request.state.correlation_id)
            attempt = service.owned_attempt(ctx, attempt_id, x_lease_token)
            row = artifact_for_attempt(ctx, attempt, artifact_id, upload=True)
            return row, attempt["project_id"]

        return verified_upload(artifact_id, await request.body(), authorize)

    @app.post("/runner/attempts/{attempt_id}/artifacts/{artifact_id}/download")
    def download(attempt_id: UUID, artifact_id: UUID, body: LeaseRequest, request: Request):
        from .database import Context, transaction
        from .security import authenticated

        def authorize(db):
            ctx = Context(db, authenticated(db, request, runner=True), request.state.correlation_id)
            attempt = service.owned_attempt(ctx, attempt_id, body.lease_token)
            return artifact_for_attempt(ctx, attempt, artifact_id)

        with transaction() as db:
            row = authorize(db)
        if row["status"] != "ready":
            raise PlatformError("INVALID_STATE", "Artifact is not ready.", 409)
        content = artifacts.ObjectStore().get(row["object_key"])
        import hashlib

        if hashlib.sha256(content).hexdigest() != row["digest"]:
            raise PlatformError("INTEGRITY_ERROR", "Assigned artifact digest verification failed.", 409)
        with transaction() as db:
            authorize(db)
        return Response(
            content,
            media_type=row["content_type"],
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    @app.post("/runner/attempts/{attempt_id}/reconciliations", status_code=201)
    def reconcile(attempt_id: UUID, body: ReconciliationIn, ctx: RunnerCtx):
        attempt = service.owned_attempt(ctx, attempt_id, body.lease_token, read_only=True)
        if attempt["execution_state"] in {"preparing", "running", "observing", "evaluating"} and (
            attempt["lease_expires_at"] <= datetime.now(UTC)
        ):
            ctx.db.execute(
                """update platform.attempts set execution_state='interrupted',business_verdict='UNKNOWN',
                reconciliation_state='pending',reason='Expired runner reconnected with late evidence' where id=%s""",
                (attempt_id,),
            )
            ctx.db.execute(
                "update platform.resource_leases set state='quarantined' where attempt_id=%s and state='leased'",
                (attempt_id,),
            )
            attempt["execution_state"] = "interrupted"
            service.project_gate(ctx.db, attempt["run_id"])
        if attempt["execution_state"] not in {"interrupted", "timed_out", "cancelled", "error"}:
            raise PlatformError("INVALID_STATE", "Reconciliation is for settled uncertain attempts only.", 409)
        data = service.dump(body)
        data.pop("lease_token")
        if len(canonical_json(data).encode()) > 1048576:
            raise PlatformError("CAPACITY_EXCEEDED", "Reconciliation exceeds the 1 MiB boundary.", 422)
        digest = content_hash(data)
        prior = ctx.db.execute(
            "select id,created_at from platform.reconciliations where attempt_id=%s and content_hash=%s",
            (attempt_id, digest),
        ).fetchone()
        if prior:
            return {
                **prior,
                "dispatch_authority": False,
                "original_verdict_unchanged": True,
                "fresh_reproduction_required": True,
            }
        # Append read-back observations without renewing a lease or changing the verdict.
        service.validate_evidence(
            ctx, attempt, EvidenceBatch(lease_token=body.lease_token, observations=body.observations)
        )
        row = ctx.db.execute(
            """insert into platform.reconciliations(workspace_id,project_id,attempt_id,actor_id,actor_kind,
            state,rationale,evidence_ids,content_hash,cleanup_state)
            values(%s,%s,%s,%s,'runner',%s,%s,%s,%s,%s) returning id,created_at""",
            (
                attempt["workspace_id"],
                attempt["project_id"],
                attempt_id,
                ctx.actor["runner_id"],
                body.state,
                body.rationale,
                [o.observation_id for o in body.observations],
                digest,
                body.cleanup_state,
            ),
        ).fetchone()
        for event in body.late_events:
            value = service.dump(event)
            event_hash = content_hash(value)
            existing = ctx.db.execute(
                """select event_id,payload_hash from platform.runner_events
                where attempt_id=%s and (event_id=%s or sequence=%s)
                union all select event_id,payload_hash from platform.late_runner_events
                where attempt_id=%s and (event_id=%s or sequence=%s)""",
                (attempt_id, event.event_id, event.sequence, attempt_id, event.event_id, event.sequence),
            ).fetchall()
            if existing:
                if any(item["event_id"] != event.event_id or item["payload_hash"] != event_hash for item in existing):
                    raise PlatformError(
                        "IDEMPOTENCY_CONFLICT", "Late journal evidence conflicts with stored events.", 409
                    )
                continue
            ctx.db.execute(
                """insert into platform.late_runner_events(workspace_id,project_id,attempt_id,reconciliation_id,
                event_id,sequence,payload,payload_hash) values(%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    attempt["workspace_id"],
                    attempt["project_id"],
                    attempt_id,
                    row["id"],
                    event.event_id,
                    event.sequence,
                    Jsonb(value),
                    event_hash,
                ),
            )
        return {
            **row,
            "dispatch_authority": False,
            "original_verdict_unchanged": True,
            "fresh_reproduction_required": True,
        }
