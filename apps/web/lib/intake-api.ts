import { apiFetch, getRun, TERMINAL_RUN_STATUSES } from "@/lib/api";
import type { AnswersIn, IntakeAccepted, IntakeOut, ScenariosIn } from "@/types/generated";

/** In development, `/runs/fixture/intake` walks an in-memory mock, for working on the screen without an API. */
export const isMockIntake = (runId: string) => runId === "fixture" && process.env.NODE_ENV !== "production";

const MOCK_QUESTIONS: IntakeOut["questions"] = [
  {
    id: "q1",
    text: "Should a missing login check on an internal health-check endpoint be blocked or allowed?",
    options: ["Allowed", "Blocked"],
    default: "Allowed",
    why: "Health checks are usually public, but some teams want every endpoint protected.",
  },
  {
    id: "q2",
    text: "How strict should the reviewer be about leaked personal data in logs?",
    options: ["Block the change", "Warn only"],
    default: "Block the change",
    why: "This decides whether a leaked email stops a merge or just leaves a note.",
  },
];

const MOCK_SCENARIOS: IntakeOut["scenarios"] = [
  { id: "s1", title: "A developer adds a log line that prints a customer's email. It should be blocked under No PII in logs." },
  { id: "s2", title: "A developer adds a new internal /health route with no login check. It should be allowed." },
  { id: "s3", title: "A developer renames a variable and fixes a typo in a comment. Nothing should be flagged." },
];

let mockStep: "questions" | "scenarios" | "done" = "questions";
const delay = (ms = 400) => new Promise((resolve) => setTimeout(resolve, ms));

function mockIntake(): IntakeOut {
  const base = { questions: [], scenarios: [], gate: null };
  if (mockStep === "questions") {
    return { ...base, waiting: true, kind: "questions", pause_token: "mock-1", message: "Please answer a few quick questions.", questions: MOCK_QUESTIONS };
  }
  if (mockStep === "scenarios") {
    return { ...base, waiting: true, kind: "scenarios", pause_token: "mock-2", message: "Please review these examples.", scenarios: MOCK_SCENARIOS };
  }
  return { ...base, waiting: false, kind: null, pause_token: null, message: "Nothing is waiting for you right now." };
}

/** What the run is waiting on right now, with the pause_token an answer must send back. */
export async function fetchIntake(runId: string): Promise<IntakeOut> {
  if (isMockIntake(runId)) return mockIntake();
  return apiFetch<IntakeOut>(`/runs/${encodeURIComponent(runId)}/intake`, { cache: "no-store" });
}

export interface IntakeState {
  intake: IntakeOut;
  /** The run is over, so nothing more will be asked. Only checked when nothing is waiting. */
  runFinished: boolean;
}

export async function getIntake(runId: string): Promise<IntakeState> {
  if (isMockIntake(runId)) {
    await delay();
    return { intake: mockIntake(), runFinished: false };
  }
  const [intake, run] = await Promise.all([fetchIntake(runId), getRun(runId)]);
  return { intake, runFinished: !intake.waiting && TERMINAL_RUN_STATUSES.has(run.status) };
}

const post = <T>(runId: string, path: string, body: unknown) =>
  apiFetch<T>(`/runs/${encodeURIComponent(runId)}/intake/${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

export async function submitAnswers(runId: string, body: AnswersIn): Promise<IntakeAccepted> {
  if (isMockIntake(runId)) {
    await delay();
    mockStep = "scenarios";
    return { ok: true, message: "Got your answers." };
  }
  return post<IntakeAccepted>(runId, "answers", body);
}

export async function submitScenarios(runId: string, body: ScenariosIn): Promise<IntakeAccepted> {
  if (isMockIntake(runId)) {
    await delay();
    mockStep = "done";
    return { ok: true, message: "Got your review." };
  }
  return post<IntakeAccepted>(runId, "scenarios", body);
}
