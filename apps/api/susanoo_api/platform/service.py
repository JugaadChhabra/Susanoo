"""Authorized immutable configuration and queue orchestration, not agent execution."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from susanoo_contracts import content_hash

from .database import PlatformError
from .security import issue_token, token_hash

ENGINEERS = {"engineer"}
REVIEWERS = {"reviewer"}
OWNERS = {"owner"}


def dump(value):
    return value.model_dump(mode="json")


def workspace_member(ctx, workspace_id, roles=None):
    if ctx.actor.get("kind") == "cli" and roles:
        raise PlatformError("FORBIDDEN", "Project CLI credentials cannot administer a workspace.", 403)
    row = ctx.db.execute(
        """select m.* from platform.memberships m join platform.workspaces w on w.id=m.workspace_id
        where m.workspace_id=%s and m.user_id=%s and w.deleted_at is null""",
        (workspace_id, ctx.actor["user_id"]),
    ).fetchone()
    if not row:
        raise PlatformError("NOT_FOUND", "Workspace not found.", 404)
    if roles and not set(row["roles"]).intersection(roles):
        raise PlatformError("FORBIDDEN", "Workspace owner permission is required.", 403)
    return row


def create_workspace(ctx, name):
    if ctx.actor.get("kind") == "cli":
        raise PlatformError("FORBIDDEN", "Workspace creation requires a browser session.", 403)
    row = ctx.db.execute("insert into platform.workspaces(name) values(%s) returning *", (name,)).fetchone()
    ctx.db.execute(
        "insert into platform.memberships values(%s,%s,%s)",
        (row["id"], ctx.actor["user_id"], ["owner", "engineer"]),
    )
    ctx.audit("workspace.created", row["id"])
    return row


def create_project(ctx, body):
    workspace_member(ctx, body.workspace_id, OWNERS)
    row = ctx.db.execute(
        """insert into platform.projects(workspace_id,name,workflow,timezone,policy)
        values(%s,%s,%s,%s,%s) returning *""",
        (body.workspace_id, body.name, body.workflow, body.timezone, Jsonb(dump(body.policy))),
    ).fetchone()
    ctx.audit("project.created", row["id"], row)
    apply_policy(ctx, row["id"], body.policy)
    return ctx.project(row["id"])


def apply_policy(ctx, project_id, policy):
    from susanoo_contracts import PolicySpec, VersionCreate

    project = ctx.project(project_id, OWNERS)
    version = create_version(
        ctx,
        project_id,
        VersionCreate(
            logical_id="data-policy",
            title="Data and execution policy",
            data=PolicySpec(policy=policy),
        ),
    )
    ctx.db.execute(
        """insert into platform.approvals(workspace_id,project_id,version_id,content_hash,actor_id,rationale)
        values(%s,%s,%s,%s,%s,'Owner authorized this data and resource policy')""",
        (project["workspace_id"], project_id, version["id"], version["content_hash"], ctx.actor["user_id"]),
    )
    ctx.db.execute(
        "update platform.projects set policy=%s,policy_version_id=%s where id=%s",
        (Jsonb(dump(policy)), version["id"], project_id),
    )
    ctx.audit("policy.activated", version["id"], project)
    return {"project_id": project_id, "policy_version_id": version["id"], "content_hash": version["content_hash"]}


def change_membership(ctx, workspace_id, body):
    workspace_member(ctx, workspace_id, OWNERS)
    if body.project_id:
        project = ctx.project(body.project_id, OWNERS)
        if project["workspace_id"] != workspace_id:
            raise PlatformError("NOT_FOUND", "Project not found.", 404)
        ctx.db.execute(
            """insert into platform.project_grants values(%s,%s,%s,%s,%s)
            on conflict(project_id,user_id) do update set roles=excluded.roles,hidden_access=excluded.hidden_access""",
            (workspace_id, body.project_id, body.user_id, list(body.roles), body.hidden_access),
        )
    else:
        ctx.db.execute("select id from platform.workspaces where id=%s for update", (workspace_id,))
        if "owner" not in body.roles:
            count = ctx.db.execute(
                "select count(*) n from platform.memberships where workspace_id=%s and 'owner'=any(roles)",
                (workspace_id,),
            ).fetchone()["n"]
            current = ctx.db.execute(
                "select roles from platform.memberships where workspace_id=%s and user_id=%s",
                (workspace_id, body.user_id),
            ).fetchone()
            if current and "owner" in current["roles"] and count <= 1:
                raise PlatformError("INVALID_INPUT", "The workspace must retain an owner.", 409)
        ctx.db.execute(
            """insert into platform.memberships values(%s,%s,%s) on conflict(workspace_id,user_id)
            do update set roles=excluded.roles""",
            (workspace_id, body.user_id, list(body.roles)),
        )
    ctx.audit("membership.changed", body.user_id, details={"workspace_id": str(workspace_id)})
    return {"user_id": body.user_id, "roles": body.roles, "hidden_access": body.hidden_access}


def referenced_versions(data):
    relationships = {
        "check": {"requirement_version_id": "requirement", "grader_version_id": "grader"},
        "case": {"fixture_version_id": "fixture", "check_version_ids": "check", "dependencies": None},
        "grader": {"positive_control_ids": "case", "negative_control_ids": "case", "heldout_control_ids": "case"},
        "suite": {"case_version_ids": "case"},
        "environment": {"fixture_version_id": "fixture"},
        "fidelity": {"simulator_version_id": "environment", "reference_version_id": "environment"},
        "probe": {"fidelity_version_id": "fidelity", "candidate_ids": "case"},
        "collision": {"environment_version_id": "environment"},
    }
    result = []
    for field, kind in relationships.get(data["kind"], {}).items():
        values = data.get(field)
        if values is not None:
            result.extend((value, kind) for value in (values if isinstance(values, list) else [values]))
    return result


def remove_member(ctx, workspace_id, user_id):
    workspace_member(ctx, workspace_id, OWNERS)
    ctx.db.execute("select id from platform.workspaces where id=%s for update", (workspace_id,))
    member = ctx.db.execute(
        "select roles from platform.memberships where workspace_id=%s and user_id=%s", (workspace_id, user_id)
    ).fetchone()
    if not member:
        raise PlatformError("NOT_FOUND", "Member not found.", 404)
    if (
        "owner" in member["roles"]
        and ctx.db.execute(
            "select count(*) n from platform.memberships where workspace_id=%s and 'owner'=any(roles)",
            (workspace_id,),
        ).fetchone()["n"]
        <= 1
    ):
        raise PlatformError("INVALID_STATE", "The workspace must retain an owner.", 409)
    ctx.db.execute("delete from platform.project_grants where workspace_id=%s and user_id=%s", (workspace_id, user_id))
    ctx.db.execute("delete from platform.memberships where workspace_id=%s and user_id=%s", (workspace_id, user_id))
    ctx.db.execute(
        "update platform.credentials set revoked_at=now() where workspace_id=%s and user_id=%s", (workspace_id, user_id)
    )
    ctx.audit("membership.revoked", user_id, details={"workspace_id": str(workspace_id)})
    return {"revoked": True}


def create_version(ctx, project_id, body):
    project = ctx.project(project_id, OWNERS if body.data.kind == "policy" else ENGINEERS)
    data = dump(body.data)
    hidden = body.hidden or data.get("visibility") == "hidden"
    if hidden and not project["hidden_access"]:
        raise PlatformError("FORBIDDEN", "A hidden-case grant is required.", 403)
    references = [ctx.version(value, project, kind) for value, kind in referenced_versions(data)]
    if data["kind"] not in {"suite", "grader"} and not hidden and any(row["hidden"] for row in references):
        raise PlatformError("INVALID_INPUT", "A public version cannot embed a hidden dependency.", 422)
    if data["kind"] == "case" and hidden and any(not row["hidden"] for row in references if row["kind"] == "check"):
        raise PlatformError("INVALID_INPUT", "Hidden case expectations must use hidden check versions.", 422)
    if data["kind"] == "grader" and data["mode"] == "semantic" and not project["policy"]["external_models_allowed"]:
        raise PlatformError("FORBIDDEN", "External semantic grading is disabled by the project policy.", 403)
    ctx.db.execute("select id from platform.projects where id=%s for update", (project_id,))
    revision = ctx.db.execute(
        "select coalesce(max(revision),0)+1 n from platform.versions where project_id=%s and kind=%s and logical_id=%s",
        (project_id, data["kind"], body.logical_id),
    ).fetchone()["n"]
    meaning = {"data": data, "provenance": body.provenance, "hidden": hidden}
    row = ctx.db.execute(
        """insert into platform.versions(workspace_id,project_id,kind,logical_id,revision,title,data,provenance,
        content_hash,hidden,created_by) values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning *""",
        (
            project["workspace_id"],
            project_id,
            data["kind"],
            body.logical_id,
            revision,
            body.title,
            Jsonb(data),
            Jsonb(body.provenance),
            content_hash(meaning),
            hidden,
            ctx.actor["user_id"],
        ),
    ).fetchone()
    for ref in references:
        ctx.db.execute(
            "insert into platform.version_links values(%s,%s,%s,%s,'declared_dependency')",
            (project["workspace_id"], project_id, row["id"], ref["id"]),
        )
    ctx.audit("version.created", row["id"], project, {"kind": row["kind"], "revision": revision})
    return row


def suite_contents(ctx, project, suite):
    cases = []
    for case_id in suite["data"]["case_version_ids"]:
        # Runner manifests may contain hidden evaluation; browser serializers apply access separately.
        trusted = {**project, "hidden_access": True}
        case = ctx.version(case_id, trusted, "case")
        checks = [ctx.version(value, trusted, "check") for value in case["data"]["check_version_ids"]]
        fixture = ctx.version(case["data"]["fixture_version_id"], trusted, "fixture")
        requirements = {
            check["data"]["requirement_version_id"]: ctx.version(
                check["data"]["requirement_version_id"], trusted, "requirement", True
            )
            for check in checks
        }
        graders = {
            check["data"]["grader_version_id"]: ctx.version(check["data"]["grader_version_id"], trusted, "grader", True)
            for check in checks
            if check["data"].get("grader_version_id")
        }
        if not any(check["data"]["required"] for check in checks) and case["data"]["required"]:
            raise PlatformError("INVALID_INPUT", "Every required case needs a required check.", 422)
        cases.append(
            {
                "case": case,
                "checks": checks,
                "fixture": fixture,
                "requirements": list(requirements.values()),
                "graders": list(graders.values()),
            }
        )
    if not any(item["case"]["data"]["required"] for item in cases):
        raise PlatformError("INVALID_INPUT", "An approved suite must contain required cases.", 422)
    return cases


def approve_version(ctx, version_id, body):
    initial = ctx.db.execute("select project_id from platform.versions where id=%s", (version_id,)).fetchone()
    if not initial:
        raise PlatformError("NOT_FOUND", "Version not found.", 404)
    project = ctx.project(initial["project_id"], REVIEWERS)
    version = ctx.version(version_id, project)
    if body.content_hash != version["content_hash"]:
        raise PlatformError("INCOMPATIBLE_VERSION", "Approval must name the exact content hash.", 409)
    if version["created_by"] == ctx.actor["user_id"]:
        raise PlatformError("FORBIDDEN", "A different reviewer must approve this version.", 403)
    if version["kind"] == "suite":
        cases = suite_contents(ctx, project, version)
        if any(item["case"]["hidden"] for item in cases) and not project["hidden_access"]:
            raise PlatformError("FORBIDDEN", "Hidden-suite approval requires an explicit hidden evaluator grant.", 403)
        for item in cases:
            for check in item["checks"]:
                ctx.version(
                    check["data"]["requirement_version_id"], {**project, "hidden_access": True}, "requirement", True
                )
                if check["data"]["severity"] == "critical" or check["data"]["operator"] == "semantic":
                    grader_id = check["data"].get("grader_version_id")
                    if not grader_id:
                        raise PlatformError(
                            "CALIBRATION_REQUIRED", "Critical checks need an approved challenged grader.", 409
                        )
                    ctx.version(grader_id, {**project, "hidden_access": True}, "grader", True)
    if version["kind"] == "grader":
        found = ctx.db.execute(
            """select 1 from platform.runs r join platform.attempts a on a.run_id=r.id
            join platform.runner_events e on e.attempt_id=a.id and e.event_type='job_result'
            where r.project_id=%s and r.kind='calibration' and r.status='completed' and r.gate='READY_FOR_ACCEPTANCE'
            and r.manifest->'version_ids' @> %s::jsonb
            and e.payload->'payload'->>'control_set_hash'=%s and e.payload->'payload'->>'controls_verified'='true'
            and (e.payload->'payload'->>'missed_defects')::int<=%s
            and (e.payload->'payload'->>'false_alarms')::int<=%s""",
            (
                project["id"],
                Jsonb([str(version_id)]),
                version["data"]["control_set_hash"],
                version["data"]["max_missed_defects"],
                version["data"]["max_false_alarms"],
            ),
        ).fetchone()
        if not found:
            raise PlatformError(
                "CALIBRATION_REQUIRED", "A successful matching control-set calibration is required.", 409
            )
    row = ctx.db.execute(
        """insert into platform.approvals(workspace_id,project_id,version_id,content_hash,actor_id,rationale)
        values(%s,%s,%s,%s,%s,%s) on conflict(version_id,content_hash) do nothing returning *""",
        (project["workspace_id"], project["id"], version_id, body.content_hash, ctx.actor["user_id"], body.rationale),
    ).fetchone()
    ctx.audit("version.approved", version_id, project)
    return row or {"version_id": version_id, "approved": True}


def create_run(ctx, project_id, body, key, *, original_run_id=None, original_finding=None):
    project = ctx.project(project_id, ENGINEERS)
    if ctx.actor.get("project_id") and ctx.actor["project_id"] != UUID(str(project_id)):
        raise PlatformError("FORBIDDEN", "The CLI credential is scoped to another project.", 403)
    if not key or len(key) > 200:
        raise PlatformError("INVALID_INPUT", "Supply an Idempotency-Key of 1 to 200 characters.", 422)
    request_hash = content_hash(
        {
            "project_id": str(project_id),
            "body": dump(body),
            "original": str(original_run_id),
            "finding_id": str(original_finding["id"]) if original_finding else None,
        }
    )
    ctx.db.execute("select id from platform.projects where id=%s for update", (project_id,))
    prior = ctx.db.execute(
        "select * from platform.runs where workspace_id=%s and operation=%s and idempotency_key=%s",
        (project["workspace_id"], f"launch:{project_id}", key),
    ).fetchone()
    if prior:
        if prior["request_hash"] != request_hash:
            raise PlatformError("IDEMPOTENCY_CONFLICT", "This launch key was used with different input.", 409)
        return prior
    suite = ctx.version(body.suite_version_id, project, "suite", True)
    connection = ctx.version(body.connection_version_id, project, "connection")
    environment = ctx.version(body.environment_version_id, project, "environment", True)
    capabilities = connection["data"]["capabilities"]
    if not capabilities["invoke"] or not capabilities["observe"]:
        raise PlatformError(
            "MISSING_CAPABILITY", "Invocation and independent observation are required for an outcome suite.", 422
        )
    reports = ctx.db.execute(
        """select e.payload from platform.runs r join platform.attempts a on a.run_id=r.id
        join platform.runner_events e on e.attempt_id=a.id and e.event_type='job_result'
        where r.project_id=%s and r.kind='preflight' and r.status='completed' and r.gate='READY_FOR_ACCEPTANCE'
        and r.manifest->'version_ids' @> %s::jsonb order by e.received_at desc limit 1""",
        (project_id, Jsonb([str(body.connection_version_id)])),
    ).fetchone()
    if not reports or not all(
        reports["payload"]["payload"].get(value) is True for value in ("invocation_ok", "observer_ok", "cleanup_ok")
    ):
        raise PlatformError(
            "PREFLIGHT_REQUIRED", "Invocation, observation and cleanup need separate successful preflight checks.", 409
        )
    limits = dump(body.limits)
    approved_limits = suite["data"]["limits"]
    if any(value > approved_limits[name] for name, value in limits.items()):
        raise PlatformError("BUDGET_EXCEEDED", "Requested limits exceed the approved suite.", 422)
    if limits["max_concurrency"] > capabilities["max_concurrency"] or (
        limits["max_concurrency"] > 1 and not capabilities["isolation_tested"]
    ):
        raise PlatformError("MISSING_CAPABILITY", "Parallel isolation has not been qualified for this adapter.", 422)
    if limits["max_concurrency"] > 1 and (
        reports["payload"]["payload"].get("isolation_verified") is not True
        or reports["payload"]["payload"].get("max_concurrency", 1) < limits["max_concurrency"]
    ):
        raise PlatformError(
            "PREFLIGHT_REQUIRED", "This concurrency needs an explicit successful isolation preflight.", 409
        )
    cases = suite_contents(ctx, project, suite)
    manifest = {
        "schema_version": "1.0",
        "kind": "outcome_suite",
        "request": dump(body),
        "suite": suite,
        "connection": connection,
        "environment": environment,
        "cases": cases,
        "policy": project["policy"],
        "timezone": project["timezone"],
        "limits": limits,
        "reproduction": {key: str(original_finding[key]) for key in ("id", "attempt_id", "check_version_id")}
        if original_finding
        else None,
    }
    # Database datetimes/UUIDs are normalized before hashing and persistence.
    manifest = json_safe(manifest)
    row = ctx.db.execute(
        """insert into platform.runs(workspace_id,project_id,kind,manifest,manifest_hash,request_hash,idempotency_key,
        operation,created_by,original_run_id) values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning *""",
        (
            project["workspace_id"],
            project_id,
            "outcome_suite",
            Jsonb(manifest),
            content_hash(manifest),
            request_hash,
            key,
            f"launch:{project_id}",
            ctx.actor["user_id"],
            original_run_id,
        ),
    ).fetchone()
    for item in cases:
        for repetition in range(body.repetitions):
            ctx.db.execute(
                """insert into platform.attempts(workspace_id,project_id,run_id,case_version_id,repetition,
                namespace,required,hidden)
                values(%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    project["workspace_id"],
                    project_id,
                    row["id"],
                    item["case"]["id"],
                    repetition,
                    "attempt-" + uuid4().hex,
                    item["case"]["data"]["required"],
                    item["case"]["hidden"],
                ),
            )
    ctx.audit("run.launched", row["id"], project, {"manifest_hash": row["manifest_hash"]})
    return row


