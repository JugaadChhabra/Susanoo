import { describe, expect, it } from "vitest";
import {
  canBuild,
  capQuestions,
  countPending,
  initialAnswers,
  intakeStep,
  isValidEdit,
  toDecisionPayload,
  type Intake,
  type IntakeQuestion,
  type ScenarioCard,
  type ScenarioDecision,
} from "./intake";

const q = (id: string, def = "a"): IntakeQuestion => ({ id, text: id, options: ["a", "b"], default: def, why: "because" });
const cards: ScenarioCard[] = [
  { id: "s1", description: "one", expected: "blocked" },
  { id: "s2", description: "two", expected: "allowed" },
];

describe("intakeStep", () => {
  const empty: Intake = { questions: [], scenarios: [] };
  it("waits when nothing is ready", () => expect(intakeStep(empty, false)).toBe("waiting"));
  it("asks questions first", () => expect(intakeStep({ ...empty, questions: [q("x")] }, false)).toBe("questions"));
  it("waits for scenarios once answers are sent", () => expect(intakeStep({ ...empty, questions: [q("x")] }, true)).toBe("waiting"));
  it("shows scenarios whenever they exist", () => expect(intakeStep({ questions: [q("x")], scenarios: cards }, false)).toBe("scenarios"));
});

describe("questions", () => {
  it("caps at five", () => expect(capQuestions(Array.from({ length: 8 }, (_, i) => q(`q${i}`)))).toHaveLength(5));
  it("pre-fills defaults", () => expect(initialAnswers([q("a", "x"), q("b", "y")])).toEqual({ a: "x", b: "y" }));
});

describe("canBuild", () => {
  const approve: ScenarioDecision = { action: "approve" };
  it("is locked with no cards", () => expect(canBuild([], {})).toBe(false));
  it("is locked while any card is undecided", () => expect(canBuild(cards, { s1: approve })).toBe(false));
  it("is locked when everything is rejected", () => expect(canBuild(cards, { s1: { action: "reject" }, s2: { action: "reject" } })).toBe(false));
  it("unlocks with one approval and the rest decided", () => expect(canBuild(cards, { s1: approve, s2: { action: "reject" } })).toBe(true));
  it("counts an edit as accepted", () =>
    expect(canBuild(cards, { s1: { action: "edit", description: "d", expected: "e" }, s2: { action: "reject" } })).toBe(true));
  it("counts pending", () => expect(countPending(cards, { s1: approve })).toBe(1));
});

describe("toDecisionPayload", () => {
  it("skips pending and trims edits", () => {
    expect(toDecisionPayload(cards, { s1: { action: "edit", description: " d ", expected: " e " } })).toEqual([
      { scenario_id: "s1", action: "edit", description: "d", expected: "e" },
    ]);
  });
  it("sends approve and reject", () =>
    expect(toDecisionPayload(cards, { s1: { action: "approve" }, s2: { action: "reject" } })).toEqual([
      { scenario_id: "s1", action: "approve" },
      { scenario_id: "s2", action: "reject" },
    ]));
  it("validates edits", () => {
    expect(isValidEdit("a", "b")).toBe(true);
    expect(isValidEdit("  ", "b")).toBe(false);
  });
});
