import type { Diagnosis, IterationMetrics, RunEvent } from "@/types/generated";

const num = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
const rec = (v: unknown): Record<string, unknown> | null => (v && typeof v === "object" ? (v as Record<string, unknown>) : null);

const METRIC_KEYS = ["iteration", "recall", "precision", "f1", "severity_accuracy", "heldout_f1", "acceptance_pass_rate", "cost_usd"] as const;

/** One point per `metrics` event, ordered by round. A later event for the same round replaces the earlier one. */
export function deriveMetrics(events: RunEvent[]): IterationMetrics[] {
  const byRound = new Map<number, IterationMetrics>();
  for (const e of events) {
    if (e.type !== "metrics") continue;
    if (!METRIC_KEYS.every((k) => num(e.payload[k]))) continue;
    const m = e.payload as unknown as IterationMetrics;
    byRound.set(m.iteration, m);
  }
  return [...byRound.values()].sort((a, b) => a.iteration - b.iteration);
}

export interface Budget {
  capUsd: number | null;
  spentUsd: number;
  iterationCap: number | null;
}

export function deriveBudget(events: RunEvent[]): Budget {
  let capUsd: number | null = null;
  let iterationCap: number | null = null;
  for (const e of events) {
    if (capUsd === null && num(e.payload.budget_usd)) capUsd = e.payload.budget_usd;
    if (iterationCap === null && num(e.payload.iteration_cap)) iterationCap = e.payload.iteration_cap;
  }
  const metrics = deriveMetrics(events);
  return { capUsd, iterationCap, spentUsd: metrics.at(-1)?.cost_usd ?? 0 };
}

export type LedgerOutcome = "pending" | "testing" | "awaiting_approval" | "kept" | "rolled_back" | "rejected";

export interface LedgerRow {
  id: string;
  layer: Diagnosis["layer"];
  hypothesis: string;
  scenarioCount: number;
  targetFiles: string[];
  outcome: LedgerOutcome;
  patchId: string | null;
  linesChanged: number | null;
  heldoutBefore: number | null;
  heldoutAfter: number | null;
}

const LAYERS = new Set(["code", "tool", "prompt_policy", "test"]);

/** Diagnoses in the order found, each with what happened to its fix. Pure fold over the event array. */
export function deriveLedger(events: RunEvent[]): LedgerRow[] {
  const rows = new Map<string, LedgerRow>();
  const byPatch = new Map<string, LedgerRow>();
  const link = (row: LedgerRow | undefined, patchId: unknown) => {
    if (row && typeof patchId === "string") {
      row.patchId = patchId;
      byPatch.set(patchId, row);
    }
  };

  for (const e of events) {
    const p = e.payload;
    if (e.head === "diagnostician" && e.type === "finished" && Array.isArray(p.diagnoses)) {
      for (const raw of p.diagnoses) {
        const d = rec(raw);
        if (!d || typeof d.id !== "string" || typeof d.hypothesis !== "string" || typeof d.layer !== "string" || !LAYERS.has(d.layer)) continue;
        if (rows.has(d.id)) continue;
        rows.set(d.id, {
          id: d.id,
          layer: d.layer as Diagnosis["layer"],
          hypothesis: d.hypothesis,
          scenarioCount: Array.isArray(d.scenario_ids) ? d.scenario_ids.length : 0,
          targetFiles: Array.isArray(d.target_files) ? d.target_files.filter((f): f is string => typeof f === "string") : [],
          outcome: "pending",
          patchId: null,
          linesChanged: null,
          heldoutBefore: null,
          heldoutAfter: null,
        });
      }
    } else if (e.head === "fixer" && e.type === "started" && typeof p.diagnosis_id === "string") {
      const row = rows.get(p.diagnosis_id);
      if (row && row.outcome === "pending") row.outcome = "testing";
    } else if (e.head === "fixer" && e.type === "approval_needed") {
      const row = typeof p.diagnosis_id === "string" ? rows.get(p.diagnosis_id) : undefined;
      if (row) {
        row.outcome = "awaiting_approval";
        link(row, p.patch_id);
        if (num(p.lines_changed)) row.linesChanged = p.lines_changed;
      }
    } else if (e.head === "fixer" && e.type === "finished") {
      const patch = rec(p.patch);
      const row = patch && typeof patch.diagnosis_id === "string" ? rows.get(patch.diagnosis_id) : undefined;
      if (row && patch) {
        row.outcome = "testing";
        link(row, patch.id);
        if (num(patch.lines_changed)) row.linesChanged = patch.lines_changed;
      }
    } else if (e.head === "verifier" && e.type === "finished" && typeof p.patch_id === "string") {
      const row = byPatch.get(p.patch_id);
      if (row && (p.verdict === "kept" || p.verdict === "rolled_back")) {
        row.outcome = p.verdict;
        row.heldoutBefore = num(p.heldout_f1_before) ? p.heldout_f1_before : null;
        row.heldoutAfter = num(p.heldout_f1_after) ? p.heldout_f1_after : null;
      }
    } else if (typeof p.patch_id === "string" && p.decision === "rejected") {
      const row = byPatch.get(p.patch_id);
      if (row) row.outcome = "rejected";
    }
  }
  return [...rows.values()];
}

export interface PendingApproval {
  patchId: string;
  diagnosisId: string | null;
  layer: string;
  linesChanged: number | null;
  file: string | null;
  summary: string;
  hypothesis: string | null;
  traceUrl: string | null;
}

/** The newest approval request no later event has answered. */
export function derivePendingApproval(events: RunEvent[]): PendingApproval | null {
  let pending: PendingApproval | null = null;
  for (const e of events) {
    const p = e.payload;
    if (e.type === "approval_needed" && typeof p.patch_id === "string") {
      const change = rec(p.proposed_change);
      const diagnosisId = typeof p.diagnosis_id === "string" ? p.diagnosis_id : null;
      const diag = diagnosisId ? deriveLedger(events.filter((x) => x.seq <= e.seq)).find((r) => r.id === diagnosisId) : undefined;
      pending = {
        patchId: p.patch_id,
        diagnosisId,
        layer: typeof p.layer === "string" ? p.layer : "test",
        linesChanged: num(p.lines_changed) ? p.lines_changed : null,
        file: typeof change?.file === "string" ? change.file : null,
        summary: typeof change?.summary === "string" ? change.summary : e.message,
        hypothesis: diag?.hypothesis ?? null,
        traceUrl: e.trace_url,
      };
    } else if (pending) {
      const patch = rec(p.patch);
      if (p.patch_id === pending.patchId || patch?.id === pending.patchId) pending = null;
    }
  }
  return pending;
}
