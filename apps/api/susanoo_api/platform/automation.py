"""Atomic semantic trigger deduplication and runtime-shared automatic-run policy."""

from uuid import UUID

from psycopg.types.json import Jsonb
from susanoo_contracts import AutomaticRunBudget, content_hash
from susanoo_runner.dependencies import check_automatic_run, trigger_dedupe_key

from . import service
from .database import PlatformError


def dispatch_automatic(ctx, project_id, body, *, trigger_kind, trigger_key, version_key, budget=None):
    project = ctx.project(project_id, service.ENGINEERS | service.OWNERS)
    # All automatic sources in a project share the same atomic admission window.
    ctx.db.execute("select id from platform.projects where id=%s for update", (project_id,))
    prior = ctx.db.execute(
        """select * from platform.automatic_decisions where project_id=%s and trigger_kind=%s
        and trigger_key=%s and version_key=%s and suite_version_id=%s""",
        (project_id, trigger_kind, trigger_key, version_key, body.suite_version_id),
    ).fetchone()
    if prior:
        return {**prior, "duplicate": True}
    config = AutomaticRunBudget.model_validate(budget) if budget else AutomaticRunBudget(
        allowed_suites=[str(body.suite_version_id)]
    )
    suite = ctx.version(body.suite_version_id, project, "suite", True)
    cases = len(suite["data"]["case_version_ids"]) * body.repetitions
    recent = ctx.db.execute(
        "select created_at from platform.automatic_decisions where project_id=%s and allowed and run_id is not null "
        "and created_at>now()-make_interval(secs=>%s)", (project_id, config.window_seconds),
    ).fetchall()
    decision = check_automatic_run(
        config, suite_id=str(body.suite_version_id), recent_runs=[row["created_at"] for row in recent],
        cases=cases, agent_invocations=body.limits.max_agent_invocations,
    )
    row = ctx.db.execute(
        """insert into platform.automatic_decisions(workspace_id,project_id,trigger_kind,trigger_key,version_key,
        suite_version_id,allowed,reasons,delayed_until,secrets_allowed) values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        returning *""",
        (project["workspace_id"], project_id, trigger_kind, trigger_key, version_key, body.suite_version_id,
         decision.allowed, Jsonb(decision.reasons), decision.delayed_until, decision.secrets_allowed),
    ).fetchone()
    if decision.allowed:
        try:
            with ctx.db.transaction():
                dedupe = trigger_dedupe_key(
                    str(project_id), trigger_kind + ":" + trigger_key, version_key, str(body.suite_version_id)
                )
                run = service.create_run(ctx, project_id, body, "automatic:" + dedupe)
                ctx.db.execute("update platform.automatic_decisions set run_id=%s where id=%s", (run["id"], row["id"]))
                row["run_id"] = run["id"]
        except PlatformError as error:
            row["allowed"], row["reasons"] = False, [error.code + ": " + error.message]
            ctx.db.execute(
                "update platform.automatic_decisions set allowed=false,reasons=%s where id=%s",
                (Jsonb(row["reasons"]), row["id"]),
            )
    ctx.audit("automatic.admitted" if row["allowed"] else "automatic.blocked", row["id"], project,
              {"trigger_kind": trigger_kind, "reasons": row["reasons"], "cost_estimate": "unknown"})
    return {**row, "duplicate": False}


def record_trigger_block(ctx, project, body, integration_id, payload, reason):
    """Retain stale/fork/branch policy reasons without granting execution or losing delivery provenance."""
    key = content_hash({"revision": payload.get("revision"), "branch": payload.get("branch")})
    ctx.db.execute(
        """insert into platform.automatic_decisions(workspace_id,project_id,trigger_kind,trigger_key,version_key,
        suite_version_id,allowed,reasons,secrets_allowed) values(%s,%s,'webhook',%s,%s,%s,false,%s,false)
        on conflict do nothing""",
        (project["workspace_id"], project["id"], str(integration_id), key,
         UUID(str(body.suite_version_id)), Jsonb([reason])),
    )
