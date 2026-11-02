"""Private artifacts and consistent authorized export snapshots."""

import io
import json
import os
import re
import tempfile
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from xml.etree.ElementTree import Element, SubElement, tostring

from psycopg.types.json import Jsonb
from reportlab.lib import colors
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from susanoo_contracts import content_hash

from .database import PlatformError
from .service import ENGINEERS, REVIEWERS, browser_run, get_run, json_safe
from .local_security import secure_local_path
from .pdf_typography import markup as escape, styles as pdf_styles

CONTENT_TYPES = {
    "json": "application/json",
    "pdf": "application/pdf",
    "junit": "application/xml",
    "bundle": "application/zip",
}


class ObjectStore:
    def __init__(self):
        self.bucket = os.environ.get("PLATFORM_S3_BUCKET")
        if self.bucket:
            import boto3
            from botocore.config import Config

            self.client = boto3.client(
                "s3",
                endpoint_url=os.environ.get("PLATFORM_S3_ENDPOINT"),
                config=Config(connect_timeout=3, read_timeout=10, retries={"max_attempts": 2}),
            )
        self.root = Path(os.environ.get("PLATFORM_ARTIFACT_DIR", ".platform/artifacts")).resolve()

    def path(self, key):
        if not re.fullmatch(r"[a-f0-9-]{36}/[a-f0-9-]{36}/[a-f0-9-]{36}", key):
            raise PlatformError("INVALID_INPUT", "Invalid object reference.", 422)
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise PlatformError("FORBIDDEN", "Invalid object location.", 403)
        return path

    def put(self, key, data, content_type):
        if self.bucket:
            self.client.put_object(
                Bucket=self.bucket, Key=key, Body=data, ContentType=content_type, ServerSideEncryption="AES256"
            )
            head = self.client.head_object(Bucket=self.bucket, Key=key)
            if head["ContentLength"] != len(data):
                raise PlatformError("UNAVAILABLE", "Object upload verification failed.", 503)
            return
        path = self.path(key)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".upload-", delete=False) as temporary:
            secure_local_path(temporary.name)
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary.name, path)

    def get(self, key):
        if self.bucket:
            return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read(10485761)
        return self.path(key).read_bytes()

    def delete(self, key):
        if self.bucket:
            for page in self.client.get_paginator("list_object_versions").paginate(Bucket=self.bucket, Prefix=key):
                versions = [
                    {"Key": item["Key"], "VersionId": item["VersionId"]}
                    for item in page.get("Versions", []) + page.get("DeleteMarkers", [])
                    if item["Key"] == key
                ]
                if versions:
                    self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": versions, "Quiet": True})
            self.client.delete_object(Bucket=self.bucket, Key=key)
        else:
            self.path(key).unlink(missing_ok=True)

    def configure_private_bucket(self):
        if not self.bucket:
            raise PlatformError("INVALID_INPUT", "Configure an existing private S3 bucket first.", 422)
        self.client.head_bucket(Bucket=self.bucket)
        self.client.put_public_access_block(
            Bucket=self.bucket,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            },
        )
        self.client.put_bucket_encryption(
            Bucket=self.bucket,
            ServerSideEncryptionConfiguration={
                "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}],
            },
        )
        self.client.put_bucket_lifecycle_configuration(
            Bucket=self.bucket,
            LifecycleConfiguration={
                "Rules": [
                    {
                        "ID": "susanoo-evidence-30-day-limit",
                        "Filter": {"Prefix": ""},
                        "Status": "Enabled",
                        "Expiration": {"Days": 30},
                        "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
                        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
                    }
                ]
            },
        )
        return {"bucket_configured": True, "private": True, "retention_days": 30}

    def sweep_unreferenced(self, referenced):
        cutoff = datetime.now(UTC) - timedelta(hours=24)
        deleted = 0
        if self.bucket:
            for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket):
                for item in page.get("Contents", []):
                    key = item["Key"]
                    if (
                        re.fullmatch(r"[a-f0-9-]{36}/[a-f0-9-]{36}/[a-f0-9-]{36}", key)
                        and key not in referenced
                        and item["LastModified"] < cutoff
                    ):
                        self.delete(key)
                        deleted += 1
        elif self.root.exists():
            for path in self.root.glob("*/*/*"):
                if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(self.root):
                    continue
                key = path.relative_to(self.root).as_posix()
                if (
                    key not in referenced
                    and datetime.fromtimestamp(path.stat().st_mtime, UTC) < cutoff
                    and (path.name.startswith(".upload-") or re.fullmatch(r"[a-f0-9-]{36}", path.name))
                ):
                    path.unlink()
                    deleted += 1
        return deleted


