import { ApiError, apiFetch, getRun, listProjects } from "@/lib/api";
import type { AgentRunInfo, TryInput, TryResult } from "@/lib/agent";
import type { AgentCard, FeedbackIn, FeedbackOut, ReportCards } from "@/types/generated";

/** In development, `/runs/fixture/agent` uses in-memory mocks, for working on the page without an API. */
export const isMockAgent = (runId: string) => runId === "fixture" && process.env.NODE_ENV !== "production";

const MOCK_CARD: AgentCard = {
  what_it_does: "Reads a code change and flags problems a careful teammate would catch before it merges.",
  can_do: ["Spot leaked secrets and customer data in logs", "Notice missing login checks on new routes", "Leave a short reason with every finding"],
  wont_do: ["Run your code or tests", "Change your code for you", "See files outside the change it is shown"],
};

const mockRound = (iteration: number, caught: number, falseAlarms: number): ReportCards["history"][number] => ({
  iteration,
  basis: "heldout",
  score: { basis: "heldout", caught, problems: 10, false_alarms: falseAlarms },
  score_percent: caught * 10,
  headline: `Caught ${caught} of 10 problems, ${falseAlarms} false ${falseAlarms === 1 ? "alarm" : "alarms"}.`,
  basis_note: "Scored on examples it never trained on.",
  comparison: null,
  trend: iteration === 0 ? "new" : "better",
  acceptance: null,
  unscored: null,
  lines: [],
});

const MOCK_REPORT: ReportCards = {
  run_id: "fixture",
  message: "Caught 9 of 10 problems, 1 false alarm.",
  latest: { ...mockRound(2, 9, 1), comparison: "Before: 8 of 10." },
  history: [mockRound(0, 6, 3), mockRound(1, 8, 2), mockRound(2, 9, 1)],
};

const delay = (ms = 400) => new Promise((resolve) => setTimeout(resolve, ms));
const base = (runId: string) => `/runs/${encodeURIComponent(runId)}`;

/** The agent card, or null while the agent isn't planned yet (the API answers 409 until then). */
export async function getAgentCard(runId: string): Promise<AgentCard | null> {
  if (isMockAgent(runId)) {
    await delay();
    return MOCK_CARD;
  }
  try {
    return await apiFetch<AgentCard>(`${base(runId)}/agent-card`, { cache: "no-store" });
  } catch (e) {
    if (e instanceof ApiError && e.status === 409) return null;
    throw e;
  }
}

export async function getReportCards(runId: string): Promise<ReportCards> {
  if (isMockAgent(runId)) {
    await delay();
    return MOCK_REPORT;
  }
  return apiFetch<ReportCards>(`${base(runId)}/report-card`, { cache: "no-store" });
}

/** The run's preview URL, plus its project's repo link (the run status doesn't carry one). */
export async function getAgentRunInfo(runId: string): Promise<AgentRunInfo> {
  if (isMockAgent(runId)) {
    await delay(200);
    return { preview_url: null, repo_url: "https://github.com/JugaadChhabra/Susanoo" };
  }
  // The repo link is a nice-to-have: if the project list fails, still show the preview.
  const [run, projects] = await Promise.all([getRun(runId), listProjects().catch(() => [])]);
  const project = projects.find((p) => p.id === run.project_id);
  return { preview_url: run.preview_url, repo_url: project?.repo_url ?? null };
}

export async function submitFeedback(runId: string, feedback: FeedbackIn): Promise<FeedbackOut> {
  if (isMockAgent(runId)) {
    await delay();
    return { accepted: true, message: "Saved as a new check for this agent.", code: null, scenario_id: "mock", title: feedback.expectation };
  }
  return apiFetch<FeedbackOut>(`${base(runId)}/feedback`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(feedback),
  });
}

/**
 * True when the API has no "try it" route yet, so the page can say so instead of showing an error. FastAPI
 * answers an unknown route with a bare "Not Found"; a missing run has its own plain-English 404.
 */
export const isTryUnavailable = (e: unknown) =>
  e instanceof ApiError && (e.status === 405 || (e.status === 404 && e.message === "Not Found"));

/** The PRD has no "try it" route; `POST /runs/{id}/try` is our assumption, flagged for Muaaz. */
export async function tryAgent(runId: string, input: TryInput): Promise<TryResult> {
  if (isMockAgent(runId)) {
    await delay(800);
    return {
      summary: "Found 1 possible problem.",
      findings: [{ file: "app/orders.py", line: 12, rule_id: "no-pii-in-logs", severity: "block", message: "A log line prints a customer's email." }],
    };
  }
  return apiFetch<TryResult>(`${base(runId)}/try`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}
