"""Typed runtime reports, scoped immutable persistence and append-only classification.

Runtime imports here are DTOs only; the control plane never invokes agents, builds or fixtures.
"""

from functools import reduce
from operator import or_
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import Query
from psycopg.types.json import Jsonb
from pydantic import Field, TypeAdapter, create_model, model_validator
from susanoo_contracts import (
    Digest, LeaseRequest, Name, ReportClassificationIn, WireModel, canonical_json, content_hash,
)
from susanoo_connectors.media.metrics import AudioMetricsReport
from susanoo_evaluator.semantic import SemanticCalibration
from susanoo_runner.agent_repair import RepairValidation as AgentRepairValidation
from susanoo_runner.capacity import CapacityReport
from susanoo_runner.dependencies import Conclusion, ValidityMark
from susanoo_runner.fidelity import ComparisonResult
from susanoo_runner.impact import PairOutcome, TwoByTwoCell
from susanoo_runner.imports import ImportBatch
from susanoo_runner.incidents import DraftScenario
from susanoo_runner.probing import Comparison as ProbeComparison, ProbePlan, ProbeRun
from susanoo_runner.reduce import ReductionResult
from susanoo_runner.repair import RepairValidation as SimulatorRepairValidation
from susanoo_runner.collision import CollisionReport
from susanoo_runner.local import RunReport
from susanoo_runner.ci import RunManifest
from susanoo_runner.suite import LocalCase
from susanoo_sdk.passive import Redactor

from . import service
from .database import PlatformError


class TwoByTwoReport(WireModel):
    cells: list[TwoByTwoCell] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def all_cells(self):
        if {(cell.agent, cell.environment) for cell in self.cells} != {
            (agent, environment) for agent in ("old", "new") for environment in ("old", "new")
        }:
            raise ValueError("Report every distinct agent/environment cell")
        lengths = {len(cell.results) for cell in self.cells}
        if len(lengths) != 1 or 0 in lengths:
            raise ValueError("Every cell needs the same nonzero repetition count")
        return self


class RuntimeExport(WireModel):
    schema_version: Literal["1.0"]
    kind: Literal["susanoo.run_export"]
    audience: Literal["operator", "engineer"]
    snapshot_digest: Digest
    generated_at: str
    lineage: dict[str, Any]
    machine: dict[str, Any]
    acceptance: dict[str, Any]
    compatibility: dict[str, Any]
    exclusions: list[Any]
    counts_definitions: dict[str, Any]
    snapshot: dict[str, Any]

    @model_validator(mode="after")
    def runtime_snapshot(self):
        if not isinstance(self.snapshot.get("report"), dict) or not isinstance(self.snapshot.get("manifest"), dict):
            raise ValueError("Runtime snapshot requires typed report and manifest objects")
        if content_hash(self.snapshot) != self.snapshot_digest:
            raise ValueError("Runtime snapshot digest mismatch")
        RunReport.model_validate(self.snapshot["report"])
        RunManifest.model_validate(self.snapshot["manifest"])
        if self.audience == "engineer":
            for attempt in self.snapshot["report"]["attempts"]:
                if attempt["visibility"] == "hidden" and (
                    attempt["title"] != "Held-out case" or any(attempt.get(key) for key in (
                        "outcomes", "turns", "injected_faults", "reconciliations"
                    ))
                ):
                    raise ValueError("Engineer exports must withhold nested held-out details")
        return self


def ingredient_case(value):
    needed = {
        "case_id", "title", "visibility", "required", "requirement_ids", "script", "observations", "checks",
        "fixture", "fixture_seed_digest", "fixture_compatibility_key"
    }
    if not needed.issubset(value):
        raise ValueError("Reproduction case is incomplete")
    return LocalCase.model_validate({
        "id": value["case_id"], "title": value["title"], "visibility": value["visibility"],
        "required": value["required"], "requirement_ids": value["requirement_ids"], "script": value["script"],
        "observations": value["observations"], "checks": value["checks"],
        "fixture": {**value["fixture"], "kind": "fixture", "digest": value["fixture_seed_digest"],
                    "compatibility_key": value["fixture_compatibility_key"]},
    })


