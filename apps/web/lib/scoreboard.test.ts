import { describe, expect, it } from "vitest";
import type { RunEvent } from "@/types/generated";
import fixture from "./fixtures/run-events.json";
import { deriveBudget, deriveLedger, deriveMetrics, derivePendingApproval, gateApproval } from "./scoreboard";

const events = fixture as RunEvent[];
const upTo = (seq: number) => events.filter((e) => e.seq <= seq);

describe("deriveMetrics", () => {
  it("is empty before any score arrives", () => {
    expect(deriveMetrics(upTo(20))).toEqual([]);
  });
  it("adds one point per metrics event, in round order", () => {
    expect(deriveMetrics(upTo(33)).map((m) => m.iteration)).toEqual([0, 1]);
    const all = deriveMetrics(events);
    expect(all).toHaveLength(3);
    expect(all[2].heldout_f1).toBeCloseTo(0.8108);
  });
  it("ignores malformed payloads", () => {
    const bad: RunEvent = { ...events[20], payload: { iteration: 1, recall: "high" } };
    expect(deriveMetrics([bad])).toEqual([]);
  });
});

describe("deriveBudget", () => {
  it("reads the cap from the first system event and spend from the latest score", () => {
    expect(deriveBudget(upTo(1))).toEqual({ capUsd: 2, spentUsd: 0, iterationCap: 5 });
    expect(deriveBudget(events)).toEqual({ capUsd: 2, spentUsd: 1.18, iterationCap: 5 });
  });
  it("has no cap when none was announced", () => {
    expect(deriveBudget([]).capUsd).toBeNull();
  });
});

describe("deriveLedger", () => {
  it("is empty until the Diagnostician reports", () => {
    expect(deriveLedger(upTo(21))).toEqual([]);
  });
  it("lists each diagnosis as untried before the Fixer starts", () => {
    const l = deriveLedger(upTo(23));
    expect(l.map((r) => [r.id, r.outcome])).toEqual([["dx-1", "pending"], ["dx-2", "pending"]]);
  });
  it("tracks trying, kept and rolled back", () => {
    expect(deriveLedger(upTo(26))[0].outcome).toBe("testing");
    const l = deriveLedger(upTo(32));
    expect(l[0].outcome).toBe("rolled_back");
    expect(l[1].outcome).toBe("kept");
    expect(l[1].heldoutBefore).toBeCloseTo(0.5806);
    expect(l[1].heldoutAfter).toBeCloseTo(0.7059);
    expect(l[1].linesChanged).toBe(11);
  });
  it("shows a test-layer fix as awaiting approval, then kept", () => {
    expect(deriveLedger(upTo(36)).at(-1)?.outcome).toBe("awaiting_approval");
    const last = deriveLedger(events).at(-1);
    expect(last?.layer).toBe("test");
    expect(last?.outcome).toBe("kept");
  });
  it("records a rejected approval", () => {
    const rejected: RunEvent = { ...events[36], payload: { patch_id: "p3", decision: "rejected" } };
    expect(deriveLedger([...upTo(36), rejected]).at(-1)?.outcome).toBe("rejected");
  });
});

describe("derivePendingApproval", () => {
  it("is null normally and set while the gate is open", () => {
    expect(derivePendingApproval(upTo(35))).toBeNull();
    const p = derivePendingApproval(upTo(36));
    expect(p?.patchId).toBe("p3");
    expect(p?.summary).toMatch(/warn/);
    expect(p?.hypothesis).toMatch(/Warn/);
  });
  it("clears once the decision lands", () => {
    expect(derivePendingApproval(upTo(37))).toBeNull();
    expect(derivePendingApproval(events)).toBeNull();
  });
});

describe("gateApproval", () => {
  it("uses the gate's id and hypothesis with the newest approval event's words", () => {
    const card = gateApproval({ approval_id: "dx-3", layer: "test", hypothesis: "The expected severity is wrong.", scenario_ids: ["s5"] }, upTo(36));
    expect(card.patchId).toBe("dx-3");
    expect(card.hypothesis).toBe("The expected severity is wrong.");
    expect(card.summary).toBe(events.find((e) => e.seq === 36)?.message);
  });
  it("falls back to plain words when no event has arrived yet", () => {
    const card = gateApproval({ approval_id: "d1", layer: "", hypothesis: "", scenario_ids: [] }, []);
    expect(card.layer).toBe("test");
    expect(card.hypothesis).toBeNull();
    expect(card.summary).toMatch(/tests/);
  });
});
