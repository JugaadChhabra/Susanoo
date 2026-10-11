import { HEADS, type HeadName } from "./run-state";

/** Content for the public landing page. [PRD › Frontend (marketing site + app) and API › Surface 1: Marketing site] */

export type Phase = "Intake" | "Build" | "Simulate" | "Diagnose" | "Fix" | "Verify";

const HEAD_DETAIL: Record<HeadName, { phase: Phase; detail: string }> = {
  planner: { phase: "Intake", detail: "Asks a few questions, then turns your spec into rules and a plan you approve." },
  builder: { phase: "Build", detail: "Builds the web app from a proven template and commits it to its own repo." },
  agent_smith: { phase: "Build", detail: "Writes the agent inside it: its instructions, its rules and its tools." },
  simulator: { phase: "Simulate", detail: "Invents test cases with known answers, and locks some away for the final exam." },
  runner: { phase: "Simulate", detail: "Starts the app in a sandbox, runs every test and scores the results." },
  diagnostician: { phase: "Diagnose", detail: "Reads the traces of each failure and names the layer that caused it." },
  fixer: { phase: "Fix", detail: "Makes the smallest change to that one layer, and nothing else." },
  verifier: { phase: "Verify", detail: "Reruns everything. Keeps the fix only if scores rise with no new failures." },
};

export const HEAD_STEPS = HEADS.map((h) => ({ ...h, ...HEAD_DETAIL[h.id] }));

/** The head the "How it works" animation lights up after `index`. */
export function nextHeadIndex(index: number): number {
  return (index + 1) % HEAD_STEPS.length;
}

export const USE_CASES: { title: string; live: boolean; body: string }[] = [
  {
    title: "Code review",
    live: true,
    body: "A reviewer that enforces your team's own rules on every pull request: no personal data in logs, auth on every endpoint, no raw SQL.",
  },
  { title: "Support", live: false, body: "Agents that answer customers from your help docs and hand off to a person when they should." },
  { title: "Operations", live: false, body: "Agents that take repetitive back-office work off your team's plate, with every step checked." },
  { title: "Compliance", live: false, body: "Agents that check documents and changes against your policies and show their reasoning." },
];

export const TRUST_POINTS: { title: string; body: string }[] = [
  {
    title: "Graded on tests it never sees",
    body: "About a third of the test cases are held back. The fixing heads can't read them, and the score we report comes from them.",
  },
  {
    title: "A person approves test changes",
    body: "If Susanoo thinks a test's expected answer is wrong, the run pauses until you agree. It can't grade its own homework.",
  },
  {
    title: "Runs in a sandbox",
    body: "Generated apps run in locked-down containers with CPU and memory limits and no access to the machine underneath.",
  },
  {
    title: "Nothing is written until you say so",
    body: "Connectors read real data, but actions like posting a comment are recorded as \"would have done\" until you switch them on.",
  },
];

export const CONNECTORS: { name: string; live: boolean; body: string }[] = [
  { name: "GitHub", live: true, body: "Reads pull requests from one repo you choose. Comments stay in dry run until you allow them." },
  { name: "Anthropic", live: true, body: "Use your own API key, or start on ours with a small spending cap." },
  { name: "Slack", live: false, body: "Post review summaries to a channel." },
  { name: "Jira", live: false, body: "Link findings to tickets." },
  { name: "Linear", live: false, body: "Link findings to issues." },
  { name: "Postgres", live: false, body: "Read-only access to your data." },
];

export const REPO_URL = "https://github.com/JugaadChhabra/Susanoo";

export const FOOTER_LINKS: { label: string; href: string }[] = [
  { label: "Source on GitHub", href: REPO_URL },
  { label: "#neatHack", href: "https://x.com/hashtag/neatHack" },
  { label: "muaazshaikh.com", href: "https://muaazshaikh.com" },
  { label: "jugaadchhabra.dev", href: "https://jugaadchhabra.dev" },
];

/** Absolute base URL for metadata and OG images. */
export function siteUrl(env: { NEXT_PUBLIC_SITE_URL?: string; VERCEL_PROJECT_PRODUCTION_URL?: string }): URL {
  if (env.NEXT_PUBLIC_SITE_URL) return new URL(env.NEXT_PUBLIC_SITE_URL);
  if (env.VERCEL_PROJECT_PRODUCTION_URL) return new URL(`https://${env.VERCEL_PROJECT_PRODUCTION_URL}`);
  return new URL("http://localhost:3000");
}

export const SITE_TITLE = "Susanoo: AI agents you can prove work";
export const SITE_DESCRIPTION =
  "Describe an AI agent in plain English. Susanoo builds it, tests it against cases it never saw, finds why it fails, fixes the right part and shows you the traces.";