def json_safe(value):
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, (UUID, datetime)):
        return str(value)
    return value


def get_run(ctx, run_id, roles=None):
    row = ctx.db.execute("select * from platform.runs where id=%s", (run_id,)).fetchone()
    if not row:
        raise PlatformError("NOT_FOUND", "Run not found.", 404)
    project = ctx.project(row["project_id"], roles)
    return row, project


def browser_run(ctx, row, project):
    result = {key: value for key, value in row.items() if key not in {"manifest", "request_hash", "idempotency_key"}}
    attempts = ctx.db.execute(
        """select id,case_version_id,repetition,arm,required,execution_state,business_verdict,
        cleanup_state,reconciliation_state,
        hidden from platform.attempts where run_id=%s and (not hidden or %s) order by id limit 201""",
        (row["id"], project["hidden_access"]),
    ).fetchall()
    counts = ctx.db.execute(
        """select count(*) scheduled,count(*) filter(where execution_state<>'queued') attempted,
        count(*) filter(where execution_state='completed') completed,
        count(*) filter(where business_verdict='PASS') pass,count(*) filter(where business_verdict='FAIL') fail,
        count(*) filter(where business_verdict='UNKNOWN') unknown,
        count(*) filter(where execution_state in ('error','timed_out','cancelled','interrupted')) errors,
        count(*) filter(where hidden) hidden from platform.attempts where run_id=%s""",
        (row["id"],),
    ).fetchone()
    result["hidden_attempt_count"] = counts.pop("hidden")
    result["counts"] = counts
    result["attempts"] = attempts[:200]
    result["attempts_next_cursor"] = attempts[199]["id"] if len(attempts) > 200 else None
    result["reproduction_outcome"] = None
    subject = row["manifest"].get("reproduction")
    if row.get("original_run_id") and subject:
        results = ctx.db.execute(
            """select e.data->>'verdict' verdict,a.execution_state,a.cleanup_state,a.reconciliation_state
            from platform.attempts a join platform.evidence e on e.attempt_id=a.id and e.kind='check'
            join platform.attempts original on original.id=%s
            where a.run_id=%s and a.case_version_id=original.case_version_id and e.version_id=%s""",
            (subject["attempt_id"], row["id"], subject["check_version_id"]),
        ).fetchall()
        expected = ctx.db.execute(
            """select count(*) n from platform.attempts a join platform.attempts original
            on original.id=%s where a.run_id=%s and a.case_version_id=original.case_version_id""",
            (subject["attempt_id"], row["id"]),
        ).fetchone()["n"]
        healthy = all(
            item["execution_state"] == "completed"
            and item["cleanup_state"] in {"succeeded", "not_required"}
            and item["reconciliation_state"] in {"resolved", "not_needed"}
            for item in results
        )
        result["reproduction_outcome"] = (
            "pending"
            if row["status"] in {"queued", "running"}
            else "reproduced"
            if any(item["verdict"] == "FAIL" for item in results)
            else "not_reproduced"
            if results and len(results) == expected and healthy and all(item["verdict"] == "PASS" for item in results)
            else "inconclusive"
        )
    result["request"] = row["manifest"].get("request")
    return result


