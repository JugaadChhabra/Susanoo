"""JSON, JUnit XML and reproduction-bundle ingredients from one authorized snapshot [Spec §16 › Required exports; T17].

Every export is rendered from a single ``ExportSnapshot``: the report view for the chosen audience plus the
audience-filtered manifest, pinned by ``snapshot_digest``. All formats carry that digest, so a reader can
check they describe the same results. The engineer view never carries held-out case content, in nested
payloads included: hidden cases keep only their id, verdicts and digests. PDF rendering is the platform's
(Aarav); it consumes the JSON export.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from susanoo_contracts import content_hash

from .ci import RunDir, RunManifest, evaluate_acceptance, machine_exit_code, write_json
from .local import RunReport
from .suite import LocalSuite

EXPORT_SCHEMA_VERSION = "1.0"
Audience = Literal["operator", "engineer"]
Format = Literal["json", "junit", "bundle"]


@dataclass(frozen=True)
class ExportSnapshot:
    audience: Audience
    report: RunReport
    manifest: RunManifest
    suite: LocalSuite
    data: dict[str, Any]
    digest: str

    @property
    def handle(self) -> str:
        return self.manifest.handle


def _manifest_view(manifest: RunManifest, audience: Audience) -> dict[str, Any]:
    data = manifest.model_dump(mode="json")
    if audience == "engineer":
        for case in data["cases"]:
            if case["visibility"] == "hidden":
                case["title"] = "Held-out case"
    return data


def build_snapshot(run_dir: RunDir, audience: Audience = "engineer") -> ExportSnapshot:
    report, manifest, suite = run_dir.report(), run_dir.manifest(), run_dir.suite()
    view = report.view(audience)
    if audience == "engineer":
        for attempt in view["attempts"]:
            if attempt["visibility"] == "hidden":
                attempt["injected_faults"] = []  # fault plans describe the held-out case; RunReport.view keeps them
    data = {"report": view, "manifest": _manifest_view(manifest, audience)}
    return ExportSnapshot(audience, report, manifest, suite, data, content_hash(data))


def export_json(snapshot: ExportSnapshot) -> dict[str, Any]:
    """Complete authorized structured results, lineage, compatibility and exclusion reasons."""
    report, manifest = snapshot.report, snapshot.manifest
    return {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "kind": "susanoo.run_export",
        "audience": snapshot.audience,
        "snapshot_digest": snapshot.digest,
        "generated_at": datetime.now(UTC).isoformat(),
        "lineage": {"handle": manifest.handle, "run_id": report.run_id, "reproduces": manifest.reproduces},
        "machine": {"gate": report.gate, "exit_code": machine_exit_code(report), "blockers": report.blockers},
        "acceptance": evaluate_acceptance(report, None).model_dump(mode="json"),
        "compatibility": {
            "suite_id": manifest.suite_id,
            "suite_hash": manifest.suite_hash,
            "evaluator_digest": manifest.evaluator_digest,
            "cases": [
                {
                    "case_id": c["case_id"],
                    "case_digest": c["case_digest"],
                    "inputs_digest": c["inputs_digest"],
                    "fixture_digest": c["fixture_digest"],
                    "fixture_compatibility_key": c["fixture_compatibility_key"],
                    "check_digests": c["check_digests"],
                }
                for c in snapshot.data["manifest"]["cases"]
            ],
        },
        "exclusions": [],  # local runs exclude nothing; counts.excluded stays in the denominator display
        "counts_definitions": {name: field.description for name, field in type(report.counts).model_fields.items()},
        "snapshot": snapshot.data,
    }


def export_junit(snapshot: ExportSnapshot) -> str:
    """Failed required checks -> <failure>; execution/cleanup/side-effect problems -> <error>;
    unresolved (UNKNOWN) required checks -> <error type="susanoo.unresolved">, a documented non-green
    outcome (never <skipped>, which some CI systems treat as green). Optional failures are reported in
    system-out and do not fail the suite. The wrapper exit code still enforces the gate."""
    report = snapshot.report
    view_attempts = snapshot.data["report"]["attempts"]
    suite = ET.Element("testsuite", name=report.suite_id, timestamp=report.started_at.isoformat())
    props = ET.SubElement(suite, "properties")
    for name, value in (
        ("susanoo.snapshot_digest", snapshot.digest),
        ("susanoo.audience", snapshot.audience),
        ("susanoo.gate", report.gate),
        ("susanoo.exit_code", str(machine_exit_code(report))),
        ("susanoo.run_id", report.run_id),
        ("susanoo.agent", report.agent),
        ("susanoo.evaluator_digest", report.evaluator_digest),
        ("susanoo.suite_hash", report.suite_hash),
    ):
        ET.SubElement(props, "property", name=name, value=value)
    failures = errors = 0
    total_time = 0.0
    for attempt in view_attempts:
        hidden = attempt["visibility"] == "hidden" and snapshot.audience == "engineer"
        total_time += attempt["duration_seconds"]
        case = ET.SubElement(
            suite,
            "testcase",
            classname=report.suite_id,
            name=f"{attempt['case_id']} {attempt['title']}",
            time=f"{attempt['duration_seconds']:.3f}",
        )
        failed_required = [o["check_id"] for o in attempt["outcomes"] if o["required"] and o["verdict"] == "FAIL"]
        unknown_required = [o["check_id"] for o in attempt["outcomes"] if o["required"] and o["verdict"] == "UNKNOWN"]
        notes = [
            f"business_verdict={attempt['business_verdict']} execution_state={attempt['execution_state']} "
            f"cleanup_state={attempt['cleanup_state']} reconciliation_state={attempt['reconciliation_state']}"
        ]
        if attempt["required"] and attempt["business_verdict"] == "FAIL":
            failures += 1
            detail = "held-out case; check details withheld" if hidden else ", ".join(failed_required)
            ET.SubElement(
                case, "failure", type="susanoo.required_failure", message=f"Required check(s) failed: {detail}"
            ).text = None if hidden else _outcome_text(attempt, "FAIL")
        problems = []
        if attempt["execution_state"] != "completed":
            problems.append((f"susanoo.execution_{attempt['execution_state']}", attempt["reason"] or "execution error"))
        if attempt["cleanup_state"] == "failed":
            problems.append(("susanoo.cleanup_failed", "Fixture cleanup failed; namespace quarantined"))
        if attempt["reconciliation_state"] in {"pending", "unresolved"}:
            problems.append(("susanoo.unresolved_side_effect", "A possible side effect could not be reconciled"))
        if attempt["required"] and attempt["business_verdict"] == "UNKNOWN":
            detail = "held-out case" if hidden else ", ".join(unknown_required) or "no evidence"
            problems.append(("susanoo.unresolved", f"Required check(s) not proven (UNKNOWN): {detail}"))
        if problems:
            errors += 1
            for kind, message in problems:
                ET.SubElement(case, "error", type=kind, message=message)
        if not attempt["required"] and attempt["business_verdict"] == "FAIL":
            notes.append("optional case failed (visible, non-blocking)")
        ET.SubElement(case, "system-out").text = "\n".join(notes)
    suite.set("tests", str(len(view_attempts)))
    suite.set("failures", str(failures))
    suite.set("errors", str(errors))
    suite.set("skipped", "0")
    suite.set("time", f"{total_time:.3f}")
    ET.indent(suite)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(suite, encoding="unicode") + "\n"


def _outcome_text(attempt: dict[str, Any], verdict: str) -> str:
    return "\n".join(
        f"{o['check_id']} [{o['requirement_id']}]: {o['reason']}"
        for o in attempt["outcomes"]
        if o["verdict"] == verdict
    )


def reproduction_ingredient(snapshot: ExportSnapshot) -> dict[str, Any]:
    """Inputs for the reproduction ZIP: authorized cases, fixture recipes, check digests, identities, command.

    No secrets, no hidden expectations: in the engineer view a held-out case contributes only digests.
    The platform assembles the ZIP; this is the runtime half of it.
    """
    manifest, suite = snapshot.manifest, snapshot.suite
    by_id = {case.id: case for case in suite.cases}
    cases = []
    for entry in snapshot.data["manifest"]["cases"]:
        withheld = entry["visibility"] == "hidden" and snapshot.audience == "engineer"
        item: dict[str, Any] = {
            "case_id": entry["case_id"],
            "visibility": entry["visibility"],
            "required": entry["required"],
            "case_digest": entry["case_digest"],
            "fixture_digest": entry["fixture_digest"],
            "fixture_compatibility_key": entry["fixture_compatibility_key"],
            "check_digests": entry["check_digests"],
            "content_withheld": withheld,
            "command": f"susanoo-runner reproduce --run {manifest.handle} --case {entry['case_id']}",
        }
        if not withheld:
            case = by_id[entry["case_id"]].model_dump(mode="json")
            item.update(
                title=case["title"],
                requirement_ids=case["requirement_ids"],
                script=case["script"],
                fixture={"adapter": case["fixture"]["adapter"], "recipe": case["fixture"]["recipe"]},
                fixture_seed_digest=entry["fixture_digest"],
                observations=case["observations"],
                checks=case["checks"],
            )
        cases.append(item)
    agent_args = ["--agent-cmd", *manifest.agent_cmd] if manifest.agent_cmd else ["--agent", manifest.agent_spec or ""]
    return {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "kind": "susanoo.reproduction_ingredient",
        "audience": snapshot.audience,
        "snapshot_digest": snapshot.digest,
        "suite": {
            "id": manifest.suite_id,
            "hash": manifest.suite_hash,
            "synthetic": manifest.synthetic,
            "approval_kind": snapshot.report.approval_kind,
            "timezone": suite.timezone,
            "limits": suite.limits.model_dump(mode="json"),
            "requirements": [r.model_dump(mode="json") for r in suite.requirements],
        },
        "agent": {"label": manifest.agent_label, "spec": manifest.agent_spec, "command": manifest.agent_cmd},
        "adapters": {
            "agent": manifest.agent_adapter,
            "fixtures": sorted({f"{c.fixture_adapter}@{c.fixture_compatibility_key}" for c in manifest.cases}),
        },
        "evaluator_digest": manifest.evaluator_digest,
        "profile": manifest.profile,
        "cases": cases,
        "command": " ".join(
            ["susanoo-runner", "run", "--suite", "suite.json"]
            + [arg for c in cases for arg in ("--case", c["case_id"])]
            + agent_args
        ),
    }


def write_exports(
    run_dir: RunDir, formats: list[Format], output: Path, audience: Audience = "engineer"
) -> dict[str, Path]:
    """Render the requested formats from one snapshot. ``output`` is a file for one format, else a directory."""
    snapshot = build_snapshot(run_dir, audience)
    names = {"json": "results.json", "junit": "results.junit.xml", "bundle": "reproduction.json"}
    single = len(formats) == 1 and output.suffix != ""
    if not single:
        output.mkdir(parents=True, exist_ok=True)
    written = {}
    for fmt in formats:
        path = output if single else output / names[fmt]
        path.parent.mkdir(parents=True, exist_ok=True)
        if fmt == "json":
            write_json(path, export_json(snapshot))
        elif fmt == "junit":
            path.write_text(export_junit(snapshot), encoding="utf-8")
        else:
            write_json(path, reproduction_ingredient(snapshot))
        written[fmt] = path
    return written
