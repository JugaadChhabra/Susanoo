import type { RunSummary } from "@/types/generated";

/** One rule per line; leading bullets and blank lines are ignored. [PRD › Non-technical users] */
export function parseRules(text: string): string[] {
  return text
    .split("\n")
    .map((line) => line.replace(/^\s*[-*•]\s*/, "").trim())
    .filter(Boolean);
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
