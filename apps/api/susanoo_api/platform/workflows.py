"""Review, reproducibility and runtime-job orchestration with explicit authorization."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from psycopg.types.json import Jsonb
from susanoo_contracts import RunCreate, content_hash

from .database import PlatformError
from .service import ENGINEERS, OWNERS, REVIEWERS, create_run, dump, get_run, json_safe, referenced_versions


def create_job(ctx, project_id, body, key, *, repair=None):
    project = ctx.project(project_id, ENGINEERS)
    if not key or len(key) > 200:
        raise PlatformError("INVALID_INPUT", "A bounded Idempotency-Key is required.", 422)
    versions = [ctx.version(value, project) for value in body.version_ids]
    from .artifacts import authorized_artifact
    from susanoo_contracts import ArtifactRecord

    input_artifacts = []
    input_hidden = False
    for artifact_id in body.input_artifact_ids:
        artifact, artifact_project = authorized_artifact(ctx, artifact_id)
        if artifact_project["id"] != project["id"] or artifact["status"] != "ready":
            raise PlatformError("INVALID_INPUT", "Job inputs must be ready artifacts from this project.", 422)
        if artifact["audience"] == "reviewers" and not project["hidden_access"]:
            raise PlatformError("FORBIDDEN", "Reviewer-only artifacts need an explicit hidden evaluator grant.", 403)
        input_artifacts.append(ArtifactRecord.model_validate(artifact).model_dump(mode="json"))
        input_hidden = input_hidden or artifact["hidden"] or artifact["audience"] == "reviewers"
    kinds = {v["kind"] for v in versions}
    required = {
        "preflight": {"connection"},
        "build": {"connection", "source"},
        "source_import": {"source"},
        "action_comparison": {"fidelity"},
        "fresh_agent_pair": {"fidelity", "connection", "suite"},
        "reduce_reproduction": {"fidelity"},
        "repair_retest": {"source"},
        "simulator_inspect": {"environment"},
        "simulator_reset": {"environment"},
        "calibration": {"grader"},
        "trace_import": {"connection"},
        "incident_draft": {"connection"},
        "collision": {"collision"},
        "probe": {"probe"},
        "voice": {"voice", "connection", "suite"},
        "patch_proposal": {"source"},
    }[body.kind]
    if not required.issubset(kinds):
        raise PlatformError(
            "INVALID_INPUT", "This job is missing the required version kinds: " + ", ".join(sorted(required)), 422
        )
    approval_kinds = {"source", "fidelity", "suite", "voice", "collision", "probe", "environment"}
    for version in versions:
        if version["kind"] in approval_kinds and not version["approved"]:
            raise PlatformError("APPROVAL_REQUIRED", "Approve the pinned job inputs before dispatch.", 409)
    if body.kind in {"fresh_agent_pair", "voice"}:
        connection = next(v for v in versions if v["kind"] == "connection")
        capability = "tool_routing" if body.kind == "fresh_agent_pair" else "audio"
        if not connection["data"]["capabilities"][capability]:
            raise PlatformError("MISSING_CAPABILITY", f"This job requires {capability}.", 422)
    if body.kind == "trace_import":
        connection = next(v for v in versions if v["kind"] == "connection")
        if connection["data"]["family"] != "I13":
            raise PlatformError("INVALID_INPUT", "Historical ingestion requires an evidence-only connection.", 422)
    if body.finding_id:
        finding(ctx, body.finding_id, project)
    seen = {str(value["id"]) for value in versions}
    cursor = 0
    while cursor < len(versions):
        for version_id, kind in referenced_versions(versions[cursor]["data"]):
            if str(version_id) not in seen:
                if len(versions) >= 1000:
                    raise PlatformError(
                        "CAPACITY_EXCEEDED", "Pinned job dependency graph exceeds 1,000 revisions.", 422
                    )
                versions.append(ctx.version(version_id, {**project, "hidden_access": True}, kind))
                seen.add(str(version_id))
        cursor += 1
    for version in versions:
        if version["kind"] == "requirement" and not version["approved"]:
            raise PlatformError("APPROVAL_REQUIRED", "Job expectations need approved requirement revisions.", 409)
        if (
            version["kind"] == "grader"
            and version["data"]["mode"] == "semantic"
            and not project["policy"]["external_models_allowed"]
        ):
            raise PlatformError("FORBIDDEN", "External semantic grading is disabled by the current policy.", 403)
        approved_limits = version["data"].get("limits")
        if approved_limits and any(value > approved_limits[key] for key, value in dump(body.limits).items()):
            raise PlatformError("BUDGET_EXCEEDED", "Job limits exceed a pinned plan's approved limits.", 422)
    if body.kind == "repair_retest" and repair is None:
        raise PlatformError("INVALID_INPUT", "Launch repair validation from the exact proposal.", 422)
    request = {"project_id": str(project_id), "job": dump(body), "repair": repair}
    digest = content_hash(request)
    ctx.db.execute("select id from platform.projects where id=%s for update", (project_id,))
    old = ctx.db.execute(
        "select * from platform.runs where workspace_id=%s and operation=%s and idempotency_key=%s",
        (project["workspace_id"], f"job:{body.kind}:{project_id}", key),
    ).fetchone()
    if old:
        if old["request_hash"] != digest:
            raise PlatformError("IDEMPOTENCY_CONFLICT", "Job key conflicts with previous input.", 409)
        return old
    manifest = json_safe(
        {
            "schema_version": "1.0",
            "kind": body.kind,
            "versions": versions,
            "version_ids": [str(value) for value in body.version_ids],
            "input_artifacts": input_artifacts,
            "finding_id": str(body.finding_id) if body.finding_id else None,
            "repair": repair,
            "policy": project["policy"],
            "limits": dump(body.limits),
            "timezone": project["timezone"],
        }
    )
    row = ctx.db.execute(
        """insert into platform.runs(workspace_id,project_id,kind,manifest,manifest_hash,request_hash,
        idempotency_key,operation,created_by)
        values(%s,%s,%s,%s,%s,%s,%s,%s,%s) returning *""",
        (
            project["workspace_id"],
            project_id,
            body.kind,
            Jsonb(manifest),
            content_hash(manifest),
            digest,
            key,
            f"job:{body.kind}:{project_id}",
            ctx.actor["user_id"],
        ),
    ).fetchone()
    ctx.db.execute(
        """insert into platform.attempts(workspace_id,project_id,run_id,namespace,hidden) values(%s,%s,%s,%s,%s)""",
        (
            project["workspace_id"],
            project_id,
            row["id"],
            "job-" + uuid4().hex,
            input_hidden or any(v["hidden"] for v in versions),
        ),
    )
    ctx.audit("job.launched", row["id"], project, {"kind": body.kind})
    return row


def finding(ctx, finding_id, project=None):
    row = ctx.db.execute("select * from platform.findings where id=%s", (finding_id,)).fetchone()
    if not row:
        raise PlatformError("NOT_FOUND", "Finding not found.", 404)
    project = project or ctx.project(row["project_id"])
    if str(project["id"]) != str(row["project_id"]) or (row["hidden"] and not project["hidden_access"]):
        raise PlatformError("NOT_FOUND", "Finding not available to this role.", 404)
    return row, project


def review(ctx, finding_id, body):
    row, project = finding(ctx, finding_id)
    project = ctx.project(project["id"], REVIEWERS)
    if body.decision not in {"request_evidence", "revoke_waiver"} and not body.evidence_ids:
        raise PlatformError("INVALID_INPUT", "This decision requires explicit authorized evidence references.", 422)
    for evidence_id in body.evidence_ids:
        evidence = ctx.db.execute(
            "select hidden from platform.evidence where id=%s and project_id=%s",
            (evidence_id, project["id"]),
        ).fetchone()
        if not evidence or (evidence["hidden"] and not project["hidden_access"]):
            raise PlatformError("NOT_FOUND", "Evidence not available.", 404)
    result = ctx.db.execute(
        """insert into platform.review_events(workspace_id,project_id,subject_id,actor_id,
        decision,rationale,evidence_ids)
        values(%s,%s,%s,%s,%s,%s,%s) returning *""",
        (
            project["workspace_id"],
            project["id"],
            row["id"],
            ctx.actor["user_id"],
            body.decision,
            body.rationale,
            body.evidence_ids,
        ),
    ).fetchone()
    ctx.audit("finding.reviewed", finding_id, project, {"decision": body.decision})
    return result


def acceptance_projection(ctx, finding_row):
    events = ctx.db.execute(
        "select * from platform.review_events where subject_id=%s order by created_at,id", (finding_row["id"],)
    ).fetchall()
    waiver = None
    accepted = False
    disputed = False
    for event in events:
        if event["decision"] == "waiver":
            waiver = event
        elif event["decision"] == "revoke_waiver":
            waiver = None
        elif event["decision"] == "accept":
            accepted, disputed = True, False
        elif event["decision"] == "dispute":
            accepted, disputed = False, True
    active = bool(waiver and waiver["expires_at"] > datetime.now(UTC))
    eligible = finding_row["verdict"] == "PASS" or active
    state = (
        "accepted_with_waiver"
        if accepted and active
        else "accepted"
        if accepted and eligible
        else "disputed"
        if disputed
        else "pending"
    )
    return {
        "state": state,
        "eligible": eligible,
        "active_waiver_id": waiver["id"] if active else None,
        "waiver_expires_at": waiver["expires_at"] if active else None,
        "machine_verdict": finding_row["verdict"],
        "default_ci_gate_unchanged": True,
    }


def waiver(ctx, finding_id, body):
    row, project = finding(ctx, finding_id)
    project = ctx.project(project["id"], REVIEWERS)
    if body.expires_at.tzinfo is None or not datetime.now(UTC) < body.expires_at <= datetime.now(UTC) + timedelta(
        days=90
    ):
        raise PlatformError("INVALID_INPUT", "Waivers require an aware future expiry within 90 days.", 422)
    result = ctx.db.execute(
        """insert into platform.review_events(workspace_id,project_id,subject_id,actor_id,decision,
        rationale,expires_at,metadata)
        values(%s,%s,%s,%s,'waiver',%s,%s,%s) returning *""",
        (
            project["workspace_id"],
            project["id"],
            finding_id,
            ctx.actor["user_id"],
            body.rationale,
            body.expires_at,
            Jsonb({"remediation_owner": body.remediation_owner}),
        ),
    ).fetchone()
    ctx.audit("waiver.created", result["id"], project)
    return result


def reproduce(ctx, finding_id, key):
    row, project = finding(ctx, finding_id)
    run, _ = get_run(ctx, row["run_id"], ENGINEERS)
    if run["kind"] != "outcome_suite":
        raise PlatformError("MISSING_CAPABILITY", "This finding is not a fresh outcome-suite reproduction.", 422)
    body = RunCreate.model_validate(run["manifest"]["request"])
    return create_run(ctx, project["id"], body, key, original_run_id=run["id"], original_finding=row)


def comparison(ctx, baseline_id, candidate_id):
    baseline, project = get_run(ctx, baseline_id)
    candidate, candidate_project = get_run(ctx, candidate_id)
    if project["id"] != candidate_project["id"]:
        raise PlatformError("INVALID_INPUT", "Comparisons must remain within one project.", 422)
    if baseline["kind"] != "outcome_suite" or candidate["kind"] != "outcome_suite":
        raise PlatformError("MISSING_CAPABILITY", "Outcome comparisons require two outcome-suite results.", 422)

    def indexed(run):
        cases = {item["case"]["id"]: item for item in run["manifest"].get("cases", [])}
        attempts = ctx.db.execute(
            "select * from platform.attempts where run_id=%s order by repetition", (run["id"],)
        ).fetchall()
        return {
            (cases[str(item["case_version_id"])]["case"]["logical_id"], item["repetition"], item["arm"]): (
                item,
                cases[str(item["case_version_id"])],
            )
            for item in attempts
            if item["case_version_id"] and (not item["hidden"] or project["hidden_access"])
        }

    before, after = indexed(baseline), indexed(candidate)
    rows = []
    for key in sorted(set(before) | set(after)):
        a, b = before.get(key), after.get(key)
        compatible = bool(
            a
            and b
            and a[1]["case"]["content_hash"] == b[1]["case"]["content_hash"]
            and baseline["manifest"]["environment"]["content_hash"]
            == candidate["manifest"]["environment"]["content_hash"]
        )
        rows.append(
            {
                "logical_id": key[0],
                "repetition": key[1],
                "arm": key[2],
                "compatible": compatible,
                "classification": "not_comparable"
                if not compatible
                else "unresolved"
                if a[0]["business_verdict"] not in {"PASS", "FAIL"}
                or b[0]["business_verdict"] not in {"PASS", "FAIL"}
                or a[0]["execution_state"] != "completed"
                or b[0]["execution_state"] != "completed"
                else "unchanged"
                if a[0]["business_verdict"] == b[0]["business_verdict"]
                else "regression"
                if a[0]["business_verdict"] == "PASS"
                else "improvement",
                "before_execution": a[0]["execution_state"] if a else None,
                "after_execution": b[0]["execution_state"] if b else None,
                "compatibility_reason": "identical_case_and_environment_semantics"
                if compatible
                else "case_missing_or_pinned_semantics_changed",
                "before": a[0]["business_verdict"] if a else None,
                "after": b[0]["business_verdict"] if b else None,
                "transition": f"{a[0]['business_verdict']} -> {b[0]['business_verdict']}"
                if compatible
                else "not_comparable",
            }
        )
    ctx.audit("runs.compared", candidate_id, project, {"baseline_id": str(baseline_id)})
    return {
        "baseline_id": baseline_id,
        "candidate_id": candidate_id,
        "rows": rows,
        "hidden_details_omitted": not project["hidden_access"],
    }


def create_schedule(ctx, project_id, body):
    project = ctx.project(project_id, ENGINEERS)
    ctx.version(body.run.suite_version_id, project, "suite", True)
    ctx.version(body.run.connection_version_id, project, "connection")
    ctx.version(body.run.environment_version_id, project, "environment", True)
    result = ctx.db.execute(
        """insert into platform.schedules(workspace_id,project_id,name,interval_seconds,configuration,
        enabled,next_due_at,created_by)
        values(%s,%s,%s,%s,%s,%s,now()+make_interval(secs=>%s),%s) returning *""",
        (
            project["workspace_id"],
            project_id,
            body.name,
            body.interval_seconds,
            Jsonb(dump(body.run)),
            body.enabled,
            body.interval_seconds,
            ctx.actor["user_id"],
        ),
    ).fetchone()
    ctx.audit("schedule.created", result["id"], project)
    return result


def add_dependency(ctx, project_id, body):
    project = ctx.project(project_id, ENGINEERS)
    ctx.version(body.source_version_id, project)
    ctx.version(body.target_version_id, project)
    if body.relationship == "observed_dependency":
        raise PlatformError("FORBIDDEN", "Observed dependencies must originate from assigned runner evidence.", 403)
    ctx.db.execute(
        "insert into platform.version_links values(%s,%s,%s,%s,%s) on conflict do nothing",
        (project["workspace_id"], project_id, body.source_version_id, body.target_version_id, body.relationship),
    )
    ctx.audit("dependency.declared", body.source_version_id, project)
    return dump(body)


def invalidate(ctx, project_id, body):
    project = ctx.project(project_id, ENGINEERS)
    ctx.version(body.version_id, project)
    change_id = uuid4()
    rows = ctx.db.execute(
        """with recursive affected(id,path) as (
        select %s::uuid,array[%s::uuid] union all
        select e.source_id,a.path||e.source_id from platform.version_links e join affected a on e.target_id=a.id
        where e.project_id=%s and not e.source_id=any(a.path) and cardinality(a.path)<100)
        select distinct on(id) id,path from affected order by id,cardinality(path)""",
        (body.version_id, body.version_id, project_id),
    ).fetchall()
    for row in rows:
        ctx.db.execute(
            """insert into platform.invalidations(workspace_id,project_id,version_id,change_id,
            classification,reason,dependency_path)
            values(%s,%s,%s,%s,%s,%s,%s)""",
            (project["workspace_id"], project_id, row["id"], change_id, body.classification, body.reason, row["path"]),
        )
    ctx.audit("change.recorded", change_id, project)
    return {
        "change_id": change_id,
        "affected_version_ids": [row["id"] for row in rows],
        "dependency_coverage": "declared_and_received_only",
    }


def delete_project(ctx, project_id):
    project = ctx.project(project_id, OWNERS)
    from .recovery import record_deletion

    ctx.db.execute("select pg_advisory_xact_lock(727004)")
    record_deletion(project)
    ctx.db.execute(
        "update platform.projects set deleted_at=now(),purge_after=now()+interval '24 hours' where id=%s", (project_id,)
    )
    ctx.db.execute(
        """insert into platform.deletion_tombstones(project_id,workspace_id,deleted_at,purge_deadline)
        values(%s,%s,now(),now()+interval '24 hours') on conflict do nothing""",
        (project_id, project["workspace_id"]),
    )
    ctx.db.execute(
        "update platform.credentials set revoked_at=now() where project_id=%s and revoked_at is null", (project_id,)
    )
    ctx.db.execute("update platform.runners set revoked_at=now() where project_id=%s", (project_id,))
    ctx.db.execute("update platform.jobs set status='cancelled' where project_id=%s and status='queued'", (project_id,))
    ctx.db.execute("update platform.schedules set enabled=false where project_id=%s", (project_id,))
    ctx.audit("project.deleted", project_id, project)
    return {"project_id": project_id, "access_revoked": True, "purge_within_hours": 24}
