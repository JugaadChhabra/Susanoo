import { describe, expect, it } from "vitest";
import type { RunSummary } from "@/types/generated";
import { EXAMPLE_SPECS, formatCost, formatScore, parseRules, rulesProblem, rulesToText, runStatusLabel } from "./projects";

const run = (overrides: Partial<RunSummary> = {}): RunSummary => ({
  id: "r1",
  status: "done",
  heldout_f1: 0.8,
  cost_usd: 1.25,
  cost_cap_usd: 2,
  created_at: "2026-10-12 08:00:00+00",
  ...overrides,
});

describe("parseRules", () => {
  it("turns one sentence per line into a list, dropping blanks and bullets", () => {
    expect(parseRules("  Never log emails.\n\n- Every endpoint needs a login check.\n• No raw SQL.\n   ")).toEqual([
      "Never log emails.",
      "Every endpoint needs a login check.",
      "No raw SQL.",
    ]);
  });

  it("keeps a leading - or * that is part of the rule itself", () => {
    expect(parseRules("**kwargs must be typed.\n-1 is never a valid id.")).toEqual([
      "**kwargs must be typed.",
      "-1 is never a valid id.",
    ]);
  });

  it("round-trips through rulesToText", () => {
    const rules = ["One.", "Two."];
    expect(parseRules(rulesToText(rules))).toEqual(rules);
  });
});

describe("rulesProblem", () => {
  it("explains too many or too long rules in plain English", () => {
    expect(rulesProblem(["Fine."])).toBeNull();
    expect(rulesProblem(Array.from({ length: 31 }, (_, i) => `Rule ${i}.`))).toMatch(/30 rules or fewer/);
    expect(rulesProblem(["Fine.", "x".repeat(501)])).toMatch(/Rule 2 is too long/);
  });
});

describe("formatting", () => {
  it("shows the held-out score as a percentage, or a dash before the first round", () => {
    expect(formatScore(run().heldout_f1)).toBe("80%");
    expect(formatScore(null)).toBe("—");
  });

  it("shows cost against its cap in dollars", () => {
    expect(formatCost(run())).toBe("$1.25 of $2.00");
  });

  it("names every run status in plain English", () => {
    expect(runStatusLabel("running")).toBe("Running");
    expect(runStatusLabel("awaiting_approval")).toBe("Needs your approval");
    expect(runStatusLabel("done")).toBe("Finished");
    expect(runStatusLabel("failed")).toBe("Stopped with a problem");
  });
});

describe("EXAMPLE_SPECS", () => {
  it("starts with the code reviewer, which has its rules as separate sentences", () => {
    const [first] = EXAMPLE_SPECS;
    expect(first.title).toMatch(/code review/i);
    expect(first.spec).not.toMatch(/Rules:/);
    expect(first.rules.length).toBeGreaterThanOrEqual(5);
    expect(first.rules.every((r) => r.endsWith("."))).toBe(true);
  });
});
