"""Retry only safe internal jobs. Expired customer attempts are interrupted, never redispatched."""

import hashlib
import logging
import os
import smtplib
import time
from email.message import EmailMessage
from uuid import uuid4

from susanoo_contracts import PatchBundle, RunCreate

from . import artifacts, service
from .database import Context, PlatformError, transaction
from .github_app import GitHubApp

log = logging.getLogger(__name__)
_last_orphan_sweep = 0.0


def tick():
    global _last_orphan_sweep
    expired = []
    with transaction() as db:
        db.execute(
            """update platform.jobs set status='dead_letter',error_code='WORKER_LEASE_EXHAUSTED'
            where status='running' and attempts>=5 and lease_expires_at<now()"""
        )
        expired = db.execute(
            """update platform.attempts set execution_state='interrupted',business_verdict='UNKNOWN',
            reconciliation_state='pending',reason='Lease expired; evidence and side effects require reconciliation'
            where execution_state in ('preparing','running','observing','evaluating') and lease_expires_at<now()
            returning run_id""",
        ).fetchall()
        for row in expired:
            service.project_gate(db, row["run_id"])
        db.execute(
            """update platform.resource_leases l set state='quarantined' from platform.attempts a
            where l.attempt_id=a.id and l.state='leased' and a.execution_state in ('interrupted','timed_out','error')"""
        )
        timed_out = db.execute(
            """update platform.attempts a set execution_state='timed_out',business_verdict='UNKNOWN',
            cleanup_state='not_required',reason='Run budget elapsed before dispatch'
            from platform.runs r where a.run_id=r.id and a.execution_state='queued'
            and r.created_at+make_interval(secs=>(r.manifest->'limits'->>'run_timeout_seconds')::int)<=now()
            returning a.run_id"""
        ).fetchall()
        for row in timed_out:
            service.project_gate(db, row["run_id"])
        schedules = db.execute(
            """select s.* from platform.schedules s join platform.projects p on p.id=s.project_id
            where s.enabled and s.next_due_at<=now() and p.deleted_at is null
            order by s.next_due_at for update of s skip locked limit 10""",
        ).fetchall()
        for schedule in schedules:
            ctx = Context(db, {"user_id": schedule["created_by"]}, str(uuid4()))
            try:
                with db.transaction():
                    service.create_run(
                        ctx,
                        schedule["project_id"],
                        RunCreate.model_validate(schedule["configuration"]),
                        f"schedule:{schedule['id']}:{schedule['next_due_at'].isoformat()}",
                    )
            except PlatformError as exc:
                ctx.audit("schedule.dispatch_blocked", schedule["id"], details={"code": exc.code})
            db.execute(
                "update platform.schedules set next_due_at=now()+make_interval(secs=>interval_seconds) where id=%s",
                (schedule["id"],),
            )
    count = 0
    for _ in range(10):
        if not process_one():
            break
        count += 1
    process_mail()
    from .source_access import revoke_settled_grants

    revoke_settled_grants()
    expire_artifacts()
    expire_evidence()
    purge_deleted()
    if time.monotonic() - _last_orphan_sweep > 3600:
        with transaction() as db:
            referenced = {
                row["object_key"] for row in db.execute("select object_key from platform.artifacts").fetchall()
            }
        artifacts.ObjectStore().sweep_unreferenced(referenced)
        _last_orphan_sweep = time.monotonic()
    return {"expired_attempts": len(expired), "internal_jobs": count}


