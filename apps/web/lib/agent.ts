/**
 * Agent page shapes and rules ([PRD › Non-technical users], [PRD › Frontend (marketing site + app) and API]).
 * The PRD lists agent-card, report-card and feedback routes but `schemas/` has no models for them yet,
 * so these types live here (like `Intake`) until Muaaz's router lands and they move to `generated.ts`.
 */

/** Plain-English summary of what the built agent does. No prompts or YAML. */
export interface AgentCard {
  summary: string;
  can_do: string[];
  cannot_do: string[];
}

export interface ReportCardRound {
  label: string;
  caught: number;
  total: number;
  false_alarms: number;
}

/** "Caught 9 of 10 problems, 1 false alarm. Last week: 6 of 10." */
export interface ReportCard {
  headline: string;
  caught: number;
  total: number;
  false_alarms: number;
  history: ReportCardRound[];
}

/** The slice of `GET /runs/{id}` this page needs. */
export interface AgentRunInfo {
  preview_url: string | null;
  repo_url: string | null;
}

export type Rating = "up" | "down";

/** Wire body for POST /runs/{id}/feedback (the PRD gives no body, so this is our choice). */
export interface FeedbackPayload {
  rating: Rating;
  /** The "it should have..." text; required for a thumbs down because it becomes a new test. */
  expectation?: string;
  /** What the agent was shown when the user tried it, so the new test can reuse it. */
  diff?: string;
}

export type TryInput = { kind: "diff"; diff: string } | { kind: "pr"; pr: string };

export interface TryResult {
  summary: string;
  findings: { text: string; why: string }[];
}

export const MAX_DIFF_CHARS = 20000;

/** Previews are embedded in an iframe, so only http(s) URLs are allowed. */
export function safePreviewUrl(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" || parsed.protocol === "http:" ? parsed.href : null;
  } catch {
    return null;
  }
}

/** Use the API's preview URL, else derive `run-<id>.<domain>` from the configured domain. */
export function resolvePreviewUrl(runId: string, info: AgentRunInfo | null, domain = process.env.NEXT_PUBLIC_PREVIEW_DOMAIN): string | null {
  const fromApi = safePreviewUrl(info?.preview_url);
  if (fromApi) return fromApi;
  if (!domain || !/^[a-z0-9.-]+$/i.test(domain) || !/^[a-z0-9-]+$/i.test(runId)) return null;
  return `https://run-${runId}.${domain}`;
}

/** Accepts a GitHub PR link or `owner/repo#123`. */
export function parsePrRef(input: string): string | null {
  const value = input.trim();
  const isUrl = /^https:\/\/github\.com\/[\w.-]+\/[\w.-]+\/pull\/\d+\/?$/.test(value);
  return isUrl || /^[\w.-]+\/[\w.-]+#\d+$/.test(value) ? value : null;
}

export function toTryInput(mode: "diff" | "pr", text: string): TryInput | null {
  if (mode === "diff") {
    const diff = text.trim();
    return diff && diff.length <= MAX_DIFF_CHARS ? { kind: "diff", diff } : null;
  }
  const pr = parsePrRef(text);
  return pr ? { kind: "pr", pr } : null;
}

/** A thumbs down must say what was expected, since that becomes a test. */
export function toFeedback(rating: Rating, expectation: string, diff?: string): FeedbackPayload | null {
  const text = expectation.trim();
  if (rating === "down" && !text) return null;
  return { rating, ...(text ? { expectation: text } : {}), ...(diff ? { diff } : {}) };
}

export function problemsLine(caught: number, total: number, falseAlarms: number): string {
  const alarms = falseAlarms === 1 ? "1 false alarm" : `${falseAlarms} false alarms`;
  return `Caught ${caught} of ${total} problems, ${alarms}.`;
}

/** Share of problems caught, clamped to 0-100 for bars. */
export function caughtPercent(caught: number, total: number): number {
  if (total <= 0) return 0;
  return Math.min(100, Math.max(0, Math.round((caught / total) * 100)));
}
