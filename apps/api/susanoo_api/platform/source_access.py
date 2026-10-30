"""Attempt-bound GitHub App read credentials with encrypted, leased revocation records."""

import base64
import hashlib
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from cryptography.fernet import Fernet
from fastapi import Request
from susanoo_contracts import SourceCredentialIn, SourceCredentialResponse

from . import service
from .database import Context, PlatformError, transaction
from .github_app import GitHubApp
from .security import authenticated, secret_key

log = logging.getLogger(__name__)


def cipher():
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"susanoo:source-grants:v1:" + secret_key()).digest()))


def authorize(db, request, attempt_id, body):
    ctx = Context(db, authenticated(db, request, runner=True), request.state.correlation_id)
    attempt = service.owned_attempt(ctx, attempt_id, body.lease_token)
    source = next(
        (
            v
            for v in attempt["manifest"].get("versions", [])
            if v["id"] == str(body.source_version_id) and v["kind"] == "source"
        ),
        None,
    )
    if not source or not source["approved"] or not source["data"]["trusted"]:
        raise PlatformError("FORBIDDEN", "Source credentials require a pinned, approved trusted source revision.", 403)
    data = source["data"]
    integration = db.execute(
        """select id from platform.integrations where project_id=%s and provider='github'
        and installation_id=%s and repository=%s and revoked_at is null limit 1""",
        (attempt["project_id"], data["installation_id"], data["repository"]),
    ).fetchone()
    if not integration:
        raise PlatformError(
            "FORBIDDEN", "No current owner-authorized installation grants access to this repository.", 403
        )
    if db.execute(
        "select 1 from platform.source_grants where attempt_id=%s and state<>'revoked' and expires_at>now()",
        (attempt_id,),
    ).fetchone():
        raise PlatformError(
            "INVALID_STATE", "A source credential was already issued; reconcile its disposition first.", 409
        )
    return ctx, attempt, data


def mount_source_access(app):
    @app.post("/runner/attempts/{attempt_id}/source-credential", response_model=SourceCredentialResponse)
    def source_credential(attempt_id: UUID, body: SourceCredentialIn, request: Request):
        with transaction() as db:
            _, _, source = authorize(db, request, attempt_id, body)
        client = GitHubApp()
        issued = None
        recorded = False
        try:
            issued = client.installation_token_info(source["installation_id"], source["repository"], write=False)
            expiry = datetime.fromisoformat(issued["expires_at"].replace("Z", "+00:00"))
            if expiry.tzinfo is None or not datetime.now(UTC) < expiry <= datetime.now(UTC) + timedelta(minutes=65):
                raise PlatformError("PROVIDER_ERROR", "Source credential expiry is outside the supported bound.", 502)
            with transaction() as db:
                ctx, attempt, source = authorize(db, request, attempt_id, body)
                db.execute(
                    """insert into platform.source_grants(workspace_id,project_id,attempt_id,repository,
                    token_cipher,expires_at)
                    values(%s,%s,%s,%s,%s,%s)""",
                    (
                        attempt["workspace_id"],
                        attempt["project_id"],
                        attempt_id,
                        source["repository"],
                        cipher().encrypt(issued["token"].encode()).decode(),
                        expiry,
                    ),
                )
                ctx.audit(
                    "source.read_credential_issued",
                    attempt_id,
                    {"workspace_id": attempt["workspace_id"], "id": attempt["project_id"]},
                )
            recorded = True
            return {
                "token": issued["token"],
                "expires_at": expiry,
                "repository": source["repository"],
                "source_commit": source["commit"],
                "read_only": True,
            }
        finally:
            try:
                if issued and not recorded:
                    client.revoke_token(issued["token"])
            finally:
                client.close()

    @app.post("/runner/attempts/{attempt_id}/source-credential/revoke")
    def revoke_source_credential(attempt_id: UUID, body: SourceCredentialIn, request: Request):
        with transaction() as db:
            ctx = Context(db, authenticated(db, request, runner=True), request.state.correlation_id)
            service.owned_attempt(ctx, attempt_id, body.lease_token, read_only=True)
            db.execute(
                "update platform.source_grants set state='revoking',claimed_at=null "
                "where attempt_id=%s and state='active'",
                (attempt_id,),
            )
        return {"revocation_queued": True}


def revoke_settled_grants():
    with transaction() as db:
        db.execute("update platform.source_grants set state='revoked',token_cipher='' where expires_at<=now()")
        rows = db.execute(
            """select g.* from platform.source_grants g join platform.attempts a on a.id=g.attempt_id
            join platform.projects p on p.id=g.project_id
            where g.state in ('active','revoking') and g.revoke_attempts<5 and g.expires_at>now()
            and (g.claimed_at is null or g.claimed_at<now()-interval '60 seconds')
            and (g.state='revoking' or a.lease_expires_at<=now()
                or a.finalized_hash is not null or p.deleted_at is not null
                or exists(select 1 from platform.integrations i where i.project_id=g.project_id
                and i.repository=g.repository and i.revoked_at is not null))
            order by g.expires_at for update of g skip locked limit 10"""
        ).fetchall()
        for row in rows:
            db.execute(
                "update platform.source_grants set state='revoking',claimed_at=now(),"
                "revoke_attempts=revoke_attempts+1 where id=%s",
                (row["id"],),
            )
    for row in rows:
        client = GitHubApp()
        try:
            client.revoke_token(cipher().decrypt(row["token_cipher"].encode()).decode())
        except Exception as exc:
            log.warning("Source credential revocation failed grant=%s type=%s", row["id"], type(exc).__name__)
            with transaction() as db:
                db.execute(
                    "update platform.source_grants set state=case when revoke_attempts>=5 then 'failed' "
                    "else 'revoking' end where id=%s",
                    (row["id"],),
                )
        else:
            with transaction() as db:
                db.execute(
                    "update platform.source_grants set state='revoked',token_cipher='' where id=%s", (row["id"],)
                )
        finally:
            client.close()