def process_one():
    worker_id = uuid4()
    with transaction() as db:
        job = db.execute(
            """select j.* from platform.jobs j join platform.projects p on p.id=j.project_id
            where (j.status='queued' or (j.status='running' and j.lease_expires_at<now()))
            and j.available_at<=now() and j.attempts<5 and p.deleted_at is null
            order by j.available_at for update of j skip locked limit 1""",
        ).fetchone()
        if not job:
            return False
        db.execute(
            "update platform.jobs set status='running',claimed_by=%s,lease_expires_at=now()+interval '60 seconds',"
            "attempts=attempts+1 where id=%s",
            (worker_id, job["id"]),
        )
    try:
        if job["kind"] == "export":
            export_job(job, worker_id)
        elif job["kind"] == "publish":
            publish_job(job)
        elif job["kind"] == "trigger":
            trigger_job(job)
        else:
            raise PlatformError("UNSUPPORTED_JOB", "Unknown internal job type.", 422)
        with transaction() as db:
            db.execute(
                "update platform.jobs set status='completed',error_code=null where id=%s and claimed_by=%s",
                (job["id"], worker_id),
            )
    except Exception as exc:
        code = exc.code if isinstance(exc, PlatformError) else "INTERNAL_JOB_ERROR"
        with transaction() as db:
            db.execute(
                """update platform.jobs set status=case when attempts>=5 or %s then 'dead_letter' else 'queued' end,
                error_code=%s,available_at=now()+make_interval(secs=>least(300,attempts*15))
                where id=%s and claimed_by=%s""",
                (code in {"FORBIDDEN", "NOT_FOUND", "STALE_BASE", "APPROVAL_REQUIRED"}, code, job["id"], worker_id),
            )
            if job["kind"] == "export":
                db.execute(
                    """update platform.artifacts set status='failed' where id=%s and status='pending'
                    and exists(select 1 from platform.jobs where id=%s and status='dead_letter')""",
                    (job["payload"]["artifact_id"], job["id"]),
                )
        log.warning("Internal job %s failed with code %s", job["id"], code)
    return True


def export_job(job, worker_id):
    with transaction() as db:
        artifact = db.execute(
            "select * from platform.artifacts where id=%s", (job["payload"]["artifact_id"],)
        ).fetchone()
        if not artifact or artifact["status"] == "ready":
            return
        ctx = Context(db, {"user_id": artifact["created_by"]}, str(uuid4()))
        artifacts.authorized_artifact(ctx, artifact["id"])
    content = artifacts.render_export(artifact["snapshot"], artifact["export_format"])
    if len(content) > 10485760:
        raise PlatformError(
            "EXPORT_TOO_LARGE", "Export exceeds the 10 MiB artifact limit; split the evaluation scope.", 422
        )
    staged_key = f"{artifact['workspace_id']}/{artifact['project_id']}/{uuid4()}"
    store = artifacts.ObjectStore()
    success = False
    try:
        store.put(staged_key, content, artifact["content_type"])
        linked = False
        with transaction() as db:
            db.execute("select id from platform.projects where id=%s for update", (artifact["project_id"],))
            current = db.execute(
                "select * from platform.jobs where id=%s and claimed_by=%s and status='running' for update",
                (job["id"], worker_id),
            ).fetchone()
            if current:
                ctx = Context(db, {"user_id": artifact["created_by"]}, str(uuid4()))
                artifacts.authorized_artifact(ctx, artifact["id"])
                linked = bool(
                    db.execute(
                        "update platform.artifacts set status='ready',digest=%s,size=%s,object_key=%s "
                        "where id=%s and status='pending' returning id",
                        (hashlib.sha256(content).hexdigest(), len(content), staged_key, artifact["id"]),
                    ).fetchone()
                )
        success = linked
    finally:
        if not success:
            store.delete(staged_key)


def publish_job(job):
    with transaction() as db:
        repair = db.execute("select * from platform.repairs where id=%s", (job["payload"]["repair_id"],)).fetchone()
        integration = db.execute(
            "select * from platform.integrations where id=%s and revoked_at is null",
            (job["payload"]["integration_id"],),
        ).fetchone()
        if not repair or not integration:
            raise PlatformError("NOT_FOUND", "Publication inputs revoked.", 404)
        if repair["pr_url"]:
            return
        if repair["state"] != "publication_pending" or not integration["configuration"].get("publish_enabled"):
            raise PlatformError("APPROVAL_REQUIRED", "Publication is not authorized.", 409)
        ctx = Context(db, {"user_id": job["payload"]["authorized_by"]}, str(uuid4()))
        ctx.project(repair["project_id"], service.REVIEWERS)
        ctx = Context(db, {"user_id": repair["approved_by"]}, str(uuid4()))
        project = ctx.project(repair["project_id"], service.REVIEWERS)
        source = ctx.version(repair["source_version_id"], project, "source", True)
        artifact, _ = artifacts.authorized_artifact(ctx, repair["patch_artifact_id"])
    bundle = PatchBundle.model_validate_json(artifacts.ObjectStore().get(artifact["object_key"]))
    client = GitHubApp()
    try:
        token = client.installation_token(integration["installation_id"], integration["repository"], write=True)
        url = client.draft_pr(integration["repository"], token, repair, bundle, source["data"]["allowed_patch_paths"])
    finally:
        client.close()
    with transaction() as db:
        db.execute(
            "update platform.repairs set state='published',pr_url=%s where id=%s and publication_key=%s",
            (url, repair["id"], repair["publication_key"]),
        )
        ctx = Context(db, {"user_id": repair["approved_by"]}, str(uuid4()))
        ctx.audit("repair.published", repair["id"], project)


