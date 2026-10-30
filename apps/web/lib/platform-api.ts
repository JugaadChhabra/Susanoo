import type { ArtifactRecord } from "@/types/platform-generated";

export class PlatformError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public correlationId?: string,
  ) {
    super(message);
  }
}

export async function platform<T>(
  path: string,
  method = "GET",
  body?: unknown,
  headers?: HeadersInit,
): Promise<T> {
  const h = new Headers(headers);
  if (method !== "GET") {
    const response = await fetch("/api/v1/auth/csrf", {
      credentials: "same-origin",
      cache: "no-store",
    });
    if (!response.ok)
      throw new PlatformError(
        response.status,
        "UNAVAILABLE",
        "The platform is unavailable.",
      );
    const csrf = (await response.json()) as { csrf_token: string };
    h.set("X-CSRF-Token", csrf.csrf_token);
    if (!(body instanceof Blob)) h.set("Content-Type", "application/json");
  }
  let response: Response;
  try {
    response = await fetch(`/api/v1${path}`, {
      method,
      headers: h,
      cache: "no-store",
      credentials: "same-origin",
      body:
        body === undefined
          ? undefined
          : body instanceof Blob
            ? body
            : JSON.stringify(body),
    });
  } catch {
    throw new PlatformError(
      0,
      "UNAVAILABLE",
      "Cannot reach the private evaluation platform.",
    );
  }
  if (!response.ok) {
    const error = (await response.json().catch(() => null)) as {
      error?: { code: string; message: string; correlation_id?: string };
    } | null;
    throw new PlatformError(
      response.status,
      error?.error?.code ?? "UNAVAILABLE",
      error?.error?.message ?? "The action could not be completed.",
      error?.error?.correlation_id,
    );
  }
  return response.status === 204
    ? (undefined as T)
    : ((await response.json()) as T);
}

export const launchKey = () => ({ "Idempotency-Key": crypto.randomUUID() });

export async function uploadArtifact(
  projectId: string,
  file: File,
  audience = "project",
  purpose = "evidence",
) {
  const bytes = await file.arrayBuffer();
  const digest = Array.from(
    new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)),
  )
    .map((v) => v.toString(16).padStart(2, "0"))
    .join("");
  const record = await platform<ArtifactRecord>(
    `/projects/${projectId}/artifacts`,
    "POST",
    {
      content_type:
        (
          {
            json: "application/json",
            jsonl: "application/x-ndjson",
            csv: "text/csv",
            zip: "application/zip",
            tar: "application/x-tar",
            gz: "application/gzip",
            tgz: "application/gzip",
            wav: "audio/wav",
            mp3: "audio/mpeg",
          } as Record<string, string>
        )[file.name.split(".").at(-1)?.toLowerCase() ?? ""] ||
        file.type ||
        "text/plain",
      size: bytes.byteLength,
      digest,
      audience,
      purpose,
    },
  );
  await platform(`/artifacts/${record.id}/content`, "PUT", file);
  return record.id;
}