def cancel_run(ctx, run_id):
    row, project = get_run(ctx, run_id, ENGINEERS)
    if row["status"] in {"completed", "cancelled", "interrupted"}:
        raise PlatformError("INVALID_STATE", "This run has settled.", 409)
    ctx.db.execute(
        "update platform.runs set cancel_requested_at=coalesce(cancel_requested_at,now()) where id=%s", (run_id,)
    )
    ctx.db.execute(
        """update platform.attempts set execution_state='cancelled',business_verdict='UNKNOWN',
        cleanup_state='not_required',reason='Cancelled before dispatch' where run_id=%s and execution_state='queued'""",
        (run_id,),
    )
    project_gate(ctx.db, run_id)
    ctx.audit("run.cancel_requested", run_id, project)
    return {"run_id": run_id, "cancel_pending": True}


def project_gate(db, run_id):
    """Project reported business verdicts and independent operational blockers; never grade observations here."""
    attempts = db.execute("select * from platform.attempts where run_id=%s", (run_id,)).fetchall()
    active = {"queued", "preparing", "running", "observing", "evaluating"}
    if any(item["execution_state"] in active for item in attempts):
        state, gate = "running", "IN_PROGRESS"
    else:
        blocked = any(
            item["cleanup_state"] == "failed"
            or item["reconciliation_state"] in {"pending", "unresolved"}
            or (item["required"] and (item["business_verdict"] == "FAIL" or item["execution_state"] != "completed"))
            for item in attempts
        )
        unknown = any(item["required"] and item["business_verdict"] != "PASS" for item in attempts)
        state = "completed"
        gate = "BLOCKED" if blocked or not attempts else "NEEDS_REVIEW" if unknown else "READY_FOR_ACCEPTANCE"
    db.execute("update platform.runs set status=%s,gate=%s where id=%s", (state, gate, run_id))
    return gate


