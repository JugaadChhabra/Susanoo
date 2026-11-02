"""Explicit integration boundaries; callbacks never dispatch new customer actions."""

import hashlib
import hmac
import os
import json
from importlib.metadata import entry_points
from uuid import UUID, uuid4

from fastapi import Query, Request
from psycopg.types.json import Jsonb
from susanoo_contracts import ApprovalIn, IntegrationIn, JobCreate, PatchBundle, PublishIn, RepairIn, content_hash

from . import artifacts, service, workflows
from .database import Context, PlatformError, transaction
from .security import authenticated
from .github_app import patch_policy


def _integration(ctx, integration_id, owner=False):
    row = ctx.db.execute(
        "select * from platform.integrations where id=%s and revoked_at is null", (integration_id,)
    ).fetchone()
    if not row:
        raise PlatformError("NOT_FOUND", "Integration not found.", 404)
    project = ctx.project(row["project_id"], service.OWNERS if owner else None)
    return row, project


def _repair(ctx, repair_id, roles=None):
    row = ctx.db.execute("select * from platform.repairs where id=%s", (repair_id,)).fetchone()
    if not row:
        raise PlatformError("NOT_FOUND", "Repair not found.", 404)
    project = ctx.project(row["project_id"], roles)
    workflows.finding(ctx, row["finding_id"], project)
    return row, project


def _request_context(db, request, mutate=False):
    actor = authenticated(db, request)
    if mutate and actor["kind"] != "session":
        raise PlatformError("FORBIDDEN", "Repair administration requires a browser session.", 403)
    return Context(db, actor, request.state.correlation_id)


def _patch_inputs(ctx, project_id, body):
    project = ctx.project(project_id, service.ENGINEERS)
    workflows.finding(ctx, body.finding_id, project)
    source = ctx.version(body.source_version_id, project, "source", True)
    artifact, artifact_project = artifacts.authorized_artifact(ctx, body.patch_artifact_id)
    if (
        artifact_project["id"] != project_id
        or artifact["status"] != "ready"
        or artifact["content_type"] != "application/json"
        or artifact["purpose"] != "patch"
    ):
        raise PlatformError("INVALID_INPUT", "Supply a ready JSON patch artifact from this project.", 422)
    return project, source, artifact


def _patch_bytes(artifact):
    content = artifacts.ObjectStore().get(artifact["object_key"])
    if hashlib.sha256(content).hexdigest() != artifact["digest"]:
        raise PlatformError("INTEGRITY_ERROR", "Patch artifact digest verification failed.", 409)
    try:
        return PatchBundle.model_validate_json(content)
    except ValueError:
        raise PlatformError(
            "INVALID_INPUT", "Patch artifact must match the canonical patch-bundle schema.", 422
        ) from None


