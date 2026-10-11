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

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_URL}${path}`, init);
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
  return (await response.json()) as T;
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
