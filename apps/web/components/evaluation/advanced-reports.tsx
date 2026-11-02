"use client";

import { useEffect, useState, type ReactNode } from "react";
import { Check, Download, RefreshCw } from "lucide-react";
import { platform } from "@/lib/platform-api";
import type { ReportSubmission } from "@/types/platform-generated";

type Review = { actor_id: string; candidate_id: string; classification: string; rationale: string; created_at: string };
type Stored<T> = T extends { report_type: string; payload: unknown }
  ? Pick<T, "report_type" | "payload"> & { id: string; report_key: string; content_hash: string;
    run_id: string; attempt_id: string; created_at: string; reviews: Review[]; hosted_heldout_exposures?: number | null }
  : never;
export type RuntimeReport = Stored<ReportSubmission>;
type Row = Record<string, unknown>;

const titles: Record<string, string> = {
  comparison: "Simulator / Reference", task_impact: "Task Impact", two_by_two: "2 x 2 Experiment",
  reduction: "Reduced Reproduction", simulator_repair: "Simulator Repair", agent_repair: "Agent Repair",
  probe_plan: "Probe Plan", probe_run: "Probe Results", probe_comparison: "Probe Strategy Comparison",
  collision: "Shared-State Collision", calibration: "Grader Calibration", media: "Audio Measurements",
  import: "Historical Import", incident: "Draft Regression Case", conclusion: "Conclusion",
  validity: "Evidence Validity", capacity: "Measured Capacity", run_export: "Runtime Results",
  reproduction: "Reproduction Materials", simulator_state: "Simulator State",
};

function rows(value: unknown): Row[] {
  return Array.isArray(value) ? value.filter((item) => item && typeof item === "object") as Row[] : [];
}
function object(value: unknown): Row {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Row : {};
}
function label(value: string) { return value.replaceAll("_", " "); }
function cell(value: unknown): ReactNode {
  if (value === null || value === undefined) return <span className="ev-muted">Unavailable</span>;
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (Array.isArray(value)) return value.length ? value.map((item, i) => <div key={i}>{cell(item)}</div>) : "None";
  if (typeof value === "object") {
    const row = object(value);
    if (row.unavailable) return <span className="ev-muted">{String(row.reason)}</span>;
    return <dl className="ev-report-facts">{Object.entries(row).map(([key, item]) =>
      <div key={key}><dt>{label(key)}</dt><dd>{cell(item)}</dd></div>)}</dl>;
  }
  return String(value);
}
function Grid({ columns, data }: { columns: [string, string][]; data: Row[] }) {
  return data.length ? <div className="ev-table-wrap"><table><thead><tr>{columns.map(([key, title]) =>
    <th key={key}>{title}</th>)}</tr></thead><tbody>{data.map((row, index) => <tr key={index}>
    {columns.map(([key]) => <td key={key}>{cell(row[key])}</td>)}
  </tr>)}</tbody></table></div> : <div className="ev-empty">No observations in this scope.</div>;
}
function Facts({ data, keys }: { data: Row; keys: string[] }) {
  return <dl className="ev-report-facts">{keys.map((key) => <div key={key}>
    <dt>{label(key)}</dt><dd>{cell(data[key])}</dd></div>)}</dl>;
}

