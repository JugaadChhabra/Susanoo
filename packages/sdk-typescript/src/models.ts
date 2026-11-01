// Adapter-boundary shapes for JSONL protocol 1.0 [Spec §10; I07]. Mirrors susanoo_sdk/models.py.
// The runner validates every frame with extra="forbid": never add fields the Python models lack.

export const PROTOCOL_VERSION = "1.0";

export const FRAME_TYPES = [
  "preflight",
  "start_session",
  "invoke",
  "cancel",
  "close_session",
  "shutdown",
  "result",
  "error",
  "event",
] as const;
export type FrameType = (typeof FRAME_TYPES)[number];

export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export type JsonObject = { [key: string]: Json };

export interface JsonlFrame {
  protocol_version: "1.0";
  request_id: string;
  type: FrameType;
  payload: JsonObject;
}

/** susanoo_contracts.Capabilities; omitted fields take the contract defaults. */
export interface Capabilities {
  invoke?: boolean;
  sessions?: boolean;
  multi_turn?: boolean;
  stream?: boolean;
  async_jobs?: boolean;
  cancel_remote?: boolean;
  observe?: boolean;
  reset?: boolean;
  tool_routing?: boolean;
  tool_events?: boolean;
  audio?: boolean;
  reference_replay?: boolean;
  patch_source?: boolean;
  local_evidence?: boolean;
  idempotent_submission?: boolean;
  isolation_tested?: boolean;
  max_concurrency?: number;
  modalities?: string[];
}

export interface ToolRoute {
  base_url: string;
  namespace: string;
  /** The one secret an agent legitimately needs; bound to namespace and operations server-side. */
  token: string;
  operations: string[];
}

/** Everything the agent may know about an attempt. No expected outcomes, checks or fixture truth. */
export interface InvocationContext {
  attempt_id: string;
  run_id: string | null;
  case_id: string | null;
  namespace: string;
  deadline: string;
  turn: number;
  tool_route: ToolRoute | null;
}

export interface Message {
  role: "user";
  text: string;
  structured: JsonObject | null;
}

export interface ToolEvent {
  operation: string;
  status: string;
  seq: number | null;
  detail: JsonObject;
}

export interface Unavailable {
  unavailable: true;
  reason: string;
}

export interface Usage {
  input_tokens: number | null;
  output_tokens: number | null;
  cost_minor: number | null;
  currency: string | null;
}

/** What an agent author returns from ``respond``. Unreported usage/tool events become ``Unavailable``. */
export interface Reply {
  text?: string | null;
  structured?: JsonObject | null;
  tool_events?: ToolEvent[] | null;
  usage?: Usage | null;
}

export interface AgentReply {
  invocation_id: string;
  session_id: string;
  text: string | null;
  structured: JsonObject | null;
  usage: Usage | Unavailable;
  tool_events: ToolEvent[] | Unavailable;
  truncated: boolean;
}

export class ProtocolError extends Error {
  readonly code: string;
  constructor(code: string, message: string) {
    super(message);
    this.name = "ProtocolError";
    this.code = code;
  }
}

function isObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function str(value: unknown, field: string, max = 10_000): string {
  if (typeof value !== "string" || value.length === 0 || value.length > max) {
    throw new ProtocolError("malformed_frame", `${field} must be a non-empty string`);
  }
  return value;
}

function optStr(value: unknown, field: string): string | null {
  return value === undefined || value === null ? null : str(value, field);
}

/** Validate one decoded input frame; unknown keys and versions are rejected like the Python model. */
export function parseFrame(raw: unknown): JsonlFrame {
  if (!isObject(raw)) throw new ProtocolError("malformed_frame", "Frame is not an object");
  for (const key of Object.keys(raw)) {
    if (!["protocol_version", "request_id", "type", "payload"].includes(key)) {
      throw new ProtocolError("malformed_frame", `Unknown frame field ${key}`);
    }
  }
  const version = raw.protocol_version ?? PROTOCOL_VERSION;
  if (version !== PROTOCOL_VERSION) throw new ProtocolError("malformed_frame", "Unsupported protocol version");
  const requestId = str(raw.request_id, "request_id", 100);
  const type = raw.type;
  if (typeof type !== "string" || !(FRAME_TYPES as readonly string[]).includes(type)) {
    throw new ProtocolError("malformed_frame", "Unknown frame type");
  }
  const payload = raw.payload ?? {};
  if (!isObject(payload)) throw new ProtocolError("malformed_frame", "payload must be an object");
  return { protocol_version: PROTOCOL_VERSION, request_id: requestId, type: type as FrameType, payload };
}

export function parseContext(raw: unknown): InvocationContext {
  if (!isObject(raw)) throw new ProtocolError("invalid_context", "context must be an object");
  const turn = raw.turn ?? 1;
  if (typeof turn !== "number" || !Number.isInteger(turn) || turn < 1) {
    throw new ProtocolError("invalid_context", "turn must be a positive integer");
  }
  let route: ToolRoute | null = null;
  if (raw.tool_route !== undefined && raw.tool_route !== null) {
    const r = raw.tool_route;
    if (!isObject(r)) throw new ProtocolError("invalid_context", "tool_route must be an object");
    const operations = r.operations ?? [];
    if (!Array.isArray(operations) || !operations.every((op) => typeof op === "string")) {
      throw new ProtocolError("invalid_context", "tool_route.operations must be strings");
    }
    route = {
      base_url: str(r.base_url, "tool_route.base_url", 500),
      namespace: str(r.namespace, "tool_route.namespace", 100),
      token: str(r.token, "tool_route.token"),
      operations: operations as string[],
    };
  }
  return {
    attempt_id: str(raw.attempt_id, "attempt_id"),
    run_id: optStr(raw.run_id, "run_id"),
    case_id: optStr(raw.case_id, "case_id"),
    namespace: str(raw.namespace, "namespace"),
    deadline: str(raw.deadline, "deadline"),
    turn,
    tool_route: route,
  };
}

export function parseMessage(raw: unknown): Message {
  if (!isObject(raw)) throw new ProtocolError("invalid_message", "message must be an object");
  if ((raw.role ?? "user") !== "user") throw new ProtocolError("invalid_message", "role must be user");
  const structured = raw.structured ?? null;
  if (structured !== null && !isObject(structured)) {
    throw new ProtocolError("invalid_message", "structured must be an object");
  }
  return { role: "user", text: str(raw.text, "message.text", 65_536), structured };
}

export function parseSession(raw: unknown): { session_id: string; attempt_id: string } {
  if (!isObject(raw)) throw new ProtocolError("invalid_session", "session must be an object");
  return { session_id: str(raw.session_id, "session_id", 200), attempt_id: str(raw.attempt_id, "attempt_id") };
}
