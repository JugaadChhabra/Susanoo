import { apiFetch } from "@/lib/api";
import type { Intake, ScenarioDecisionPayload } from "@/lib/intake";

/** In development, `/runs/fixture/intake` uses an in-memory mock until Muaaz's intake router lands. */
export const isMockIntake = (runId: string) => runId === "fixture" && process.env.NODE_ENV !== "production";

const MOCK_QUESTIONS: Intake["questions"] = [
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

const MOCK_SCENARIOS: Intake["scenarios"] = [
  {
    id: "s1",
    description: "A developer adds a log line that prints a customer's email.",
    expected: "Blocked under No PII in logs.",
  },
  {
    id: "s2",
    description: "A developer adds a new internal /health route with no login check.",
    expected: "Allowed, because health checks are public by default.",
  },
  {
    id: "s3",
    description: "A developer renames a variable and fixes a typo in a comment.",
    expected: "Nothing is flagged.",
  },
];

let mockAnswered = false;
const delay = (ms = 400) => new Promise((resolve) => setTimeout(resolve, ms));

export async function getIntake(runId: string): Promise<Intake> {
  if (isMockIntake(runId)) {
    await delay();
    return { questions: MOCK_QUESTIONS, scenarios: mockAnswered ? MOCK_SCENARIOS : [] };
  }
  return apiFetch<Intake>(`/runs/${encodeURIComponent(runId)}/intake`, { cache: "no-store" });
}

const post = (runId: string, path: string, body: unknown) =>
  apiFetch<unknown>(`/runs/${encodeURIComponent(runId)}/intake/${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

export async function submitAnswers(runId: string, answers: Record<string, string>): Promise<void> {
  if (isMockIntake(runId)) {
    await delay();
    mockAnswered = true;
    return;
  }
  await post(runId, "answers", { answers });
}

export async function submitScenarios(runId: string, decisions: ScenarioDecisionPayload[]): Promise<void> {
  if (isMockIntake(runId)) {
    await delay();
    return;
  }
  await post(runId, "scenarios", { decisions });
}
