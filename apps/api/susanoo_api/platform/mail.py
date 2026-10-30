"""Credential mail outbox. Sending is an idempotency-aware internal worker concern."""

import base64
import hashlib
import os
from datetime import UTC, datetime

from cryptography.fernet import Fernet


from .database import Context, PlatformError
from .security import create_identity, credential, issue_token, password_hash, password_matches
from .service import OWNERS, workspace_member


def _require_mail():
    if not os.environ.get("PLATFORM_SMTP_HOST"):
        raise PlatformError("UNAVAILABLE", "Configure the private deployment's email service first.", 503)


def mail_cipher():
    secret = os.environ.get("PLATFORM_SECRET_KEY", "")
    if len(secret) < 32:
        raise PlatformError("UNAVAILABLE", "A strong platform secret is required for credential mail.", 503)
    key = hashlib.sha256(b"susanoo:credential-mail:v1:" + secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def _enqueue(db, recipient, subject, token, route):
    origin = os.environ.get("PLATFORM_ORIGIN", "http://localhost:3000").rstrip("/")
    # The fragment avoids putting a credential in proxy request logs or referrer URLs.
    link = f"{origin}/evaluation/{route}#token={token['token']}"
    db.execute(
        "insert into platform.mail_outbox(recipient,subject,body,credential_id) values(%s,%s,%s,%s)",
        (
            recipient,
            subject,
            mail_cipher().encrypt(f"Open your authorized Susanoo link before it expires:\n{link}\n".encode()).decode(),
            token["credential_id"],
        ),
    )


def invite(ctx, workspace_id, body):
    _require_mail()
    workspace_member(ctx, workspace_id, OWNERS)
    project = ctx.project(body.project_id, OWNERS) if body.project_id else None
    if project and project["workspace_id"] != workspace_id:
        raise PlatformError("NOT_FOUND", "Project not found.", 404)
    metadata = {
        "email": body.email.lower(),
        "roles": list(body.roles),
        "hidden_access": body.hidden_access,
        "workspace_id": str(workspace_id),
    }
    token = issue_token(
        ctx.db, "invitation", project=project, user_id=ctx.actor["user_id"], hours=24, metadata=metadata
    )
    _enqueue(ctx.db, body.email, "Susanoo project invitation", token, "accept")
    ctx.audit("invitation.created", token["credential_id"], project)
    return {"invitation_id": token["credential_id"], "delivery": "queued"}


def accept_invitation(db, token, password):
    row = credential(db, token, {"invitation"})
    row = db.execute("select * from platform.credentials where id=%s for update", (row["id"],)).fetchone()
    if row["consumed_at"] or row["revoked_at"] or row["expires_at"] <= datetime.now(UTC):
        raise PlatformError("UNAUTHENTICATED", "Invitation already consumed or revoked.", 401)
    meta = row["metadata"]
    workspace_member(
        Context(db, {"user_id": row["user_id"]}, "00000000-0000-0000-0000-000000000000"), meta["workspace_id"], OWNERS
    )
    user = db.execute("select * from platform.identities where email=%s", (meta["email"],)).fetchone()
    if user:
        if user["disabled"] or not password_matches(password, user["password_hash"]):
            raise PlatformError(
                "UNAUTHENTICATED", "Use the existing account's password to accept this invitation.", 401
            )
    else:
        user = create_identity(db, meta["email"], password)
    db.execute(
        "insert into platform.memberships values(%s,%s,%s) on conflict do nothing",
        (meta["workspace_id"], user["id"], meta["roles"] if not row["project_id"] else ["viewer"]),
    )
    if row["project_id"]:
        db.execute(
            """insert into platform.project_grants values(%s,%s,%s,%s,%s)
            on conflict(project_id,user_id) do update set roles=excluded.roles,hidden_access=excluded.hidden_access""",
            (meta["workspace_id"], row["project_id"], user["id"], meta["roles"], meta["hidden_access"]),
        )
    db.execute("update platform.credentials set consumed_at=now() where id=%s", (row["id"],))
    db.execute("update platform.mail_outbox set body='' where credential_id=%s", (row["id"],))
    return {"accepted": True, "sign_in_required": True}


def request_password_reset(db, email):
    _require_mail()
    user = db.execute(
        "select id from platform.identities where email=%s and not disabled", (email.strip().lower(),)
    ).fetchone()
    if user:
        db.execute("select id from platform.identities where id=%s for update", (user["id"],))
        recent = db.execute(
            "select count(*) n from platform.credentials where user_id=%s and kind='password_reset' "
            "and created_at>now()-interval '1 hour'",
            (user["id"],),
        ).fetchone()["n"]
        if recent < 3:
            token = issue_token(db, "password_reset", user_id=user["id"], hours=1 / 2)
            _enqueue(db, email, "Susanoo password reset", token, "reset")
    return {"message": "If this account is eligible, a reset link will be sent."}


def complete_password_reset(db, token, password):
    row = credential(db, token, {"password_reset"})
    consumed = db.execute(
        "update platform.credentials set consumed_at=now() where id=%s and consumed_at is null "
        "and revoked_at is null and expires_at>now() returning id",
        (row["id"],),
    ).fetchone()
    if not consumed:
        raise PlatformError("UNAUTHENTICATED", "Reset link already consumed.", 401)
    db.execute("update platform.identities set password_hash=%s where id=%s", (password_hash(password), row["user_id"]))
    db.execute(
        "update platform.credentials set revoked_at=now() "
        "where user_id=%s and kind in ('session','cli','password_reset')",
        (row["user_id"],),
    )
    db.execute("update platform.mail_outbox set body='' where credential_id=%s", (row["id"],))
    return {"reset": True, "sessions_revoked": True}
