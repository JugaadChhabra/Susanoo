// Records the demo video screen track segment by segment (CDP screencast frames with timestamps),
// following the timeline in the narration. Usage: node record.mjs <outDir> [segmentName...]
import { chromium } from "../../../tests/e2e/node_modules/playwright/index.mjs";
import fs from "node:fs";
import path from "node:path";

const OUT = path.resolve(process.argv[2] ?? "out");
const ONLY = process.argv.slice(3);
const LIVE = "https://app.susanoo.jugaadchhabra.dev";
const LOCAL = "http://localhost:3100"; // signed-in copy backed by tests/e2e/stub
const STUB = "http://127.0.0.1:54400";
const PLAN = "file://" + path.resolve(import.meta.dirname, "../../../susanoo-business-plan.html");
const VIEW = { width: 1600, height: 900 };
const DSF = 1.2; // 1920x1080 frames

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const CURSOR_JS = `(() => {
  const mk = () => {
    if (document.getElementById('__cur')) return;
    const c = document.createElement('div'); c.id = '__cur';
    c.innerHTML = '<svg width="26" height="26" viewBox="0 0 24 24"><path d="M4 2 L4 19 L8.5 15 L11.5 22 L14.5 20.7 L11.6 14 L17.5 14 Z" fill="#111" stroke="#fff" stroke-width="1.4" stroke-linejoin="round"/></svg>';
    Object.assign(c.style, {position:'fixed', left:'0', top:'0', zIndex:2147483647, pointerEvents:'none', transform:'translate(' + (window.__cx ?? 1100) + 'px,' + (window.__cy ?? 520) + 'px)', filter:'drop-shadow(0 1px 2px rgba(0,0,0,.35))'});
    document.documentElement.appendChild(c);
    addEventListener('mousemove', e => { window.__cx = e.clientX; window.__cy = e.clientY; c.style.transform = 'translate(' + e.clientX + 'px,' + e.clientY + 'px)'; }, true);
  };
  if (document.readyState === 'loading') addEventListener('DOMContentLoaded', mk); else mk();
})();`;

// Shared mouse position across a page so moves are continuous.
const mouse = new WeakMap();
async function moveTo(page, x, y, ms = 800) {
  const [x0, y0] = mouse.get(page) ?? [1100, 520];
  const n = Math.max(2, Math.round(ms / 16));
  for (let i = 1; i <= n; i++) {
    const t = i / n, e = t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
    await page.mouse.move(x0 + (x - x0) * e, y0 + (y - y0) * e);
    await sleep(ms / n);
  }
  mouse.set(page, [x, y]);
}
async function moveToEl(page, locator, ms = 800, dx = 0.5, dy = 0.5) {
  const b = await locator.boundingBox();
  if (!b) throw new Error("no box for " + locator);
  await moveTo(page, b.x + b.width * dx, b.y + b.height * dy, ms);
}
// Eased scroll to an absolute Y over ms.
async function scrollTo(page, y, ms) {
  await page.evaluate(([y, ms]) => new Promise((res) => {
    const y0 = scrollY, t0 = performance.now();
    const step = (now) => {
      const t = Math.min(1, (now - t0) / ms), e = t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
      scrollTo(0, y0 + (y - y0) * e);
      t < 1 ? requestAnimationFrame(step) : res();
    };
    requestAnimationFrame(step);
  }), [y, ms]);
}
const scrollBy = async (page, dy, ms) => scrollTo(page, (await page.evaluate(() => scrollY)) + dy, ms);
const docY = (page, locator) => locator.evaluate((e) => e.getBoundingClientRect().top + scrollY);

async function newPage(ctx) {
  const page = await ctx.newPage();
  return page;
}