def registration_token(ctx, project_id):
    project = ctx.project(project_id, OWNERS)
    result = issue_token(ctx.db, "registration", project=project, user_id=ctx.actor["user_id"], hours=1 / 6)
    ctx.audit("runner.registration_issued", UUID(result["credential_id"]), project)
    return result


def register_runner(db, body):
    from .security import credential

    registration = credential(db, body.registration_token, {"registration"})
    found = db.execute(
        "update platform.credentials set consumed_at=now() where id=%s and consumed_at is null returning id",
        (registration["id"],),
    ).fetchone()
    if not found:
        raise PlatformError("UNAUTHENTICATED", "Registration token already consumed.", 401)
    runner = db.execute(
        """insert into platform.runners(workspace_id,project_id,name,version,protocol_version,
        capabilities,last_heartbeat)
        values(%s,%s,%s,%s,%s,%s,now()) returning *""",
        (
            registration["workspace_id"],
            registration["project_id"],
            body.name,
            body.version,
            body.protocol_version,
            body.capabilities,
        ),
    ).fetchone()
    token = issue_token(
        db,
        "runner",
        project={"workspace_id": runner["workspace_id"], "id": runner["project_id"]},
        runner_id=runner["id"],
        hours=24,
    )
    return {
        **token,
        "runner_id": str(runner["id"]),
        "heartbeat_seconds": 10,
        "control_poll_seconds": 2,
        "lease_seconds": 30,
    }