def trigger_job(job):
    with transaction() as db:
        integration = db.execute(
            "select * from platform.integrations where id=%s and revoked_at is null",
            (job["payload"]["integration_id"],),
        ).fetchone()
        if not integration:
            raise PlatformError("NOT_FOUND", "Trigger installation revoked.", 404)
        configuration = integration["configuration"]
        payload = db.execute(
            "select payload from platform.deliveries where integration_id=%s and delivery_id=%s",
            (integration["id"], job["payload"]["delivery_id"]),
        ).fetchone()["payload"]
        if configuration.get("approved_branch") and payload.get("branch") != configuration["approved_branch"]:
            raise PlatformError("FORBIDDEN", "Trigger branch not approved.", 403)
        project = db.execute(
            "select * from platform.projects where id=%s and deleted_at is null", (integration["project_id"],)
        ).fetchone()
        if not integration["created_by"]:
            raise PlatformError("FORBIDDEN", "No authorized trigger owner.", 403)
        ctx = Context(db, {"user_id": integration["created_by"]}, str(uuid4()))
        ctx.project(project["id"], service.OWNERS)
        body = RunCreate.model_validate(configuration["trigger_run"])
        connection = ctx.version(body.connection_version_id, ctx.project(project["id"]), "connection")
        if connection["data"].get("source_commit") != payload.get("revision"):
            raise PlatformError(
                "STALE_BASE", "Trigger revision requires a new approved pinned connection version.", 409
            )
        service.create_run(
            ctx, project["id"], body, f"trigger:{integration['id']}:{payload.get('revision')}:{body.suite_version_id}"
        )


def process_mail():
    host = os.environ.get("PLATFORM_SMTP_HOST")
    if not host:
        return
    with transaction() as db:
        db.execute(
            """update platform.mail_outbox set status='failed' where status='sending' and attempts>=5
            and claimed_at<now()-interval '2 minutes'"""
        )
        rows = db.execute(
            """select m.* from platform.mail_outbox m join platform.credentials c on c.id=m.credential_id
            where (m.status='queued' or (m.status='sending' and m.claimed_at<now()-interval '2 minutes'))
            and m.attempts<5 and c.expires_at>now() and c.revoked_at is null and c.consumed_at is null
            order by m.created_at for update of m skip locked limit 10""",
        ).fetchall()
        for row in rows:
            db.execute(
                "update platform.mail_outbox set status='sending',claimed_at=now(),attempts=attempts+1 where id=%s",
                (row["id"],),
            )
    # The claim is committed before network I/O. SMTP may duplicate a message after
    # an uncertain delivery, but a stable Message-ID and single-use token bound its effects.
    from .mail import mail_cipher

    for row in rows:
        try:
            message = EmailMessage()
            message["From"] = os.environ.get("PLATFORM_SMTP_FROM", "susanoo@localhost")
            message["To"] = row["recipient"]
            message["Subject"] = row["subject"]
            message["Message-ID"] = f"<{row['id']}@susanoo.local>"
            message.set_content(mail_cipher().decrypt(row["body"].encode()).decode())
            with smtplib.SMTP(host, int(os.environ.get("PLATFORM_SMTP_PORT", "1025")), timeout=10) as smtp:
                if os.environ.get("PLATFORM_SMTP_TLS") == "true":
                    smtp.starttls()
                if os.environ.get("PLATFORM_SMTP_USER"):
                    smtp.login(os.environ["PLATFORM_SMTP_USER"], os.environ["PLATFORM_SMTP_PASSWORD"])
                smtp.send_message(message)
        except Exception:
            with transaction() as db:
                db.execute(
                    "update platform.mail_outbox set status=case when attempts>=5 then 'failed' else 'queued' end "
                    "where id=%s",
                    (row["id"],),
                )
            log.warning("Credential mail delivery failed; no token logged (%s)", row["id"])
        else:
            with transaction() as db:
                db.execute("update platform.mail_outbox set status='sent',body='' where id=%s", (row["id"],))
    with transaction() as db:
        db.execute(
            """update platform.mail_outbox m set status='expired',body='' from platform.credentials c
            where m.credential_id=c.id
            and (c.expires_at<=now() or c.revoked_at is not null or c.consumed_at is not null)"""
        )


