"""Persistent inbound evidence only. No callback or trace can submit customer work."""

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs
from uuid import UUID

from fastapi import Query, Request, Response
from psycopg.types.json import Jsonb
from susanoo_contracts import CollectorCredentialIn, RelayPoll, RelayTaskRegistration, content_hash
from susanoo_connectors.webhook import (
    RelayEvent, WebhookEventMapping, WebhookRejected, WebhookVerification, handle_callback,
)
from susanoo_connectors.media.twilio import (
    CALL_STATUSES, TERMINAL_STATUSES, CallbackVerifier, TwilioCredentials,
)
from susanoo_runner.collector import CollectorReceiver
from susanoo_runner.imports import ImportLimits
from susanoo_sdk.passive import Redactor

from . import security, service
from .database import PlatformError, transaction


def relay_config(row):
    config = row["configuration"].get("relay")
    if row["provider"] not in {"webhook", "media"} or not config:
        raise PlatformError("MISSING_CAPABILITY", "Configure a typed runtime callback relay first.", 409)
    return config


class PersistentTaskRegistry:
    def __init__(self, db, integration, policy):
        self.db, self.integration, self.policy = db, integration, policy
        self.task = None

    async def is_registered(self, task_id):
        self.task = self.db.execute(
            "select * from platform.relay_tasks where integration_id=%s and task_id=%s "
            "and expires_at>now() for update", (self.integration["id"], task_id),
        ).fetchone()
        return self.task is not None

    async def record(self, event):
        task = self.task
        if not task or task["task_id"] != event.task_id:
            return "unknown_task"
        # Store a digest of the normalized source event, not its secret-bearing headers/raw body.
        original = event.model_dump(mode="json", exclude={"received_at"})
        digest = content_hash(original)
        prior = self.db.execute(
            "select payload_hash from platform.relay_events where task_id=%s and event_id=%s",
            (task["id"], event.event_id),
        ).fetchone()
        if prior:
            if prior["payload_hash"] != digest:
                raise PlatformError("IDEMPOTENCY_CONFLICT", "Callback event ID conflicts with prior content.", 409)
            return "duplicate"
        disposition = (
            "after_terminal" if task["terminal"] else "out_of_order"
            if event.sequence is not None and event.sequence <= task["last_sequence"] else "accepted"
        )
        result = {
            key: value for key, value in (event.result or {}).items() if key in self.policy["upload_fields"]
        }
        result, _ = Redactor().value(result)
        stored = event.model_copy(update={"result": result or None}).model_dump(mode="json")
        self.db.execute(
            """insert into platform.relay_events
            (workspace_id,project_id,task_id,event_id,payload_hash,payload,disposition)
            values(%s,%s,%s,%s,%s,%s,%s)""",
            (task["workspace_id"], task["project_id"], task["id"], event.event_id, digest, Jsonb(stored), disposition),
        )
        if disposition == "accepted":
            self.db.execute(
                "update platform.relay_tasks set last_sequence=coalesce(%s,last_sequence),terminal=%s where id=%s",
                (event.sequence, event.terminal, task["id"]),
            )
        return disposition


class PersistentRelayQueue:
    def __init__(self, db, task_id=None):
        self.db, self.task_id = db, task_id

    async def put(self, event):
        # Registry receipt and queue insertion are the same atomic row, in the callback transaction.
        return None

    async def poll(self, task_id, after, limit=100):
        rows = self.db.execute(
            "select cursor,payload from platform.relay_events where task_id=%s and cursor>%s "
            "and disposition='accepted' order by cursor limit %s", (self.task_id, after, min(limit, 100)),
        ).fetchall()
        return [RelayEvent.model_validate(row["payload"]) for row in rows], rows[-1]["cursor"] if rows else after


def minimize_trace(value, policy):
    value = {**value, "provenance": {**value["provenance"], "source_location": None}}
    allowed = policy["upload_fields"] if policy["mode"] != "local_only" else []
    for name in ("turns", "outcomes"):
        if name not in allowed:
            value[name] = []
    if "conversation_id" not in allowed and not re.fullmatch(r"sha256:[a-f0-9]{64}", value["conversation_id"]):
        value["conversation_id"] = "sha256:" + content_hash(value["conversation_id"])
    return value