def runner_claim(ctx, body):
    import secrets

    runner = ctx.actor["runner"]
    ctx.db.execute("update platform.runners set last_heartbeat=now() where id=%s", (runner["id"],))
    if not set(body.supported_job_kinds).issubset(set(runner["capabilities"])):
        raise PlatformError("MISSING_CAPABILITY", "The claim advertises an unregistered job capability.", 422)
    # A project lock serializes independent runner claims and the per-run concurrency check.
    ctx.db.execute("select id from platform.projects where id=%s for update", (runner["project_id"],))
    row = ctx.db.execute(
        """select a.*,r.kind,r.manifest,r.manifest_hash,r.created_at run_created_at from platform.attempts a
        join platform.runs r on r.id=a.run_id join platform.projects p on p.id=a.project_id
        where a.project_id=%s and a.workspace_id=%s and a.execution_state='queued' and a.started_at is null
        and r.cancel_requested_at is null and r.kind=any(%s) and p.deleted_at is null
        and r.created_at+make_interval(secs=>(r.manifest->'limits'->>'run_timeout_seconds')::int)>now()
        and not exists(select 1 from platform.resource_leases l where l.workspace_id=a.workspace_id
            and l.resource_key=r.manifest->'environment'->'data'->>'resource_key' and l.state<>'released')
        and (select count(*) from platform.attempts active where active.run_id=r.id
            and active.execution_state in ('preparing','running','observing','evaluating'))
            <(r.manifest->'limits'->>'max_concurrency')::int
        order by r.created_at,a.id for update of a skip locked limit 1""",
        (runner["project_id"], runner["workspace_id"], body.supported_job_kinds),
    ).fetchone()
    if not row:
        return None
    generation = ctx.db.execute("select nextval('platform.resource_fence_seq') generation").fetchone()["generation"]
    resource_key = row["manifest"].get("environment", {}).get("data", {}).get("resource_key")
    if resource_key:
        leased = ctx.db.execute(
            """insert into platform.resource_leases(workspace_id,project_id,attempt_id,resource_key,
            fencing_generation,state) values(%s,%s,%s,%s,%s,'leased') on conflict do nothing returning id""",
            (row["workspace_id"], row["project_id"], row["id"], resource_key, generation),
        ).fetchone()
        if not leased:
            return None
    token = secrets.token_urlsafe(40)
    timeout = row["manifest"]["limits"]["case_timeout_seconds"]
    run_deadline = row["run_created_at"] + timedelta(seconds=row["manifest"]["limits"]["run_timeout_seconds"])
    claim = ctx.db.execute(
        """update platform.attempts set execution_state='preparing',runner_id=%s,lease_hash=%s,
        lease_expires_at=least(%s,now()+make_interval(secs=>%s),now()+interval '30 seconds'),
        fencing_generation=%s,
        started_at=now(),deadline=least(%s,now()+make_interval(secs=>%s)) where id=%s
        returning lease_expires_at,fencing_generation,deadline""",
        (runner["id"], token_hash(token), run_deadline, timeout, generation, run_deadline, timeout, row["id"]),
    ).fetchone()
    ctx.db.execute("update platform.runners set last_heartbeat=now() where id=%s", (runner["id"],))
    ctx.db.execute("update platform.runs set status='running' where id=%s", (row["run_id"],))
    cases = row["manifest"].get("cases", [])
    case = next((item for item in cases if item["case"]["id"] == str(row["case_version_id"])), None)
    agent_context = {
        "attempt_id": str(row["id"]),
        "run_id": str(row["run_id"]),
        "namespace": row["namespace"],
        "deadline": str(claim["deadline"]),
        "case_id": str(row["case_version_id"]) if row["case_version_id"] else None,
        "connection": row["manifest"].get("connection", {}).get("data"),
        "script": case["case"]["data"]["script"] if case else [],
    }
    return {
        "protocol_version": "1.0",
        "attempt_id": row["id"],
        "run_id": row["run_id"],
        "kind": row["kind"],
        "lease_token": token,
        "manifest_hash": row["manifest_hash"],
        **claim,
        "namespace": row["namespace"],
        "agent_context": agent_context,
        "evaluator_context": case,
        "execution_manifest": row["manifest"]
        if not case
        else {name: row["manifest"][name] for name in ("environment", "limits", "policy", "timezone")},
    }