def mount_integration_routes(app):
    from .router import Ctx, Idempotency, RunnerCtx

    @app.post("/projects/{project_id}/integrations", status_code=201)
    def integration(project_id: UUID, body: IntegrationIn, ctx: Ctx):
        project = ctx.project(project_id, service.OWNERS)
        if body.provider == "webhook" and not body.relay:
            raise PlatformError("INVALID_INPUT", "Webhook integrations require callback configuration.", 422)
        if body.relay:
            from susanoo_connectors.webhook import WebhookEventMapping

            WebhookEventMapping.model_validate(
                body.relay.model_dump(exclude={"public_base_url", "previous_secret_ref"})
            )
            if body.provider not in {"webhook", "media"}:
                raise PlatformError("INVALID_INPUT", "Only webhook/media integrations can enable a relay.", 422)
        if body.provider in {"webhook", "media"} and body.trigger_run:
            raise PlatformError("FORBIDDEN", "Runtime callbacks never trigger new customer work.", 403)
        if body.provider == "github" and (not body.repository or not body.installation_id):
            raise PlatformError("INVALID_INPUT", "GitHub installation and repository are required.", 422)
        if body.trigger_run:
            ctx.version(body.trigger_run.suite_version_id, project, "suite", True)
            ctx.version(body.trigger_run.connection_version_id, project, "connection")
            ctx.version(body.trigger_run.environment_version_id, project, "environment", True)
        row = ctx.db.execute(
            """insert into platform.integrations(workspace_id,project_id,provider,installation_id,repository,
            secret_ref,configuration,created_by)
            values(%s,%s,%s,%s,%s,%s,%s,%s) returning *""",
            (
                project["workspace_id"],
                project_id,
                body.provider,
                body.installation_id,
                body.repository,
                body.secret_ref,
                Jsonb(body.model_dump(mode="json")),
                ctx.actor["user_id"],
            ),
        ).fetchone()
        ctx.audit("integration.created", row["id"], project)
        return {key: row[key] for key in ("id", "provider", "installation_id", "repository", "configuration")}

    @app.get("/projects/{project_id}/integrations")
    def integrations(project_id: UUID, ctx: Ctx):
        ctx.project(project_id, service.OWNERS)
        return {
            "items": ctx.db.execute(
                "select id,provider,installation_id,repository,configuration,revoked_at "
                "from platform.integrations where project_id=%s order by id",
                (project_id,),
            ).fetchall()
        }

    @app.delete("/integrations/{integration_id}")
    def revoke_integration(integration_id: UUID, ctx: Ctx):
        row, project = _integration(ctx, integration_id, True)
        ctx.db.execute("update platform.integrations set revoked_at=now() where id=%s", (integration_id,))
        ctx.audit("integration.revoked", integration_id, project)
        return {"revoked": True}

    @app.get("/projects/{project_id}/publication-targets")
    def publication_targets(project_id: UUID, ctx: Ctx):
        ctx.project(project_id, service.REVIEWERS)
        return {
            "items": ctx.db.execute(
                """select id,provider,repository,configuration,revoked_at from platform.integrations
            where project_id=%s and provider='github' and revoked_at is null
            and configuration->>'publish_enabled'='true' order by id""",
                (project_id,),
            ).fetchall()
        }

    @app.post("/callbacks/{integration_id}", status_code=202)
    async def callback(integration_id: UUID, request: Request):
        raw = await request.body()
        with transaction() as db:
            row = db.execute(
                """select i.*,p.policy from platform.integrations i join platform.projects p on p.id=i.project_id
                where i.id=%s and i.revoked_at is null and p.deleted_at is null for update of i""",
                (integration_id,),
            ).fetchone()
            if not row:
                raise PlatformError("NOT_FOUND", "Callback route not found.", 404)
            if row["provider"] == "webhook" or row["configuration"].get("relay"):
                raise PlatformError("INVALID_INPUT", "Use the task-correlated runtime callback endpoint.", 422)
            secret = os.environ.get(row["secret_ref"], "")
            if not secret:
                raise PlatformError("UNAVAILABLE", "The callback verifier is not configured.", 503)
            delivery = (
                request.headers.get("X-GitHub-Delivery")
                if row["provider"] == "github"
                else request.headers.get("X-Susanoo-Delivery")
            )
            if not delivery or len(delivery) > 200:
                raise PlatformError("INVALID_INPUT", "A bounded delivery identity is required.", 422)
            signature = (
                request.headers.get("X-Hub-Signature-256")
                if row["provider"] == "github"
                else request.headers.get("X-Susanoo-Signature")
            )
            expected = "sha256=" + hmac.new(secret.encode(), raw, "sha256").hexdigest()
            if row["provider"] in {"github", "ci"}:
                if not hmac.compare_digest(signature or "", expected):
                    raise PlatformError("FORBIDDEN", "Invalid callback signature.", 403)
                import json

                try:
                    payload = json.loads(raw)
                except ValueError:
                    raise PlatformError("INVALID_INPUT", "Callback JSON is invalid.", 422) from None
            else:
                if row["policy"]["mode"] == "local_only":
                    raise PlatformError("FORBIDDEN", "Local-only media cannot use the hosted relay.", 403)
                providers = entry_points(group="susanoo.callback_verifiers")
                media = next((entry for entry in providers if entry.name == "media"), None)
                if not media:
                    raise PlatformError(
                        "MISSING_CAPABILITY", "Install the runtime lane's media signature verifier.", 409
                    )
                payload = media.load()(raw, dict(request.headers), secret, row["configuration"])
            if not isinstance(payload, dict):
                raise PlatformError("INVALID_INPUT", "Callback must contain an object.", 422)
            if row["provider"] == "github":
                event = request.headers.get("X-GitHub-Event")
                installation_event = event in {"installation", "installation_repositories"}
                if payload.get("installation", {}).get("id") != row["installation_id"] or (
                    not installation_event and payload.get("repository", {}).get("full_name") != row["repository"]
                ):
                    raise PlatformError("FORBIDDEN", "Callback installation or repository does not match.", 403)
                if event not in {"push", "pull_request", "installation", "installation_repositories"}:
                    raise PlatformError("INVALID_INPUT", "Unsupported GitHub event.", 422)
                if event == "pull_request":
                    head = payload.get("pull_request", {}).get("head", {})
                    if head.get("repo", {}).get("full_name") != row["repository"]:
                        raise PlatformError("FORBIDDEN", "Untrusted fork code cannot obtain test credentials.", 403)
                    revision = head.get("sha")
                    branch = payload.get("pull_request", {}).get("base", {}).get("ref")
                else:
                    revision = payload.get("after")
                    branch = str(payload.get("ref", "")).removeprefix("refs/heads/")
                minimal = {"event": event, "revision": revision, "branch": branch}
            else:
                permitted = {
                    "task_id",
                    "status",
                    "event_id",
                    "revision",
                    "stream_id",
                    "call_id",
                    "sequence",
                    "timestamp",
                    "branch",
                }
                if not set(payload).issubset(permitted):
                    raise PlatformError("FORBIDDEN", "Callback payload exceeds the relay policy.", 403)
                if any(
                    type(value) not in {str, int, bool, type(None)} or (isinstance(value, str) and len(value) > 200)
                    for value in payload.values()
                ):
                    raise PlatformError(
                        "INVALID_INPUT", "Callback metadata must contain bounded scalar values only.", 422
                    )
                minimal = payload
            digest = hashlib.sha256(raw).hexdigest()
            prior = db.execute(
                "select payload_hash from platform.deliveries where integration_id=%s and delivery_id=%s",
                (integration_id, delivery),
            ).fetchone()
            if prior:
                if prior["payload_hash"] != digest:
                    raise PlatformError("IDEMPOTENCY_CONFLICT", "Delivery ID reused with different payload.", 409)
                return {"accepted": True, "duplicate": True}
            db.execute(
                "insert into platform.deliveries(workspace_id,project_id,integration_id,delivery_id,"
                "payload_hash,payload) "
                "values(%s,%s,%s,%s,%s,%s)",
                (row["workspace_id"], row["project_id"], integration_id, delivery, digest, Jsonb(minimal)),
            )
            removed = {
                item.get("full_name") for item in payload.get("repositories_removed", []) if isinstance(item, dict)
            }
            revoked = row["provider"] == "github" and (
                (event == "installation" and payload.get("action") == "deleted")
                or (event == "installation_repositories" and row["repository"] in removed)
            )
            if revoked:
                db.execute("update platform.integrations set revoked_at=now() where id=%s", (integration_id,))
            configuration = row["configuration"]
            execute_event = row["provider"] != "github" or event in {"push", "pull_request"}
            if configuration.get("trigger_run") and execute_event and not revoked:
                db.execute(
                    """insert into platform.jobs(workspace_id,project_id,kind,payload,dedupe_key)
                    values(%s,%s,'trigger',%s,%s) on conflict do nothing""",
                    (
                        row["workspace_id"],
                        row["project_id"],
                        Jsonb({"integration_id": str(integration_id), "delivery_id": delivery}),
                        delivery,
                    ),
                )
            return {"accepted": True, "duplicate": False}

    @app.post("/callbacks/github/app", status_code=202)
    async def github_app_callback(request: Request):
        raw = await request.body()
        secret = os.environ.get("PLATFORM_GITHUB_WEBHOOK_SECRET", "")
        signature = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(secret.encode(), raw, "sha256").hexdigest()
        if not secret or not hmac.compare_digest(signature, expected):
            raise PlatformError("FORBIDDEN", "Invalid GitHub App callback signature.", 403)
        try:
            payload = json.loads(raw)
            installation_id = payload["installation"]["id"]
            repository = payload.get("repository", {}).get("full_name")
        except (ValueError, KeyError, TypeError, AttributeError):
            raise PlatformError("INVALID_INPUT", "GitHub App callback payload is invalid.", 422) from None
        installation_event = request.headers.get("X-GitHub-Event") in {"installation", "installation_repositories"}
        with transaction() as db:
            rows = db.execute(
                """select id from platform.integrations where provider='github' and revoked_at is null
                and secret_ref='PLATFORM_GITHUB_WEBHOOK_SECRET' and installation_id=%s
                and (%s or repository=%s) order by id""",
                (installation_id, installation_event, repository),
            ).fetchall()
        for row in rows:
            await callback(row["id"], request)
        return {"accepted": True, "routes_processed": len(rows)}

    @app.get("/runner/integrations/{integration_id}/events")
    def callback_events(integration_id: UUID, ctx: RunnerCtx, after: int = Query(0, ge=0)):
        row = ctx.db.execute(
            "select * from platform.integrations where id=%s and project_id=%s and revoked_at is null",
            (integration_id, ctx.actor["project_id"]),
        ).fetchone()
        if not row:
            raise PlatformError("NOT_FOUND", "Integration not assigned to this runner.", 404)
        return {
            "items": ctx.db.execute(
                "select ingestion_seq,delivery_id,payload,created_at from platform.deliveries "
                "where integration_id=%s and ingestion_seq>%s order by ingestion_seq limit 100",
                (integration_id, after),
            ).fetchall()
        }

    @app.post("/projects/{project_id}/repairs", status_code=201)
    def repair(project_id: UUID, body: RepairIn, request: Request):
        with transaction() as db:
            _, _, artifact = _patch_inputs(_request_context(db, request, True), project_id, body)
        bundle = _patch_bytes(artifact)
        with transaction() as db:
            db.execute("select id from platform.projects where id=%s for update", (project_id,))
            ctx = _request_context(db, request, True)
            project, source, artifact = _patch_inputs(ctx, project_id, body)
            if bundle.base_commit != source["data"]["commit"]:
                raise PlatformError("STALE_BASE", "Patch base differs from the pinned source.", 409)
            patch_policy(bundle, source["data"]["allowed_patch_paths"])
            digest = content_hash({"request": body.model_dump(mode="json"), "patch_digest": artifact["digest"]})
            row = db.execute(
                """insert into platform.repairs(workspace_id,project_id,finding_id,source_version_id,patch_artifact_id,
                layer,base_commit,proposal_hash,publication_key,created_by)
                values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning *""",
                (
                    project["workspace_id"],
                    project_id,
                    body.finding_id,
                    body.source_version_id,
                    body.patch_artifact_id,
                    body.layer,
                    bundle.base_commit,
                    digest,
                    uuid4().hex,
                    ctx.actor["user_id"],
                ),
            ).fetchone()
            ctx.audit("repair.proposed", row["id"], project)
        return row

    @app.get("/projects/{project_id}/repairs")
    def repairs(project_id: UUID, ctx: Ctx):
        project = ctx.project(project_id)
        return {
            "items": ctx.db.execute(
                "select r.* from platform.repairs r join platform.findings f on f.id=r.finding_id "
                "where r.project_id=%s and (not f.hidden or %s) order by r.created_at desc limit 200",
                (project_id, project["hidden_access"]),
            ).fetchall()
        }

    @app.get("/repairs/{repair_id}")
    def repair_detail(repair_id: UUID, request: Request):
        with transaction() as db:
            ctx = _request_context(db, request)
            row, _ = _repair(ctx, repair_id)
            artifact, _ = artifacts.authorized_artifact(ctx, row["patch_artifact_id"])
        bundle = _patch_bytes(artifact)
        with transaction() as db:
            ctx = _request_context(db, request)
            row, _ = _repair(ctx, repair_id)
            artifacts.authorized_artifact(ctx, row["patch_artifact_id"])
        row["patch"] = bundle.model_dump(mode="json")
        return row

    @app.post("/repairs/{repair_id}/validate", status_code=202)
    def validate_repair(repair_id: UUID, key: Idempotency, ctx: Ctx):
        row, project = _repair(ctx, repair_id, service.ENGINEERS)
        job = workflows.create_job(
            ctx,
            project["id"],
            JobCreate(kind="repair_retest", version_ids=[row["source_version_id"]], finding_id=row["finding_id"]),
            key,
            repair=service.json_safe(
                {
                    name: row[name]
                    for name in ("id", "proposal_hash", "patch_artifact_id", "base_commit", "layer", "finding_id")
                }
            ),
        )
        ctx.db.execute(
            "update platform.repairs set validation_run_id=%s,state='validating',approved_by=null,approved_at=null "
            "where id=%s",
            (job["id"], repair_id),
        )
        return {"repair_id": repair_id, "validation_run_id": job["id"]}

    @app.post("/repairs/{repair_id}/approve")
    def approve_repair(repair_id: UUID, body: ApprovalIn, ctx: Ctx):
        row, project = _repair(ctx, repair_id, service.REVIEWERS)
        if row["created_by"] == ctx.actor["user_id"] or body.content_hash != row["proposal_hash"]:
            raise PlatformError("FORBIDDEN", "A different reviewer must approve the exact proposal hash.", 403)
        validation = ctx.db.execute(
            """select e.payload->'payload' result from platform.runs r join platform.attempts a on a.run_id=r.id
            join platform.runner_events e on e.attempt_id=a.id and e.event_type='job_result'
            where r.id=%s and r.status='completed' and r.gate='READY_FOR_ACCEPTANCE'""",
            (row["validation_run_id"],),
        ).fetchone()
        required = ("validation_passed", "heldout_passed", "controls_passed", "criteria_unchanged")
        if not validation or not all(validation["result"].get(value) is True for value in required):
            raise PlatformError(
                "VALIDATION_REQUIRED", "Reproduction, unchanged controls and held-out validation must pass.", 409
            )
        if validation["result"].get("base_commit") != row["base_commit"] or validation["result"].get(
            "patch_artifact_id"
        ) != str(row["patch_artifact_id"]):
            raise PlatformError(
                "VALIDATION_REQUIRED", "Validation must identify this exact patch and pinned base.", 409
            )
        ctx.db.execute(
            "update platform.repairs set state='approved',approved_by=%s,approved_at=now() where id=%s",
            (ctx.actor["user_id"], repair_id),
        )
        ctx.audit(
            "repair.approved",
            repair_id,
            project,
            {
                "proposal_hash": row["proposal_hash"],
                "rationale": body.rationale,
            },
        )
        return {"id": repair_id, "state": "approved"}

    @app.post("/repairs/{repair_id}/publish", status_code=202)
    def publish(repair_id: UUID, body: PublishIn, ctx: Ctx):
        row, project = _repair(ctx, repair_id, service.REVIEWERS)
        integration, integration_project = _integration(ctx, body.integration_id)
        if (
            integration_project["id"] != project["id"]
            or integration["provider"] != "github"
            or not integration["configuration"].get("publish_enabled")
        ):
            raise PlatformError(
                "FORBIDDEN", "An owner must separately enable this repository's publication permission.", 403
            )
        if (
            row["state"] not in {"approved", "publication_pending", "published"}
            or body.proposal_hash != row["proposal_hash"]
        ):
            raise PlatformError("APPROVAL_REQUIRED", "Approve the exact validated proposal before publication.", 409)
        source = ctx.version(row["source_version_id"], project, "source", True)
        if source["data"]["repository"] != integration["repository"]:
            raise PlatformError("FORBIDDEN", "Publication repository differs from the validated source.", 403)
        if row["pr_url"]:
            return {"id": repair_id, "state": "published", "pr_url": row["pr_url"]}
        ctx.db.execute("update platform.repairs set state='publication_pending' where id=%s", (repair_id,))
        ctx.db.execute(
            """insert into platform.jobs(workspace_id,project_id,kind,payload,dedupe_key)
            values(%s,%s,'publish',%s,%s) on conflict do nothing""",
            (
                project["workspace_id"],
                project["id"],
                Jsonb(
                    {
                        "repair_id": str(repair_id),
                        "integration_id": str(body.integration_id),
                        "authorized_by": str(ctx.actor["user_id"]),
                    }
                ),
                row["publication_key"],
            ),
        )
        ctx.audit("repair.publication_authorized", repair_id, project)
        return {"id": repair_id, "state": "publication_pending"}
