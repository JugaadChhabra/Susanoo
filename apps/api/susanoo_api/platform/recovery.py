"""Encrypted private backups and explicit empty-database restore with deletion replay."""

import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
from cryptography.fernet import Fernet
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from .artifacts import ObjectStore
from .database import PlatformError
from .local_security import secure_local_path

ROOT = Path(__file__).resolve().parents[4]
MAX_BACKUP_BYTES = 512 * 1024 * 1024


def cipher():
    secret = os.environ.get("PLATFORM_BACKUP_KEY", "")
    if not secret and os.environ.get("PLATFORM_ENV") == "development":
        secret = os.environ.get("PLATFORM_SECRET_KEY", "")
    if len(secret) < 32:
        raise PlatformError("UNAVAILABLE", "Configure a strong separate PLATFORM_BACKUP_KEY.", 503)
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"susanoo:backup:v1:" + secret.encode()).digest()))


def atomic_private(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        secure_local_path(temporary)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def journal_path():
    return Path(os.environ.get("PLATFORM_DELETION_JOURNAL", str(ROOT / ".platform/deletion-journal.enc")))


def record_deletion(project):
    """Called under a global DB advisory lock; a rollback can only over-restrict restore."""
    path = journal_path()
    data = (
        json.loads(cipher().decrypt(path.read_bytes())) if path.exists() else {"schema_version": "1.0", "deletions": {}}
    )
    data["deletions"][str(project["id"])] = {
        "workspace_id": str(project["workspace_id"]),
        "deleted_at": datetime.now(UTC).isoformat(),
    }
    data["updated_at"] = datetime.now(UTC).isoformat()
    atomic_private(path, cipher().encrypt(json.dumps(data, sort_keys=True).encode()))


def postgres_tool(name):
    configured = os.environ.get("PLATFORM_PG_BIN")
    executable = name + (".exe" if os.name == "nt" else "")
    candidate = (
        Path(configured) / executable if configured else ROOT / ".platform/postgres-tools/pgsql/bin" / executable
    )
    if candidate.is_file():
        return str(candidate)
    found = shutil.which(name)
    if not found:
        raise PlatformError("UNAVAILABLE", f"Install PostgreSQL client tools ({name}).", 503)
    return found


def pg_environment(url):
    values = conninfo_to_dict(url)
    environment = {**os.environ, "PGPASSWORD": values.pop("password", ""), "PGCONNECT_TIMEOUT": "10"}
    # Credentials stay in the subprocess environment, never its command line.
    from psycopg.conninfo import make_conninfo

    return make_conninfo(**values), environment


def backup(destination):
    destination = Path(destination)
    if destination.exists():
        raise PlatformError("INVALID_INPUT", "Backup destination already exists.", 409)
    url = os.environ["PLATFORM_DATABASE_URL"]
    arguments, environment = pg_environment(url)
    payload = io.BytesIO()
    with tempfile.TemporaryDirectory(prefix="susanoo-backup-") as temporary:
        secure_local_path(temporary)
        dump = Path(temporary) / "platform.dump"
        with psycopg.connect(url, row_factory=dict_row) as db:
            db.execute("set transaction isolation level repeatable read, read only")
            snapshot = db.execute("select pg_export_snapshot() snapshot").fetchone()["snapshot"]
            inventory = db.execute(
                "select object_key,digest,size,content_type from platform.artifacts where status='ready'"
            ).fetchall()
            subprocess.run(
                [
                    postgres_tool("pg_dump"),
                    "--dbname",
                    arguments,
                    "--format=custom",
                    "--schema=platform",
                    "--no-owner",
                    "--no-acl",
                    "--snapshot",
                    snapshot,
                    "--file",
                    str(dump),
                ],
                env=environment,
                check=True,
                timeout=300,
                capture_output=True,
            )
            manifest = {
                "schema_version": "1.0",
                "created_at": datetime.now(UTC).isoformat(),
                "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
                "objects": [],
            }
            with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.write(dump, "platform.dump")
                for index, item in enumerate(inventory):
                    content = ObjectStore().get(item["object_key"])
                    if hashlib.sha256(content).hexdigest() != item["digest"] or len(content) != item["size"]:
                        raise PlatformError("INTEGRITY_ERROR", "Backup object digest verification failed.", 409)
                    name = f"objects/{index}"
                    archive.writestr(name, content)
                    manifest["objects"].append({**item, "entry": name})
                    if payload.tell() > MAX_BACKUP_BYTES:
                        raise PlatformError(
                            "CAPACITY_EXCEEDED", "Use an offline storage-native backup above 512 MiB.", 409
                        )
                archive.writestr("manifest.json", json.dumps(manifest, sort_keys=True))
    from .database import transaction

    with transaction() as db:
        db.execute("select pg_advisory_xact_lock(727004)")
        if not journal_path().exists():
            atomic_private(
                journal_path(), cipher().encrypt(json.dumps({"schema_version": "1.0", "deletions": {}}).encode())
            )
    if payload.getbuffer().nbytes > MAX_BACKUP_BYTES:
        raise PlatformError("CAPACITY_EXCEEDED", "Backup exceeds the private-tool memory bound.", 409)
    atomic_private(destination, cipher().encrypt(payload.getvalue()))
    return {
        "path": str(destination.resolve()),
        "objects": len(inventory),
        "expires_at": manifest["expires_at"],
        "deletion_journal": str(journal_path().resolve()),
        "scope": "platform_schema_and_ready_objects",
    }


def restore(source, target_url, confirmation, deletion_journal):
    target = conninfo_to_dict(target_url)
    if target.get("dbname") != confirmation or target_url == os.environ.get("PLATFORM_DATABASE_URL"):
        raise PlatformError(
            "FORBIDDEN",
            "Restore requires an explicit different empty database and exact database-name confirmation.",
            403,
        )
    with psycopg.connect(target_url) as db:
        if db.execute(
            "select count(*) from pg_tables where schemaname not in ('pg_catalog','information_schema')"
        ).fetchone()[0]:
            raise PlatformError("FORBIDDEN", "Restore target must be empty; existing data is never overwritten.", 403)
    journal = json.loads(cipher().decrypt(Path(deletion_journal).read_bytes()))
    if journal.get("schema_version") != "1.0" or not isinstance(journal.get("deletions"), dict):
        raise PlatformError("INVALID_INPUT", "An authenticated current deletion journal is required.", 422)
    if Path(source).stat().st_size > MAX_BACKUP_BYTES * 2:
        raise PlatformError("CAPACITY_EXCEEDED", "Backup exceeds the private restore memory bound.", 422)
    encrypted = Path(source).read_bytes()
    with zipfile.ZipFile(io.BytesIO(cipher().decrypt(encrypted))) as archive:
        if sum(entry.file_size for entry in archive.infolist()) > MAX_BACKUP_BYTES:
            raise PlatformError("CAPACITY_EXCEEDED", "Expanded backup exceeds the restore bound.", 422)
        manifest = json.loads(archive.read("manifest.json"))
        if manifest["schema_version"] != "1.0" or datetime.fromisoformat(manifest["expires_at"]) <= datetime.now(UTC):
            raise PlatformError("EXPIRED_BACKUP", "Expired or unsupported backup cannot be restored.", 409)
        arguments, environment = pg_environment(target_url)
        with tempfile.TemporaryDirectory(prefix="susanoo-restore-") as temporary:
            secure_local_path(temporary)
            dump = Path(temporary) / "platform.dump"
            atomic_private(dump, archive.read("platform.dump"))
            subprocess.run(
                [
                    postgres_tool("pg_restore"),
                    "--dbname",
                    arguments,
                    "--no-owner",
                    "--no-acl",
                    "--exit-on-error",
                    "--single-transaction",
                    str(dump),
                ],
                env=environment,
                check=True,
                timeout=300,
                capture_output=True,
            )
        # Revoke all restored credentials before any service may access the database.
        with psycopg.connect(target_url) as db:
            db.execute("update platform.credentials set revoked_at=now() where revoked_at is null")
            db.execute("update platform.runners set revoked_at=now()")
            db.execute("update platform.source_grants set state='revoking',claimed_at=null where state<>'revoked'")
            db.execute("update platform.schedules set enabled=false")
            db.execute("update platform.integrations set revoked_at=now()")
            db.execute("update platform.jobs set status='cancelled' where status in ('queued','running')")
            for project_id, deleted in journal["deletions"].items():
                db.execute(
                    "update platform.projects set deleted_at=%s where id=%s", (deleted["deleted_at"], project_id)
                )
                db.execute(
                    """insert into platform.deletion_tombstones(project_id,workspace_id,deleted_at,purge_deadline)
                           select id,workspace_id,deleted_at,now() from platform.projects where id=%s
                           on conflict(project_id) do update set purged_at=null""",
                    (project_id,),
                )
        for item in manifest["objects"]:
            project_id = item["object_key"].split("/")[1]
            if project_id in journal["deletions"]:
                continue
            content = archive.read(item["entry"])
            if len(content) != item["size"] or hashlib.sha256(content).hexdigest() != item["digest"]:
                raise PlatformError("INTEGRITY_ERROR", "Restored object failed verification.", 409)
            ObjectStore().put(item["object_key"], content, item["content_type"])
    previous = os.environ.get("PLATFORM_DATABASE_URL")
    os.environ["PLATFORM_DATABASE_URL"] = target_url
    try:
        from .jobs import purge_deleted

        purge_deleted()
    finally:
        if previous is not None:
            os.environ["PLATFORM_DATABASE_URL"] = previous
        else:
            os.environ.pop("PLATFORM_DATABASE_URL", None)
    return {
        "restored": True,
        "deletions_reapplied": len(journal["deletions"]),
        "credentials_revoked": True,
        "schedules_disabled": True,
        "service_restart_required": True,
    }


def expire_backups(directory):
    removed = 0
    for path in Path(directory).glob("*.susanoobak"):
        # Only this tool's authenticated format is eligible for deletion.
        try:
            with zipfile.ZipFile(io.BytesIO(cipher().decrypt(path.read_bytes()))) as archive:
                expiry = datetime.fromisoformat(json.loads(archive.read("manifest.json"))["expires_at"])
            if expiry <= datetime.now(UTC):
                path.unlink()
                removed += 1
        except Exception:
            continue
    return {"expired_backups_removed": removed}
