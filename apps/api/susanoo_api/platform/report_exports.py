"""Domain-specific PDF projections, always from the export's authorized immutable snapshot."""

from reportlab.platypus import Paragraph, Spacer, Table, TableStyle
from .pdf_typography import markup as escape


def report_story(reports, styles):
    story = []

    def text(value):
        return Paragraph(escape(str(value)), styles["BodyText"])

    def table(headers, rows):
        if not rows:
            return
        value = Table([[text(value) for value in headers]] + [[text(value) for value in row] for row in rows],
                      repeatRows=1, hAlign="LEFT", colWidths=[468 / len(headers)] * len(headers), splitInRow=1)
        value.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                   ("GRID", (0, 0), (-1, -1), 0.25, "#ccd5d3")]))
        story.extend([value, Spacer(1, 10)])

    def detail(title, value):
        story.append(Paragraph(escape(title.replace("_", " ").title()), styles["Heading3"]))
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(item, dict | list):
                    detail(key, item)
                else:
                    story.append(text(key.replace("_", " ") + ": " + str(item)))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, dict | list):
                    detail(str(index + 1), item)
                else:
                    story.append(text(item))
            if not value:
                story.append(text("None recorded"))
        else:
            story.append(text(value))

    for report in reports:
        kind, data = report["report_type"], report["payload"]
        story.extend([Paragraph(escape(kind.replace("_", " ").title()), styles["Heading2"]),
                      text("Report " + str(report["id"]) + " | digest " + report["content_hash"])])
        if kind == "comparison":
            table(["Prerequisites", "Status", "Normalizers"], [[data["preflight"]["problems"], data["status"],
                                                                  data["normalizers_applied"]]])
            table(["Category", "Observed difference", "Proposed classification"], [
                [value["category"], value["summary"], value["classification"]] for value in data["candidates"]
            ])
            table(["Step", "Simulator", "Reference", "Differences"], [
                [value["step"], value["simulator"]["status"], value["reference"]["status"],
                 value["differences"]] for value in data["steps"]
            ])
            table(["Reviewer", "Decision", "Rationale"], [
                [value["actor_id"], value["classification"], value["rationale"]] for value in report.get("reviews", [])
            ])
        elif kind == "two_by_two":
            table(["Agent", "Environment", "All repetition verdicts"], [
                [cell["agent"], cell["environment"], [row["business_verdict"] for row in cell["results"]]]
                for cell in data["cells"]
            ])
        elif kind == "task_impact":
            table(["Case / repetition", "Simulator", "Reference", "Relationship / attribution"], [[
                f"{data['case_id']} / {data['repetition']}", data["simulator"]["business_verdict"],
                data["reference"]["business_verdict"], data["relationship"] + " / " + data["attribution"],
            ]])
            table(["Dependency", "Evidence coverage"], list(data["dependencies"].items()))
        elif kind == "reduction":
            story.extend([text(data["label"]), text(f"Runs {data['runs_used']} / {data['budget']}"),
                          text("Stop: " + data["stopped_because"])])
            table(["Transformation", "Candidate steps", "Outcome", "Runs"], [
                [row["description"], row["steps"], row["outcome"], row["runs"]] for row in data["attempts"]
            ])
            for instruction in data["reproduction"]:
                story.append(text(instruction))
        elif kind in {"agent_repair", "simulator_repair"}:
            story.append(text("Repair gate: " + data["outcome"]))
            table(["Gate", "Passed", "Evidence / reason"], [
                [row.get("name", row.get("id", "Gate")), row.get("passed"), row.get("detail", row.get("reason", ""))]
                for row in data["checks"]
            ])
            for key in ("pins", "pinned", "flags", "heldout", "heldout_exposures"):
                if key in data:
                    detail(key, data[key])
            if report.get("hosted_heldout_exposures") is not None:
                detail("Hosted held-out exposures", report["hosted_heldout_exposures"])
        elif kind in {"probe_run", "probe_comparison", "collision", "calibration", "media", "capacity"}:
            for key, value in data.items():
                if isinstance(value, str | int | float | bool) or value is None:
                    story.append(text(key.replace("_", " ") + ": " + str(value)))
            if kind == "probe_run":
                table(["Candidate", "Calls", "Reserved", "Cost", "Outcome"], [
                    [row["candidate_id"], row["calls"], row["reserved"], row["cost"], row["status"]]
                    for row in data["executions"]
                ])
            elif kind == "probe_comparison":
                table(["Strategy", "Reference calls", "Cost", "Findings", "Unknowns"], [
                    [data[arm][key] for key in
                     ("strategy", "reference_calls", "cost_used", "accepted_findings", "unknowns")]
                    for arm in ("baseline", "priority")
                ])
            elif kind == "media":
                for key in ("transcript_quality", "response_latency", "interruptions", "speech_quality"):
                    detail(key, data[key])
            elif kind in {"collision", "calibration"}:
                for key, value in data.items():
                    if isinstance(value, dict | list):
                        detail(key, value)
        elif kind == "run_export":
            run = data["snapshot"]["report"]
            story.append(text("Runtime machine gate: " + run["gate"]))
            table(["Case", "Execution", "Business verdict", "Cleanup", "Reconciliation"], [
                [row["title"], row["execution_state"], row["business_verdict"], row["cleanup_state"],
                 row["reconciliation_state"]] for row in run["attempts"]
            ])
        elif kind in {"import", "incident", "conclusion", "validity", "probe_plan", "simulator_state", "reproduction"}:
            for key, value in data.items():
                detail(key, value)
        story.append(Spacer(1, 12))
    return story