def owned_attempt(ctx, attempt_id, token, *, allow_finalized=False, read_only=False):
    row = ctx.db.execute(
        """select a.*,r.manifest,r.kind,r.cancel_requested_at from platform.attempts a
        join platform.runs r on r.id=a.run_id join platform.projects p on p.id=a.project_id
        where a.id=%s and a.runner_id=%s and a.workspace_id=%s and a.project_id=%s
        and p.deleted_at is null for update of a""",
        (attempt_id, ctx.actor["runner_id"], ctx.actor["workspace_id"], ctx.actor["project_id"]),
    ).fetchone()
    if not row or not token or token_hash(token) != row["lease_hash"]:
        raise PlatformError("NOT_FOUND", "Assigned attempt not found.", 404)
    if read_only or (allow_finalized and row["finalized_hash"]):
        return row
    if row["lease_expires_at"] < datetime.now(UTC) or row["execution_state"] in {
        "completed",
        "error",
        "timed_out",
        "cancelled",
        "interrupted",
    }:
        raise PlatformError("LEASE_EXPIRED", "Dispatch authority expired; reconcile instead of resubmitting.", 409)
    return row


def runner_control(ctx, attempt_id, body, renew=False):
    row = owned_attempt(ctx, attempt_id, body.lease_token)
    if renew:
        ctx.db.execute("update platform.runners set last_heartbeat=now() where id=%s", (ctx.actor["runner_id"],))
        row = {
            **row,
            **ctx.db.execute(
                "update platform.attempts set lease_expires_at=least(deadline,now()+interval '30 seconds') "
                "where id=%s returning lease_expires_at",
                (attempt_id,),
            ).fetchone(),
        }
    return {
        "attempt_id": attempt_id,
        "cancel_requested": row["cancel_requested_at"] is not None,
        "lease_expires_at": row["lease_expires_at"],
        "server_time": datetime.now(UTC),
        "deadline": row["deadline"],
    }


