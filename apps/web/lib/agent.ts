/**
 * Agent page rules ([PRD › Non-technical users], [PRD › Frontend (marketing site + app) and API]).
 * API data uses the generated shapes; only the "Try it" request is ours (the PRD names no route for it).
 */
import type { FeedbackIn, Finding, ProjectSummary, ReportCard, RunStatus } from "@/types/generated";

/** What the preview panel needs: the run's preview URL and its project's repo link. */
export type AgentRunInfo = Pick<RunStatus, "preview_url"> & Pick<ProjectSummary, "repo_url">;

export type Rating = "up" | "down";

/** Body for `POST /runs/{id}/try`, our assumption (flagged for Muaaz); the API may not serve it yet. */
export type TryInput = { kind: "diff"; diff: string } | { kind: "pr"; pr: string };

export interface TryResult {
  summary: string;
  findings: Finding[];
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

export const PREVIEW_ASLEEP = "This preview is asleep";

/**
 * Whether the preview's app answers `/health` in time. The sandbox's reaper stops previews after a timeout, and
 * Caddy then answers with an error, so anything but a 200 (or no answer) means it's asleep. Never throws.
 */
export async function probePreview(url: string, fetchImpl: typeof fetch = fetch, timeoutMs = 8000): Promise<"awake" | "asleep"> {
  try {
    const reply = await fetchImpl(new URL("/health", url).href, { cache: "no-store", signal: AbortSignal.timeout(timeoutMs) });
    return reply.ok ? "awake" : "asleep";
  } catch {
    return "asleep";
  }
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

/** A thumbs down becomes a new test, so it needs what was expected and the change the agent reviewed. */
export function toFeedbackIn(expectation: string, diff: string | undefined, findings: Finding[] = []): FeedbackIn | null {
  const text = expectation.trim();
  if (!text || !diff) return null;
  return { thumbs_down: true, expectation: text, diff, findings };
}

/** "app.py line 12: Logs a customer's email." */
export function findingLine(f: Finding): string {
  const where = f.file ? `${f.file}${f.line > 0 ? ` line ${f.line}` : ""}: ` : "";
  return `${where}${f.message}`;
}

/** The bar label for one round of the report card. */
export const roundLabel = (card: ReportCard) => (card.iteration === 0 ? "First round" : `Round ${card.iteration}`);

/** Share of problems caught, clamped to 0-100 for bars. */
export function caughtPercent(caught: number, total: number): number {
  if (total <= 0) return 0;
  return Math.min(100, Math.max(0, Math.round((caught / total) * 100)));
}
