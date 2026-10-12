import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { PROOF_RUN, proofStats, traceUrl } from "./proof";

const saved = JSON.parse(
  readFileSync(path.resolve(__dirname, "../../../docs/evidence/hero-2-run.json"), "utf8"),
) as {
  run_id: string;
  metrics: { iteration: number }[];
  results: { scenario_id: string; trace_id: string; passed: boolean }[];
};

describe("PROOF_RUN", () => {
  it("copies the before and after numbers from the saved run, unchanged", () => {
    expect(saved.run_id).toBe(PROOF_RUN.evidenceRunId);
    expect(saved.metrics[0]).toEqual(PROOF_RUN.before);
    expect(saved.metrics[saved.metrics.length - 1]).toEqual(PROOF_RUN.after);
  });
  it("links traces that the saved run really recorded, with the right outcome", () => {
    for (const t of PROOF_RUN.traces) {
      const r = saved.results.find((x) => x.scenario_id === t.scenarioId);
      expect(r?.trace_id).toBe(t.traceId);
    }
    expect(saved.results.find((x) => x.scenario_id === "sim-04")?.passed).toBe(true);
    expect(saved.results.find((x) => x.scenario_id === "seed-10-two-problems-two-files")?.passed).toBe(false);
  });
});

describe("proofStats", () => {
  it("shows hidden-test F1, recall and false alarms as plain numbers", () => {
    expect(proofStats(PROOF_RUN.before, PROOF_RUN.after)).toMatchObject([
      { label: "Score on hidden tests", before: "0.91", after: "0.91" },
      { label: "Problems caught", before: "100%", after: "100%" },
      { label: "False alarms", before: "29%", after: "0%" },
    ]);
  });
});

describe("traceUrl", () => {
  it("fills the trace id into the template", () => {
    expect(traceUrl("https://app.example/traces/{trace_id}?tab=spans", "abc")).toBe(
      "https://app.example/traces/abc?tab=spans",
    );
  });
  it("gives no link when the template is missing or has no placeholder", () => {
    expect(traceUrl(undefined, "abc")).toBeNull();
    expect(traceUrl("  ", "abc")).toBeNull();
    expect(traceUrl("https://app.example/traces", "abc")).toBeNull();
  });
});
