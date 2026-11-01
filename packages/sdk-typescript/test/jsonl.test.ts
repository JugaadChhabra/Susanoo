// JSONL bridge, agent side: protocol frames, bounds, errors and cancellation [Spec §10; I07].

import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { PassThrough, Readable } from "node:stream";
import { test } from "node:test";
import {
  boundedLines,
  type InvocationContext,
  type Message,
  parseFrame,
  type Reply,
  SusanooAgent,
  serve,
  toAgentReply,
} from "../src/index.ts";

const CONTEXT = { attempt_id: "a1", namespace: "ns", deadline: "2026-11-02T10:00:00Z" };
const SESSION = { session_id: "s1", attempt_id: "a1" };

const frame = (requestId: string, type: string, payload: object = {}) =>
  `${JSON.stringify({ protocol_version: "1.0", request_id: requestId, type, payload })}\n`;

class Echo extends SusanooAgent {
  async respond(_sessionId: string, message: Message, _context: InvocationContext, signal: AbortSignal): Promise<Reply> {
    if (message.text === "boom") throw new RangeError("secret internal detail");
    if (message.text === "hang") {
      await new Promise((_resolve, reject) => signal.addEventListener("abort", () => reject(signal.reason)));
    }
    return { text: `echo: ${message.text}` };
  }
}

async function run(lines: string[], agent: SusanooAgent = new Echo(), maxFrameBytes?: number) {
  const out: Record<string, unknown>[] = [];
  const logs: string[] = [];
  await serve(agent, {
    input: Readable.from(lines.map((line) => Buffer.from(line))),
    output: (line) => out.push(JSON.parse(line) as Record<string, unknown>),
    log: (line) => logs.push(line),
    onShutdown: () => {},
    ...(maxFrameBytes ? { maxFrameBytes } : {}),
  });
  return { out, logs, byId: Object.fromEntries(out.map((f) => [f.request_id, f])) as Record<string, any> };
}

test("round trip: preflight, session, invoke, close, shutdown", async () => {
  const { byId, out } = await run([
    frame("p", "preflight"),
    frame("s", "start_session", { context: CONTEXT }),
    frame("i", "invoke", { invocation_id: "inv-1", session: SESSION, message: { text: "hi" }, context: CONTEXT }),
    frame("c", "close_session", { session: SESSION }),
    frame("x", "shutdown"),
  ]);
  assert.equal(out.length, 5);
  for (const f of out) assert.equal(f.protocol_version, "1.0");
  assert.equal(byId.p.type, "result");
  assert.equal(byId.p.payload.adapter, "jsonl");
  assert.equal(byId.p.payload.capabilities.invoke, true);
  assert.equal(byId.s.payload.attempt_id, "a1");
  assert.match(byId.s.payload.session_id, /^session-/);
  assert.deepEqual(byId.i.payload, {
    invocation_id: "inv-1",
    session_id: "s1",
    text: "echo: hi",
    structured: null,
    usage: { unavailable: true, reason: "agent did not report usage" },
    tool_events: { unavailable: true, reason: "not reported" },
    truncated: false,
  });
  assert.deepEqual(byId.c.payload, {});
  assert.deepEqual(byId.x.payload, {});
});

test("malformed and oversize input frames are dropped, never answered", async () => {
  const { out, logs } = await run(
    ["not json\n", `${"x".repeat(5000)}\n`, `{"request_id":"r","type":"bogus"}\n`, frame("ok", "preflight")],
    new Echo(),
    1024,
  );
  assert.deepEqual(
    out.map((f) => f.request_id),
    ["ok"],
  );
  assert.ok(logs.some((l) => l.includes("malformed")));
  assert.ok(logs.some((l) => l.includes("oversize")));
});

test("agent exceptions become error frames carrying only the class name", async () => {
  const { byId, logs } = await run([
    frame("i", "invoke", { invocation_id: "inv-1", session: SESSION, message: { text: "boom" }, context: CONTEXT }),
    frame("u", "result"),
    frame("v", "invoke", { invocation_id: "inv-2", session: SESSION, message: { text: "" }, context: CONTEXT }),
  ]);
  assert.deepEqual(byId.i, {
    protocol_version: "1.0",
    request_id: "i",
    type: "error",
    payload: { code: "agent_error", message: "RangeError" },
  });
  assert.ok(!JSON.stringify(byId).includes("secret internal detail"));
  assert.ok(!logs.join("").includes("secret internal detail"));
  assert.equal(byId.u.type, "error"); // runner-only frame types are rejected
  assert.equal(byId.v.payload.code, "agent_error"); // empty message text is invalid, like the Python model
});