def runner_events(ctx, attempt_id, body):
    row = owned_attempt(ctx, attempt_id, body.lease_token)
    accepted, duplicates = [], []
    for event in body.events:
        allowed = {
            "step_prepared": {"request_digest", "operation_id", "action_kind"},
            "step_dispatching": {"operation_id"},
            "step_response_recorded": {"dispatch_state", "response_artifact_id", "side_effect_state"},
            "observation_recorded": {"observation_id"},
            "check_recorded": {"check_version_id"},
            "cleanup_recorded": {"state", "reason_code"},
            "reconciliation_recorded": {"state", "evidence_id"},
            "cancel_acknowledged": set(),
            "progress": {"code"},
            "job_result": {
                "invocation_ok",
                "observer_ok",
                "cleanup_ok",
                "isolation_verified",
                "max_concurrency",
                "control_set_hash",
                "controls_verified",
                "missed_defects",
                "false_alarms",
                "artifact_ids",
                "status",
                "tested_version",
                "classification",
                "reproduced",
                "source_commit",
                "image_digest",
                "dependency_ids",
                "discrepancy_ids",
                "reduction_ids",
                "patch_artifact_id",
                "base_commit",
                "validation_passed",
                "heldout_passed",
                "controls_passed",
                "criteria_unchanged",
            },
        }[event.type]
        if not set(event.payload).issubset(allowed):
            raise PlatformError("FORBIDDEN", "Event includes fields outside its evidence contract.", 403)
        data = dump(event)
        digest = content_hash(data)
        old = ctx.db.execute(
            "select event_id,payload_hash from platform.runner_events "
            "where attempt_id=%s and (event_id=%s or sequence=%s)",
            (attempt_id, event.event_id, event.sequence),
        ).fetchall()
        if old:
            if len(old) != 1 or old[0]["event_id"] != event.event_id or old[0]["payload_hash"] != digest:
                raise PlatformError("IDEMPOTENCY_CONFLICT", "Event ID or sequence conflicts with stored evidence.", 409)
            duplicates.append(event.event_id)
            continue
        if row["cancel_acknowledged_at"] and event.type == "step_dispatching":
            raise PlatformError("INVALID_STATE", "No new action may dispatch after cancellation acknowledgement.", 409)
        if event.type == "cancel_acknowledged":
            ctx.db.execute("update platform.attempts set cancel_acknowledged_at=now() where id=%s", (attempt_id,))
            row["cancel_acknowledged_at"] = datetime.now(UTC)
        if event.type == "step_dispatching":
            if not event.step_id:
                raise PlatformError("INVALID_INPUT", "Dispatch events require a durable step ID.", 422)
            prepared = ctx.db.execute(
                """select payload->'payload' intent from platform.runner_events
                where attempt_id=%s and event_type='step_prepared'
                and payload->>'step_id'=%s and sequence<%s""",
                (attempt_id, event.step_id, event.sequence),
            ).fetchone()
            if not prepared:
                raise PlatformError("INVALID_STATE", "Record the prepared intent before dispatch.", 409)
            operation = event.payload.get("operation_id")
            if not operation or prepared["intent"].get("operation_id") != operation:
                raise PlatformError("INVALID_INPUT", "Dispatch operation must match its prepared intent.", 422)
            reserved = ctx.db.execute(
                "select 1 from platform.budget_reservations where run_id=%s and attempt_id=%s and operation_id=%s",
                (row["run_id"], attempt_id, operation),
            ).fetchone()
            if not reserved:
                raise PlatformError("BUDGET_REQUIRED", "Reserve the operation budget before dispatch.", 409)
            duplicate = ctx.db.execute(
                """select 1 from platform.runner_events where attempt_id=%s and event_type='step_dispatching'
                and payload->'payload'->>'operation_id'=%s""",
                (attempt_id, operation),
            ).fetchone()
            if duplicate:
                raise PlatformError("INVALID_STATE", "A dispatched operation cannot be dispatched again.", 409)
        ctx.db.execute(
            """insert into platform.runner_events(workspace_id,project_id,attempt_id,event_id,sequence,
            event_type,payload,
            payload_hash,occurred_at) values(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                row["workspace_id"],
                row["project_id"],
                attempt_id,
                event.event_id,
                event.sequence,
                event.type,
                Jsonb(data),
                digest,
                event.occurred_at,
            ),
        )
        accepted.append(event.event_id)
    sequences = ctx.db.execute(
        "select sequence from platform.runner_events where attempt_id=%s order by sequence", (attempt_id,)
    ).fetchall()
    contiguous = 0
    for item in sequences:
        if item["sequence"] != contiguous + 1:
            break
        contiguous += 1
    return {"accepted": accepted, "duplicates": duplicates, "highest_contiguous_sequence": contiguous}


def runner_evidence(ctx, attempt_id, body):
    row = owned_attempt(ctx, attempt_id, body.lease_token)
    return validate_evidence(ctx, row, body)


def validate_evidence(ctx, row, body):
    attempt_id = row["id"]
    case = next(
        (item for item in row["manifest"].get("cases", []) if item["case"]["id"] == str(row["case_version_id"])), None
    )
    allowed_checks = {check["id"] for check in case["checks"]} if case else set()
    current_policy = ctx.db.execute(
        "select policy from platform.projects where id=%s", (row["project_id"],)
    ).fetchone()["policy"]
    allowed_fields = set(row["manifest"]["policy"]["upload_fields"]) & set(current_policy["upload_fields"])
    for observation in body.observations:
        if observation.namespace != row["namespace"]:
            raise PlatformError("INVALID_INPUT", "Observation namespace does not match the attempt.", 422)
        if observation.storage_mode == "local_only" and observation.values:
            raise PlatformError("FORBIDDEN", "Local-only values must not be uploaded.", 403)
        if not set(observation.values).issubset(allowed_fields):
            raise PlatformError("FORBIDDEN", "Observation contains fields outside the upload policy.", 403)
        if observation.storage_mode == "redacted_hosted" and content_hash(observation.values) != observation.digest:
            raise PlatformError("INVALID_INPUT", "Hosted observation digest does not match its redacted values.", 422)
        if (
            row["manifest"]["policy"]["mode"] == "local_only" or current_policy["mode"] == "local_only"
        ) and observation.storage_mode != "local_only":
            raise PlatformError("FORBIDDEN", "This attempt permits local evidence digests only.", 403)
        for artifact_id in observation.artifact_ids:
            exists = ctx.db.execute(
                "select 1 from platform.artifacts where id=%s and attempt_id=%s "
                "and status='ready' and expires_at>now()",
                (artifact_id, attempt_id),
            ).fetchone()
            if not exists:
                raise PlatformError("INVALID_INPUT", "Observation artifact is not ready for this attempt.", 422)
        data = dump(observation)
        _store_evidence(ctx.db, row, observation.observation_id, "observation", None, data)
    for result in body.check_results:
        if str(result.check_version_id) not in allowed_checks:
            raise PlatformError("INVALID_INPUT", "Check does not belong to the pinned attempt.", 422)
        pinned_check = next(check for check in case["checks"] if check["id"] == str(result.check_version_id))
        grader_id = pinned_check["data"].get("grader_version_id")
        if grader_id:
            grader = next(value for value in case["graders"] if value["id"] == grader_id)
            if result.evaluator_digest != grader["data"]["evaluator_digest"]:
                raise PlatformError("INCOMPATIBLE_VERSION", "The evaluator digest differs from the pinned grader.", 409)
        for observation_id in result.observation_ids:
            exists = ctx.db.execute(
                "select data from platform.evidence where id=%s and attempt_id=%s and kind='observation'",
                (observation_id, attempt_id),
            ).fetchone()
            if not exists:
                raise PlatformError("INVALID_INPUT", "Check references unavailable observation evidence.", 422)
            if result.verdict != "UNKNOWN" and (
                exists["data"]["availability"] != "available"
                or (
                    pinned_check["data"]["requires_complete_observation"]
                    and (
                        exists["data"]["completeness"] != "complete_for_scope"
                        or not exists["data"]["window_elapsed"]
                        or exists["data"]["window_seconds"] < pinned_check["data"]["window_seconds"]
                    )
                )
            ):
                raise PlatformError(
                    "INVALID_INPUT", "Unavailable or incomplete evidence cannot establish this check.", 422
                )
        if result.verdict != "UNKNOWN" and not result.observation_ids:
            raise PlatformError("INVALID_INPUT", "A definitive check requires independent evidence.", 422)
        if not set(result.measured).issubset(allowed_fields):
            raise PlatformError("FORBIDDEN", "Measured values exceed the upload policy.", 403)
        _store_evidence(ctx.db, row, uuid4(), "check", result.check_version_id, dump(result))
    return {"observations": len(body.observations), "check_results": len(body.check_results)}


def _store_evidence(db, attempt, evidence_id, kind, version_id, data):
    old = db.execute(
        "select content_hash,attempt_id from platform.evidence "
        "where id=%s or (attempt_id=%s and kind=%s and version_id=%s)",
        (evidence_id, attempt["id"], kind, version_id),
    ).fetchone()
    digest = content_hash(data)
    if old:
        if old["attempt_id"] != attempt["id"] or old["content_hash"] != digest:
            raise PlatformError("IDEMPOTENCY_CONFLICT", "Evidence is immutable; append a reconciliation instead.", 409)
        return
    db.execute(
        """insert into platform.evidence(id,workspace_id,project_id,attempt_id,kind,version_id,data,content_hash,hidden)
        values(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            evidence_id,
            attempt["workspace_id"],
            attempt["project_id"],
            attempt["id"],
            kind,
            version_id,
            Jsonb(data),
            digest,
            attempt["hidden"],
        ),
    )


