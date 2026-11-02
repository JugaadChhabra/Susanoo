import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { ReportContent, type RuntimeReport } from "@/components/evaluation/advanced-reports";

function render(report_type: string, payload: unknown) {
  return renderToStaticMarkup(createElement(ReportContent, { report: {
    report_type, payload, reviews: [],
  } as unknown as RuntimeReport }));
}

describe("specialized report projections", () => {
  it("keeps all four experiment cells and every repetition", () => {
    const cells = ["old", "new"].flatMap(agent => ["old", "new"].map(environment => ({ agent, environment,
      results: [{ business_verdict: "FAIL" }, { business_verdict: "PASS" }],
    })));
    const html = render("two_by_two", { cells });
    expect((html.match(/Agent \/ /g) ?? []).length).toBe(4);
    expect((html.match(/>PASS</g) ?? []).length).toBe(4);
    expect((html.match(/>FAIL</g) ?? []).length).toBe(4);
  });
  it("uses canonical action fields rather than invented id/action/after fields", () => {
    const html = render("reduction", { reduced_plan: { actions: [{ step: "write-1", operation: "create_promise",
      request: { account: "synthetic" } }] }, attempts: [], reproduction: [] });
    expect(html).toContain("write-1"); expect(html).toContain("create_promise"); expect(html).toContain("synthetic");
  });
  it("keeps transcript-only audio metrics explicitly unavailable", () => {
    const value = { unavailable: true, reason: "Transcript only; audio not captured" };
    const html = render("media", { modality: "transcript_only", transcript_quality: value, response_latency: value,
      interruptions: value, speech_quality: value });
    expect((html.match(/Transcript only; audio not captured/g) ?? []).length).toBe(4);
    expect(html).not.toContain("100%");
  });
  it("shows simulator state and pinned digest", () => {
    const html = render("simulator_state", { captured_at: "2026-11-02T00:00:00Z", digest: "pinned-state",
      state: { accounts: [{ account_id: "SYNTH-1", balance: 500 }] } });
    expect(html).toContain("pinned-state"); expect(html).toContain("SYNTH-1"); expect(html).toContain("500");
  });
  it("shows probe scores and selected ordering from canonical DTO fields", () => {
    const html = render("probe_plan", { budget_cap: 25, score_inputs: [{ candidate_id: "candidate-A", score: 0.8,
      severity: 3, expected_cost: 2 }], selected: ["candidate-A"] });
    expect(html).toContain("candidate-A"); expect(html).toContain("0.8"); expect(html).toContain("25");
  });
});