test("cancel aborts the invocation and is acknowledged", async () => {
  const input = new PassThrough();
  const out: Record<string, any>[] = [];
  const served = serve(new Echo(), {
    input,
    output: (line) => out.push(JSON.parse(line)),
    log: () => {},
    onShutdown: () => input.end(),
  });
  input.write(frame("i", "invoke", { invocation_id: "inv-h", session: SESSION, message: { text: "hang" }, context: CONTEXT }));
  await new Promise((resolve) => setTimeout(resolve, 50));
  input.write(frame("c", "cancel", { invocation_id: "inv-h" }));
  input.write(frame("c2", "cancel", { invocation_id: "inv-unknown" }));
  await new Promise((resolve) => setTimeout(resolve, 50));
  input.write(frame("x", "shutdown"));
  await served;
  const byId = Object.fromEntries(out.map((f) => [f.request_id, f]));
  assert.deepEqual(byId.i.payload, { code: "cancelled" });
  assert.equal(byId.c.payload.acknowledged, true);
  assert.equal(byId.c2.payload.acknowledged, false);
});

test("closing stdin cancels outstanding work and serve returns", async () => {
  const input = new PassThrough();
  const out: Record<string, any>[] = [];
  const served = serve(new Echo(), { input, output: (line) => out.push(JSON.parse(line)), log: () => {} });
  input.write(frame("i", "invoke", { invocation_id: "inv-h", session: SESSION, message: { text: "hang" }, context: CONTEXT }));
  input.end();
  await served;
  assert.equal(out[0]?.payload.code, "cancelled");
});

test("boundedLines splits across chunk boundaries and never buffers past the limit", async () => {
  const dropped: number[] = [];
  const chunks = [Buffer.from("ab"), Buffer.from("c\nde"), Buffer.from("f\n" + "z".repeat(20)), Buffer.from("z\nlast")];
  const lines: string[] = [];
  for await (const line of boundedLines(chunks, 8, (n) => dropped.push(n))) lines.push(line.toString());
  assert.deepEqual(lines, ["abc", "def", "last"]);
  assert.deepEqual(dropped, [21]);
});

test("parseFrame rejects unknown fields and protocol versions", () => {
  assert.throws(() => parseFrame({ request_id: "r", type: "invoke", payload: {}, extra: 1 }));
  assert.throws(() => parseFrame({ protocol_version: "2.0", request_id: "r", type: "invoke" }));
  assert.throws(() => parseFrame({ protocol_version: "1.0", request_id: "", type: "invoke" }));
  assert.throws(() => parseFrame({ protocol_version: "1.0", request_id: "r", type: "invoke", payload: [] }));
  assert.equal(parseFrame({ request_id: "r", type: "preflight" }).protocol_version, "1.0");
});

test("toAgentReply truncates oversize text on a UTF-8 boundary", () => {
  const reply = toAgentReply({ text: "₹".repeat(10) }, "inv", "s", 10);
  assert.equal(reply.truncated, true);
  assert.equal(reply.text, "₹₹₹");
  assert.ok(Buffer.byteLength(reply.text ?? "") <= 10);
  const full = toAgentReply({ text: "ok", tool_events: [] }, "inv", "s");
  assert.equal(full.truncated, false);
  assert.deepEqual(full.tool_events, []);
});

test("subprocess: stdout carries frames only and the env is what the runner passed", async () => {
  const script = fileURLToPath(new URL("./fixtures/echo-agent.ts", import.meta.url));
  const child = spawn(process.execPath, [script], { env: { PATH: process.env.PATH ?? "" }, stdio: "pipe" });
  let stdout = "";
  let stderr = "";
  child.stdout.on("data", (d) => (stdout += d));
  child.stderr.on("data", (d) => (stderr += d));
  child.stdin.write(frame("s", "start_session", { context: CONTEXT }));
  child.stdin.write(frame("i", "invoke", { invocation_id: "inv-1", session: SESSION, message: { text: "env" }, context: CONTEXT }));
  child.stdin.write(frame("j", "invoke", { invocation_id: "inv-2", session: SESSION, message: { text: "yo" }, context: CONTEXT }));
  await new Promise((resolve) => setTimeout(resolve, 300));
  child.stdin.write(frame("x", "shutdown"));
  const code = await new Promise((resolve) => child.on("exit", resolve));
  assert.equal(code, 0);
  const frames = stdout.trim().split("\n").map((line) => JSON.parse(line));
  const byId = Object.fromEntries(frames.map((f) => [f.request_id, f]));
  assert.equal(byId.i.payload.text, "absent");
  assert.equal(byId.j.payload.text, "echo: yo");
  assert.ok(stderr.includes("diagnostic via console.log"));
  assert.ok(stderr.includes("stray stdout write"));
});
