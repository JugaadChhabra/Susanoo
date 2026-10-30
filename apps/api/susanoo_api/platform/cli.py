"""Private platform administration. Does not execute customer agents or deploy remotely."""

import argparse
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv
from .local_security import secure_local_path

ROOT = Path(__file__).resolve().parents[4]
STATE = ROOT / ".platform"
ENV = ROOT / ".env.platform"


def private_write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        secure_local_path(path)
        stream.write(value)


def migrate():
    from .database import transaction

    with transaction() as db:
        db.execute("select pg_advisory_xact_lock(727003)")
        exists = db.execute("select to_regclass('platform.schema_versions') found").fetchone()["found"]
        applied = (
            {row["version"] for row in db.execute("select version from platform.schema_versions").fetchall()}
            if exists
            else set()
        )
        for path in sorted((ROOT / "supabase/migrations").glob("20*platform*.sql")):
            version = path.name.split("_", 1)[0]
            if version not in applied:
                db.execute(path.read_text(encoding="utf-8"))
                print(f"Applied {path.name}")


def bootstrap():
    STATE.mkdir(exist_ok=True, mode=0o700)
    secure_local_path(STATE)
    if ENV.exists():
        secure_local_path(ENV)
    if not ENV.exists():
        password = secrets.token_urlsafe(32)
        tools = STATE / "postgres-tools/pgsql/bin"
        if not os.environ.get("PLATFORM_DATABASE_URL") and tools.exists():
            password_file = STATE / "postgres-password"
            private_write(password_file, password)
            data = STATE / "postgres-data"
            if not data.exists():
                subprocess.run(
                    [
                        str(tools / "initdb.exe"),
                        "-D",
                        str(data),
                        "-U",
                        "susanoo",
                        "--auth=scram-sha-256",
                        "--pwfile",
                        str(password_file),
                        "--encoding=UTF8",
                        "--locale=C",
                    ],
                    check=True,
                )
            subprocess.run(
                [
                    str(tools / "pg_ctl.exe"),
                    "-D",
                    str(data),
                    "-l",
                    str(STATE / "postgres.log"),
                    "-o",
                    "-p 55432 -h 127.0.0.1",
                    "-w",
                    "start",
                ],
                check=True,
                timeout=40,
            )
            url = f"postgresql://susanoo:{password}@127.0.0.1:55432/postgres"
        else:
            url = os.environ.get("PLATFORM_DATABASE_URL")
            if not url:
                raise SystemExit(
                    "Set PLATFORM_DATABASE_URL to a private PostgreSQL database, or start deploy/platform.compose.yml."
                )
        private_write(
            ENV,
            (
                f"PLATFORM_DATABASE_URL={url}\nPLATFORM_SECRET_KEY={secrets.token_urlsafe(48)}\n"
                "PLATFORM_ENV=development\nPLATFORM_ORIGIN=http://localhost:3000\n"
                f"PLATFORM_ARTIFACT_DIR={STATE.as_posix()}/artifacts\n"
            ),
        )
    load_dotenv(ENV, override=False)
    migrate()
    seed()


def seed():
    if os.environ.get("PLATFORM_ENV") != "development":
        raise SystemExit("Synthetic accounts require development mode. Use init-owner for a private release.")
    from .database import Context, transaction
    from .security import create_identity
    from .service import create_project, create_workspace
    from susanoo_contracts import ProjectCreate

    created = []
    with transaction() as db:
        for role in ("owner", "reviewer", "viewer"):
            email = f"{role}@susanoo.local"
            row = db.execute("select id from platform.identities where email=%s", (email,)).fetchone()
            if not row:
                password = secrets.token_urlsafe(24)
                row = create_identity(db, email, password)
                created.append({"email": email, "password": password})
            if role == "owner":
                owner = row["id"]
        ctx = Context(db, {"user_id": owner}, str(uuid4()))
        workspace = db.execute(
            "select w.* from platform.workspaces w join platform.memberships m on m.workspace_id=w.id "
            "where m.user_id=%s and w.deleted_at is null order by w.created_at limit 1",
            (owner,),
        ).fetchone()
        if not workspace:
            workspace = create_workspace(ctx, "Susanoo Private Evaluation")
        for role in ("reviewer", "viewer"):
            user = db.execute(
                "select id from platform.identities where email=%s", (f"{role}@susanoo.local",)
            ).fetchone()
            db.execute(
                "insert into platform.memberships values(%s,%s,%s) on conflict do nothing",
                (workspace["id"], user["id"], [role]),
            )
        if not db.execute(
            "select 1 from platform.projects where workspace_id=%s and deleted_at is null", (workspace["id"],)
        ).fetchone():
            create_project(
                ctx,
                ProjectCreate(workspace_id=workspace["id"], name="Collections Evaluation", workflow="Promise to pay"),
            )
    if created:
        destination = STATE / f"demo-credentials-{uuid4().hex[:8]}.json"
        private_write(destination, json.dumps({"synthetic_local_accounts": created}, indent=2))
        print(f"Generated local-only credentials in {destination}")
    print("Seeded platform accounts/projects. No customer runner or outcomes are fabricated.")