def finalize_attempt(ctx, attempt_id, body):
    row = owned_attempt(ctx, attempt_id, body.lease_token, allow_finalized=True)
    data = dump(body)
    data.pop("lease_token")
    digest = content_hash(data)
    if row["finalized_hash"]:
        if row["finalized_hash"] != digest:
            raise PlatformError("IDEMPOTENCY_CONFLICT", "Attempt was finalized with different results.", 409)
        return {"attempt_id": attempt_id, "gate": project_gate(ctx.db, row["run_id"])}
    events = ctx.db.execute(
        "select sequence from platform.runner_events where attempt_id=%s order by sequence", (attempt_id,)
    ).fetchall()
    if [e["sequence"] for e in events] != list(range(1, body.final_sequence + 1)):
        raise PlatformError("INCOMPLETE_EVIDENCE", "Resolve event sequence gaps before finalization.", 409)
    if row["kind"] == "outcome_suite":
        case = next(item for item in row["manifest"]["cases"] if item["case"]["id"] == str(row["case_version_id"]))
        results = ctx.db.execute(
            "select * from platform.evidence where attempt_id=%s and kind='check'", (attempt_id,)
        ).fetchall()
        required = {check["id"] for check in case["checks"] if check["data"]["required"]}
        actual = {str(result["version_id"]): result["data"]["verdict"] for result in results}
        if body.execution_state == "completed" and not required.issubset(actual):
            raise PlatformError("INCOMPLETE_EVIDENCE", "Required check results are missing.", 409)
        if body.business_verdict == "PASS" and any(actual.get(check_id) != "PASS" for check_id in required):
            raise PlatformError("INVALID_INPUT", "A PASS contradicts missing or non-passing required evidence.", 422)
        if any(actual.get(check_id) == "FAIL" for check_id in required) and body.business_verdict != "FAIL":
            raise PlatformError("INVALID_INPUT", "A required FAIL cannot be relabeled by finalization.", 422)
        for result in results:
            if result["data"]["verdict"] != "PASS":
                ctx.db.execute(
                    """insert into platform.findings(workspace_id,project_id,run_id,attempt_id,
                    check_version_id,verdict,hidden)
                    values(%s,%s,%s,%s,%s,%s,%s) on conflict(attempt_id,check_version_id) do nothing""",
                    (
                        row["workspace_id"],
                        row["project_id"],
                        row["run_id"],
                        attempt_id,
                        result["version_id"],
                        result["data"]["verdict"],
                        row["hidden"],
                    ),
                )
    elif body.execution_state == "completed":
        results = ctx.db.execute(
            "select payload->'payload' result from platform.runner_events "
            "where attempt_id=%s and event_type='job_result'",
            (attempt_id,),
        ).fetchall()
        if len(results) != 1:
            raise PlatformError("INCOMPLETE_EVIDENCE", "Completed jobs require exactly one immutable result.", 409)
        result = results[0]["result"]
        if row["kind"] == "preflight":
            fields = ("invocation_ok", "observer_ok", "cleanup_ok")
            if not all(type(result.get(key)) is bool for key in fields):
                raise PlatformError("INVALID_INPUT", "Preflight must report all three boolean checks.", 422)
            if body.business_verdict == "PASS" and not all(result[key] for key in fields):
                raise PlatformError("INVALID_INPUT", "Preflight PASS contradicts its checks.", 422)
            concurrency = result.get("max_concurrency", 1)
            if type(concurrency) is not int or not 1 <= concurrency <= 3:
                raise PlatformError("INVALID_INPUT", "Preflight concurrency must be an integer from 1 to 3.", 422)
        if row["kind"] == "calibration":
            if type(result.get("controls_verified")) is not bool or any(
                type(result.get(key)) is not int or result[key] < 0 for key in ("missed_defects", "false_alarms")
            ):
                raise PlatformError("INVALID_INPUT", "Calibration requires typed nonnegative control counts.", 422)
    ctx.db.execute(
        """update platform.attempts set execution_state=%s,business_verdict=%s,cleanup_state=%s,
        reconciliation_state=%s,final_sequence=%s,finalized_hash=%s,reason=%s where id=%s""",
        (
            body.execution_state,
            body.business_verdict,
            body.cleanup_state,
            body.reconciliation_state,
            body.final_sequence,
            digest,
            body.reason,
            attempt_id,
        ),
    )
    leases = ctx.db.execute(
        "select id from platform.resource_leases where attempt_id=%s and state='leased'", (attempt_id,)
    ).fetchall()
    if leases:
        cleaned = ctx.db.execute(
            """select 1 from platform.runner_events where attempt_id=%s and event_type='cleanup_recorded'
            and payload->'payload'->>'state'='succeeded'""",
            (attempt_id,),
        ).fetchone()
        released = bool(
            cleaned and body.cleanup_state == "succeeded" and body.reconciliation_state in {"resolved", "not_needed"}
        )
        ctx.db.execute(
            "update platform.resource_leases set state=%s,released_at=case when %s then now() else null end "
            "where attempt_id=%s",
            ("released" if released else "quarantined", released, attempt_id),
        )
        if not released:
            ctx.db.execute("update platform.attempts set reconciliation_state='pending' where id=%s", (attempt_id,))
    gate = project_gate(ctx.db, row["run_id"])
    return {"attempt_id": attempt_id, "gate": gate}