def assigned_integration(ctx, attempt, integration_id):
    row = ctx.db.execute(
        "select * from platform.integrations where id=%s and project_id=%s and revoked_at is null",
        (integration_id, attempt["project_id"]),
    ).fetchone()
    connections = list(attempt["manifest"].get("versions", []))
    if attempt["manifest"].get("connection"):
        connections.append(attempt["manifest"]["connection"])
    if not row or not any(
        value.get("kind") == "connection"
        and value["data"].get("mapping", {}).get("relay_integration_id") == str(integration_id)
        for value in connections
    ):
        raise PlatformError("NOT_FOUND", "Relay is not pinned to this assignment.", 404)
    relay_config(row)
    return row


def mount_relay_routes(app):
    from .router import Ctx, RunnerCtx

    @app.post("/runner/attempts/{attempt_id}/tasks", status_code=201)
    def register_task(attempt_id: UUID, body: RelayTaskRegistration, ctx: RunnerCtx):
        attempt = service.owned_attempt(ctx, attempt_id, body.lease_token)
        integration = assigned_integration(ctx, attempt, body.integration_id)
        if attempt["manifest"]["policy"]["mode"] == "local_only":
            raise PlatformError("FORBIDDEN", "Local-only data cannot use a hosted callback relay.", 403)
        operation = ctx.db.execute(
            "select 1 from platform.runner_events where attempt_id=%s and event_type='step_dispatching' "
            "and payload->'payload'->>'operation_id'=%s", (attempt_id, body.operation_id),
        ).fetchone()
        if not operation:
            raise PlatformError("INVALID_STATE", "Register only a journaled dispatched operation.", 409)
        ctx.db.execute("select id from platform.integrations where id=%s for update", (integration["id"],))
        existing = ctx.db.execute(
            "select * from platform.relay_tasks where integration_id=%s and task_id=%s",
            (integration["id"], body.task_id),
        ).fetchone()
        if existing:
            if (existing["attempt_id"], existing["invocation_id"], existing["operation_id"]) != (
                attempt_id, body.invocation_id, body.operation_id
            ):
                raise PlatformError("IDEMPOTENCY_CONFLICT", "Provider task is already bound to another operation.", 409)
            return {"id": existing["id"], "duplicate": True}
        row = ctx.db.execute(
            """insert into platform.relay_tasks(workspace_id,project_id,attempt_id,integration_id,task_id,
            invocation_id,operation_id,expires_at) values(%s,%s,%s,%s,%s,%s,%s,%s) returning id""",
            (attempt["workspace_id"], attempt["project_id"], attempt_id, integration["id"], body.task_id,
             body.invocation_id, body.operation_id, datetime.now(UTC) + timedelta(days=1)),
        ).fetchone()
        return {**row, "duplicate": False}

    @app.post("/runner/attempts/{attempt_id}/tasks/{task_id}/poll")
    async def poll_task(attempt_id: UUID, task_id: UUID, body: RelayPoll, ctx: RunnerCtx):
        attempt = service.owned_attempt(ctx, attempt_id, body.lease_token, read_only=True)
        task = ctx.db.execute(
            "select t.* from platform.relay_tasks t join platform.integrations i on i.id=t.integration_id "
            "where t.id=%s and t.attempt_id=%s and i.revoked_at is null and t.expires_at>now()",
            (task_id, attempt["id"]),
        ).fetchone()
        if not task:
            raise PlatformError("NOT_FOUND", "Task is not assigned to this attempt.", 404)
        queue = PersistentRelayQueue(ctx.db, task_id)
        events, cursor = await queue.poll(task["task_id"], body.after)
        policy = ctx.db.execute("select policy from platform.projects where id=%s",
                                (attempt["project_id"],)).fetchone()["policy"]
        if policy["mode"] == "local_only":
            raise PlatformError("FORBIDDEN", "Hosted relay access is disabled by the current policy.", 403)
        events = [event.model_copy(update={"result": {
            key: value for key, value in (event.result or {}).items() if key in policy["upload_fields"]
        } or None}) for event in events]
        return {"events": events, "cursor": cursor, "dispatch_authority": False}

    @app.post("/callbacks/runtime/{integration_id}")
    async def runtime_callback(integration_id: UUID, request: Request):
        raw = await request.body()
        if len(raw) > 65536:
            raise PlatformError("PAYLOAD_TOO_LARGE", "Callback exceeds 64 KiB.", 413)
        with transaction() as db:
            row = db.execute(
                "select i.*,p.policy from platform.integrations i join platform.projects p on p.id=i.project_id "
                "where i.id=%s and i.revoked_at is null and p.deleted_at is null for update of i",
                (integration_id,),
            ).fetchone()
            if not row:
                raise PlatformError("NOT_FOUND", "Callback route not found.", 404)
            config = relay_config(row)
            if row["policy"]["mode"] == "local_only":
                raise PlatformError("FORBIDDEN", "Local-only data cannot use this hosted relay.", 403)
            registry = PersistentTaskRegistry(db, row, row["policy"])
            queue = PersistentRelayQueue(db)
            try:
                if row["provider"] == "webhook":
                    mapping = WebhookEventMapping.model_validate({
                        key: value for key, value in config.items() if key.endswith("_path") or key == "terminal_states"
                    })
                    outcome = await handle_callback(
                        raw, request.headers,
                        verification=WebhookVerification(
                            secret_ref=row["secret_ref"], previous_secret_ref=config.get("previous_secret_ref")
                        ), mapping=mapping, registry=registry, queue=queue,
                    )
                    return outcome.model_dump(mode="json")
                is_json = request.headers.get("Content-Type", "").split(";")[0] == "application/json"
                if is_json:
                    if "bodySHA256" not in parse_qs(request.url.query):
                        raise PlatformError("FORBIDDEN", "JSON callbacks must bind their body digest.", 403)
                    values = json.loads(raw)
                    if not isinstance(values, dict):
                        raise ValueError("object required")
                    params = None
                else:
                    params = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
                    if any(len(params.get(key, [])) != 1 for key in ("CallSid", "CallStatus")):
                        raise ValueError("one task and status required")
                    values = {key: items[-1] for key, items in params.items()}
                verifier = CallbackVerifier(config["public_base_url"], TwilioCredentials(
                    account_sid_ref="SUSANOO_TWILIO_ACCOUNT_SID", auth_token_ref=row["secret_ref"]
                ))
                query = "?" + request.url.query if request.url.query else ""
                check = verifier.verify(
                    f"/api/v1/callbacks/runtime/{integration_id}{query}", request.headers.get("X-Twilio-Signature"),
                    params=params, body=raw, now=datetime.now(UTC),
                )
                if not check.valid:
                    raise PlatformError("FORBIDDEN", "Twilio callback signature is invalid.", 403)
                status = values.get("CallStatus")
                if status not in CALL_STATUSES or not values.get("CallSid"):
                    raise PlatformError("INVALID_INPUT", "Twilio callback lacks a valid call identity/status.", 422)
                if not await registry.is_registered(values["CallSid"]):
                    raise PlatformError("NOT_FOUND", "Unknown provider task.", 404)
                sequence = values.get("SequenceNumber")
                event = RelayEvent(
                    task_id=values["CallSid"], event_id="twilio-" + hashlib.sha256(raw).hexdigest(),
                    sequence=int(sequence) if sequence is not None else None, status=status,
                    terminal=status in TERMINAL_STATUSES, result={"task_outcome": "not_established"},
                    received_at=datetime.now(UTC),
                )
                disposition = await registry.record(event)
                return {"disposition": disposition, "task_outcome": "not_established"}
            except WebhookRejected as error:
                raise PlatformError(
                    "INVALID_INPUT", "Callback verification or normalization failed.", error.status_code
                ) from None
            except (ValueError, UnicodeError):
                raise PlatformError("INVALID_INPUT", "Callback is malformed.", 422) from None

    @app.post("/projects/{project_id}/collector-credentials", status_code=201)
    def collector_credential(project_id: UUID, body: CollectorCredentialIn, ctx: Ctx):
        project = ctx.project(project_id, service.OWNERS)
        if project["policy"]["mode"] == "local_only":
            raise PlatformError("FORBIDDEN", "Local-only projects cannot enable hosted collection.", 403)
        result = security.issue_token(
            ctx.db, "collector", user_id=ctx.actor["user_id"], project=project, hours=body.hours
        )
        ctx.audit("collector.enabled", UUID(result["credential_id"]), project)
        return result

    @app.get("/projects/{project_id}/collector-credentials")
    def collector_credentials(project_id: UUID, ctx: Ctx):
        ctx.project(project_id, service.OWNERS)
        return {"items": ctx.db.execute(
            "select id,expires_at,revoked_at from platform.credentials where project_id=%s and kind='collector' "
            "order by expires_at desc limit 100", (project_id,),
        ).fetchall()}

    @app.get("/projects/{project_id}/relay-tasks")
    def relay_tasks(project_id: UUID, ctx: Ctx):
        project = ctx.project(project_id, service.OWNERS)
        return {"items": ctx.db.execute(
            "select t.id,t.integration_id,t.attempt_id,t.terminal,t.last_sequence,t.expires_at "
            "from platform.relay_tasks t join platform.attempts a on a.id=t.attempt_id "
            "where t.project_id=%s and (not a.hidden or %s) order by t.created_at desc limit 100",
            (project_id, project["hidden_access"]),
        ).fetchall()}

    @app.post("/v1/traces")
    async def traces(request: Request):
        raw = await request.body()
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "bearer":
            raise PlatformError("UNAUTHENTICATED", "Use a project collector credential.", 401)
        with transaction() as db:
            actor = security.credential(db, token, {"collector"})
            project = db.execute(
                "select * from platform.projects where id=%s and deleted_at is null for update", (actor["project_id"],)
            ).fetchone()
            if project["policy"]["mode"] == "local_only":
                raise PlatformError("FORBIDDEN", "Hosted collection was disabled by the current data policy.", 403)
            current = db.execute(
                "select count(*) n from platform.passive_records where project_id=%s", (project["id"],)
            ).fetchone()["n"]
            if current >= 100000:
                raise PlatformError("CAPACITY_EXCEEDED", "Passive evidence retention capacity reached.", 429)

            def sink(project_id, records):
                for record in records:
                    value = minimize_trace(record.model_dump(mode="json"), project["policy"])
                    db.execute(
                        "insert into platform.passive_records(workspace_id,project_id,record_id,payload) "
                        "values(%s,%s,%s,%s) on conflict do nothing",
                        (project["workspace_id"], project["id"], record.record_id, Jsonb(value)),
                    )

            receiver = CollectorReceiver(
                tokens={security.token_hash(token): str(project["id"])}, sink=sink,
                limits=ImportLimits(max_records=min(1000, 100000-current), max_text_chars=4096),
            )
            response = receiver.handle("POST", "/v1/traces", request.headers, raw)
            return Response(response.body, status_code=response.status, media_type=response.content_type)

    @app.get("/projects/{project_id}/passive-records")
    def passive_records(project_id: UUID, ctx: Ctx, after: str = "", limit: int = Query(50, ge=1, le=100)):
        project = ctx.project(project_id, service.ENGINEERS | service.REVIEWERS | service.OWNERS)
        records = ctx.db.execute(
            "select record_id,payload,created_at from platform.passive_records where project_id=%s and record_id>%s "
            "order by record_id limit %s", (project_id, after, limit),
        ).fetchall()
        for record in records:
            record["payload"] = minimize_trace(record["payload"], project["policy"])
        return {"items": records, "next_cursor": records[-1]["record_id"] if records else after,
                "evidence_only": True, "replay_authorized": False}
