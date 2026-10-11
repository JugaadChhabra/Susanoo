/**
 * Intake shapes and rules ([PRD › Intake: questions and approved scenarios]).
 * The PRD lists the three routes but `schemas/` has no intake models yet, so these types live here
 * (like `Health` in api.ts) until Muaaz's router lands and they move to `generated.ts`.
 */
export const MAX_QUESTIONS = 5;

export interface IntakeQuestion {
  id: string;
  text: string;
  /** Multiple choice where possible; an empty list means a free-text answer. */
  options: string[];
  /** The suggested default; pre-selected so a user can accept everything. */
  default: string;
  /** One line on why the answer matters. */
  why: string;
}

/** A plain-English example scenario with its expected outcome. */
export interface ScenarioCard {
  id: string;
  description: string;
  expected: string;
}

export interface Intake {
  questions: IntakeQuestion[];
  scenarios: ScenarioCard[];
}

export type IntakeStep = "waiting" | "questions" | "scenarios";

export type ScenarioDecision =
  | { action: "pending" }
  | { action: "approve" }
  | { action: "reject" }
  | { action: "edit"; description: string; expected: string };

/** Wire body for POST /runs/{id}/intake/scenarios (the PRD gives no body, so this is our choice). */
export interface ScenarioDecisionPayload {
  scenario_id: string;
  action: "approve" | "edit" | "reject";
  description?: string;
  expected?: string;
}

/** Scenarios only exist once answers are in, so their presence decides the step. */
export function intakeStep(intake: Intake, answersSent: boolean): IntakeStep {
  if (intake.scenarios.length > 0) return "scenarios";
  if (intake.questions.length > 0 && !answersSent) return "questions";
  return "waiting";
}

/** At most five questions, each pre-filled with its suggested default. */
export function capQuestions(questions: IntakeQuestion[]): IntakeQuestion[] {
  return questions.slice(0, MAX_QUESTIONS);
}

export function initialAnswers(questions: IntakeQuestion[]): Record<string, string> {
  return Object.fromEntries(questions.map((q) => [q.id, q.default]));
}

export function isValidEdit(description: string, expected: string): boolean {
  return description.trim().length > 0 && expected.trim().length > 0;
}

/** Build unlocks only when every card has been decided and at least one is approved or edited. */
export function canBuild(cards: ScenarioCard[], decisions: Record<string, ScenarioDecision>): boolean {
  if (cards.length === 0) return false;
  let accepted = 0;
  for (const card of cards) {
    const d = decisions[card.id] ?? { action: "pending" };
    if (d.action === "pending") return false;
    if (d.action === "approve" || d.action === "edit") accepted += 1;
  }
  return accepted > 0;
}

export function countPending(cards: ScenarioCard[], decisions: Record<string, ScenarioDecision>): number {
  return cards.filter((c) => (decisions[c.id]?.action ?? "pending") === "pending").length;
}

export function toDecisionPayload(
  cards: ScenarioCard[],
  decisions: Record<string, ScenarioDecision>,
): ScenarioDecisionPayload[] {
  return cards.flatMap((card): ScenarioDecisionPayload[] => {
    const d = decisions[card.id];
    if (!d || d.action === "pending") return [];
    if (d.action === "edit") {
      return [{ scenario_id: card.id, action: "edit", description: d.description.trim(), expected: d.expected.trim() }];
    }
    return [{ scenario_id: card.id, action: d.action }];
  });
}