// Records `act` for exactly `dur` seconds of timeline; returns frames [{t, file}].
async function capture(page, name, dur, act) {
  const dir = path.join(OUT, name);
  fs.rmSync(dir, { recursive: true, force: true });
  fs.mkdirSync(dir, { recursive: true });
  const cdp = await page.context().newCDPSession(page);
  const frames = [];
  let t0 = null, i = 0;
  cdp.on("Page.screencastFrame", async (f) => {
    cdp.send("Page.screencastFrameAck", { sessionId: f.sessionId }).catch(() => {});
    if (t0 === null) return;
    const t = f.metadata.timestamp - t0;
    const file = `f${String(i++).padStart(5, "0")}.jpg`;
    fs.writeFileSync(path.join(dir, file), Buffer.from(f.data, "base64"));
    frames.push({ t, file });
  });
  await cdp.send("Page.startScreencast", { format: "jpeg", quality: 92, maxWidth: 1920, maxHeight: 1080, everyNthFrame: 1 });
  await sleep(300);
  // Nudge a repaint so the first frame arrives at t=0.
  await page.evaluate(() => { document.documentElement.style.outline = '0px solid transparent'; });
  t0 = Date.now() / 1000;
  await page.screenshot({ path: path.join(dir, "first.jpg"), type: "jpeg", quality: 92 });
  frames.push({ t: 0, file: "first.jpg" });
  const start = Date.now();
  const at = async (s) => { const w = start + s * 1000 - Date.now(); if (w > 0) await sleep(w); };
  await act(at);
  await at(dur + 0.3);
  await cdp.send("Page.stopScreencast");
  await sleep(200);
  frames.sort((a, b) => a.t - b.t);
  fs.writeFileSync(path.join(dir, "frames.json"), JSON.stringify({ dur, frames }));
  console.log(`${name}: ${frames.length} frames over ${dur}s`);
}

// Screencast frames come out at CSS-pixel size unless the browser itself runs at the scale factor,
// so each layout gets its own browser: 1600x900 at 1.2 (marketing pages) and 1280x720 at 1.5 (app,
// GitHub, plan), both 1920x1080 on video.
const launch = (w, h, dsf) => chromium.launch({ args: ["--hide-scrollbars", "--force-color-profile=srgb", `--force-device-scale-factor=${dsf}`, `--window-size=${w},${h}`] });
const browsers = { wide: await launch(VIEW.width, VIEW.height, DSF), big: await launch(1280, 720, 1.5) };
const mkCtx = (big = false) => browsers[big ? "big" : "wide"].newContext({ viewport: null, colorScheme: "light" });

// Timeline cut points, taken from the pauses in narration-sarvam-timed.mp3.
const SEG = {
  s1: [0, 9.66], s2: [9.66, 18.64], s3: [18.64, 23.05], s4: [23.05, 48.0],
  s5: [48.0, 63.5], s6: [63.5, 75.6], s7: [75.6, 89.88], s8: [89.88, 96.3],
};
const D = (k) => +(SEG[k][1] - SEG[k][0]).toFixed(3);
const rel = (k, abs) => abs - SEG[k][0];
const want = (k) => ONLY.length === 0 || ONLY.includes(k);

async function preparedLanding(ctx) {
  const page = await newPage(ctx);
  await page.addInitScript(CURSOR_JS);
  await page.goto(LIVE + "/", { waitUntil: "networkidle" });
  await page.evaluate(() => document.fonts.ready);
  await sleep(4000); // let the hero finish composing itself
  await page.mouse.move(1150, 560); mouse.set(page, [1150, 560]);
  return page;
}

// s1: hold on the hero, then scroll to the dark spec band.
if (want("s1")) {
  const ctx = await mkCtx();
  const page = await preparedLanding(ctx);
  const band = page.locator("#spec-heading").locator("xpath=ancestor::section[1]");
  const bandY = await docY(page, band);
  const bandH = (await band.boundingBox()).height;
  await capture(page, "s1", D("s1"), async (at) => {
    await at(rel("s1", 6.0));
    await scrollTo(page, bandY - Math.max(0, (VIEW.height - bandH) / 2), 3000);
  });
  await ctx.close();
}

// s2: signed-in New project screen (local copy, stubbed auth).
if (want("s2")) {
  const ctx = await mkCtx(true);
  const page = await newPage(ctx);
  await page.addInitScript(CURSOR_JS);
  await fetch(STUB + "/__e2e/reset", { method: "POST" });
  await page.goto(LOCAL + "/projects/new");
  await page.getByLabel("Email").fill("jugaad@susanoo.dev");
  await page.getByRole("button", { name: "Email me a sign-in link" }).click();
  await sleep(800);
  const { link } = await (await fetch(STUB + "/__e2e/inbox?email=jugaad%40susanoo.dev")).json();
  await page.goto(link);
  await page.goto(LOCAL + "/projects/new", { waitUntil: "networkidle" });
  await page.getByLabel("What should Susanoo build?").waitFor();
  await page.evaluate(() => document.fonts.ready);
  await sleep(1500);
  await page.mouse.move(900, 450); mouse.set(page, [900, 450]);
  const box = page.getByLabel("What should Susanoo build?");
  const rules = page.getByText(/^Rules/).first();
  await capture(page, "s2", D("s2"), async (at) => {
    await at(0.2);
    await moveToEl(page, box, 900, 0.6, 0.45);
    await at(rel("s2", 15.0));
    const ry = await docY(page, rules);
    await scrollTo(page, Math.max(0, ry - 200), 2200);
    await moveToEl(page, rules, 700, 0.3, 0.5);
  });
  await ctx.close();
}