def authorized_artifact(ctx, artifact_id):
    row = ctx.db.execute("select * from platform.artifacts where id=%s", (artifact_id,)).fetchone()
    if not row:
        raise PlatformError("NOT_FOUND", "Artifact not found.", 404)
    project = ctx.project(row["project_id"], REVIEWERS if row["audience"] == "reviewers" else None)
    if row["hidden"] and not project["hidden_access"]:
        raise PlatformError("NOT_FOUND", "Artifact not available to this role.", 404)
    if row["policy_hash"] != content_hash(project["policy"]):
        raise PlatformError("POLICY_CHANGED", "Artifact access needs a new export under the current data policy.", 409)
    if row["expires_at"] <= datetime.now(UTC):
        raise PlatformError("NOT_FOUND", "Artifact retention has expired.", 404)
    return row, project


def reserve_upload(ctx, project_id, body):
    project = ctx.project(project_id, ENGINEERS)
    validate_upload_policy(project["policy"], body, lambda value: ctx.version(value, project, "voice", True))
    if project["policy"]["mode"] == "local_only":
        raise PlatformError("FORBIDDEN", "This project keeps evidence local.", 403)
    if body.content_type == "audio/wav" and not project["policy"]["media_upload_allowed"]:
        raise PlatformError("FORBIDDEN", "Hosted media requires explicit project permission.", 403)
    if body.audience == "reviewers":
        ctx.project(project_id, REVIEWERS)
    artifact_id = uuid4()
    return ctx.db.execute(
        """insert into platform.artifacts(id,workspace_id,project_id,content_type,digest,size,object_key,audience,
        created_by,expires_at,policy_hash,purpose,authorization_version_id)
        values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        returning id,status,digest,size,content_type""",
        (
            artifact_id,
            project["workspace_id"],
            project_id,
            body.content_type,
            body.digest,
            body.size,
            f"{project['workspace_id']}/{project_id}/{artifact_id}",
            body.audience,
            ctx.actor["user_id"],
            datetime.now(UTC) + timedelta(days=project["policy"]["hosted_retention_days"]),
            content_hash(project["policy"]),
            body.purpose,
            body.authorization_version_id,
        ),
    ).fetchone()


def validate_upload_policy(policy, body, voice_lookup):
    if policy["mode"] == "local_only":
        raise PlatformError("FORBIDDEN", "This project keeps evidence local.", 403)
    if body.purpose in {"source", "patch"} and not policy["source_upload_allowed"]:
        raise PlatformError("FORBIDDEN", "Hosted source or patches require explicit data-policy permission.", 403)
    if body.content_type.startswith("audio/") or body.purpose == "media":
        if not policy["media_upload_allowed"] or not body.authorization_version_id:
            raise PlatformError(
                "FORBIDDEN", "Media needs permission and an approved consent-bearing voice revision.", 403
            )
        voice = voice_lookup(body.authorization_version_id)["data"]
        if not voice["recording_consent"] or voice["media_location"] != "approved_hosted":
            raise PlatformError("FORBIDDEN", "This voice revision does not authorize hosted recordings.", 403)


