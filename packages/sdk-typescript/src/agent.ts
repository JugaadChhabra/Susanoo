// Agent-author API [Spec §10; I07]: extend SusanooAgent, call tools through ToolClient, serve over JSONL.
// An agent only receives an InvocationContext and the user's message; never fixture truth or checks.

import { randomUUID } from "node:crypto";
import type { AgentReply, Capabilities, InvocationContext, Message, Reply } from "./models.ts";

export const MAX_REPLY_BYTES = 262_144;

export abstract class SusanooAgent {
  name = "agent";
  version = "0.0.0";
  capabilities: Capabilities = { invoke: true, sessions: true, multi_turn: true, tool_events: true };

  async startSession(_context: InvocationContext): Promise<string> {
    return `session-${randomUUID().replaceAll("-", "").slice(0, 12)}`;
  }

  /** Answer one user turn. ``signal`` aborts when the runner cancels the invocation. */
  abstract respond(sessionId: string, message: Message, context: InvocationContext, signal: AbortSignal): Promise<Reply>;

  async closeSession(_sessionId: string): Promise<void> {}
}

/** Normalize an author reply: truncate oversize text, mark unreported usage/tool events unavailable. */
export function toAgentReply(reply: Reply, invocationId: string, sessionId: string, limit = MAX_REPLY_BYTES): AgentReply {
  let text = reply.text ?? null;
  let truncated = false;
  if (text !== null) {
    const bytes = Buffer.from(text, "utf8");
    if (bytes.length > limit) {
      // Decoding a cut buffer may leave a replacement char at the end; strip it like errors="ignore".
      text = bytes.subarray(0, limit).toString("utf8").replace(/�+$/u, "");
      truncated = true;
    }
  }
  return {
    invocation_id: invocationId,
    session_id: sessionId,
    text,
    structured: reply.structured ?? null,
    usage: reply.usage ?? { unavailable: true, reason: "agent did not report usage" },
    tool_events: reply.tool_events ?? { unavailable: true, reason: "not reported" },
    truncated,
  };
}