// s3: the eight heads section; the lit card moves on its own.
if (want("s3")) {
  const ctx = await mkCtx();
  const page = await newPage(ctx);
  await page.addInitScript(CURSOR_JS);
  await page.goto(LIVE + "/#how-it-works", { waitUntil: "networkidle" });
  await page.evaluate(() => document.fonts.ready);
  const sec = page.locator("#how-it-works");
  const first = page.getByRole("heading", { name: "Planner", exact: true }).first();
  const top = (await docY(page, first)) - 210;
  await page.evaluate((y) => scrollTo(0, y), top);
  await sleep(2500);
  await page.mouse.move(1300, 470); mouse.set(page, [1300, 470]);
  await capture(page, "s3", D("s3"), async () => {});
  await ctx.close();
}

// s4: demo replay. Its own pace (one event per 0.9 s) doesn't match the narration, so the replay's
// timer is rescheduled to show each event when the narration reaches it. Times are video-timeline seconds.
const R0 = 21.0; // when the replay is released; s4 starts at 23.05, so the Planner events are already in
const EVENT_AT = [
  ...Array.from({ length: 9 }, (_, i) => 21.0 + i * 0.25), // 0-8 Planner
  23.6, 24.2, 24.8, 25.3, 25.9, // 9-13 Builder
  26.4, 27.6, // 14-15 Agent Smith
  28.6, 29.3, 30.0, 30.8, // 16-19 Simulator: "32 scenarios ready"
  32.4, 33.2, 34.0, // 20-22 Runner baseline
  34.6, 34.9, 35.2, 35.5, 35.8, 36.1, 36.4, // 23-29 test-layer round, compressed
  37.0, // 30 "3 of 28 failed ... a live warehouse access token"
  39.5, 45.3, // 31-32 Diagnostician: "Found 1 cause: the agent's instructions"
  48.3, 49, 49.5, 50, 50.5, 51, 51.5, 52, 52.5, 53, 53.5, 54, // 33-44, after the cut
];
const PACE_JS = (sched) => `(() => {
  const orig = window.setTimeout.bind(window);
  window.__sched = null; window.__k = 0;
  window.setTimeout = (fn, d, ...a) => {
    if (window.__sched && (d === 900 || d === 3600)) {
      const k = ++window.__k;
      const due = window.__rel + (window.__sched[k] ?? 1e6) * 1000;
      return orig(fn, Math.max(0, due - performance.now()), ...a);
    }
    return orig(fn, d, ...a);
  };
  window.__arm = () => { window.__rel = performance.now(); window.__sched = ${JSON.stringify(sched)}; };
})();`;
if (want("s4")) {
  const ctx = await mkCtx(true);
  const page = await newPage(ctx);
  await page.addInitScript(CURSOR_JS);
  await page.addInitScript(PACE_JS(EVENT_AT.map((t) => t - R0)));
  let releaseDemo;
  await page.route(/api\.susanoo\.jugaadchhabra\.dev\/demo\//, async (route) => {
    const res = await route.fetch(); const body = await res.body();
    await new Promise((r) => (releaseDemo = r)); // hold the events until the capture is running
    await route.fulfill({ response: res, body });
  });
  await page.goto(LIVE + "/demo", { waitUntil: "domcontentloaded" });
  await page.getByText("Watch Susanoo work").waitFor();
  await page.evaluate(() => document.fonts.ready);
  while (!releaseDemo) await sleep(20);
  await sleep(1000);
  await page.mouse.move(1000, 300); mouse.set(page, [1000, 300]);
  await capture(page, "s4raw", 48.5 - R0, async (at) => {
    await page.evaluate(() => window.__arm());
    releaseDemo();
    await at(0.6);
    await scrollTo(page, 90, 1200); // tuck the title away so the whole graph is in view
    await at(32.4 - R0);
    const sb = await docY(page, page.getByRole("heading", { name: "Scoreboard" }));
    await scrollTo(page, sb - 150, 3000);
    await at(39.3 - R0);
    const bottom = () => page.evaluate(() => document.documentElement.scrollHeight - innerHeight);
    await scrollTo(page, await bottom(), 2500);
    await moveTo(page, 760, 560, 900);
    await at(45.6 - R0); // "Found 1 cause: the agent's instructions" has just landed at the foot of the log
    await scrollTo(page, await bottom(), 400);
    await moveTo(page, 640, 640, 900);
  });
  const a = SEG.s4[0] - R0, b = SEG.s4[1] - R0;
  const src = JSON.parse(fs.readFileSync(path.join(OUT, "s4raw", "frames.json")));
  const before = src.frames.filter((f) => f.t <= a).at(-1);
  const frames = [{ t: 0, file: "../s4raw/" + before.file }, ...src.frames.filter((f) => f.t > a && f.t < b).map((f) => ({ t: f.t - a, file: "../s4raw/" + f.file }))];
  fs.mkdirSync(path.join(OUT, "s4"), { recursive: true });
  fs.writeFileSync(path.join(OUT, "s4", "frames.json"), JSON.stringify({ dur: b - a, frames }));
  await ctx.close();
}

