"""Local private-deployment auth and scoped opaque credentials; no user-editable JWT roles."""

import hashlib
import hmac
import os
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from psycopg.types.json import Jsonb

from .database import PlatformError

COOKIE = "susanoo_session"
CSRF_COOKIE = "susanoo_csrf"


def secret_key() -> bytes:
    value = os.environ.get("PLATFORM_SECRET_KEY", "")
    if len(value) < 32:
        raise PlatformError("UNAVAILABLE", "Configure a strong platform secret before signing in.", 503)
    return value.encode()


def secure_cookies() -> bool:
    return os.environ.get("PLATFORM_ENV", "production") != "development"


def password_hash(password: str) -> str:
    if not 12 <= len(password) <= 1024:
        raise PlatformError("INVALID_INPUT", "Passwords must contain 12 to 1024 characters.", 422)
    salt = secrets.token_hex(16)
    value = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
    return f"scrypt${salt}${value}"


def password_matches(password: str, encoded: str | None) -> bool:
    if not encoded or len(password) > 1024:
        return False
    try:
        _, salt, expected = encoded.split("$")
        actual = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def issue_token(db, kind: str, *, user_id=None, project=None, runner_id=None, hours=1, metadata=None):
    token = "snp_" + secrets.token_urlsafe(40)
    row = db.execute(
        """insert into platform.credentials(token_hash,kind,user_id,workspace_id,project_id,
        runner_id,expires_at,metadata)
        values(%s,%s,%s,%s,%s,%s,%s,%s) returning id""",
        (
            token_hash(token),
            kind,
            user_id,
            project["workspace_id"] if project else None,
            project["id"] if project else None,
            runner_id,
            datetime.now(UTC) + timedelta(hours=hours),
            Jsonb(metadata or {}),
        ),
    ).fetchone()
    return {"token": token, "credential_id": str(row["id"])}


def credential(db, token: str, kinds: set[str]) -> dict[str, Any]:
    row = db.execute(
        """select c.* from platform.credentials c left join platform.identities i on i.id=c.user_id
        where token_hash=%s and c.revoked_at is null and c.consumed_at is null and c.expires_at>now()
        and (i.id is null or not i.disabled)""",
        (token_hash(token),),
    ).fetchone()
    if not row or row["kind"] not in kinds:
        raise PlatformError("UNAUTHENTICATED", "Sign in again or use a valid scoped credential.", 401)
    if row["project_id"]:
        alive = db.execute(
            """select 1 from platform.projects p join platform.workspaces w on w.id=p.workspace_id
            where p.id=%s and p.deleted_at is null and w.deleted_at is null""",
            (row["project_id"],),
        ).fetchone()
        if not alive:
            raise PlatformError("UNAUTHENTICATED", "This credential's project is no longer available.", 401)
    return row


def csrf_value() -> str:
    nonce = secrets.token_urlsafe(24)
    return nonce + "." + hmac.new(secret_key(), nonce.encode(), "sha256").hexdigest()


def check_csrf(request):
    cookie = request.cookies.get(CSRF_COOKIE, "")
    supplied = request.headers.get("X-CSRF-Token", "")
    try:
        nonce, signature = cookie.split(".")
    except ValueError:
        raise PlatformError("CSRF_FAILED", "Refresh the page before submitting this action.", 403) from None
    expected = hmac.new(secret_key(), nonce.encode(), "sha256").hexdigest()
    origin = request.headers.get("Origin", "")
    allowed = os.environ.get("PLATFORM_ORIGIN", "http://localhost:3000").rstrip("/")
    if not hmac.compare_digest(cookie, supplied) or not hmac.compare_digest(signature, expected) or origin != allowed:
        raise PlatformError("CSRF_FAILED", "The action did not originate from the approved application.", 403)


def authenticated(db, request, runner=False):
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() == "bearer":
        actor = credential(db, token, {"runner"} if runner else {"cli"})
    else:
        if runner:
            raise PlatformError("UNAUTHENTICATED", "A runner credential is required.", 401)
        actor = credential(db, request.cookies.get(COOKIE, ""), {"session"})
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            check_csrf(request)
    if actor.get("runner_id"):
        found = db.execute(
            "select * from platform.runners where id=%s and revoked_at is null", (actor["runner_id"],)
        ).fetchone()
        if not found:
            raise PlatformError("UNAUTHENTICATED", "Runner credential revoked.", 401)
        actor["runner"] = found
    return actor


def create_identity(db, email: str, password: str):
    return db.execute(
        "insert into platform.identities(id,email,password_hash) values(%s,%s,%s) returning id,email",
        (uuid4(), email.strip().lower(), password_hash(password)),
    ).fetchone()
