import { apiFetch } from "@/lib/api";
import type { AgentCard, AgentRunInfo, FeedbackPayload, ReportCard, TryInput, TryResult } from "@/lib/agent";

/** In development, `/runs/fixture/agent` uses in-memory mocks until Muaaz's agent, report and feedback routes land. */
export const isMockAgent = (runId: string) => runId === "fixture" && process.env.NODE_ENV !== "production";

const MOCK_CARD: AgentCard = {
  summary: "Reads a code change and flags problems a careful teammate would catch before it merges.",
  can_do: ["Spot leaked secrets and customer data in logs", "Notice missing login checks on new routes", "Leave a short reason with every finding"],
  cannot_do: ["Run your code or tests", "Change your code for you", "See files outside the change it is shown"],
};

const MOCK_REPORT: ReportCard = {
  headline: "Caught 9 of 10 problems, 1 false alarm. Last round: 6 of 10.",
  caught: 9,
  total: 10,
  false_alarms: 1,
  history: [
    { label: "Round 1", caught: 6, total: 10, false_alarms: 3 },
    { label: "Round 2", caught: 8, total: 10, false_alarms: 2 },
    { label: "Round 3", caught: 9, total: 10, false_alarms: 1 },
  ],
};

const MOCK_INFO: AgentRunInfo = { preview_url: null, repo_url: "https://github.com/JugaadChhabra/Susanoo" };
const delay = (ms = 400) => new Promise((resolve) => setTimeout(resolve, ms));
const base = (runId: string) => `/runs/${encodeURIComponent(runId)}`;

export async function getAgentCard(runId: string): Promise<AgentCard> {
  if (isMockAgent(runId)) {
    await delay();
    return MOCK_CARD;
  }
  return apiFetch<AgentCard>(`${base(runId)}/agent-card`, { cache: "no-store" });
}

export async function getReportCard(runId: string): Promise<ReportCard> {
  if (isMockAgent(runId)) {
    await delay();
    return MOCK_REPORT;
  }
  return apiFetch<ReportCard>(`${base(runId)}/report-card`, { cache: "no-store" });
}

export async function getAgentRunInfo(runId: string): Promise<AgentRunInfo> {
  if (isMockAgent(runId)) {
    await delay(200);
    return MOCK_INFO;
  }
  return apiFetch<AgentRunInfo>(base(runId), { cache: "no-store" });
}

export async function submitFeedback(runId: string, feedback: FeedbackPayload): Promise<void> {
  if (isMockAgent(runId)) {
    await delay();
    return;
  }
  await apiFetch<unknown>(`${base(runId)}/feedback`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(feedback),
  });
}

/** The PRD has no "try it" route; `POST /runs/{id}/try` is our assumption, flagged for Muaaz. */
export async function tryAgent(runId: string, input: TryInput): Promise<TryResult> {
  if (isMockAgent(runId)) {
    await delay(800);
    return {
      summary: "Found 1 possible problem.",
      findings: [{ text: "A log line prints a customer's email.", why: "Logs are widely readable, so personal data should never be written there." }],
    };
  }
  return apiFetch<TryResult>(`${base(runId)}/try`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}