class ReproductionIngredient(WireModel):
    schema_version: Literal["1.0"]
    kind: Literal["susanoo.reproduction_ingredient"]
    audience: Literal["operator", "engineer"]
    snapshot_digest: Digest
    suite: dict[str, Any]
    agent: dict[str, Any]
    adapters: dict[str, Any]
    evaluator_digest: Digest
    profile: dict[str, Any]
    cases: list[dict[str, Any]] = Field(max_length=500)
    command: str

    @model_validator(mode="after")
    def authorized_cases(self):
        visible_requirements = set()
        for value in self.cases:
            if value.get("content_withheld"):
                if set(value) - {
                    "case_id", "visibility", "required", "case_digest", "fixture_digest",
                    "fixture_compatibility_key", "check_digests", "content_withheld", "command"
                }:
                    raise ValueError("Withheld case includes unauthorized content")
            else:
                ingredient_case(value)
                visible_requirements.update(value["requirement_ids"])
            if (self.audience == "engineer" and value.get("visibility") == "hidden"
                    and not value.get("content_withheld")):
                raise ValueError("Held-out case must be withheld from engineer ingredients")
        if self.audience == "engineer" and any(
            not isinstance(requirement, dict) or requirement.get("id") not in visible_requirements
            for requirement in self.suite.get("requirements", [])
        ):
            raise ValueError("Unreferenced/held-out requirements must be withheld")
        return self


REPORTS = {
    "comparison": (ComparisonResult, {"action_comparison"}),
    "task_impact": (PairOutcome, {"fresh_agent_pair"}),
    "two_by_two": (TwoByTwoReport, {"fresh_agent_pair", "repair_retest"}),
    "reduction": (ReductionResult, {"reduce_reproduction"}),
    "simulator_repair": (SimulatorRepairValidation, {"repair_retest"}),
    "agent_repair": (AgentRepairValidation, {"repair_retest"}),
    "probe_plan": (ProbePlan, {"probe"}),
    "probe_run": (ProbeRun, {"probe"}),
    "probe_comparison": (ProbeComparison, {"probe"}),
    "collision": (CollisionReport, {"collision"}),
    "calibration": (SemanticCalibration, {"calibration"}),
    "media": (AudioMetricsReport, {"voice"}),
    "import": (ImportBatch, {"trace_import"}),
    "incident": (DraftScenario, {"incident_draft"}),
    "conclusion": (Conclusion, set()),
    "validity": (ValidityMark, set()),
    "capacity": (CapacityReport, set()),
    "run_export": (RuntimeExport, {"outcome_suite"}),
    "reproduction": (ReproductionIngredient, {"outcome_suite"}),
}


class ReportBase(LeaseRequest):
    report_key: Name = "primary"
    audience: Literal["project", "reviewers"] = "reviewers"

    @model_validator(mode="after")
    def modality_contract(self):
        if self.report_type in {"run_export", "reproduction"}:
            if self.payload.audience == "operator" and self.audience != "reviewers":
                raise ValueError("Operator runtime exports require the reviewer audience")
        if self.report_type == "media" and self.payload.modality == "transcript_only":
            from susanoo_sdk import Unavailable

            if any(not isinstance(getattr(self.payload, field), Unavailable) for field in (
                "transcript_quality", "response_latency", "interruptions", "speech_quality"
            )):
                raise ValueError("Transcript-only evidence cannot claim audio measurements")
        return self


_variants = tuple(create_model(
    "".join(part.title() for part in name.split("_")) + "ReportSubmission",
    __base__=ReportBase, report_type=(Literal[name], name), payload=(model, ...),
) for name, (model, _) in REPORTS.items())
ReportSubmission = Annotated[reduce(or_, _variants), Field(discriminator="report_type")]
REPORT_ADAPTER = TypeAdapter(ReportSubmission)


