import { getAccessToken } from "@/lib/supabase";
import type { NewProject, ProjectRules, ProjectSummary, RunQuota } from "@/types/generated";

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

export type ApprovalDecision = "approve" | "reject";

/** Answer a gated (test-layer) change. The PRD lists the route but not the body, so we send `{decision}`. */
export const decideApproval = (runId: string, patchId: string, decision: ApprovalDecision) =>
  apiFetch<unknown>(`/runs/${encodeURIComponent(runId)}/approvals/${encodeURIComponent(patchId)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decision }),
  });

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