export function ReportContent({ report }: { report: RuntimeReport }) {
  const data = report.payload as unknown as Row;
  switch (report.report_type) {
    case "comparison": return <>
      <Facts data={data} keys={["status", "normalizers_applied"]} />
      <Facts data={object(data.preflight)} keys={["ok", "problems", "simulator_seed_digest", "reference_seed_digest"]} />
      <h4>Discrepancies</h4><Grid data={rows(data.candidates)} columns={[
        ["category", "Category"], ["summary", "Observed Difference"], ["classification", "Machine Classification"],
        ["matches_declared_simplification", "Declared Simplification Match"],
      ]} />
      <h4>Action Responses</h4><Grid data={rows(data.steps)} columns={[
        ["step", "Step"], ["operation", "Operation"], ["simulator", "Simulator"], ["reference", "Reference"],
        ["differences", "Differences"],
      ]} />
      <h4>State Differences</h4><Grid data={rows(data.state_differences)} columns={[
        ["kind", "Entity Kind"], ["entity", "Entity"], ["difference", "Observed Difference"],
      ]} />
      <h4>Reviewer Classifications</h4><Grid data={report.reviews as unknown as Row[]} columns={[
        ["candidate_id", "Candidate"], ["classification", "Decision"], ["rationale", "Rationale"],
        ["actor_id", "Reviewer"], ["created_at", "Recorded"],
      ]} />
    </>;
    case "task_impact": return <>
      <Facts data={data} keys={["case_id", "repetition", "relationship", "attribution"]} />
      <Grid columns={[["arm", "Environment"], ["business_verdict", "Business Verdict"],
        ["execution_state", "Execution"], ["failed_checks", "Failed Checks"], ["operations", "Observed Operations"]]}
        data={[{ arm: "Simulator", ...object(data.simulator) }, { arm: "Reference", ...object(data.reference) }]} />
      <h4>Dependency Coverage</h4>{cell(data.dependencies)}
      <h4>Controlled Interventions</h4><Grid data={rows(data.interventions)} columns={[
        ["delta_id", "Changed Configuration"], ["simulator_after_swap", "Result After Intervention"],
        ["flipped_to_reference", "Matched Reference Result"],
      ]} />
    </>;
    case "two_by_two": return <div className="ev-experiment-grid">{rows(data.cells).map((value) =>
      <section key={`${value.agent}-${value.environment}`}>
        <h4>{String(value.agent)} Agent / {String(value.environment)} Environment</h4>
        <Grid columns={[["repetition", "Repetition"], ["business_verdict", "Business Verdict"],
          ["execution_state", "Execution"], ["failed_checks", "Failed Checks"]]}
          data={rows(value.results).map((result, index) => ({ repetition: index + 1, ...result }))} />
      </section>)}</div>;
    case "reduction": return <>
      <Facts data={data} keys={["label", "runs_used", "budget", "stopped_because"]} />
      <div className="ev-inline"><span>Original actions: {rows(object(data.original_plan).actions).length}</span>
        <span>Reduced actions: {rows(object(data.reduced_plan).actions).length}</span></div>
      <Grid data={rows(data.attempts)} columns={[["index", "Attempt"], ["description", "Transformation"],
        ["steps", "Candidate Steps"], ["outcome", "Outcome"], ["runs", "Runs Used"]]} />
      <h4>Fresh Reproduction</h4>{cell(data.reproduction)}
      <h4>Retained Action Plan</h4><Grid data={rows(object(data.reduced_plan).actions)} columns={[
        ["step", "Step"], ["operation", "Approved Operation"], ["request", "Request"],
      ]} />
    </>;
    case "agent_repair": case "simulator_repair": return <>
      <Facts data={data} keys={["layer", "outcome", report.report_type === "agent_repair" ? "patch_digest" : "proposal_digest"]} />
      <h4>Pinned Layers</h4>{cell(data.pins ?? data.pinned)}
      <h4>Repair Gates</h4><Grid data={rows(data.checks)} columns={[
        ["name", "Gate"], ["passed", "Passed"], ["detail", "Evidence"],
      ]} />
      {data.flags !== undefined && <><h4>Risk Flags</h4>{cell(data.flags)}</>}
      {data.heldout !== undefined && <><h4>Held-Out Summary</h4>{cell(data.heldout)}</>}
      {report.report_type === "agent_repair" && <Facts data={{
        local_exposures: data.heldout_exposures, hosted_exposures: report.hosted_heldout_exposures,
      }} keys={["local_exposures", "hosted_exposures"]} />}
    </>;
    case "probe_plan": return <>
      <Facts data={data} keys={["strategy", "seed", "budget_cap", "cleanup_allowance", "pool_digest"]} />
      <Grid data={rows(data.score_inputs)} columns={[["candidate_id", "Candidate"], ["score", "Priority"],
        ["severity", "Severity"], ["expected_cost", "Expected Cost"]]} />
      <h4>Selected Order</h4>{cell(data.selected)}
    </>;
    case "probe_run": return <>
      <Facts data={data} keys={["stopped_reason", "budget_cap", "budget_reserved", "remaining_budget",
        "reference_calls", "cost_used", "not_run"]} />
      <Grid data={rows(data.executions)} columns={[["candidate_id", "Candidate"], ["reserved", "Reserved"],
        ["calls", "Calls"], ["cost", "Cost"], ["cleanup_cost", "Cleanup"], ["retries", "Retries"],
        ["status", "Outcome"], ["detail", "Evidence"]]} />
    </>;
    case "probe_comparison": return <>
      <Grid data={[object(data.baseline), object(data.priority)]} columns={[["strategy", "Strategy"],
        ["budget_cap", "Budget"], ["reference_calls", "Reference Calls"], ["cost_used", "Cost"],
        ["accepted_findings", "Accepted Findings"], ["unknowns", "Unknowns"], ["review_effort", "Review Effort"]]} />
      <Facts data={data} keys={["accepted_delta", "note"]} />
    </>;
    case "collision": return <>
      <Facts data={data} keys={["schedule_id", "shared_namespace", "isolated", "defects_found", "caught", "note"]} />
      {rows(data.repetitions).map((value, index) => <section key={index}>
        <h4>Repetition {String(value.repetition)}</h4>
        <Facts data={value} keys={["namespace", "outcome", "priority", "actor_errors"]} />
        <h4>Independent State Observation</h4>{cell(value.observation)}
        <Grid data={rows(value.evidence)} columns={[["step", "Step"], ["actor", "Actor"], ["event", "Event"],
          ["operation", "Operation"], ["status", "Status"], ["elapsed_ms", "Elapsed (ms)"], ["detail", "Evidence"]]} />
      </section>)}
    </>;
    case "calibration": return <>
      <Facts data={data} keys={["grader_version_id", "model_id", "accepted", "reasons", "control_set_hash"]} />
      <Grid columns={[["split", "Control Split"], ["examples", "Examples"], ["agreement", "Agreement"],
        ["false_pass_rate", "False Pass Rate"], ["false_fail_rate", "False Fail Rate"], ["errors", "Errors"]]}
        data={[{ split: "Calibration", ...object(data.calibration) }, { split: "Held-out", ...object(data.heldout) }]} />
      <h4>Approved Thresholds</h4>{cell(data.thresholds)}
      <Facts data={data} keys={["false_passes", "false_fails", "excluded_self_labeled", "errors"]} />
    </>;
    case "media": return <>
      <Facts data={data} keys={["modality"]} />
      <h4>Transcript Measurements</h4>{cell(data.transcript_quality)}
      <h4>Response Latency</h4>{cell(data.response_latency)}
      <h4>Interruptions</h4>{cell(data.interruptions)}
      <h4>Speech Quality</h4>{cell(data.speech_quality)}
    </>;
    case "import": return <>
      <Facts data={data} keys={["source_kind", "duplicates", "cursor"]} />
      <Grid data={rows(data.records)} columns={[["conversation_id", "Conversation"], ["evidence_class", "Evidence Class"],
        ["provenance", "Provenance"], ["proves_backend_state", "Proves Backend State"],
        ["replay_authorized", "Replay Authorized"]]} />
      <h4>Record Errors</h4><Grid data={rows(data.errors)} columns={[["location", "Location"], ["code", "Code"],
        ["message", "Reason"]]} />
    </>;
    case "incident": return <>
      <Facts data={data} keys={["status", "title", "rationale", "source_signature", "source_count",
        "requires_approval", "auto_replay", "approved_requirement"]} />
      <h4>Draft Scenario</h4>{cell(data.case)}<h4>Synthetic Fixture</h4>{cell(data.fixture)}
    </>;
    case "conclusion": case "validity": return <Facts data={data} keys={Object.keys(data)} />;
    case "capacity": return <Facts data={data} keys={["measured_at", "host", "python", "suite_id", "concurrency",
      "cases_per_run", "repetitions", "attempts", "passed", "not_passed", "execution_errors", "wall_seconds",
      "attempts_per_second", "attempt_p50_seconds", "attempt_p95_seconds", "attempt_max_seconds", "peak_rss_mb",
      "journal_bytes", "gates"]} />;
    case "run_export": return <>
      <Facts data={object(data.machine)} keys={["gate", "exit_code", "blockers"]} />
      <Grid data={rows(object(object(data.snapshot).report).attempts)} columns={[["title", "Case"],
        ["execution_state", "Execution"], ["business_verdict", "Business Verdict"], ["cleanup_state", "Cleanup"],
        ["reconciliation_state", "Reconciliation"]]} />
      <h4>Exclusions</h4>{cell(data.exclusions)}
    </>;
    case "reproduction": return <Grid data={rows(data.cases)} columns={[["case_id", "Case"],
      ["visibility", "Visibility"], ["content_withheld", "Content Withheld"], ["case_digest", "Pinned Digest"]]} />;
    case "simulator_state": return <>
      <Facts data={data} keys={["captured_at", "digest"]} />
      {Object.entries(object(data.state)).map(([kind, entities]) => <section key={kind}>
        <h4>{label(kind)}</h4>{cell(entities)}
      </section>)}
    </>;
    default: return null;
  }
}

