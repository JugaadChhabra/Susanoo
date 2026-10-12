/**
 * Stand-in for Supabase Auth and the Susanoo API in demo mode, so the smoke test needs no
 * network, keys or database. [PRD › Team and ownership › Testing]
 *
 * Auth follows Supabase's magic-link PKCE flow for real (the code verifier must match the
 * challenge), so the web app's login form, callback route and proxy run unchanged.
 * Every run streams the saved demo run's events, which keeps the run view deterministic.
 *
 * Shapes come from apps/web/types/generated.ts, and the runs, intake and approval routes follow
 * apps/api/susanoo_api/routers/ (pause tokens, 409 for a step that moved on, `?after=` resume).
 * Run with: node stub/server.ts (Node 22.18+ strips the types itself).
 */
import { createHash, randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import type {
  AgentCard,
  IntakeOut,
  ProjectSummary,
  ReportCard,
  ReportCards,
  RunEvent,
  RunQuota,
  RunStatus,
  RunSummary,
} from "../../../apps/web/types/generated.ts";
import { ANON_KEY, DEMO_RUN_ID, STUB_PORT } from "../constants.ts";

/** What a run is paused on: the kind of step and the token an answer must send back. */
interface Pause {
  kind: "questions" | "scenarios";
  token: string;
}

/** A run as the API lists it, plus its pause and what the intake screen sent (exposed only via /__e2e). */
interface Run extends RunSummary {
  projectId: string;
  pause: Pause | null;
  answers: unknown;
  scenarios: unknown;
}

interface Project extends Omit<ProjectSummary, "runs"> {
  ownerId: string;
  runs: Run[];
}

const DEMO_EVENTS = JSON.parse(
  readFileSync(new URL("../../fixtures/run_events.json", import.meta.url), "utf8"),
) as RunEvent[];

/** Gap between streamed events: fast enough for CI, slow enough to watch the view update. */
const EVENT_INTERVAL_MS = 25;

// ---- state (one test worker, so one in-memory world) -------------------------------------

const otps = new Map<string, { email: string; challenge: string; method: string; redirectTo: string }>();
const inbox = new Map<string, string>(); // email -> latest magic link
const sessions = new Map<string, { id: string; email: string }>(); // access token -> user
const refreshTokens = new Map<string, { id: string; email: string }>();
const users = new Map<string, string>(); // email -> user id
const projects = new Map<string, Project>(); // id -> project

// ---- helpers -----------------------------------------------------------------------------

const b64url = (input: string | Buffer) => Buffer.from(input).toString("base64url");

function send(res: ServerResponse, status: number, body?: unknown) {
  if (body === undefined) {
    res.writeHead(status).end();
    return;
  }
  res.writeHead(status, { "Content-Type": "application/json" }).end(JSON.stringify(body));
}

/** The Susanoo API's error shape: {"error": {"code", "message"}}. */
const apiError = (res: ServerResponse, status: number, code: string, message: string) =>
  send(res, status, { error: { code, message } });

/** Supabase Auth's error shape. */
const authError = (res: ServerResponse, status: number, code: string, msg: string) =>
  send(res, status, { code: status, error_code: code, msg });

async function readJson(req: IncomingMessage): Promise<Record<string, unknown>> {
  const chunks: Buffer[] = [];
  for await (const chunk of req) chunks.push(chunk as Buffer);
  const text = Buffer.concat(chunks).toString("utf8");
  return text ? (JSON.parse(text) as Record<string, unknown>) : {};
}

/** Shaped like a Supabase JWT so nothing that peeks at it chokes; the stub never checks a signature. */
function fakeJwt(userId: string, email: string): string {
  const now = Math.floor(Date.now() / 1000);
  const header = b64url(JSON.stringify({ alg: "HS256", typ: "JWT" }));
  const claims = b64url(
    JSON.stringify({ sub: userId, email, aud: "authenticated", role: "authenticated", iat: now, exp: now + 3600, session_id: randomUUID() }),
  );
  return `${header}.${claims}.${b64url("e2e-signature")}`;
}

function userObject(id: string, email: string) {
  const at = new Date().toISOString();
  return {
    id,
    aud: "authenticated",
    role: "authenticated",
    email,
    email_confirmed_at: at,
    app_metadata: { provider: "email", providers: ["email"] },
    user_metadata: {},
    identities: [],
    created_at: at,
    updated_at: at,
  };
}

function issueSession(user: { id: string; email: string }) {
  const accessToken = fakeJwt(user.id, user.email);
  const refreshToken = randomUUID();
  sessions.set(accessToken, user);
  refreshTokens.set(refreshToken, user);
  return {
    access_token: accessToken,
    token_type: "bearer",
    expires_in: 3600,
    expires_at: Math.floor(Date.now() / 1000) + 3600,
    refresh_token: refreshToken,
    user: userObject(user.id, user.email),
  };
}

function bearerUser(req: IncomingMessage) {
  const header = req.headers.authorization ?? "";
  const token = header.startsWith("Bearer ") ? header.slice(7) : "";
  return sessions.get(token) ?? null;
}

function pkceMatches(verifier: string, challenge: string, method: string): boolean {
  if (method === "plain") return verifier === challenge;
  return b64url(createHash("sha256").update(verifier).digest()) === challenge;
}

/** Pass a user id to only look in that user's projects, like RLS would. */
function findRun(runId: string, userId?: string): Run | null {
  for (const project of userId ? projectsOf(userId) : projects.values()) {
    const run = project.runs.find((r) => r.id === runId);
    if (run) return run;
  }
  return null;
}

// ---- Supabase Auth -----------------------------------------------------------------------

async function handleAuth(req: IncomingMessage, res: ServerResponse, url: URL) {
  if (req.headers.apikey !== ANON_KEY) return authError(res, 401, "no_api_key", "No API key found in request");
  const path = url.pathname.slice("/auth/v1".length);

  if (req.method === "POST" && path === "/otp") {
    const body = await readJson(req);
    const email = String(body.email ?? "").trim().toLowerCase();
    const challenge = String(body.code_challenge ?? "");
    if (!email || !challenge) return authError(res, 400, "validation_failed", "Email and a PKCE code challenge are required");
    const redirectTo = url.searchParams.get("redirect_to") ?? "";
    const code = randomUUID();
    otps.set(code, { email, challenge, method: String(body.code_challenge_method ?? "s256"), redirectTo });
    // Real Supabase emails a /verify link that bounces to redirect_to with ?code=; we hand back the bounce.
    const link = new URL(redirectTo);
    link.searchParams.set("code", code);
    inbox.set(email, link.toString());
    return send(res, 200, {});
  }

  if (req.method === "POST" && path === "/token") {
    const body = await readJson(req);
    const grant = url.searchParams.get("grant_type");
    if (grant === "pkce") {
      const otp = otps.get(String(body.auth_code ?? ""));
      if (!otp) return authError(res, 404, "flow_state_not_found", "invalid flow state, no valid flow state found");
      otps.delete(String(body.auth_code));
      if (!pkceMatches(String(body.code_verifier ?? ""), otp.challenge, otp.method)) {
        return authError(res, 403, "bad_code_verifier", "code challenge does not match previously saved code verifier");
      }
      let id = users.get(otp.email);
      if (!id) {
        id = randomUUID();
        users.set(otp.email, id);
      }
      return send(res, 200, issueSession({ id, email: otp.email }));
    }
    if (grant === "refresh_token") {
      const user = refreshTokens.get(String(body.refresh_token ?? ""));
      if (!user) return authError(res, 400, "refresh_token_not_found", "Invalid Refresh Token: Refresh Token Not Found");
      refreshTokens.delete(String(body.refresh_token));
      return send(res, 200, issueSession(user));
    }
    return authError(res, 400, "unsupported_grant_type", "unsupported grant type");
  }

  if (req.method === "GET" && path === "/user") {
    const user = bearerUser(req);
    if (!user) return authError(res, 403, "bad_jwt", "invalid JWT: unable to parse or verify signature");
    return send(res, 200, userObject(user.id, user.email));
  }

  if (req.method === "POST" && path === "/logout") {
    const header = req.headers.authorization ?? "";
    sessions.delete(header.replace(/^Bearer /, ""));
    return send(res, 204);
  }

  return authError(res, 404, "not_found", `The stub has no auth route ${req.method} ${path}`);
}

// ---- Susanoo API -------------------------------------------------------------------------

const INTAKE_QUESTIONS = [
  {
    id: "q1",
    text: "Should a missing login check on an internal health-check endpoint be blocked or allowed?",
    options: ["Allowed", "Blocked"],
    default: "Allowed",
    why: "Health checks are usually public, but some teams want every endpoint protected.",
  },
  {
    id: "q2",
    text: "How strict should the reviewer be about leaked personal data in logs?",
    options: ["Block the change", "Warn only"],
    default: "Block the change",
    why: "This decides whether a leaked email stops a merge or just leaves a note.",
  },
];

const INTAKE_SCENARIOS = [
  { id: "s1", title: "A developer adds a log line that prints a customer's email. It should be blocked under No PII in logs." },
  { id: "s2", title: "A developer adds a new internal /health route with no login check. It should be allowed." },
];

/** Same wording as the real API (routers/runs.py STALE_PAUSE). */
const STALE_PAUSE = "This question was already answered or has moved on; please reload.";

const RUNS_PER_DAY = 3;

const projectsOf = (userId: string) => [...projects.values()].filter((p) => p.ownerId === userId);

function quota(userId: string): RunQuota {
  const resets = new Date();
  resets.setUTCHours(24, 0, 0, 0);
  const used = projectsOf(userId).reduce((n, p) => n + p.runs.length, 0);
  return { runs_per_day: RUNS_PER_DAY, runs_used_today: used, runs_left_today: Math.max(0, RUNS_PER_DAY - used), paused: false, resets_at: resets.toISOString() };
}

/** Same rule as name_from_spec in apps/api/susanoo_api/routers/projects.py. */
function nameFromSpec(spec: string): string {
  const firstLine = spec.split("\n").map((l) => l.trim()).find(Boolean) ?? "Untitled project";
  const end = firstLine.indexOf(". ");
  const sentence = end === -1 ? firstLine : firstLine.slice(0, end + 1);
  return sentence.length <= 60 ? sentence : `${sentence.slice(0, 60).trimEnd()}…`;
}

const toSummary = ({ ownerId: _o, runs, ...p }: Project): ProjectSummary => ({ ...p, runs: runs.map(publicRun) });

/** Public, like the real /demo/*: replays only the saved demo run. */
function handleDemo(res: ServerResponse, runId: string) {
  if (runId !== DEMO_RUN_ID) return apiError(res, 404, "not_found", "We couldn't find that demo run.");
  return send(res, 200, DEMO_EVENTS);
}

/** GET /runs/{id}/events as SSE, resumable with ?after=N. Demo mode: every run replays the saved run. */
function streamEvents(req: IncomingMessage, res: ServerResponse, url: URL, run: Run) {
  const after = Number(url.searchParams.get("after") ?? 0);
  const events = DEMO_EVENTS.filter((e) => e.seq > after).map((e) => ({ ...e, run_id: run.id }));
  res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache", Connection: "keep-alive" });
  res.flushHeaders();
  res.write("retry: 3000\n\n");
  let i = 0;
  const timer = setInterval(() => {
    if (i >= events.length) {
      clearInterval(timer);
      run.status = "done";
      res.end();
      return;
    }
    res.write(`id: ${events[i].seq}\ndata: ${JSON.stringify(events[i])}\n\n`);
    i += 1;
  }, EVENT_INTERVAL_MS);
  req.on("close", () => clearInterval(timer));
}

function runStatus(run: Run): RunStatus {
  return {
    id: run.id,
    project_id: run.projectId,
    status: run.status,
    message: run.status === "awaiting_approval" ? "Your run is waiting for you." : "Your run is in progress.",
    cost_usd: run.cost_usd,
    cost_cap_usd: run.cost_cap_usd,
    preview_url: null,
    created_at: run.created_at,
    cancel_requested: false,
    error: null,
    metrics: [],
  };
}

function intakeOf(run: Run): IntakeOut {
  const none = { questions: [], scenarios: [], gate: null };
  if (!run.pause) return { ...none, waiting: false, kind: null, pause_token: null, message: "Nothing is waiting for you right now." };
  const { kind, token } = run.pause;
  return kind === "questions"
    ? { ...none, waiting: true, kind, pause_token: token, message: "Please answer a few quick questions.", questions: INTAKE_QUESTIONS }
    : { ...none, waiting: true, kind, pause_token: token, message: "Please review these examples.", scenarios: INTAKE_SCENARIOS };
}

/** Like the real pending_for_answer: 409 unless the run waits on exactly this kind of step with this token. */
async function answerPause(req: IncomingMessage, res: ServerResponse, run: Run, kind: Pause["kind"]) {
  const body = await readJson(req);
  if (!run.pause || run.pause.kind !== kind || run.pause.token !== body.pause_token) {
    apiError(res, 409, "http_error", STALE_PAUSE);
    return null;
  }
  return body;
}

function reportRound(iteration: number, caught: number, falseAlarms: number): ReportCard {
  const alarms = falseAlarms === 1 ? "1 false alarm" : `${falseAlarms} false alarms`;
  return {
    iteration,
    basis: "heldout",
    score: { basis: "heldout", caught, problems: 22, false_alarms: falseAlarms },
    score_percent: Math.round((caught / 22) * 100),
    headline: `Caught ${caught} of 22 problems, ${alarms}.`,
    basis_note: "Scored on examples it never trained on.",
    comparison: iteration === 0 ? null : "First round: 14 of 22.",
    trend: iteration === 0 ? "new" : "better",
    acceptance: null,
    unscored: null,
    lines: [],
  };
}

async function handleApi(req: IncomingMessage, res: ServerResponse, url: URL) {
  const path = url.pathname;
  if (req.method === "GET" && path === "/health") return send(res, 200, { status: "ok", supabase: { status: "ok", message: "Stubbed." } });

  const demo = path.match(/^\/demo\/([^/]+)$/);
  if (req.method === "GET" && demo) return handleDemo(res, decodeURIComponent(demo[1]));

  const user = bearerUser(req);
  if (!user) return apiError(res, 401, "unauthorized", "Please sign in again.");

  if (req.method === "GET" && path === "/me/quota") return send(res, 200, quota(user.id));
  if (req.method === "GET" && path === "/connectors") return send(res, 200, []);
  if (req.method === "GET" && path === "/projects") {
    return send(res, 200, projectsOf(user.id).reverse().map(toSummary));
  }
  if (req.method === "POST" && path === "/projects") {
    const body = await readJson(req);
    const spec = String(body.spec ?? "").trim();
    if (!spec) return apiError(res, 422, "validation_error", "Describe what you want to build first.");
    const rules = Array.isArray(body.rules) ? body.rules.map(String) : [];
    // The stub user has no connections (GET /connectors is empty), so any picked id isn't theirs, as in the real API.
    if (Array.isArray(body.connection_ids) && body.connection_ids.length > 0) {
      return apiError(res, 422, "validation_error", "One of the connections you picked isn't yours or has been removed. Refresh the page and pick again.");
    }
    const project: Project = {
      id: randomUUID(),
      ownerId: user.id,
      name: nameFromSpec(spec),
      spec,
      rules,
      connection_ids: [],
      repo_url: null,
      runs: [],
    };
    projects.set(project.id, project);
    return send(res, 201, toSummary(project));
  }

  const startRoute = path.match(/^\/projects\/([^/]+)\/runs$/);
  if (req.method === "POST" && startRoute) {
    const project = projects.get(decodeURIComponent(startRoute[1]));
    if (!project || project.ownerId !== user.id) return apiError(res, 404, "not_found", "We couldn't find that project.");
    if (quota(user.id).runs_left_today <= 0) return apiError(res, 429, "http_error", "You've used all 3 of today's runs.");
    // The real worker reaches the Planner's first pause in a few seconds; the stub is there at once.
    const run: Run = {
      id: randomUUID(),
      projectId: project.id,
      status: "awaiting_approval",
      heldout_f1: null,
      cost_usd: 0,
      cost_cap_usd: 2,
      created_at: new Date().toISOString(),
      pause: { kind: "questions", token: randomUUID() },
      answers: null,
      scenarios: null,
    };
    project.runs.unshift(run);
    return send(res, 201, runStatus(run));
  }

  const runRoute = path.match(/^\/runs\/([^/]+)(\/.*)?$/);
  if (runRoute) {
    const run = findRun(decodeURIComponent(runRoute[1]), user.id);
    if (!run) return apiError(res, 404, "not_found", "We couldn't find that run.");
    const rest = runRoute[2] ?? "";
    if (req.method === "GET" && rest === "") return send(res, 200, runStatus(run));
    if (req.method === "GET" && rest === "/events") return streamEvents(req, res, url, run);
    if (req.method === "GET" && rest === "/intake") return send(res, 200, intakeOf(run));
    if (req.method === "POST" && rest === "/intake/answers") {
      const body = await answerPause(req, res, run, "questions");
      if (!body) return;
      run.answers = body;
      run.pause = { kind: "scenarios", token: randomUUID() };
      return send(res, 200, { ok: true, message: "Got your answers. We're carrying on with the build." });
    }
    if (req.method === "POST" && rest === "/intake/scenarios") {
      const body = await answerPause(req, res, run, "scenarios");
      if (!body) return;
      run.scenarios = body;
      run.pause = null;
      run.status = "running";
      return send(res, 200, { ok: true, message: "Got your review. The build continues." });
    }
    if (req.method === "GET" && rest === "/agent-card") {
      const card: AgentCard = {
        what_it_does: "Reads a code change and flags problems a careful teammate would catch before it merges.",
        can_do: ["Spot leaked secrets and customer data in logs", "Notice missing login checks on new routes"],
        wont_do: ["Run your code or tests", "Change your code for you"],
      };
      return send(res, 200, card);
    }
    if (req.method === "GET" && rest === "/report-card") {
      const history = [reportRound(0, 14, 4), reportRound(1, 18, 3), reportRound(2, 21, 1)];
      const report: ReportCards = { run_id: run.id, message: history[2].headline, latest: history[2], history };
      return send(res, 200, report);
    }
  }

  return apiError(res, 404, "not_found", `The stub has no API route ${req.method} ${path}`);
}

const publicRun = ({ projectId: _p, pause: _q, answers: _a, scenarios: _s, ...run }: Run): RunSummary => run;

// ---- test-only hooks ---------------------------------------------------------------------

function handleTestHooks(req: IncomingMessage, res: ServerResponse, url: URL) {
  // Stands in for reading the sign-in email.
  if (url.pathname === "/__e2e/inbox") {
    const link = inbox.get((url.searchParams.get("email") ?? "").toLowerCase());
    return link ? send(res, 200, { link }) : send(res, 404, { error: "No sign-in email sent to that address yet." });
  }
  // Stands in for another tab answering first: the step gets a new token, so the old one is stale.
  const moveOn = url.pathname.match(/^\/__e2e\/runs\/([^/]+)\/move-on$/);
  if (req.method === "POST" && moveOn) {
    const found = findRun(decodeURIComponent(moveOn[1]));
    if (!found?.pause) return send(res, 404, { error: "That run isn't paused." });
    found.pause = { ...found.pause, token: randomUUID() };
    return send(res, 204);
  }
  const run = url.pathname.match(/^\/__e2e\/runs\/([^/]+)$/);
  if (run) {
    const found = findRun(decodeURIComponent(run[1]));
    return found ? send(res, 200, found) : send(res, 404, { error: "No such run." });
  }
  if (req.method === "POST" && url.pathname === "/__e2e/reset") {
    for (const store of [otps, inbox, sessions, refreshTokens, users, projects]) store.clear();
    return send(res, 204);
  }
  return send(res, 404, { error: "No such test hook." });
}

// ---- server ------------------------------------------------------------------------------

const server = createServer((req, res) => {
  // The browser calls us from the web app's origin with credentials, so echo the origin rather than "*".
  const origin = req.headers.origin;
  if (origin) {
    res.setHeader("Access-Control-Allow-Origin", origin);
    res.setHeader("Access-Control-Allow-Credentials", "true");
    res.setHeader("Vary", "Origin");
  }
  if (req.method === "OPTIONS") {
    res.setHeader("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS");
    res.setHeader("Access-Control-Allow-Headers", req.headers["access-control-request-headers"] ?? "authorization, content-type");
    return send(res, 204);
  }

  const url = new URL(req.url ?? "/", `http://${req.headers.host}`);
  const handler = url.pathname.startsWith("/auth/v1/")
    ? handleAuth
    : url.pathname.startsWith("/__e2e/")
      ? handleTestHooks
      : handleApi;
  Promise.resolve(handler(req, res, url)).catch((err: unknown) => {
    console.error(err);
    if (!res.headersSent) apiError(res, 500, "stub_error", "The e2e stub crashed; see its log.");
  });
});

server.listen(STUB_PORT, "127.0.0.1", () => {
  console.log(`e2e stub (Supabase Auth + Susanoo API, demo mode) on http://127.0.0.1:${STUB_PORT}`);
});
