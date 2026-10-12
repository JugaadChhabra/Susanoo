import { describe, expect, it } from "vitest";
import type { IntakeOut, IntakeQuestionOut, ScenarioCardOut } from "@/types/generated";
import { canBuild, capQuestions, countPending, initialAnswers, intakeStep, isValidEdit, toScenariosIn, type ScenarioDecision } from "./intake";

const q = (id: string, def = "a"): IntakeQuestionOut => ({ id, text: id, options: ["a", "b"], default: def, why: "because" });
const cards: ScenarioCardOut[] = [
  { id: "s1", title: "one" },
  { id: "s2", title: "two" },
];
const idle: IntakeOut = { waiting: false, kind: null, pause_token: null, message: "Nothing is waiting.", questions: [], scenarios: [], gate: null };
const paused = (over: Partial<IntakeOut>): IntakeOut => ({ ...idle, waiting: true, pause_token: "t1", ...over });

describe("intakeStep", () => {
  it("waits while nothing is asked", () => expect(intakeStep(idle, false)).toBe("waiting"));
  it("says finished once the run is over", () => expect(intakeStep(idle, true)).toBe("finished"));
  it("asks questions", () => expect(intakeStep(paused({ kind: "questions", questions: [q("x")] }), false)).toBe("questions"));
  it("shows scenarios", () => expect(intakeStep(paused({ kind: "scenarios", scenarios: cards }), false)).toBe("scenarios"));
  it("points at the run for a gate", () =>
    expect(intakeStep(paused({ kind: "gate", gate: { approval_id: "d1", layer: "test", hypothesis: "h", scenario_ids: [] } }), false)).toBe("gate"));
  it("shows a pause even with an empty list, but waits without a token", () => {
    expect(intakeStep(paused({ kind: "scenarios" }), false)).toBe("scenarios");
    expect(intakeStep(paused({ kind: "questions", questions: [q("x")], pause_token: null }), false)).toBe("waiting");
  });
});

describe("questions", () => {
  it("caps at five", () => expect(capQuestions(Array.from({ length: 8 }, (_, i) => q(`q${i}`)))).toHaveLength(5));
  it("pre-fills defaults", () => expect(initialAnswers([q("a", "x"), q("b", "y")])).toEqual({ a: "x", b: "y" }));
});

describe("canBuild", () => {
  const approve: ScenarioDecision = { action: "approve" };
  it("lets the run carry on when there are no cards to approve", () => expect(canBuild([], {})).toBe(true));
  it("is locked while any card is undecided", () => expect(canBuild(cards, { s1: approve })).toBe(false));
  it("is locked when everything is rejected", () => expect(canBuild(cards, { s1: { action: "reject" }, s2: { action: "reject" } })).toBe(false));
  it("unlocks with one approval and the rest decided", () => expect(canBuild(cards, { s1: approve, s2: { action: "reject" } })).toBe(true));
  it("counts an edit as accepted", () => expect(canBuild(cards, { s1: { action: "edit", title: "t" }, s2: { action: "reject" } })).toBe(true));
  it("counts pending", () => expect(countPending(cards, { s1: approve })).toBe(1));
});

describe("toScenariosIn", () => {
  it("sends the pause token, approved and rejected ids, and trimmed edits", () => {
    expect(toScenariosIn("t1", cards, { s1: { action: "edit", title: " better " }, s2: { action: "reject" } })).toEqual({
      pause_token: "t1",
      approved_ids: ["s1"],
      rejected_ids: ["s2"],
      edits: { s1: { title: "better" } },
    });
  });
  it("skips undecided cards", () =>
    expect(toScenariosIn("t1", cards, { s2: { action: "approve" } })).toEqual({ pause_token: "t1", approved_ids: ["s2"], rejected_ids: [], edits: {} }));
  it("validates edits", () => {
    expect(isValidEdit("a")).toBe(true);
    expect(isValidEdit("  ")).toBe(false);
    expect(isValidEdit("x".repeat(301))).toBe(false);
  });
});
