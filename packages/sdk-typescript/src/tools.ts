// Approved tool-route client [Spec §10, §14; I07]. Mirrors susanoo_sdk.agent.ToolClient over fetch.
// The route token travels only as a bearer header to the route's base URL; it is never logged.

import type { JsonObject, ToolEvent, ToolRoute } from "./models.ts";

/** The environment did not answer cleanly. The write may or may not have happened. */
export class ToolTransportError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ToolTransportError";
    this.status = status;
  }
}

export interface ToolCall {
  operation: string;
  status: string;
  code: string | null;
  response: JsonObject;
  seq: number | null;
}

export interface ToolClientOptions {
  timeoutMs?: number;
  maxResponseBytes?: number;
  /** Aborts in-flight calls, e.g. when the runner cancels the invocation. */
  signal?: AbortSignal;
  fetch?: typeof fetch;
}

const OPERATION = /^[A-Za-z_][A-Za-z0-9_]{0,99}$/;

export class ToolClient {
  readonly route: ToolRoute;
  readonly events: ToolEvent[] = [];
  private readonly timeoutMs: number;
  private readonly maxResponseBytes: number;
  private readonly signal: AbortSignal | undefined;
  private readonly fetchImpl: typeof fetch;

  constructor(route: ToolRoute, options: ToolClientOptions = {}) {
    this.route = route;
    this.timeoutMs = options.timeoutMs ?? 30_000;
    this.maxResponseBytes = options.maxResponseBytes ?? 1_048_576;
    this.signal = options.signal;
    this.fetchImpl = options.fetch ?? fetch;
  }

  async call(operation: string, request: JsonObject, idempotencyKey: string | null = null): Promise<ToolCall> {
    if (!OPERATION.test(operation)) throw new ToolTransportError(0, `Invalid operation name ${operation}`);
    const base = this.route.base_url.replace(/\/+$/u, "");
    const url = `${base}/fixtures/${encodeURIComponent(this.route.namespace)}/operations/${operation}`;
    const signals = [AbortSignal.timeout(this.timeoutMs)];
    if (this.signal) signals.push(this.signal);
    let response: Response;
    try {
      response = await this.fetchImpl(url, {
        method: "POST",
        headers: { "content-type": "application/json", authorization: `Bearer ${this.route.token}` },
        body: JSON.stringify({ request, idempotency_key: idempotencyKey }),
        redirect: "error",
        signal: AbortSignal.any(signals),
      });
    } catch (error) {
      if (this.signal?.aborted) throw error; // cancellation propagates as-is
      this.events.push({ operation, status: "transport_error", seq: null, detail: {} });
      throw new ToolTransportError(0, `Environment unreachable: ${(error as Error).name}`);
    }
    const text = await readBounded(response, this.maxResponseBytes);
    if (response.status >= 500) {
      this.events.push({ operation, status: "transport_error", seq: null, detail: {} });
      let detail = "environment error";
      try {
        const parsed = JSON.parse(text) as { detail?: unknown };
        if (typeof parsed.detail === "string") detail = parsed.detail;
      } catch {
        // keep the generic detail
      }
      throw new ToolTransportError(response.status, detail);
    }
    if (response.status >= 400) throw new ToolTransportError(response.status, text);
    let body: { status?: unknown; code?: unknown; response?: unknown; seq?: unknown };
    try {
      body = JSON.parse(text) as typeof body;
    } catch {
      throw new ToolTransportError(response.status, "Environment returned a malformed body");
    }
    if (typeof body.status !== "string" || typeof body.response !== "object" || body.response === null) {
      throw new ToolTransportError(response.status, "Environment returned an unexpected body");
    }
    const seq = typeof body.seq === "number" ? body.seq : null;
    this.events.push({ operation, status: body.status, seq, detail: {} });
    return {
      operation,
      status: body.status,
      code: typeof body.code === "string" ? body.code : null,
      response: body.response as JsonObject,
      seq,
    };
  }
}

async function readBounded(response: Response, limit: number): Promise<string> {
  if (!response.body) return "";
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > limit) {
      await reader.cancel();
      throw new ToolTransportError(response.status, "Environment response exceeds the size limit");
    }
    chunks.push(value);
  }
  return Buffer.concat(chunks).toString("utf8");
}
