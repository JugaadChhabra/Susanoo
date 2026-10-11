import { describe, expect, it } from "vitest";
import type { RunEvent } from "@/types/generated";
import fixture from "./fixtures/run-events.json";
import { commitLinks, findRepoUrl, deriveRunState, safeHref } from "./run-state";

const events = fixture as RunEvent[];

describe("deriveRunState", () => {
  it("is idle and waiting with no events", () => {
    const s = deriveRunState([]);
    expect(s.status).toBe("waiting");
    expect(s.activeHead).toBeNull();
    expect(Object.values(s.heads).every((h) => h === "idle")).toBe(true);
  });

  it("marks the started head active", () => {
    const s = deriveRunState(events.slice(0, 2));
    expect(s.heads.planner).toBe("active");
    expect(s.activeHead).toBe("planner");
    expect(s.status).toBe("running");
  });

  it("pauses on question_needed", () => {
    const s = deriveRunState(events.slice(0, 3));
    expect(s.heads.planner).toBe("question");
    expect(s.status).toBe("paused");
  });

  it("ends complete with every head resolved for the fixture run", () => {
    const s = deriveRunState(events);
    expect(s.status).toBe("complete");
    expect(s.activeHead).toBeNull();
    expect(s.iteration).toBeGreaterThan(0);
  });

  it("shows approval_needed on the fixer", () => {
    const idx = events.findIndex((e) => e.type === "approval_needed");
    const s = deriveRunState(events.slice(0, idx + 1));
    expect(s.heads.fixer).toBe("approval");
    expect(s.status).toBe("paused");
  });
});

describe("commitLinks", () => {
  it("links the fixer's patch commit using the builder's repo url", () => {
    const fixer = events.find((e) => e.head === "fixer" && e.type === "finished")!;
    const links = commitLinks(fixer, findRepoUrl(events));
    expect(links[0].href).toMatch(/^https:\/\/github\.com\/example-org\/code-review-agent\/commit\/a41d7c2/);
  });
  it("returns nothing before the repo is known", () => {
    const fixer = events.find((e) => e.head === "fixer" && e.type === "finished")!;
    expect(commitLinks(fixer, findRepoUrl([fixer]))).toEqual([]);
  });
});

describe("safeHref", () => {
  it("rejects non-http urls", () => {
    expect(safeHref("javascript:alert(1)")).toBeNull();
    expect(safeHref("https://a.b/c")).toBe("https://a.b/c");
    expect(safeHref(null)).toBeNull();
  });
});

describe("findRepoUrl", () => {
  it("ignores non-http repo urls", () => {
    const bad = { ...events[0], payload: { repo_url: "javascript:alert(1)" } };
    expect(findRepoUrl([bad])).toBeNull();
  });
});

describe("round handling", () => {
  it("clears approval once the next head starts and resets downstream heads on a new round", () => {
    const idx = events.findIndex((e) => e.type === "approval_needed");
    const afterNext = deriveRunState(events.slice(0, idx + 1).concat({ ...events[0], seq: 999, head: "verifier", type: "started" }));
    expect(afterNext.heads.fixer).toBe("done");
    const lastRunnerStart = events.map((e) => e.head + e.type).lastIndexOf("runnerstarted");
    if (lastRunnerStart >= 0) {
      const s = deriveRunState(events.slice(0, lastRunnerStart + 1));
      expect(s.heads.verifier).toBe("idle");
    }
  });
});
