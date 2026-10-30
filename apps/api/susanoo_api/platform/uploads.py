"""Staged verified uploads: network I/O outside DB transactions, authorization again at commit."""

import hashlib
from uuid import uuid4

from .artifacts import ObjectStore
from .database import PlatformError, transaction


def verified_upload(artifact_id, content, authorize):
    with transaction() as db:
        row, project_id = authorize(db)
        if len(content) != row["size"] or hashlib.sha256(content).hexdigest() != row["digest"]:
            raise PlatformError("INVALID_INPUT", "Upload differs from its reserved digest and size.", 422)
        if row["status"] == "ready":
            return {"id": artifact_id, "status": "ready"}
        if row["status"] != "pending":
            raise PlatformError("INVALID_STATE", "Artifact cannot accept bytes.", 409)
    staged_key = f"{row['workspace_id']}/{project_id}/{uuid4()}"
    store = ObjectStore()
    committed = False
    try:
        store.put(staged_key, content, row["content_type"])
        linked = False
        with transaction() as db:
            # Deletion/policy change is serialized only for the short metadata commit.
            db.execute("select id from platform.projects where id=%s for update", (project_id,))
            current, _ = authorize(db)
            if current["status"] == "pending":
                linked = bool(
                    db.execute(
                        "update platform.artifacts set object_key=%s,status='ready' "
                        "where id=%s and status='pending' returning id",
                        (staged_key, artifact_id),
                    ).fetchone()
                )
            elif current["status"] != "ready":
                raise PlatformError("INVALID_STATE", "Artifact was revoked before completion.", 409)
        committed = linked
        return {"id": artifact_id, "status": "ready"}
    finally:
        if not committed:
            store.delete(staged_key)
