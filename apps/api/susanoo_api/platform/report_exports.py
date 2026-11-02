"""Domain-specific PDF projections, always from the export's authorized immutable snapshot."""

from xml.sax.saxutils import escape

from reportlab.platypus import Paragraph, Spacer, Table, TableStyle


def report_story(reports, styles):
    story = []

    def text(value):
        return Paragraph(escape(str(value)), styles["BodyText"])

    def table(headers, rows):
        if not rows:
            return
        value = Table([[text(value) for value in headers]] + [[text(value) for value in row] for row in rows],
                      repeatRows=1, hAlign="LEFT")
        value.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                   ("GRID", (0, 0), (-1, -1), 0.25, "#ccd5d3")]))
        story.extend([value, Spacer(1, 10)])

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
                for key in ("transcript_quality", "response_latency", "speech_quality"):
                    story.append(text(key.replace("_", " ") + ": " + str(data[key])))
        elif kind == "run_export":
            run = data["snapshot"]["report"]
            story.append(text("Runtime machine gate: " + run["gate"]))
            table(["Case", "Execution", "Business verdict", "Cleanup", "Reconciliation"], [
                [row["title"], row["execution_state"], row["business_verdict"], row["cleanup_state"],
                 row["reconciliation_state"]] for row in run["attempts"]
            ])
        story.append(Spacer(1, 12))
    return story
