/**
 * Intake rules ([PRD › Intake: questions and approved scenarios]) on the shapes of GET /runs/{id}/intake.
 * The run pauses twice before building: once for questions, once for example scenarios. A later pause
 * ("gate") is a test-layer change, decided on the run view.
 */
import type { IntakeOut, IntakeQuestionOut, ScenarioCardOut, ScenariosIn } from "@/types/generated";

export const MAX_QUESTIONS = 5;

export type IntakeStep = "waiting" | "questions" | "scenarios" | "gate" | "finished";

/** What the intake screen shows for the run's current pause. `finished` comes from the run's status. */
export function intakeStep(intake: IntakeOut, runFinished: boolean): IntakeStep {
  if (intake.waiting && intake.pause_token) {
    // Even with an empty list the run waits on this step, so show it (it can be sent as is).
    if (intake.kind === "questions") return "questions";
    if (intake.kind === "scenarios") return "scenarios";
    if (intake.kind === "gate" && intake.gate) return "gate";
  }
  return runFinished ? "finished" : "waiting";
}

export type ScenarioDecision =
  | { action: "pending" }
  | { action: "approve" }
  | { action: "reject" }
  | { action: "edit"; title: string };

/** At most five questions, each pre-filled with its suggested default. */
export function capQuestions(questions: IntakeQuestionOut[]): IntakeQuestionOut[] {
  return questions.slice(0, MAX_QUESTIONS);
}

export function initialAnswers(questions: IntakeQuestionOut[]): Record<string, string> {
  return Object.fromEntries(questions.map((q) => [q.id, q.default]));
}

export const MAX_TITLE_CHARS = 300;

export function isValidEdit(title: string): boolean {
  const text = title.trim();
  return text.length > 0 && text.length <= MAX_TITLE_CHARS;
}

/**
 * Build unlocks only when every card has been decided and at least one is approved or edited.
 * With no cards at all there is nothing to approve, so the run may carry on.
 */
export function canBuild(cards: ScenarioCardOut[], decisions: Record<string, ScenarioDecision>): boolean {
  if (cards.length === 0) return true;
  let accepted = 0;
  for (const card of cards) {
    const d = decisions[card.id] ?? { action: "pending" };
    if (d.action === "pending") return false;
    if (d.action === "approve" || d.action === "edit") accepted += 1;
  }
  return accepted > 0;
}

export function countPending(cards: ScenarioCardOut[], decisions: Record<string, ScenarioDecision>): number {
  return cards.filter((c) => (decisions[c.id]?.action ?? "pending") === "pending").length;
}

/** Body for POST /runs/{id}/intake/scenarios. An edited card is approved with its new title. */
export function toScenariosIn(
  pauseToken: string,
  cards: ScenarioCardOut[],
  decisions: Record<string, ScenarioDecision>,
): ScenariosIn {
  const body: Required<ScenariosIn> = { pause_token: pauseToken, approved_ids: [], rejected_ids: [], edits: {} };
  for (const card of cards) {
    const d = decisions[card.id];
    if (!d || d.action === "pending") continue;
    if (d.action === "reject") {
      body.rejected_ids.push(card.id);
      continue;
    }
    body.approved_ids.push(card.id);
    if (d.action === "edit") body.edits[card.id] = { title: d.title.trim() };
  }
  return body;
}
