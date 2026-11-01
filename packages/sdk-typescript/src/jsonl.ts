// Bounded JSONL bridge, agent side [Spec §10; I07]. Same wire protocol as susanoo_sdk.jsonl.serve().
//
// Frames are {protocol_version, request_id, type, payload}, one per line. Standard output carries
// protocol frames only; console output and stray stdout writes are rerouted to stderr. Input lines
// over the frame limit and malformed frames are dropped with a stderr diagnostic, never answered
// as success. Errors report only an error class name so agent internals never reach the runner.

import type { Readable } from "node:stream";
import { type SusanooAgent, toAgentReply } from "./agent.ts";
import {
  type JsonlFrame,
  type JsonObject,
  PROTOCOL_VERSION,
  parseContext,
  parseFrame,
  parseMessage,
  parseSession,
} from "./models.ts";

export const DEFAULT_MAX_FRAME_BYTES = 1_048_576;

export interface ServeOptions {
  input?: Readable;
  /** Protocol sink. Defaults to the real stdout, which is then reserved for frames. */
  output?: (line: string) => void;
  log?: (line: string) => void;
  maxFrameBytes?: number;
  /** Concurrent requests handled at once; the runner also bounds outstanding requests. */
  maxConcurrent?: number;
  /** Called after the shutdown acknowledgement is written. Defaults to exiting once stdout drains. */
  onShutdown?: () => void;
}

export class CancelledError extends Error {
  constructor() {
    super("cancelled");
    this.name = "CancelledError";
  }
}

export function encode(frame: JsonlFrame): string {
  return `${JSON.stringify(frame)}\n`;
}

/** Split a byte stream into lines without ever buffering more than ``maxBytes`` of one line. */
export async function* boundedLines(
  input: AsyncIterable<Buffer | string>,
  maxBytes: number,
  onOversize: (bytes: number) => void,
): AsyncGenerator<Buffer> {
  let parts: Buffer[] = [];
  let size = 0;
  let discarding = false;
  for await (const raw of input) {
    let chunk = typeof raw === "string" ? Buffer.from(raw, "utf8") : raw;
    while (chunk.length > 0) {
      const newline = chunk.indexOf(0x0a);
      const piece = newline === -1 ? chunk : chunk.subarray(0, newline);
      if (!discarding) {
        size += piece.length;
        if (size > maxBytes) {
          discarding = true;
          parts = [];
        } else {
          parts.push(piece);
        }
      } else {
        size += piece.length;
      }
      if (newline === -1) break;
      if (discarding) onOversize(size);
      else yield Buffer.concat(parts);
      parts = [];
      size = 0;
      discarding = false;
      chunk = chunk.subarray(newline + 1);
    }
  }
  if (!discarding && size > 0) yield Buffer.concat(parts);
  else if (discarding) onOversize(size);
}

