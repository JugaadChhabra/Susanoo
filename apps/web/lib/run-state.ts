import type { RunEvent } from "@/types/generated";

export type HeadName = Exclude<RunEvent["head"], "system">;

/** The eight heads in graph order. */
export const HEADS: { id: HeadName; label: string; blurb: string }[] = [
  { id: "planner", label: "Planner", blurb: "Understands your spec" },
  { id: "builder", label: "Builder", blurb: "Builds the agent and app" },
  { id: "agent_smith", label: "Agent Smith", blurb: "Hardens the agent" },
  { id: "simulator", label: "Simulator", blurb: "Invents test cases" },
  { id: "runner", label: "Runner", blurb: "Runs the tests" },
  { id: "diagnostician", label: "Diagnostician", blurb: "Finds what broke" },
  { id: "fixer", label: "Fixer", blurb: "Patches the right layer" },
  { id: "verifier", label: "Verifier", blurb: "Proves the fix" },
];

export type HeadStatus = "idle" | "active" | "done" | "error" | "approval" | "question";
export type RunStatus = "waiting" | "running" | "paused" | "complete" | "failed";

export interface RunSnapshot {
  heads: Record<HeadName, HeadStatus>;
  /** The head currently working (glows), if any. */
  activeHead: HeadName | null;
  status: RunStatus;
  iteration: number;
}

const isHead = (h: RunEvent["head"]): h is HeadName => h !== "system";

/** Fold an ordered event array into what the graph needs. Pure, so live, fixture and demo share it. */
export function deriveRunState(events: RunEvent[]): RunSnapshot {
  const heads = Object.fromEntries(HEADS.map((h) => [h.id, "idle"])) as Record<HeadName, HeadStatus>;
  let activeHead: HeadName | null = null;
  let status: RunStatus = "waiting";
  let iteration = 0;

  for (const event of events) {
    if (!isHead(event.head)) {
      if (event.type === "started") status = "running";
      else if (event.type === "finished") status = "complete";
      else if (event.type === "error") status = "failed";
      continue;
    }
    const head = event.head;
    if (status === "waiting") status = "running";
    switch (event.type) {
      case "started":
        // A new head starting means any pending approval/question was answered.
        for (const h of HEADS) if (heads[h.id] === "approval" || heads[h.id] === "question") heads[h.id] = "done";
        // A new test round: the downstream heads start fresh.
        if (head === "runner") for (const id of ["diagnostician", "fixer", "verifier"] as const) heads[id] = "idle";
        heads[head] = "active";
        activeHead = head;
        if (head === "runner") iteration += 1;
        break;
      case "progress":
        if (heads[head] === "idle" || heads[head] === "done") heads[head] = "active";
        if (heads[head] === "active") activeHead = head;
        break;
      case "finished":
        heads[head] = "done";
        if (activeHead === head) activeHead = null;
        break;
      case "error":
        heads[head] = "error";
        if (activeHead === head) activeHead = null;
        break;
      case "approval_needed":
        heads[head] = "approval";
        activeHead = null;
        break;
      case "question_needed":
        heads[head] = "question";
        activeHead = null;
        break;
      default:
        break;
    }
  }

  if (status === "running" && Object.values(heads).some((s) => s === "approval" || s === "question")) {
    status = "paused";
  }
  return { heads, activeHead, status, iteration };
}

/** Friendly labels for the diagnosis layers ([PRD › Non-technical users]). */
export const LAYER_LABELS: Record<string, string> = {
  code: "App code",
  tool: "Agent's tools",
  prompt_policy: "Agent's rules",
  test: "The test itself",
};

export interface EventLink {
  label: string;
  href: string;
}

const SHA_KEYS: [string, string][] = [
  ["commit_sha", "Commit"],
  ["head_commit", "Commit"],
  ["revert_commit", "Revert commit"],
];

export function findRepoUrl(events: RunEvent[]): string | null {
  for (const e of events) {
    const url = e.payload.repo_url;
    if (typeof url === "string") {
      const safe = safeHref(url);
      if (safe) return safe.replace(/\/$/, "");
    }
  }
  return null;
}

/** Commit links for one event; needs the repo URL announced earlier in the run. */
export function commitLinks(event: RunEvent, repo: string | null): EventLink[] {
  if (!repo) return [];
  const sources: Record<string, unknown>[] = [event.payload];
  const patch = event.payload.patch;
  if (patch && typeof patch === "object") sources.push(patch as Record<string, unknown>);
  const links: EventLink[] = [];
  for (const src of sources) {
    for (const [key, label] of SHA_KEYS) {
      const sha = src[key];
      if (typeof sha === "string" && /^[0-9a-f]{7,40}$/i.test(sha)) {
        links.push({ label: `${label} ${sha.slice(0, 7)}`, href: `${repo}/commit/${sha}` });
      }
    }
  }
  return links;
}

/** Only follow http(s) links the API gave us. */
export function safeHref(url: string | null): string | null {
  if (!url) return null;
  try {
    const u = new URL(url);
    return u.protocol === "https:" || u.protocol === "http:" ? u.href : null;
  } catch {
    return null;
  }
}