def export_snapshot(ctx, run, project, audience):
    hidden = audience == "reviewers" and project["hidden_access"]
    view_project = {**project, "hidden_access": hidden}
    view = browser_run(ctx, run, view_project)
    # Export snapshots are asynchronous and complete even when interactive pages are bounded.
    view["attempts"] = ctx.db.execute(
        """select id,case_version_id,repetition,arm,required,execution_state,business_verdict,
        cleanup_state,reconciliation_state,hidden from platform.attempts where run_id=%s
        and (not hidden or %s) order by id""",
        (run["id"], hidden),
    ).fetchall()
    view["attempts_next_cursor"] = None
    attempts = view["attempts"]
    attempt_ids = [a["id"] for a in attempts]
    evidence = ctx.db.execute(
        "select * from platform.evidence where attempt_id=any(%s::uuid[]) and (not hidden or %s) order by id",
        (attempt_ids, hidden),
    ).fetchall()
    evidence = scoped_evidence(evidence, project)
    findings = ctx.db.execute(
        "select * from platform.findings where run_id=%s and (not hidden or %s) order by id",
        (run["id"], hidden),
    ).fetchall()
    from .workflows import acceptance_projection

    for finding in findings:
        finding["acceptance"] = acceptance_projection(ctx, finding)
    reviews = ctx.db.execute(
        "select * from platform.review_events where subject_id=any(%s::uuid[]) order by created_at,id",
        ([f["id"] for f in findings] + [run["id"]],),
    ).fetchall()
    manifest = run["manifest"]
    permitted_cases = [case for case in manifest.get("cases", []) if not case["case"]["hidden"] or hidden]
    scoped_manifest = {
        key: manifest[key] for key in ("schema_version", "kind", "policy", "timezone", "limits") if key in manifest
    }
    scoped_manifest["cases"] = permitted_cases
    for key in ("connection", "environment", "suite"):
        if key in manifest:
            value = manifest[key]
            scoped_manifest[key] = (
                value
                if not value.get("hidden") or hidden
                else {
                    "id": value["id"],
                    "hidden_details_omitted": True,
                }
            )
    if "request" in manifest:
        scoped_manifest["request"] = manifest["request"]
    if "versions" in manifest:
        scoped_manifest["versions"] = [value for value in manifest["versions"] if not value["hidden"] or hidden]
    from .reports import visible_reports

    advanced = visible_reports(ctx, run, project, audience=audience)
    return json_safe(
        {
            "schema_version": "1.0",
            "run": view,
            "manifest_hash": run["manifest_hash"],
            "manifest": scoped_manifest,
            "findings": findings,
            "evidence": evidence,
            "reviews": reviews,
            "advanced_reports": advanced,
            "audience": audience,
            "hidden_details_omitted": not hidden,
            "local_evidence": "Local-only payloads are not present in this export.",
            "retention": ctx.db.execute(
                "select purged_at,reason from platform.retention_receipts where run_id=%s", (run["id"],)
            ).fetchone(),
            "reproduction": {
                "requires_customer_runner": True,
                "fresh_run_required": True,
                "original_run_id": str(run["id"]),
                "live_verification": "not_established_by_export",
            },
        }
    )


def scoped_evidence(items, project):
    allowed_fields = set(project["policy"].get("upload_fields", []))
    for item in items:
        original_hash = content_hash(item["data"])
        item["data"] = dict(item["data"])
        field = "values" if item["kind"] == "observation" else "measured"
        item["data"][field] = {
            key: value
            for key, value in item["data"].get(field, {}).items()
            if key in allowed_fields and project["policy"]["mode"] != "local_only"
        }
        item["view_hash"] = content_hash(item["data"])
        item["view_redacted"] = original_hash != item["view_hash"]
    return items


def request_export(ctx, run_id, body):
    run, project = get_run(ctx, run_id)
    if body.audience == "reviewers":
        ctx.project(project["id"], REVIEWERS)
        if body.format == "bundle":
            raise PlatformError(
                "FORBIDDEN", "Reproduction bundles use the project audience; held-out content stays withheld.", 403
            )
    if run["status"] not in {"completed", "interrupted", "cancelled"}:
        raise PlatformError("INVALID_STATE", "Export a settled immutable result snapshot.", 409)
    if body.snapshot_artifact_id:
        source, _ = authorized_artifact(ctx, body.snapshot_artifact_id)
        if source["run_id"] != run_id or source["audience"] != body.audience or not source["snapshot"]:
            raise PlatformError("INVALID_INPUT", "Use an authorized export snapshot for this run and audience.", 422)
        snapshot = source["snapshot"]
    else:
        snapshot = export_snapshot(ctx, run, project, body.audience)
        snapshot["snapshot_created_at"] = datetime.now(UTC).isoformat()
        snapshot["snapshot_hash"] = content_hash(snapshot)
    artifact_id = uuid4()
    row = ctx.db.execute(
        """insert into platform.artifacts(id,workspace_id,project_id,run_id,export_format,content_type,digest,size,
        object_key,audience,hidden,snapshot,created_by,expires_at,policy_hash)
        values(%s,%s,%s,%s,%s,%s,%s,0,%s,%s,%s,%s,%s,%s,%s)
        returning id,status,export_format,content_type,digest,size,audience,expires_at""",
        (
            artifact_id,
            project["workspace_id"],
            project["id"],
            run_id,
            body.format,
            CONTENT_TYPES[body.format],
            "pending",
            f"{project['workspace_id']}/{project['id']}/{artifact_id}",
            body.audience,
            body.audience == "reviewers" and project["hidden_access"],
            Jsonb(snapshot),
            ctx.actor["user_id"],
            datetime.now(UTC) + timedelta(days=project["policy"]["hosted_retention_days"]),
            content_hash(project["policy"]),
        ),
    ).fetchone()
    ctx.db.execute(
        """insert into platform.jobs(workspace_id,project_id,kind,payload,dedupe_key)
        values(%s,%s,'export',%s,%s)""",
        (project["workspace_id"], project["id"], Jsonb({"artifact_id": str(artifact_id)}), str(artifact_id)),
    )
    ctx.audit(
        "export.requested", artifact_id, project, {"format": body.format, "snapshot_hash": content_hash(snapshot)}
    )
    return row