/** Serve ``agent`` until stdin closes or the runner sends ``shutdown``. */
export async function serve(agent: SusanooAgent, options: ServeOptions = {}): Promise<void> {
  const maxFrameBytes = options.maxFrameBytes ?? DEFAULT_MAX_FRAME_BYTES;
  const maxConcurrent = options.maxConcurrent ?? 64;
  const log = options.log ?? ((line: string) => process.stderr.write(`${line}\n`));
  let output = options.output;
  if (!output) {
    const protocolWrite = process.stdout.write.bind(process.stdout);
    output = (line: string) => {
      protocolWrite(line);
    };
    // Anything else the agent prints goes to stderr, not the protocol channel.
    process.stdout.write = process.stderr.write.bind(process.stderr) as typeof process.stdout.write;
    for (const method of ["log", "info", "debug"] as const) console[method] = console.error.bind(console);
  }
  const write = output;
  const onShutdown =
    options.onShutdown ??
    (() => {
      process.stdout.once("drain", () => process.exit(0));
      if (!process.stdout.writableNeedDrain) process.exit(0);
    });

  const invocations = new Map<string, AbortController>();
  const inFlight = new Set<Promise<void>>();
  let stopped = false;

  const send = (requestId: string, type: "result" | "error", payload: JsonObject): void => {
    if (stopped && type !== "result") return;
    write(encode({ protocol_version: PROTOCOL_VERSION, request_id: requestId, type, payload }));
  };

  const handle = async (frame: JsonlFrame): Promise<void> => {
    const payload = frame.payload;
    try {
      let result: JsonObject;
      switch (frame.type) {
        case "preflight":
          result = {
            adapter: "jsonl",
            adapter_version: agent.version,
            protocol_version: PROTOCOL_VERSION,
            capabilities: agent.capabilities as JsonObject,
            checks: {},
            notes: [],
          };
          break;
        case "start_session": {
          const context = parseContext(payload.context);
          const sessionId = await agent.startSession(context);
          result = { session_id: sessionId, attempt_id: context.attempt_id };
          break;
        }
        case "invoke": {
          const context = parseContext(payload.context);
          const session = parseSession(payload.session);
          const message = parseMessage(payload.message);
          const invocationId = payload.invocation_id;
          if (typeof invocationId !== "string" || invocationId.length === 0) {
            throw new TypeError("invocation_id must be a string");
          }
          const controller = new AbortController();
          invocations.set(invocationId, controller);
          try {
            const reply = await raceAbort(
              agent.respond(session.session_id, message, context, controller.signal),
              controller.signal,
            );
            result = toAgentReply(reply, invocationId, session.session_id) as unknown as JsonObject;
          } finally {
            invocations.delete(invocationId);
          }
          break;
        }
        case "cancel": {
          const invocationId = String(payload.invocation_id ?? "");
          const controller = invocations.get(invocationId);
          controller?.abort(new CancelledError());
          result = {
            invocation_id: invocationId,
            acknowledged: controller !== undefined,
            remote_cancel_attempted: false,
            note: null,
          };
          break;
        }
        case "close_session":
          await agent.closeSession(parseSession(payload.session).session_id);
          result = {};
          break;
        case "shutdown":
          stopped = true;
          for (const controller of invocations.values()) controller.abort(new CancelledError());
          send(frame.request_id, "result", {});
          onShutdown();
          return;
        default:
          throw new TypeError(`Unsupported frame type ${frame.type}`);
      }
      send(frame.request_id, "result", result);
    } catch (error) {
      if (error instanceof CancelledError) {
        send(frame.request_id, "error", { code: "cancelled" });
        return;
      }
      const name = error instanceof Error ? error.name : "Error";
      log(`agent error: ${name}`);
      send(frame.request_id, "error", { code: "agent_error", message: name });
    }
  };

  const input = options.input ?? process.stdin;
  const lines = boundedLines(input as AsyncIterable<Buffer>, maxFrameBytes, (bytes) =>
    log(`agent: dropped oversize input frame (${bytes} bytes)`),
  );
  for await (const line of lines) {
    if (stopped) break;
    if (line.length === 0) continue;
    let frame: JsonlFrame;
    try {
      frame = parseFrame(JSON.parse(line.toString("utf8")));
    } catch {
      log("agent: ignored malformed input frame");
      continue;
    }
    while (inFlight.size >= maxConcurrent) await Promise.race(inFlight);
    const task: Promise<void> = handle(frame).finally(() => inFlight.delete(task));
    inFlight.add(task);
  }
  // Input closed: nothing can read our replies any more, so stop outstanding work.
  for (const controller of invocations.values()) controller.abort(new CancelledError());
  await Promise.allSettled(inFlight);
}

function raceAbort<T>(work: Promise<T>, signal: AbortSignal): Promise<T> {
  if (signal.aborted) return Promise.reject(new CancelledError());
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => reject(new CancelledError());
    signal.addEventListener("abort", onAbort, { once: true });
    work.then(
      (value) => {
        signal.removeEventListener("abort", onAbort);
        resolve(value);
      },
      (error: unknown) => {
        signal.removeEventListener("abort", onAbort);
        reject(signal.aborted ? new CancelledError() : error);
      },
    );
  });
}
