import { getAccessToken } from "@/lib/supabase";
import type {
  ApprovalIn,
  ApprovalOut,
  ConnectionSummary,
  ConnectionTest,
  ConnectorInfo,
  NewConnection,
  NewProject,
  ProjectRules,
  ProjectSummary,
  RunQuota,
  RunStatus,
} from "@/types/generated";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

/** Shape of every API error: {"error": {"code", "message"}}. */
export interface ApiErrorBody {
  error: { code: string; message: string };
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message);
  }
}

// /health is not a Pydantic contract, so its type lives here, not in generated.ts.
export interface ServiceStatus {
  status: string;
  message: string;
}

export interface Health {
  status: "ok" | "degraded";
  supabase: ServiceStatus;
}

/** /health and /demo/* are public; they never need (and must not fail on) a stale token. */
export const isPublicApiPath = (path: string) => path === "/health" || path.startsWith("/demo/");

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    // Logged-in browsers send their Supabase token; the API rejects protected routes without it.
    const token = isPublicApiPath(path) ? null : await getAccessToken();
    const headers = new Headers(init?.headers);
    if (token) headers.set("Authorization", `Bearer ${token}`);
    response = await fetch(`${API_URL}${path}`, { ...init, headers });
  } catch {
    throw new ApiError(0, "network_error", "We couldn't reach the server.");
  }
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as ApiErrorBody | null;
    throw new ApiError(
      response.status,
      body?.error.code ?? "http_error",
      body?.error.message ?? "Something went wrong.",
    );
  }
  // Some POST routes may answer with no body (204 or empty 200).
  const text = await response.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

export const getHealth = () => apiFetch<Health>("/health", { cache: "no-store" });

const sendJson = (method: "POST" | "PUT", body: unknown): RequestInit => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

/** The signed-in user's projects with their runs, newest first. */
export const listProjects = () => apiFetch<ProjectSummary[]>("/projects", { cache: "no-store" });

export const createProject = (body: NewProject) => apiFetch<ProjectSummary>("/projects", sendJson("POST", body));

export const updateProjectRules = (projectId: string, body: ProjectRules) =>
  apiFetch<ProjectSummary>(`/projects/${encodeURIComponent(projectId)}/rules`, sendJson("PUT", body));

/** How many runs the signed-in user can still start today. */
export const getRunQuota = () => apiFetch<RunQuota>("/me/quota", { cache: "no-store" });

/** Available connectors with their setup guides, and the signed-in user's connection to each, if any. */
export const listConnectors = () => apiFetch<ConnectorInfo[]>("/connectors", { cache: "no-store" });

/** Save a credential (it goes to Vault and is never sent back). Saving again replaces the old one. */
export const saveConnection = (body: NewConnection) =>
  apiFetch<ConnectionSummary>("/connections", sendJson("POST", body));

export const testConnection = (connectionId: string) =>
  apiFetch<ConnectionTest>(`/connections/${encodeURIComponent(connectionId)}/test`, { method: "POST" });

/** Start a run of a project (checks the daily quota, then queues it for a worker). */
export const startRun = (projectId: string) =>
  apiFetch<RunStatus>(`/projects/${encodeURIComponent(projectId)}/runs`, { method: "POST" });

export const getRun = (runId: string) => apiFetch<RunStatus>(`/runs/${encodeURIComponent(runId)}`, { cache: "no-store" });

/** Statuses after which a run sends no more events. */
export const TERMINAL_RUN_STATUSES: ReadonlySet<RunStatus["status"]> = new Set(["done", "failed", "cancelled"]);

/** Decide a gated (test-layer) change. `approvalId` is the gate's `approval_id` from GET /runs/{id}/intake. */
export const decideApproval = (runId: string, approvalId: string, body: ApprovalIn) =>
  apiFetch<ApprovalOut>(
    `/runs/${encodeURIComponent(runId)}/approvals/${encodeURIComponent(approvalId)}`,
    sendJson("POST", body),
  );

/**
 * Answers and approvals carry the pause_token of the step the user saw. A 409 means that step is gone
 * (answered in another tab, a double click, or the run moved on), so the screen says so and reloads it.
 */
export const STEP_MOVED_ON =
  "This step has moved on: it was already answered, or the run went ahead without it. We've reloaded it so you see where things are now.";

export const isStepMovedOn = (e: unknown) => e instanceof ApiError && e.status === 409;

export const revokeConnection = (connectionId: string) =>
  apiFetch<void>(`/connections/${encodeURIComponent(connectionId)}`, { method: "DELETE" });
