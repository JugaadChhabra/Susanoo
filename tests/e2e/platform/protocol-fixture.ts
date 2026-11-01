// Signed protocol test client only. It never invokes or verifies a business backend.
import { request, type APIRequestContext } from "@playwright/test";
import { createHash, randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";

export class BrowserApi {
  public client: APIRequestContext;
  constructor(client: APIRequestContext) { this.client = client; }
  async call(method: string, path: string, body?: unknown, bearer?: string) {
    const headers: Record<string, string> = { Origin: "http://localhost:3000" };
    if (bearer) headers.Authorization = `Bearer ${bearer}`;
    else if (method !== "GET") headers["X-CSRF-Token"] = (await this.client.get("/api/v1/auth/csrf").then(r => r.json())).csrf_token;
    if (method === "POST" && /\/(runs|jobs|reproductions)$/.test(path)) headers["Idempotency-Key"] = randomUUID();
    const response = await this.client.fetch(`/api/v1${path}`, { method, headers, data: body });
    if (!response.ok()) throw new Error(`Protocol fixture HTTP ${response.status()} at ${path}`);
    return response.status() === 204 ? null : await response.json();
  }
}

export async function protocolFixture(baseURL: string, credentialsPath: string) {
  const accounts = JSON.parse(readFileSync(credentialsPath, "utf8")).synthetic_local_accounts as { email: string; password: string }[];
  async function session(name: string) {
    const account = accounts.find(row => row.email === `${name}@susanoo.local`)!;
    const api = new BrowserApi(await request.newContext({ baseURL }));
    await api.call("POST", "/auth/login", account);
    return api;
  }
  const owner = await session("owner");
  const reviewer = await session("reviewer");
  const me = await owner.call("GET", "/me");
  const project = await owner.call("POST", "/projects", { workspace_id: me.workspaces.find((w: { roles: string[] }) => w.roles.includes("owner")).id,
    name: `Browser protocol only ${randomUUID().slice(0, 8)}`, workflow: "Synthetic protocol fixture, not a business agent",
    policy: { upload_fields: ["count"] } });
  async function revision(data: unknown, title: string) {
    return await owner.call("POST", `/projects/${project.id}/versions`, { logical_id: `fixture-${randomUUID()}`, title, data });
  }
  async function approve(row: { id: string; content_hash: string }) {
    return reviewer.call("POST", `/versions/${row.id}/approve`, { content_hash: row.content_hash, rationale: "Synthetic browser protocol fixture review" });
  }
  const requirement = await revision({ kind: "requirement", source_clause: "UI-PROTOCOL-001", expected: "Exactly one synthetic record", owner: "Synthetic reviewer" }, "Browser Requirement");
  const check = await revision({ kind: "check", requirement_version_id: requirement.id, operator: "count", observation: "synthetic_records", expected: 1 }, "Browser Check");
  const fixture = await revision({ kind: "fixture", adapter: "protocol_fixture_no_business_backend", digest: createHash("sha256").update("{}").digest("hex"), compatibility_key: "browser-protocol-v1" }, "Browser Fixture");
  const caseVersion = await revision({ kind: "case", fixture_version_id: fixture.id, check_version_ids: [check.id], script: [{ message: "Protocol test only" }] }, "Browser Case");
  const suite = await revision({ kind: "suite", case_version_ids: [caseVersion.id] }, "Browser Suite");
  const environment = await revision({ kind: "environment", adapter: "protocol_fixture_no_business_backend", version: "test-only", allowed_operations: ["synthetic_write"], identity_profile: "synthetic", fixture_version_id: fixture.id }, "Browser Environment");
  const connection = await revision({ kind: "connection", family: "I03", adapter: "protocol_fixture_no_business_backend", capabilities: { invoke: true, observe: true } }, "Browser Connection");
  const registration = await owner.call("POST", `/projects/${project.id}/runner-registration`);
  const token = (await owner.call("POST", "/runner/register", { registration_token: registration.token, name: "Browser protocol test double", version: "not-a-business-runtime", capabilities: ["preflight", "outcome_suite"] })).token;
  const runner = (path: string, body: unknown) => owner.call("POST", `/runner${path}`, body, token);
  const claim = (kind: string) => runner("/claims", { supported_job_kinds: [kind] });
  const event = (attempt: { attempt_id: string; lease_token: string }, sequence: number, type: string, payload = {}) => runner(`/attempts/${attempt.attempt_id}/events`, { lease_token: attempt.lease_token,
    events: [{ event_id: randomUUID(), sequence, type, occurred_at: new Date().toISOString(), payload }] });
  const finalize = (attempt: { attempt_id: string; lease_token: string }, verdict: string, execution = "completed") => runner(`/attempts/${attempt.attempt_id}/finalize`, {
    lease_token: attempt.lease_token, execution_state: execution, business_verdict: verdict, final_sequence: 1, cleanup_state: "succeeded", reconciliation_state: "not_needed",
  });
  return { project, requirement, check, suite, environment, connection, accounts, owner, reviewer, runner, claim, event, finalize,
    async ready() {
      await approve(environment); await approve(suite);
      await owner.call("POST", `/projects/${project.id}/jobs`, { kind: "preflight", version_ids: [connection.id] });
      const attempt = await claim("preflight");
      await event(attempt, 1, "job_result", { invocation_ok: true, observer_ok: true, cleanup_ok: true });
      await finalize(attempt, "PASS");
    },
    async reportFailure() {
      const attempt = await claim("outcome_suite");
      const id = randomUUID(); const values = { count: 0 };
      await runner(`/attempts/${attempt.attempt_id}/evidence`, { lease_token: attempt.lease_token,
        observations: [{ observation_id: id, name: "synthetic_records", source: "protocol_test_double_not_business_backend", captured_at: new Date().toISOString(), availability: "available", completeness: "complete_for_scope", namespace: attempt.namespace,
          window_seconds: 10, window_elapsed: true, storage_mode: "redacted_hosted", values, redaction_version: "synthetic-test", digest: createHash("sha256").update(JSON.stringify(values)).digest("hex") }],
        check_results: [{ check_version_id: check.id, evaluator_digest: "b".repeat(64), verdict: "FAIL", reason: "Synthetic protocol fixture failure; no business evaluator executed", observation_ids: [id] }],
      });
      await event(attempt, 1, "progress", { code: "synthetic_protocol_evidence_ready" }); await finalize(attempt, "FAIL");
      return attempt;
    },
    async cleanup() { await owner.call("DELETE", `/projects/${project.id}`); await owner.client.dispose(); await reviewer.client.dispose(); },
  };
}
