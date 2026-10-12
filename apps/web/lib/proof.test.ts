import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { PROOF_RUN, countUpFrame, proofStats, traceUrl } from "./proof";

const saved = JSON.parse(
  readFileSync(path.resolve(__dirname, "../../../docs/evidence/hero-2-run.json"), "utf8"),
) as {
  run_id: string;
  cost_usd: number;
  metrics: { iteration: number }[];
  results: { scenario_id: string; iteration: number; trace_id: string; passed: boolean }[];
};

describe("PROOF_RUN", () => {
  it("copies the before and after numbers from the saved run, unchanged", () => {
    expect(saved.run_id).toBe(PROOF_RUN.evidenceRunId);
    expect(saved.metrics.find((m) => m.iteration === PROOF_RUN.before.iteration)).toEqual(PROOF_RUN.before);
    expect(saved.metrics[saved.metrics.length - 1]).toEqual(PROOF_RUN.after);
    expect(saved.cost_usd).toBe(PROOF_RUN.totalCostUsd);
  });
  it("links traces that the saved run really recorded, with the right outcome", () => {
    const after = saved.results.filter((x) => x.iteration === PROOF_RUN.after.iteration);
    const find = (id: string) => after.find((x) => x.scenario_id === id);
    for (const t of PROOF_RUN.traces) expect(find(t.scenarioId)?.trace_id).toBe(t.traceId);
    expect(find("sim-04")?.passed).toBe(true);
    expect(find("seed-10-two-problems-two-files")?.passed).toBe(false);
  });
});

describe("proofStats", () => {
  it("shows hidden-test F1, recall and false alarms as plain numbers", () => {
    expect(proofStats(PROOF_RUN.before, PROOF_RUN.after)).toMatchObject([
      { label: "Score on hidden tests", before: "0.91", after: "0.91" },
      { label: "Problems caught", before: "88%", after: "100%" },
      { label: "False alarms", before: "0%", after: "0%" },
    ]);
  });
});

describe("traceUrl", () => {
  it("fills the trace id into every placeholder, like the backend does", () => {
    expect(traceUrl("https://app.example/traces/{trace_id}?focus={trace_id}", "abc")).toBe(
      "https://app.example/traces/abc?focus=abc",
    );
  });
  it("gives no link when the template is missing or has no placeholder", () => {
    expect(traceUrl(undefined, "abc")).toBeNull();
    expect(traceUrl("  ", "abc")).toBeNull();
    expect(traceUrl("https://app.example/traces", "abc")).toBeNull();
  });
});

describe("countUpFrame", () => {
  it("climbs from the before value to the after value, keeping the after value's decimals and suffix", () => {
    expect(countUpFrame("88%", "100%", 0)).toBe("88%");
    expect(countUpFrame("88%", "100%", 0.5)).toBe("94%");
    expect(countUpFrame("0.50", "0.91", 0.5)).toBe("0.71");
  });
  it("ends on the after value exactly, and clamps progress outside 0 to 1", () => {
    for (const s of proofStats(PROOF_RUN.before, PROOF_RUN.after)) expect(countUpFrame(s.before, s.after, 1)).toBe(s.after);
    expect(countUpFrame("88%", "100%", 1.4)).toBe("100%");
    expect(countUpFrame("88%", "100%", -1)).toBe("88%");
  });
  it("leaves an unchanged number, or anything that isn't a number, as the after value", () => {
    expect(countUpFrame("0%", "0%", 0.5)).toBe("0%");
    expect(countUpFrame("n/a", "100%", 0.5)).toBe("100%");
    expect(countUpFrame("88%", "n/a", 0.5)).toBe("n/a");
  });
});