export function AdvancedReports({ runId, revisionKey, reviewer }: {
  runId: string; revisionKey: string; reviewer: boolean;
}) {
  const [reports, setReports] = useState<RuntimeReport[]>([]);
  const [selected, setSelected] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  async function reload() {
    setBusy(true); setError("");
    try { setReports((await platform<{ items: RuntimeReport[] }>(`/runs/${runId}/reports`)).items); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(false); setLoaded(true); }
  }
  useEffect(() => {
    let active = true;
    const load = () => platform<{ items: RuntimeReport[] }>(`/runs/${runId}/reports`)
      .then((value) => { if (active) { setReports(value.items); setLoaded(true); } })
      .catch((e) => { if (active) { setError(e.message); setLoaded(true); } });
    void load();
    const timer = ["queued", "running"].includes(revisionKey) ? setInterval(() => void load(), 5000) : undefined;
    return () => { active = false; if (timer) clearInterval(timer); };
  }, [runId, revisionKey]);
  const report = reports.find((item) => item.id === selected) ?? reports[0];
  return <section className="ev-report-section" aria-label="Runtime reports">
    <div className="ev-inline"><h3>Runtime Reports</h3>
      <button type="button" className="ev-icon" title="Refresh reports" aria-label="Refresh reports"
        disabled={busy} onClick={() => void reload()}><RefreshCw size={16} /></button>
    </div>
    {error && <div className="ev-error" role="alert">{error}</div>}
    {!loaded ? <div role="status">Loading reports...</div> : !report ?
      <div className="ev-empty">No permitted runtime reports received.</div> : <>
      <div className="ev-toolbar"><select aria-label="Runtime report" value={report.id}
        onChange={(e) => setSelected(e.target.value)}>{reports.map((value) =>
          <option key={value.id} value={value.id}>{titles[value.report_type]} / {value.report_key}</option>)}</select>
        <a className="ev-icon" title="Download authorized report" aria-label="Download authorized report"
          href={`/api/v1/reports/${report.id}`} download={`susanoo-${report.report_type}.json`}><Download size={16} /></a>
      </div>
      <h4>{titles[report.report_type]}</h4><ReportContent report={report} />
      {reviewer && report.report_type === "comparison" && (report.payload.candidates ?? []).length > 0 &&
        <form className="ev-form-grid" key={report.id} onSubmit={async (event) => {
          event.preventDefault(); const form = new FormData(event.currentTarget); setBusy(true); setError("");
          try {
            await platform(`/reports/${report.id}/classifications`, "POST", {
              candidate_id: form.get("candidate"), classification: form.get("classification"),
              rationale: form.get("rationale"), evidence_ids: [report.id],
            }); await reload();
          } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
        }}>
          <label>Discrepancy<select name="candidate" required>{(report.payload.candidates ?? []).map((candidate) =>
            <option key={candidate.id} value={candidate.id}>{candidate.category}: {candidate.summary}</option>)}</select></label>
          <label>Classification<select name="classification" required>{["confirmed_defect", "intentional_simplification",
            "reference_limitation", "unresolved"].map((value) => <option key={value} value={value}>{label(value)}</option>)}
          </select></label>
          <label>Rationale<textarea name="rationale" required maxLength={4000} /></label>
          <div className="ev-form-actions"><button className="ev-primary" disabled={busy}><Check size={15} />
            Record Classification</button></div>
        </form>}
      <details><summary>Report Lineage</summary><Facts data={report as unknown as Row}
        keys={["id", "run_id", "attempt_id", "content_hash", "created_at"]} /></details>
    </>}
  </section>;
}
