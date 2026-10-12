import type { IterationMetrics } from "@/types/generated";

/**
 * The landing page's before/after, copied from the saved demo run (hero-2, saved as run
 * 0ca2e19e-f4c9-44a2-802f-959e1f574c63; full record in docs/evidence/hero-2-run.json).
 * "Before" and "after" bracket the run's one kept fix (patch-1): round 1 and round 2 score the same
 * 28 scenarios, so the comparison isn't skewed by the round-0 scenarios set aside at the test gate.
 * proof.test.ts checks every number here against that file, so they can't drift into placeholders.
 * [PRD › Frontend (marketing site + app) and API › Surface 1: Marketing site]
 */
export const PROOF_RUN = {
  evidenceRunId: "hero-2",
  before: {
    iteration: 1,
    recall: 0.875,
    precision: 1.0,
    f1: 0.9333333333333333,
    severity_accuracy: 1.0,
    heldout_f1: 0.9090909090909091,
    acceptance_pass_rate: 0.9,
    cost_usd: 1.163223,
  } satisfies IterationMetrics,
  after: {
    iteration: 2,
    recall: 1.0,
    precision: 1.0,
    f1: 1.0,
    severity_accuracy: 1.0,
    heldout_f1: 0.9090909090909091,
    acceptance_pass_rate: 1.0,
    cost_usd: 1.517075,
  } satisfies IterationMetrics,
  /** Everything the run spent, including the work after the last score. */
  totalCostUsd: 1.750983,
  /** neatlogs traces from the after round, one per scenario. */
  traces: [
    { scenarioId: "sim-04", traceId: "8e854d899f7d6115ef062a45b7f3ce09", label: "The token it now catches" },
    { scenarioId: "seed-10-two-problems-two-files", traceId: "e9e29a67ae96ae3e0f2b3b0bc43cfedc", label: "The hidden check it still misses" },
  ],
} as const;

export type ProofStat = { label: string; before: string; after: string; note: string };

const pct = (x: number) => `${Math.round(x * 100)}%`;

/** The three numbers the PRD asks for, in plain words. False alarms are the share of flags that were wrong (1 − precision). */
export function proofStats(before: IterationMetrics, after: IterationMetrics): ProofStat[] {
  return [
    {
      label: "Score on hidden tests",
      before: before.heldout_f1.toFixed(2),
      after: after.heldout_f1.toFixed(2),
      note: "Held-out F1 on tests the fixing heads never see.",
    },
    {
      label: "Problems caught",
      before: pct(before.recall),
      after: pct(after.recall),
      note: "Recall on the practice tests.",
    },
    {
      label: "False alarms",
      before: pct(1 - before.precision),
      after: pct(1 - after.precision),
      note: "Share of its flags that were wrong.",
    },
  ];
}

/**
 * Link to a trace in the neatlogs dashboard, or null if the link format isn't configured.
 * Same rule as NEATLOGS_TRACE_URL_TEMPLATE in packages/core/susanoo/integrations/tracing.py.
 */
export function traceUrl(template: string | undefined, traceId: string): string | null {
  const t = template?.trim();
  if (!t || !t.includes("{trace_id}")) return null;
  return t.replaceAll("{trace_id}", traceId);
}
