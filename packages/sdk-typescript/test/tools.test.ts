// ToolClient over fetch against a local fake environment route [Spec §10, §14; I07].

import assert from "node:assert/strict";
import { createServer, type IncomingMessage, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { after, before, test } from "node:test";
import { ToolClient, ToolTransportError } from "../src/index.ts";

const seen: { url: string; auth: string | undefined; operation: string | string[] | undefined; body: unknown }[] = [];
let server: Server;
let base = "";

async function body(req: IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = [];
  for await (const chunk of req) chunks.push(chunk as Buffer);
  return JSON.parse(Buffer.concat(chunks).toString() || "null");
}

before(async () => {
  server = createServer(async (req, res) => {
    seen.push({ url: req.url ?? "", auth: req.headers.authorization,
      operation: req.headers["x-susanoo-operation-id"], body: await body(req) });
    const op = (req.url ?? "").split("/").pop();
    res.setHeader("content-type", "application/json");
    if (op === "fail") return res.writeHead(503).end(JSON.stringify({ detail: "injected fault" }));
    if (op === "forbidden") return res.writeHead(403).end(JSON.stringify({ detail: "operation not allowed" }));
    if (op === "redirect") return res.writeHead(302, { location: "http://example.invalid/" }).end();
    if (op === "huge") return res.end(JSON.stringify({ status: "ok", response: { pad: "x".repeat(4096) } }));
    res.end(JSON.stringify({ status: "ok", code: null, response: { echoed: true }, seq: 7 }));
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  base = `http://127.0.0.1:${(server.address() as AddressInfo).port}/`;
});

after(() => server.close());

const route = () => ({ base_url: base, namespace: "ns-1", token: "route-token", operations: [] });

test("calls the namespaced operation route with the bearer token and records tool events", async () => {
  const tools = new ToolClient({ ...route(), operation_id: "attempt-1:turn-001" });
  const call = await tools.call("create_promise", { account_id: "ACC-1" }, "key-1");
  assert.deepEqual(call, { operation: "create_promise", status: "ok", code: null, response: { echoed: true }, seq: 7 });
  const last = seen.at(-1);
  assert.equal(last?.url, "/fixtures/ns-1/operations/create_promise");
  assert.equal(last?.auth, "Bearer route-token");
  assert.equal(last?.operation, "attempt-1:turn-001");
  assert.deepEqual(last?.body, { request: { account_id: "ACC-1" }, idempotency_key: "key-1" });
  assert.deepEqual(tools.events, [{ operation: "create_promise", status: "ok", seq: 7, detail: {} }]);
});

test("5xx is a transport error with a transport_error event; 4xx raises without one", async () => {
  const tools = new ToolClient(route());
  await assert.rejects(tools.call("fail", {}), (e: unknown) => e instanceof ToolTransportError && e.status === 503 && e.message === "injected fault");
  await assert.rejects(tools.call("forbidden", {}), (e: unknown) => e instanceof ToolTransportError && e.status === 403);
  assert.deepEqual(
    tools.events.map((e) => e.status),
    ["transport_error"],
  );
});

test("redirects are not followed, oversize bodies and bad operation names are refused", async () => {
  const tools = new ToolClient(route(), { maxResponseBytes: 1024 });
  await assert.rejects(tools.call("redirect", {}), ToolTransportError);
  await assert.rejects(tools.call("huge", {}), /size limit/);
  await assert.rejects(tools.call("../admin", {}), /Invalid operation/);
});

test("an aborted invocation cancels in-flight tool calls", async () => {
  const controller = new AbortController();
  controller.abort(new Error("cancelled by runner"));
  const tools = new ToolClient(route(), { signal: controller.signal });
  await assert.rejects(tools.call("create_promise", {}), /cancelled by runner|aborted/);
  assert.equal(tools.events.length, 0);
});
