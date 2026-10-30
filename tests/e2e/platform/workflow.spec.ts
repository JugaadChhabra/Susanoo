import { test, expect } from "@playwright/test";
import { resolve } from "node:path";
import { protocolFixture } from "./protocol-fixture.ts";

test("approval, launch, evidence, export, fresh reproduction and cancel with an explicit protocol test double", async ({ page, baseURL }, info) => {
  test.setTimeout(120000);
  const credentials = process.env.PLATFORM_BROWSER_CREDENTIALS;
  test.skip(!credentials, "Ignored local credentials are required for real platform browser checks");
  const fixture = await protocolFixture(baseURL!, credentials!);
  async function login(name: string) {
    const account = fixture.accounts.find(row => row.email === `${name}@susanoo.local`)!;
    await page.goto("/evaluation");
    await page.getByLabel("Email", { exact: true }).fill(account.email);
    await page.getByLabel("Password", { exact: true }).fill(account.password);
    await page.getByRole("button", { name: "Sign In", exact: true }).click();
    await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();
    await page.getByLabel("Active project").selectOption(fixture.project.id);
  }
  async function navigate(name: string) {
    const mobile = page.getByRole("combobox", { name: "Workspace navigation" });
    if (await mobile.isVisible()) await mobile.selectOption(name);
    else await page.getByRole("button", { name, exact: true }).click();
  }
  try {
    await login("reviewer"); await navigate("Review");
    await page.getByRole("button", { name: "Browser Requirement", exact: true }).click();
    await page.getByLabel("Review Rationale").fill("Synthetic browser protocol review, no business qualification claimed");
    await page.getByRole("button", { name: "Approve Revision", exact: true }).click();
    await expect(page.getByText("Exact revision approved.")).toBeVisible();
    await page.getByRole("button", { name: "Sign out" }).click();
    await fixture.ready();
    await login("owner"); await navigate("Runs");
    await page.getByRole("button", { name: "Launch Suite", exact: true }).click();
    await page.getByRole("combobox", { name: "Approved Suite", exact: true }).selectOption(fixture.suite.id);
    await page.getByRole("combobox", { name: "Agent Connection", exact: true }).selectOption(fixture.connection.id);
    await page.getByRole("combobox", { name: "Approved Environment", exact: true }).selectOption(fixture.environment.id);
    await page.getByRole("button", { name: "Launch Approved Suite", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Run Evidence", exact: true })).toBeVisible();
    const attempt = await fixture.reportFailure();
    await expect(page.locator(".ev-status.bad").filter({ hasText: "BLOCKED" }).first()).toBeVisible();
    await page.getByRole("button", { name: "JSON", exact: true }).click();
    const download = page.getByRole("link", { name: "Download", exact: true });
    await expect(download).toBeVisible({ timeout: 20000 });
    const response = await page.request.get((await download.getAttribute("href"))!);
    expect(response.ok()).toBe(true);
    expect((await response.json()).run.counts.fail).toBe(1);
    await page.getByRole("button", { name: "Refresh evidence", exact: true }).click();
    await expect(page.getByText("protocol_test_double_not_business_backend", { exact: false }).first()).toBeVisible();
    await page.screenshot({ path: resolve(`../../.platform/workflow-${info.project.name}.png`), fullPage: true });
    await navigate("Findings");
    const findings = await fixture.owner.call("GET", `/projects/${fixture.project.id}/findings`);
    await page.getByRole("button", { name: findings.items[0].id.slice(0, 8), exact: true }).click();
    await page.getByRole("button", { name: "Fresh Reproduction", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Run Evidence", exact: true })).toBeVisible();
    const reproduced = await fixture.claim("outcome_suite");
    expect(reproduced.namespace).not.toBe(attempt.namespace);
    const cancellation = page.waitForResponse(response => response.url().endsWith(`/runs/${reproduced.run_id}/cancel`) && response.request().method() === "POST");
    await page.getByRole("button", { name: "Cancel Run", exact: true }).click();
    expect((await cancellation).ok()).toBe(true);
    const control = await fixture.runner(`/attempts/${reproduced.attempt_id}/control`, { lease_token: reproduced.lease_token });
    expect(control.cancel_requested).toBe(true);
    await fixture.event(reproduced, 1, "cancel_acknowledged");
    await fixture.finalize(reproduced, "UNKNOWN", "cancelled");
    await expect(page.locator(".ev-status.bad").filter({ hasText: "BLOCKED" }).first()).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  } finally { await fixture.cleanup(); }
});