def expire_evidence():
    with transaction() as db:
        runs = db.execute(
            """select r.* from platform.runs r join platform.projects p on p.id=r.project_id
            where r.status in ('completed','interrupted','cancelled') and p.deleted_at is null
            and r.created_at+make_interval(days=>(p.policy->>'hosted_retention_days')::int)<=now()
            and not exists(select 1 from platform.retention_receipts t where t.run_id=r.id)
            order by r.created_at for update of r skip locked limit 50"""
        ).fetchall()
        for run in runs:
            for table in ("evidence", "runner_events"):
                from psycopg import sql

                db.execute(
                    sql.SQL(
                        "delete from platform.{} where attempt_id in (select id from platform.attempts where run_id=%s)"
                    ).format(sql.Identifier(table)),
                    (run["id"],),
                )
            db.execute(
                "insert into platform.retention_receipts(workspace_id,project_id,run_id,reason) values(%s,%s,%s,%s)",
                (
                    run["workspace_id"],
                    run["project_id"],
                    run["id"],
                    "Hosted evidence retention elapsed; original verdicts retained",
                ),
            )


def expire_artifacts():
    with transaction() as db:
        rows = db.execute(
            "select id,object_key from platform.artifacts where expires_at<=now() and status<>'purged' limit 100"
        ).fetchall()
    for row in rows:
        artifacts.ObjectStore().delete(row["object_key"])
        with transaction() as db:
            db.execute("update platform.artifacts set status='purged',snapshot=null where id=%s", (row["id"],))


def purge_deleted():
    with transaction() as db:
        projects = db.execute(
            """select t.project_id from platform.deletion_tombstones t where t.purged_at is null
            and not exists(select 1 from platform.source_grants g where g.project_id=t.project_id
            and g.state<>'revoked' and g.expires_at>now()) limit 10"""
        ).fetchall()
    for project in projects:
        project_id = project["project_id"]
        with transaction() as db:
            objects = db.execute(
                "select object_key from platform.artifacts where project_id=%s", (project_id,)
            ).fetchall()
        for obj in objects:
            artifacts.ObjectStore().delete(obj["object_key"])
        with transaction() as db:
            db.execute(
                """update platform.projects set policy_version_id=null,policy='{}'::jsonb,
                name='Deleted project',workflow='deleted' where id=%s and deleted_at is not null""",
                (project_id,),
            )
            db.execute(
                """delete from platform.mail_outbox where credential_id in
                (select id from platform.credentials where project_id=%s)""",
                (project_id,),
            )
            # This fixed order follows the private-schema FK graph; no user-provided table names.
            for table in (
                "retention_receipts",
                "source_grants",
                "resource_leases",
                "reconciliations",
                "budget_reservations",
                "repairs",
                "deliveries",
                "integrations",
                "jobs",
                "schedules",
                "invalidations",
                "review_events",
                "findings",
                "evidence",
                "runner_events",
                "artifacts",
                "attempts",
                "runs",
                "credentials",
                "runners",
                "version_links",
                "approvals",
                "versions",
                "project_grants",
                "audit",
            ):
                from psycopg import sql

                db.execute(
                    sql.SQL("delete from platform.{} where project_id=%s").format(sql.Identifier(table)), (project_id,)
                )
            db.execute("update platform.deletion_tombstones set purged_at=now() where project_id=%s", (project_id,))
