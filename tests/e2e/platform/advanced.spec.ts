import { test, expect } from "@playwright/test";
import { resolve } from "node:path";
import { protocolFixture } from "./protocol-fixture.ts";

test("typed comparison review and collector controls use the actual platform", async ({ page, baseURL }, info) => {
  test.setTimeout(120000);
  const credentials = process.env.PLATFORM_BROWSER_CREDENTIALS;
  test.skip(!credentials, "Local synthetic platform accounts required");
  const fixture = await protocolFixture(baseURL!, credentials!);
  async function navigate(name: string) {
    const mobile = page.getByRole("combobox", { name: "Workspace navigation" });
    if (await mobile.isVisible()) await mobile.selectOption(name);
    else await page.getByRole("button", { name, exact: true }).click();
  }
  async function login(name: string) {
    const account = fixture.accounts.find(row => row.email === `${name}@susanoo.local`)!;
    await page.goto("/evaluation");
    await page.getByLabel("Email", { exact: true }).fill(account.email);
    await page.getByLabel("Password", { exact: true }).fill(account.password);
    await page.getByRole("button", { name: "Sign In", exact: true }).click();
    await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();
    await page.getByLabel("Active project").selectOption(fixture.project.id);
  }
  try {
    const { run } = await fixture.comparison();
    await login("reviewer");
    expect((await fixture.reviewer.call("GET", `/projects/${fixture.project.id}`)).roles).toContain("reviewer");
    await navigate("Settings");
    await expect(page.locator(".ev-status").filter({ hasText: "reviewer" }).first()).toBeVisible();
    await navigate("Runs");
    await page.getByRole("button", { name: run.id.slice(0, 8), exact: true }).click();
    const reports = page.getByRole("region", { name: "Runtime reports" });
    await expect(reports.getByText("Synthetic duplicate state", { exact: true })).toBeVisible();
    await reports.getByRole("combobox", { name: "Classification", exact: true }).selectOption("intentional_simplification");
    await reports.getByLabel("Rationale", { exact: true }).fill("Reviewed synthetic protocol scope only");
    await reports.getByRole("button", { name: "Record Classification", exact: true }).click();
    await expect(reports.getByText("Reviewed synthetic protocol scope only", { exact: true })).toBeVisible();
    await expect(reports.getByRole("cell", { name: "unresolved", exact: true })).toBeVisible();
    await page.screenshot({ path: resolve(`../../.platform/advanced-${info.project.name}.png`), fullPage: true });
    await page.getByRole("button", { name: "Sign out" }).click();
    await login("owner");
    await navigate("Settings");
    await page.getByRole("button", { name: "Issue Collector Credential", exact: true }).click();
    await expect(page.getByText("New Collector Credential", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Hide collector credential", exact: true }).click();
    await expect(page.getByText("New Collector Credential", { exact: true })).toHaveCount(0);
    await page.getByRole("button", { name: "Revoke collector credential", exact: true }).click();
    await expect(page.getByRole("cell", { name: "Revoked", exact: true })).toBeVisible();
    await page.screenshot({ path: resolve(`../../.platform/operations-${info.project.name}.png`), fullPage: true });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await expect(page.locator('.ev-error[role="alert"]')).toHaveCount(0);
  } finally { await fixture.cleanup(); }
});
