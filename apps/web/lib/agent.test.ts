import { describe, expect, it } from "vitest";
import { caughtPercent, parsePrRef, problemsLine, resolvePreviewUrl, safePreviewUrl, toFeedback, toTryInput } from "./agent";

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

  it("requires an expectation for a thumbs down only", () => {
    expect(toFeedback("down", "  ")).toBeNull();
    expect(toFeedback("down", " flag the secret ", "+ x")).toEqual({ rating: "down", expectation: "flag the secret", diff: "+ x" });
    expect(toFeedback("up", "")).toEqual({ rating: "up" });
  });

  it("words the report line and clamps percentages", () => {
    expect(problemsLine(9, 10, 1)).toBe("Caught 9 of 10 problems, 1 false alarm.");
    expect(problemsLine(6, 10, 0)).toBe("Caught 6 of 10 problems, 0 false alarms.");
    expect(caughtPercent(5, 0)).toBe(0);
    expect(caughtPercent(15, 10)).toBe(100);
  });
});
