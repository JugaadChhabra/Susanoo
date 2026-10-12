import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { STUB_URL, TEST_EMAIL } from "../constants.ts";

/** The last line of the saved demo run (tests/fixtures/run_events.json, seq 42). */
const LAST_DEMO_MESSAGE = "Done. The reviewer now catches 21 of 22 planted problems, up from 14.";

async function magicLink(request: APIRequestContext, email: string): Promise<string> {
  const res = await request.get(`${STUB_URL}/__e2e/inbox`, { params: { email } });
  expect(res.ok(), "a sign-in email was sent").toBe(true);
  return ((await res.json()) as { link: string }).link;
}

/** Phone width: the page must not scroll sideways. [IMPLEMENTATION_JUGAAD.md §7] */
async function expectNoSideScroll(page: Page) {
  const width = await page.evaluate(() => document.documentElement.scrollWidth);
  expect(width, "no horizontal scroll at phone width").toBeLessThanOrEqual(page.viewportSize()!.width);
}

test.beforeEach(async ({ request }) => {
  await request.post(`${STUB_URL}/__e2e/reset`);
});

test("log in, create a project, answer intake, watch the run, open the agent", async ({ page, request }) => {
  await page.setViewportSize({ width: 375, height: 812 });

  // Log in: protected pages bounce to /login, and the magic link brings you back.
  await page.goto("/projects");
  await expect(page).toHaveURL(/\/login\?next=%2Fprojects$/);
  await page.getByLabel("Email").fill(TEST_EMAIL);
  await page.getByRole("button", { name: "Email me a sign-in link" }).click();
  await expect(page.getByText(`we sent a sign-in link to ${TEST_EMAIL}`)).toBeVisible();
  await page.goto(await magicLink(request, TEST_EMAIL));
  await expect(page).toHaveURL(/\/projects$/);
  await expect(page.getByRole("heading", { name: "Your projects" })).toBeVisible();
  await expect(page.getByText("No projects yet")).toBeVisible();
  await expect(page.getByText("3 of 3 runs left today")).toBeVisible();

  // New project: start from the pre-filled code reviewer example; Start saves it and starts a run.
  await page.getByRole("link", { name: "New project" }).first().click();
  await expect(page.getByRole("heading", { name: "New project" })).toBeVisible();
  await expect(page.getByLabel("What should Susanoo build?")).toHaveValue(/code review app/);
  await page.getByRole("button", { name: "Start" }).click();

  // Intake: the run's first pause asks questions, each answer carrying that pause's token.
  await expect(page).toHaveURL(/\/runs\/[0-9a-f-]+\/intake$/);
  const runId = new URL(page.url()).pathname.split("/")[2];
  await expect(page.getByRole("heading", { name: "A few quick questions" })).toBeVisible();
  await expect(page.getByText("Question 1 of 2")).toBeVisible();
  await expectNoSideScroll(page);

  // A stale tab: the step moves on before we answer, so the API says 409 and the screen reloads it.
  await request.post(`${STUB_URL}/__e2e/runs/${runId}/move-on`);
  await page.getByRole("button", { name: "Continue with these answers" }).click();
  await expect(page.getByText(/^This step has moved on/)).toBeVisible();
  await page.getByRole("button", { name: "Continue with these answers" }).click();

  const cards = page.getByRole("listitem").filter({ has: page.getByRole("button", { name: "Approve" }) });
  await expect(cards).toHaveCount(2);
  await expectNoSideScroll(page);
  const build = page.getByRole("button", { name: "Build my agent" });
  await expect(build).toBeDisabled();
  for (const card of await cards.all()) await card.getByRole("button", { name: "Approve" }).click();
  await build.click();

  // Run view: events stream in until the run finishes.
  await expect(page).toHaveURL(new RegExp(`/runs/${runId}$`));
  await expect(page.getByRole("heading", { name: "Your run" })).toBeVisible();
  const log = page.getByRole("region", { name: "What's happening" });
  await expect(log.getByText("Run started. Reading your spec.")).toBeVisible();
  await expect(log.getByText(LAST_DEMO_MESSAGE)).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText("Finished", { exact: true })).toBeVisible();
  await expectNoSideScroll(page);

  // The stub saw exactly what the intake screen sent, with the live pause tokens.
  const saved = (await (await request.get(`${STUB_URL}/__e2e/runs/${runId}`)).json()) as {
    answers: { pause_token: string; answers: Record<string, string> };
    scenarios: { pause_token: string; approved_ids: string[]; rejected_ids: string[]; edits: Record<string, unknown> };
  };
  expect(saved.answers.answers).toEqual({ q1: "Allowed", q2: "Block the change" });
  expect(saved.answers.pause_token).toBeTruthy();
  expect(saved.scenarios).toMatchObject({ approved_ids: ["s1", "s2"], rejected_ids: [], edits: {} });
  expect(saved.scenarios.pause_token).toBeTruthy();

  // Projects: the new project lists its run, and one of today's runs is used.
  await page.goto("/projects");
  await expect(page.getByRole("heading", { name: "Build a code review app for our Python services.", exact: true })).toBeVisible();
  await expect(page.getByText("2 of 3 runs left today")).toBeVisible();
  await expect(page.locator(`a[href="/runs/${runId}"]`)).toBeVisible();
  await expectNoSideScroll(page);

  // Agent page: card, report card, preview and Try it all load.
  await page.goto(`/runs/${runId}/agent`);
  await expect(page.getByRole("heading", { name: "Your agent", level: 1 })).toBeVisible();
  await expect(page.getByText("Reads a code change and flags problems")).toBeVisible();
  await expect(page.getByText("What it won't do")).toBeVisible();
  await expect(page.getByText("Caught 21 of 22 problems, 1 false alarm.")).toBeVisible();
  await expect(page.getByText("The preview isn't ready yet.")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Try it" })).toBeVisible();
  await expectNoSideScroll(page);
});

test("the public demo replays the saved run without logging in", async ({ page }) => {
  await page.goto("/demo");
  await expect(page.getByRole("heading", { name: "Watch Susanoo work" })).toBeVisible();
  await expect(page.getByText("Replay of a saved run")).toBeVisible();
  await expect(page.getByText("Run started. Reading your spec.")).toBeVisible();
});

test("the landing page shows the saved run's before/after and plays the replay in place", async ({ page }) => {
  await page.goto("/");
  const proof = page.locator("#proof");
  await expect(proof.getByRole("heading", { name: "Before and after a fix, from a real run" })).toBeVisible();
  await expect(proof.getByLabel("before 88%")).toBeVisible();
  await expect(proof.getByLabel("after 100%")).toBeVisible();
  await page.locator("#demo").getByRole("button", { name: "Play the replay" }).click();
  await expect(page.locator("#demo").getByText("Run started. Reading your spec.")).toBeVisible();
});