// s5: the Fixer's commit on GitHub; point at the Entire-Checkpoint line.
if (want("s5")) {
  const ctx = await mkCtx(true);
  const page = await newPage(ctx);
  await page.addInitScript(CURSOR_JS);
  await page.goto("https://github.com/susanoo-apps/python-code-review-hero7/commit/60edf30", { waitUntil: "load" });
  await page.getByText(/Entire-Checkpoint:/).first().waitFor();
  await page.addStyleTag({ content: "ghcc-consent,#ghcc,.js-cookie-consent-banner{display:none!important}" });
  await sleep(1500);
  await page.mouse.move(950, 330); mouse.set(page, [950, 330]);
  const cp = page.getByText(/Entire-Checkpoint:/).first();
  await capture(page, "s5", D("s5"), async (at) => {
    await at(0.3);
    await moveToEl(page, page.getByText(/fix\(prompt_policy\)/).first(), 1000, 0.6, 0.5);
    await at(rel("s5", 56.0));
    await moveToEl(page, cp, 1100, 0.85, 0.55);
  });
  await ctx.close();
}

// s6: the before/after stat cards, then the paragraph with the cost.
if (want("s6")) {
  const ctx = await mkCtx();
  const page = await newPage(ctx);
  await page.addInitScript(CURSOR_JS);
  await page.goto(LIVE + "/#proof", { waitUntil: "networkidle" });
  await page.evaluate(() => document.fonts.ready);
  const py = await docY(page, page.locator("#proof"));
  await page.evaluate((y) => scrollTo(0, y), py);
  await sleep(3000);
  await page.mouse.move(1180, 560); mouse.set(page, [1180, 560]);
  const cost = page.locator("#proof").getByText(/\$1\.75/).first();
  await capture(page, "s6", D("s6"), async (at) => {
    await at(rel("s6", 70.0));
    const cy = await docY(page, cost);
    await scrollTo(page, cy - 520, 3500);
    await moveToEl(page, cost, 900, 0.5, 0.5);
  });
  await ctx.close();
}

// s7: cfo.ai business plan page.
if (want("s7")) {
  const ctx = await mkCtx(true);
  const page = await newPage(ctx);
  await page.addInitScript(CURSOR_JS);
  await page.goto(PLAN, { waitUntil: "load" });
  await page.addStyleTag({ content: "@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans+Condensed:wght@400;600&display=swap');" });
  await sleep(1500);
  await page.evaluate(() => document.fonts.ready);
  await page.mouse.move(950, 520); mouse.set(page, [950, 520]);
  const cash = page.getByText("$13,488");
  await capture(page, "s7", D("s7"), async (at) => {
    await at(0.3);
    await moveToEl(page, page.getByText("76%"), 1000, 0.5, 0.6);
    await at(rel("s7", 83.0));
    const ty = await docY(page, page.getByRole("heading", { name: "12-month forecast" }));
    await scrollTo(page, ty - 30, 2500);
    await moveToEl(page, cash, 900, 0.6, 0.55);
  });
  await ctx.close();
}

// s8: back to the hero to close.
if (want("s8")) {
  const ctx = await mkCtx();
  const page = await preparedLanding(ctx);
  await capture(page, "s8", D("s8"), async () => {});
  await ctx.close();
}

await Promise.all(Object.values(browsers).map((b) => b.close()));