def render_export(snapshot, format):
    if format == "json":
        return json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False).encode()
    if format == "junit":
        root = Element(
            "testsuite", name="Susanoo scoped business evaluation", snapshot_hash=snapshot.get("snapshot_hash", "")
        )
        failures, errors = 0, 0
        for attempt in snapshot["run"]["attempts"]:
            node = SubElement(root, "testcase", name=str(attempt["case_version_id"]), classname="susanoo.outcome")
            if not attempt["required"] and attempt["business_verdict"] != "PASS":
                SubElement(node, "skipped", message="Optional outcome; see the evidence report")
                continue
            if attempt["execution_state"] != "completed":
                SubElement(node, "error", message=attempt["execution_state"])
                errors += 1
            elif attempt["business_verdict"] == "FAIL":
                SubElement(node, "failure", message="Required business outcome failed")
                failures += 1
            elif attempt["business_verdict"] != "PASS":
                SubElement(node, "error", message="Required evidence unresolved")
                errors += 1
        if snapshot["run"]["gate"] != "READY_FOR_ACCEPTANCE" and not failures and not errors:
            SubElement(SubElement(root, "testcase", name="operational-gate"), "error", message=snapshot["run"]["gate"])
            errors += 1
        root.set("tests", str(len(root)))
        root.set("failures", str(failures))
        root.set("errors", str(errors))
        return tostring(root, encoding="utf-8", xml_declaration=True)
    if format == "bundle":
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("result.json", render_export(snapshot, "json"))
            archive.writestr("manifest.json", json.dumps(snapshot["manifest"], ensure_ascii=False, indent=2))
            for report in snapshot.get("advanced_reports", []):
                archive.writestr(
                    f"reports/{report['id']}.json", json.dumps(report, ensure_ascii=False, indent=2)
                )
            ingredients = [item["payload"] for item in snapshot.get("advanced_reports", [])
                           if item["report_type"] == "reproduction"]
            runtime = [item["payload"] for item in snapshot.get("advanced_reports", [])
                       if item["report_type"] == "run_export"]
            for index, ingredient in enumerate(ingredients):
                if not any(item["snapshot_digest"] == ingredient["snapshot_digest"] for item in runtime):
                    raise PlatformError(
                        "INCOMPLETE_EVIDENCE", "Reproduction ingredients need their matching snapshot.", 409
                    )
                from .reports import ingredient_case

                cases = [ingredient_case(value).model_dump(mode="json") for value in ingredient["cases"]
                         if not value.get("content_withheld") and value.get("visibility") != "hidden"]
                required_ids = {value for case in cases for value in case["requirement_ids"]}
                suite = {
                    "schema_version": "1.0", "id": ingredient["suite"]["id"],
                    "synthetic": ingredient["suite"]["synthetic"], "timezone": ingredient["suite"]["timezone"],
                    "limits": ingredient["suite"]["limits"], "cases": cases,
                    "requirements": [item for item in ingredient["suite"].get("requirements", [])
                                     if item.get("id") in required_ids],
                }
                folder = f"reproduction-{index}"
                archive.writestr(f"{folder}/suite.json", json.dumps(suite, ensure_ascii=False, indent=2))
                archive.writestr(
                    f"{folder}/profile.json", json.dumps(ingredient["profile"], ensure_ascii=False, indent=2)
                )
                archive.writestr(f"{folder}/reproduction.json", json.dumps({
                    **ingredient, "cases": [value for value in ingredient["cases"] if
                                             not value.get("content_withheld") and value.get("visibility") != "hidden"],
                    "suite": {**ingredient["suite"], "requirements": suite["requirements"]},
                    "command": ("susanoo-runner run --suite suite.json --profile profile.json "
                                "--agent-cmd LOCAL_APPROVED_AGENT"),
                }, ensure_ascii=False, indent=2))
            archive.writestr(
                "REPRODUCTION.md",
                (
                    "# Fresh reproduction\n\n"
                    "Use an authorized compatible customer runner and a new fixture namespace.\n"
                    "This is not a replay of cached answers. Local-only material is not included.\n"
                    "Required local material: approved adapter/runtime, local credentials, "
                    "fixture source and retained evidence.\n"
                    "Submit the approved manifest request to POST /api/v1/projects/{project_id}/runs "
                    "with a new Idempotency-Key,\n"
                    "or use the runtime lane's documented reproduce command once installed.\n"
                    "The original result remains immutable; record reproduced, not reproduced or inconclusive.\n"
                ),
            )
        return output.getvalue()
    if format != "pdf":
        raise ValueError("Unsupported export format")
    if len(snapshot["findings"]) > 1000:
        raise PlatformError(
            "EXPORT_TOO_LARGE", "PDF summaries are bounded to 1,000 findings; split the evaluation scope.", 422
        )
    output = io.BytesIO()
    styles = pdf_styles()
    story = [Paragraph("Susanoo Evaluation Evidence", styles["Title"]), Spacer(1, 12)]
    run = snapshot["run"]
    for label, value in (
        ("Run", run["id"]),
        ("Gate", run["gate"]),
        ("Manifest", snapshot["manifest_hash"]),
        ("Snapshot", snapshot.get("snapshot_hash", "Unavailable")),
        ("Audience", snapshot["audience"]),
        ("Local evidence", snapshot["local_evidence"]),
    ):
        story.append(Paragraph(f"<b>{label}:</b> {escape(str(value))}", styles["BodyText"]))
    story.append(Spacer(1, 16))
    story.append(Paragraph("Pinned scope", styles["Heading2"]))
    for key in ("suite", "connection", "environment"):
        version = snapshot["manifest"].get(key)
        if version:
            text = f"{key.title()}: {version.get('title', 'Restricted revision')} | {version['id']}"
            if "revision" in version:
                text += f" | revision {version['revision']}"
            story.append(Paragraph(escape(text), styles["BodyText"]))
    story.append(Spacer(1, 12))
    rows = [
        ["Scheduled", "Attempted", "Completed", "Pass", "Fail", "Unknown", "Errors"],
        [
            str(run["counts"][key])
            for key in ("scheduled", "attempted", "completed", "pass", "fail", "unknown", "errors")
        ],
    ]
    table = Table(rows, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dce9e9")),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
            ]
        )
    )
    story.extend([table, Spacer(1, 16), Paragraph("Findings and uncertainty", styles["Heading2"])])
    by_check = {
        (item["attempt_id"], item["version_id"]): item for item in snapshot["evidence"] if item["kind"] == "check"
    }
    for finding in snapshot["findings"]:
        check = by_check.get((finding["attempt_id"], finding["check_version_id"]), {})
        story.append(Paragraph(escape(f"{finding['verdict']}: {finding['check_version_id']}"), styles["Heading3"]))
        story.append(Paragraph(escape(check.get("data", {}).get("reason", "Evidence unavailable")), styles["BodyText"]))
        acceptance = finding.get("acceptance", {})
        story.append(Paragraph(escape("Scoped acceptance: " + acceptance.get("state", "pending")), styles["BodyText"]))
    story.append(Paragraph("Reviews and waivers (machine verdict unchanged)", styles["Heading2"]))
    for review in snapshot["reviews"]:
        story.append(
            Paragraph(
                escape(
                    f"{review['decision']}: {review['rationale']} | actor {review['actor_id']} "
                    f"| expiry {review.get('expires_at')}"
                ),
                styles["BodyText"],
            )
        )
    from .report_exports import report_story

    story.extend(report_story(snapshot.get("advanced_reports", []), styles))
    story.append(
        Paragraph(
            "Fresh reproduction requires the compatible authorized runner and local materials. "
            "This report is scoped evidence, not certification or live-provider verification.",
            styles["BodyText"],
        )
    )
    SimpleDocTemplate(output, title="Susanoo Evaluation Evidence", author="Susanoo").build(story)
    return output.getvalue()
