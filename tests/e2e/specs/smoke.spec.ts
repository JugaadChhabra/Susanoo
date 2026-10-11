import { expect, test, type APIRequestContext } from "@playwright/test";
import { STUB_URL, TEST_EMAIL } from "../constants.ts";

/** The last line of the saved demo run (tests/fixtures/run_events.json, seq 42). */
const LAST_DEMO_MESSAGE = "Done. The reviewer now catches 21 of 22 planted problems, up from 14.";

async function magicLink(request: APIRequestContext, email: string): Promise<string> {
  const res = await request.get(`${STUB_URL}/__e2e/inbox`, { params: { email } });
  expect(res.ok(), "a sign-in email was sent").toBe(true);
  return ((await res.json()) as { link: string }).link;
}

test.beforeEach(async ({ request }) => {
  await request.post(`${STUB_URL}/__e2e/reset`);
});

test("log in, create a project, answer intake, watch the run, open the agent", async ({ page, request }) => {
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

  // New project: start from the pre-filled code reviewer example.
  await page.getByRole("link", { name: "New project" }).first().click();
  await expect(page.getByRole("heading", { name: "New project" })).toBeVisible();
  await expect(page.getByLabel("What should Susanoo build?")).toHaveValue(/code review app/);
  await page.getByRole("button", { name: "Start" }).click();
  await expect(page).toHaveURL(/\/projects$/);
  await expect(page.getByRole("heading", { name: "Build a code review app for our Python services" })).toBeVisible();
  await expect(page.getByText("2 of 3 runs left today")).toBeVisible();
  const runLink = page.getByRole("link", { name: /Running/ });
  const runHref = await runLink.getAttribute("href");
  expect(runHref).toMatch(/^\/runs\/[0-9a-f-]+$/);
  const runId = runHref!.split("/").pop()!;

  // Intake: nothing links a project to its intake until the runs router lands, so go there directly.
  await page.goto(`${runHref}/intake`);
  await expect(page.getByRole("heading", { name: "A few quick questions" })).toBeVisible();
  await page.getByRole("button", { name: "Continue with these answers" }).click();
  const cards = page.getByRole("listitem").filter({ has: page.getByRole("button", { name: "Approve" }) });
  await expect(cards).toHaveCount(2);
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

  // The stub saw exactly what the intake screen sent.
  const saved = (await (await request.get(`${STUB_URL}/__e2e/runs/${runId}`)).json()) as {
    answers: Record<string, string>;
    decisions: { scenario_id: string; action: string }[];
  };
  expect(saved.answers).toEqual({ q1: "Allowed", q2: "Block the change" });
  expect(saved.decisions).toEqual([
    { scenario_id: "s1", action: "approve" },
    { scenario_id: "s2", action: "approve" },
  ]);

  // Agent page: card, report card, preview and Try it all load.
  await page.goto(`/runs/${runId}/agent`);
  await expect(page.getByRole("heading", { name: "Your agent", level: 1 })).toBeVisible();
  await expect(page.getByText("Reads a code change and flags problems")).toBeVisible();
  await expect(page.getByText("Caught 21 of 22 problems, 1 false alarm.")).toBeVisible();
  await expect(page.getByText("The preview isn't ready yet.")).toBeVisible();
});

test("the public demo replays the saved run without logging in", async ({ page }) => {
  await page.goto("/demo");
  await expect(page.getByRole("heading", { name: "Watch Susanoo work" })).toBeVisible();
  await expect(page.getByText("Replay of a saved run")).toBeVisible();
  await expect(page.getByText("Run started. Reading your spec.")).toBeVisible();
});