def visible_reports(ctx, run, project, *, audience="reviewers"):
    hidden = bool(project["hidden_access"] and audience == "reviewers")
    reviewer = bool(set(project["roles"]) & (service.REVIEWERS | service.OWNERS)) and audience == "reviewers"
    reports = ctx.db.execute(
        "select * from platform.advanced_reports where run_id=%s and (not hidden or %s) "
        "and (audience='project' or %s) and policy_hash=%s order by created_at,id",
        (run["id"], hidden, reviewer, content_hash(project["policy"])),
    ).fetchall()
    for report in reports:
        report["reviews"] = ctx.db.execute(
            "select actor_id,candidate_id,classification,rationale,evidence_ids,created_at "
            "from platform.report_reviews where report_id=%s order by created_at,id", (report["id"],),
        ).fetchall()
    return reports


def authorized_report(ctx, report_id, *, reviewer=False):
    row = ctx.db.execute("select * from platform.advanced_reports where id=%s", (report_id,)).fetchone()
    if not row:
        raise PlatformError("NOT_FOUND", "Report not found.", 404)
    project = ctx.project(row["project_id"], service.REVIEWERS if reviewer else None)
    if row["policy_hash"] != content_hash(project["policy"]):
        raise PlatformError("POLICY_CHANGED", "Report access requires evidence under the current policy.", 409)
    if row["hidden"] and not project["hidden_access"] or (
        row["audience"] == "reviewers" and not set(project["roles"]) & (service.REVIEWERS | service.OWNERS)
    ):
        raise PlatformError("NOT_FOUND", "Report not available to this role.", 404)
    return row, project


