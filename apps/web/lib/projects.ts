import type { RunQuota, RunSummary } from "@/types/generated";

/** One rule per line; a leading "- ", "* " or "• " bullet and blank lines are ignored. [PRD › Non-technical users] */
export function parseRules(text: string): string[] {
  return text
    .split("\n")
    .map((line) => line.replace(/^\s*[-*•]\s+/, "").trim())
    .filter(Boolean);
}

// Mirror the API's limits (apps/api/susanoo_api/models.py) so people get a plain-English hint before saving.
export const MAX_RULES = 30;
export const MAX_RULE_LENGTH = 500;

/** A plain-English problem with these rules, or null if they can be saved. */
export function rulesProblem(rules: string[]): string | null {
  if (rules.length > MAX_RULES) return `Please keep it to ${MAX_RULES} rules or fewer (you have ${rules.length}).`;
  const long = rules.findIndex((r) => r.length > MAX_RULE_LENGTH);
  if (long !== -1) return `Rule ${long + 1} is too long. Please keep each rule under ${MAX_RULE_LENGTH} characters.`;
  return null;
}

export const rulesToText = (rules: string[]) => rules.join("\n");

/** Held-out F1 as a percentage; a dash until the first round is scored. */
export const formatScore = (f1: number | null) => (f1 === null ? "—" : `${Math.round(f1 * 100)}%`);

const usd = (n: number) => `$${n.toFixed(2)}`;
export const formatCost = (run: Pick<RunSummary, "cost_usd" | "cost_cap_usd">) =>
  `${usd(run.cost_usd)} of ${usd(run.cost_cap_usd)}`;

const STATUS_LABELS: Record<RunSummary["status"], string> = {
  running: "Running",
  awaiting_approval: "Needs your approval",
  done: "Finished",
  failed: "Stopped with a problem",
};

export const runStatusLabel = (status: RunSummary["status"]) => STATUS_LABELS[status];

/** When the daily counts start over, in the viewer's own clock (e.g. "5:30 AM"). */
export const formatResetTime = (resetsAt: string, timeZone?: string) =>
  new Date(resetsAt).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", timeZone });

/** Runs left today, in plain English. [PRD › Connectors and API budget › Anthropic budget] */
export function runsLeftLabel(quota: RunQuota, timeZone?: string): string {
  const runs = quota.runs_per_day === 1 ? "run" : "runs";
  const resets = formatResetTime(quota.resets_at, timeZone);
  if (quota.paused) {
    return `New runs are paused until ${resets} because Susanoo hit its daily spending limit. The demo still works.`;
  }
  if (quota.runs_left_today === 0) return `You've used all ${quota.runs_per_day} ${runs} for today. More at ${resets}.`;
  return `${quota.runs_left_today} of ${quota.runs_per_day} ${runs} left today`;
}

export interface ExampleSpec {
  title: string;
  spec: string;
  rules: string[];
}

/** The hero spec (specs/code-review.md) and the backup (specs/backup.md), with their rules split out as sentences. */
export const EXAMPLE_SPECS: ExampleSpec[] = [
  {
    title: "Code reviewer",
    spec: "Build a code review app for our Python services. Developers paste a diff or a PR link and get a review. Severity: block, warn, or nit.",
    rules: [
      "Never log PII.",
      "Every FastAPI endpoint needs auth.",
      "No raw SQL string formatting.",
      "No secrets in code.",
      "Flag missing error handling on external calls.",
    ],
  },
  {
    title: "Test reviewer",
    spec: "Build a review app for the tests in our Python services. Developers paste a diff of test files and get a review. Severity: block, warn, or nit.",
    rules: [
      "Every test must assert something.",
      "No time.sleep in tests.",
      "Skipped tests need a reason.",
      "Unit tests must not make real network calls.",
      "Never swallow exceptions in a test with except: pass.",
    ],
  },
];