def initialize_owner():
    from .database import Context, transaction
    from .security import create_identity
    from .service import create_workspace

    email = os.environ.get("PLATFORM_BOOTSTRAP_EMAIL", "").strip().lower()
    password = os.environ.get("PLATFORM_BOOTSTRAP_PASSWORD", "")
    if "@" not in email or not password:
        raise SystemExit("Inject PLATFORM_BOOTSTRAP_EMAIL and PLATFORM_BOOTSTRAP_PASSWORD for first-owner setup.")
    with transaction() as db:
        db.execute("select pg_advisory_xact_lock(727005)")
        if db.execute("select 1 from platform.memberships where 'owner'=any(roles) limit 1").fetchone():
            raise SystemExit("An owner already exists. Use the authorized invitation/recovery workflow.")
        user = create_identity(db, email, password)
        workspace = create_workspace(Context(db, {"user_id": user["id"]}, str(uuid4())), "Private Workspace")
    print(
        json.dumps(
            {"owner_id": str(user["id"]), "workspace_id": str(workspace["id"]), "synthetic_accounts_created": False}
        )
    )


def recover_user():
    from .database import Context, transaction
    from .security import password_hash

    email = os.environ.get("PLATFORM_RECOVERY_EMAIL", "").strip().lower()
    password = os.environ.get("PLATFORM_RECOVERY_PASSWORD", "")
    operator = os.environ.get("PLATFORM_OPERATOR_ID", "").strip()
    if not email or not password or not operator:
        raise SystemExit(
            "Inject recovery email/password and a real PLATFORM_OPERATOR_ID; server administrator access is required."
        )
    with transaction() as db:
        user = db.execute("select id from platform.identities where email=%s and not disabled", (email,)).fetchone()
        if not user:
            raise SystemExit("No enabled identity matches the recovery request.")
        db.execute("update platform.identities set password_hash=%s where id=%s", (password_hash(password), user["id"]))
        db.execute("update platform.credentials set revoked_at=now() where user_id=%s", (user["id"],))
        Context(db, {}, str(uuid4())).audit(
            "identity.local_admin_recovery",
            user["id"],
            details={"operator": operator, "authority": "local_database_administrator"},
        )
    print(json.dumps({"user_id": str(user["id"]), "password_updated": True, "credentials_revoked": True}))


def compose_environment():
    destination = ROOT / ".env.platform.compose"
    private_write(
        destination,
        "PLATFORM_ENV=development\nPLATFORM_ORIGIN=https://localhost:8443\n"
        f"PLATFORM_DB_PASSWORD={secrets.token_urlsafe(32)}\n"
        f"PLATFORM_SECRET_KEY={secrets.token_urlsafe(48)}\n"
        f"PLATFORM_BACKUP_KEY={secrets.token_urlsafe(48)}\n",
    )
    print(f"Created ignored private Compose configuration: {destination}")