def mount_report_routes(app):
    from .router import Ctx, RunnerCtx

    @app.post("/runner/attempts/{attempt_id}/reports", status_code=201)
    def submit_report(attempt_id: UUID, body: ReportSubmission, ctx: RunnerCtx):
        attempt = service.owned_attempt(ctx, attempt_id, body.lease_token)
        kinds = REPORTS[body.report_type][1]
        if kinds and attempt["kind"] not in kinds:
            raise PlatformError("INVALID_INPUT", "Report type does not match the assigned job kind.", 422)
        if body.report_type == "run_export" and body.payload.snapshot["report"]["run_id"] != str(attempt["run_id"]):
            raise PlatformError("INVALID_INPUT", "Runtime export lineage differs from the assigned run.", 422)
        project = ctx.db.execute("select * from platform.projects where id=%s", (attempt["project_id"],)).fetchone()
        for policy in (attempt["manifest"]["policy"], project["policy"]):
            if policy["mode"] == "local_only" or (
                policy["mode"] != "synthetic" and not policy.get("report_upload_allowed", False)
            ):
                raise PlatformError("FORBIDDEN", "The pinned and current policies must permit report upload.", 403)
        payload = service.json_safe(body.payload.model_dump(mode="python"))
        # Never silently alter a typed report or a digest. Non-synthetic producers must minimize locally.
        if project["policy"]["mode"] != "synthetic" and Redactor().value(payload)[1]:
            raise PlatformError("FORBIDDEN", "Minimize sensitive report values locally before upload.", 403)
        try:
            encoded = canonical_json(payload).encode()
        except (ValueError, TypeError):
            raise PlatformError("INVALID_INPUT", "Report is not bounded canonical JSON.", 422) from None
        if len(encoded) > 1048576:
            raise PlatformError("PAYLOAD_TOO_LARGE", "Report exceeds 1 MiB.", 413)
        ctx.db.execute("select id from platform.runs where id=%s for update", (attempt["run_id"],))
        digest = content_hash(payload)
        existing = ctx.db.execute(
            "select * from platform.advanced_reports where attempt_id=%s and report_type=%s and report_key=%s",
            (attempt_id, body.report_type, body.report_key),
        ).fetchone()
        audience = "reviewers" if attempt["hidden"] else body.audience
        if existing:
            if existing["policy_hash"] != content_hash(project["policy"]):
                raise PlatformError("POLICY_CHANGED", "Use a new approved report key under the current policy.", 409)
            if existing["content_hash"] != digest or existing["audience"] != audience:
                raise PlatformError("IDEMPOTENCY_CONFLICT", "An immutable report key has different content.", 409)
            return {"id": existing["id"], "content_hash": digest, "duplicate": True}
        used = ctx.db.execute(
            "select coalesce(sum(octet_length(payload::text)),0) n from platform.advanced_reports where run_id=%s",
            (attempt["run_id"],),
        ).fetchone()["n"]
        if used + len(encoded) > attempt["manifest"]["limits"]["max_storage_bytes"]:
            raise PlatformError("BUDGET_EXCEEDED", "Run report storage budget exhausted.", 409)
        row = ctx.db.execute(
            """insert into platform.advanced_reports(workspace_id,project_id,run_id,attempt_id,report_type,
            report_key,content_hash,payload,hidden,audience,policy_hash)
            values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id""",
            (attempt["workspace_id"], attempt["project_id"], attempt["run_id"], attempt_id, body.report_type,
             body.report_key, digest, Jsonb(payload), attempt["hidden"], audience, content_hash(project["policy"])),
        ).fetchone()
        return {**row, "content_hash": digest, "duplicate": False}

    @app.get("/runs/{run_id}/reports")
    def reports(run_id: UUID, ctx: Ctx):
        run, project = service.get_run(ctx, run_id)
        return {"items": visible_reports(ctx, run, project), "hidden_details_omitted": not project["hidden_access"]}

    @app.get("/reports/{report_id}")
    def report(report_id: UUID, ctx: Ctx):
        row, project = authorized_report(ctx, report_id)
        run, _ = service.get_run(ctx, row["run_id"])
        return next(item for item in visible_reports(ctx, run, project) if item["id"] == row["id"])

    @app.post("/reports/{report_id}/classifications", status_code=201)
    def classify(report_id: UUID, body: ReportClassificationIn, ctx: Ctx):
        row, project = authorized_report(ctx, report_id, reviewer=True)
        if row["report_type"] != "comparison" or body.candidate_id not in {
            candidate["id"] for candidate in row["payload"]["candidates"]
        }:
            raise PlatformError("NOT_FOUND", "Discrepancy candidate is not in this report.", 404)
        for evidence_id in body.evidence_ids:
            if evidence_id == report_id:
                continue
            evidence = ctx.db.execute(
                "select hidden from platform.evidence where id=%s and project_id=%s", (evidence_id, project["id"]),
            ).fetchone()
            if not evidence or evidence["hidden"] and not project["hidden_access"]:
                raise PlatformError("NOT_FOUND", "Classification evidence is not available.", 404)
        result = ctx.db.execute(
            """insert into platform.report_reviews(workspace_id,project_id,report_id,actor_id,candidate_id,
            classification,rationale,evidence_ids) values(%s,%s,%s,%s,%s,%s,%s,%s) returning *""",
            (project["workspace_id"], project["id"], report_id, ctx.actor["user_id"], body.candidate_id,
             body.classification, body.rationale, body.evidence_ids),
        ).fetchone()
        ctx.audit("discrepancy.classified", report_id, project, {"classification": body.classification})
        return result

    @app.get("/projects/{project_id}/automatic-decisions")
    def automatic_decisions(project_id: UUID, ctx: Ctx, limit: int = Query(100, ge=1, le=200)):
        ctx.project(project_id, service.ENGINEERS | service.REVIEWERS | service.OWNERS)
        return {"items": ctx.db.execute(
            "select * from platform.automatic_decisions where project_id=%s order by created_at desc limit %s",
            (project_id, limit),
        ).fetchall()}
