import { describe, expect, it } from "vitest";
import type { Finding } from "@/types/generated";
import { caughtPercent, findingLine, parsePrRef, resolvePreviewUrl, roundLabel, safePreviewUrl, toFeedbackIn, toTryInput } from "./agent";

describe("agent rules", () => {
  it("only allows http(s) preview URLs", () => {
    expect(safePreviewUrl("https://run-1.example.com")).toBe("https://run-1.example.com/");
    expect(safePreviewUrl("javascript:alert(1)")).toBeNull();
    expect(safePreviewUrl("nonsense")).toBeNull();
    expect(safePreviewUrl(null)).toBeNull();
  });

  it("prefers the API preview URL, else derives run-<id>.<domain>", () => {
    expect(resolvePreviewUrl("abc", { preview_url: "https://x.dev/a", repo_url: null }, "ex.com")).toBe("https://x.dev/a");
    expect(resolvePreviewUrl("abc", null, "ex.com")).toBe("https://run-abc.ex.com");
    expect(resolvePreviewUrl("abc", null, undefined)).toBeNull();
    expect(resolvePreviewUrl("a/b", null, "ex.com")).toBeNull();
    expect(resolvePreviewUrl("abc", null, "ex.com/evil")).toBeNull();
  });

  it("parses PR references", () => {
    expect(parsePrRef("https://github.com/a/b/pull/7")).toBe("https://github.com/a/b/pull/7");
    expect(parsePrRef(" a/b#7 ")).toBe("a/b#7");
    expect(parsePrRef("https://evil.com/a/b/pull/7")).toBeNull();
  });

  it("builds try-it input", () => {
    expect(toTryInput("diff", "   ")).toBeNull();
    expect(toTryInput("diff", "+ x")).toEqual({ kind: "diff", diff: "+ x" });
    expect(toTryInput("pr", "not a pr")).toBeNull();
    expect(toTryInput("pr", "a/b#7")).toEqual({ kind: "pr", pr: "a/b#7" });
  });

  it("turns a thumbs down into feedback only with an expectation and the reviewed diff", () => {
    const finding: Finding = { file: "a.py", line: 3, rule_id: "r1", severity: "warn", message: "Looks risky." };
    expect(toFeedbackIn("  ", "+ x")).toBeNull();
    expect(toFeedbackIn("flag the secret", undefined)).toBeNull();
    expect(toFeedbackIn(" flag the secret ", "+ x", [finding])).toEqual({ thumbs_down: true, expectation: "flag the secret", diff: "+ x", findings: [finding] });
    expect(toFeedbackIn("flag it", "+ x")).toEqual({ thumbs_down: true, expectation: "flag it", diff: "+ x", findings: [] });
  });

  it("words findings and rounds plainly", () => {
    expect(findingLine({ file: "a.py", line: 3, rule_id: "r1", severity: "warn", message: "Looks risky." })).toBe("a.py line 3: Looks risky.");
    expect(findingLine({ file: "", line: 0, rule_id: "r1", severity: "warn", message: "Looks risky." })).toBe("Looks risky.");
    const card = { iteration: 0 } as Parameters<typeof roundLabel>[0];
    expect(roundLabel(card)).toBe("First round");
    expect(roundLabel({ ...card, iteration: 2 })).toBe("Round 2");
  });

  it("clamps percentages", () => {
    expect(caughtPercent(5, 0)).toBe(0);
    expect(caughtPercent(15, 10)).toBe(100);
  });
});