def production_check():
    issues = []
    if os.environ.get("PLATFORM_ENV") != "production":
        issues.append("Set PLATFORM_ENV=production; development cookie/auth settings are not production-qualified.")
    if not os.environ.get("PLATFORM_ORIGIN", "").startswith("https://"):
        issues.append("PLATFORM_ORIGIN must be an explicit HTTPS origin.")
    if len(os.environ.get("PLATFORM_SECRET_KEY", "")) < 32:
        issues.append("A strong platform secret is required.")
    for key in ("PLATFORM_DATABASE_URL", "PLATFORM_SMTP_HOST", "PLATFORM_BACKUP_KEY"):
        if not os.environ.get(key):
            issues.append(f"Configure {key}.")
    if len(os.environ.get("PLATFORM_BACKUP_KEY", "")) < 32:
        issues.append("The separate backup key must contain at least 32 characters.")
    if os.environ.get("PLATFORM_BACKUP_KEY") == os.environ.get("PLATFORM_SECRET_KEY"):
        issues.append("Use a different backup key from the session/application secret.")
    if os.environ.get("PLATFORM_SMTP_HOST") in {"mail", "localhost", "127.0.0.1"}:
        issues.append("The development mailbox is not production email delivery.")
    if os.environ.get("PLATFORM_SMTP_TLS") != "true":
        issues.append("Configure verified TLS for production SMTP delivery.")
    from .database import transaction

    try:
        with transaction() as db:
            applied = {row["version"] for row in db.execute("select version from platform.schema_versions").fetchall()}
            expected = {path.name.split("_", 1)[0] for path in (ROOT / "supabase/migrations").glob("20*platform*.sql")}
            if not expected.issubset(applied):
                issues.append("Expanded platform migrations are incomplete.")
            if db.execute(
                "select count(*) n from platform.identities where email like '%%@susanoo.local' and not disabled"
            ).fetchone()["n"]:
                issues.append("Disable synthetic demo identities before production startup.")
    except Exception:
        issues.append("Database/migrations unavailable.")
    print(
        json.dumps(
            {
                "ready": not issues,
                "scope": "configuration_only",
                "issues": issues,
                "unchecked_controls": [
                    "target TLS trust",
                    "database/volume encryption",
                    "cloud storage policy",
                    "restore drill",
                    "matching runtime acceptance",
                    "live providers",
                ],
                "product_qualified": False,
                "deploy_performed": False,
            },
            indent=2,
        )
    )
    return bool(issues)


def main():
    parser = argparse.ArgumentParser(description="Susanoo private platform administration")
    parser.add_argument(
        "command",
        choices=[
            "bootstrap",
            "migrate",
            "seed",
            "serve",
            "worker",
            "tick",
            "production-check",
            "init-owner",
            "storage-init",
            "recover-user",
            "compose-env",
            "backup-worker",
            "backup",
            "restore",
            "expire-backups",
        ],
    )
    parser.add_argument("--path")
    parser.add_argument("--target-url-env", default="PLATFORM_RESTORE_DATABASE_URL")
    parser.add_argument("--confirm-database")
    parser.add_argument("--deletion-journal")
    args = parser.parse_args()
    load_dotenv(ENV, override=False)
    if args.command in {"backup", "restore", "expire-backups"}:
        from . import recovery

        if not args.path:
            parser.error("--path is required")
        if args.command == "backup":
            result = recovery.backup(args.path)
        elif args.command == "expire-backups":
            result = recovery.expire_backups(args.path)
        else:
            if not args.confirm_database or not args.deletion_journal:
                parser.error("Restore requires --confirm-database and --deletion-journal")
            result = recovery.restore(
                args.path, os.environ[args.target_url_env], args.confirm_database, args.deletion_journal
            )
        print(json.dumps(result, indent=2))
    elif args.command == "bootstrap":
        bootstrap()
    elif args.command == "migrate":
        migrate()
    elif args.command == "seed":
        seed()
    elif args.command == "init-owner":
        initialize_owner()
    elif args.command == "recover-user":
        recover_user()
    elif args.command == "compose-env":
        compose_environment()
    elif args.command == "storage-init":
        from .artifacts import ObjectStore

        print(json.dumps(ObjectStore().configure_private_bucket()))
    elif args.command == "backup-worker":
        from .backup_worker import main as run_backups

        run_backups()
    elif args.command == "production-check":
        sys.exit(production_check())
    elif args.command == "serve":
        import uvicorn

        uvicorn.run("susanoo_api.main:app", host="127.0.0.1", port=int(os.environ.get("PLATFORM_PORT", "8000")))
    else:
        from .jobs import tick

        while True:
            try:
                result = tick()
                if args.command == "tick":
                    print(json.dumps(result))
                    return
            except KeyboardInterrupt:
                return
            except Exception as exc:
                print(
                    f"Internal worker tick failed ({type(exc).__name__}); no customer action redispatched.",
                    file=sys.stderr,
                )
                if args.command == "tick":
                    raise
            time.sleep(2)


if __name__ == "__main__":
    main()
