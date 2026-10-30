import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

test("private console navigates real API/database and preserves permission boundaries", async ({ page }, info) => {
  const path = process.env.PLATFORM_BROWSER_CREDENTIALS;
  test.skip(!path, "Set PLATFORM_BROWSER_CREDENTIALS to the ignored local seed credentials file");
  const accounts = JSON.parse(readFileSync(path!, "utf8")).synthetic_local_accounts as { email: string; password: string }[];
  const owner = accounts.find(row => row.email === "owner@susanoo.local")!;
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto("/evaluation");
  await page.getByLabel("Email", { exact: true }).fill(owner.email);
  await page.getByLabel("Password", { exact: true }).fill(owner.password);
  await page.getByRole("button", { name: "Sign In", exact: true }).click();
  await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();
  await expect(page.getByLabel("Active project")).toHaveValue(/.+/);
  for (const section of ["Connections", "Cases", "Runs", "Simulator", "Findings", "Repairs", "Review", "Settings"]) {
    const navigation = page.getByRole("combobox", { name: "Workspace navigation" });
    if (await navigation.isVisible()) await navigation.selectOption(section);
    else await page.getByRole("button", { name: section, exact: true }).click();
    await expect(page.getByRole("heading", { name: section, exact: true, level: 2 })).toBeVisible();
    await expect(page.locator('.ev-error[role="alert"]')).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  }
  await page.screenshot({ path: resolve(`../../.platform/console-${info.project.name}.png`), fullPage: true });
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByRole("heading", { name: "Sign In", exact: true })).toBeVisible();
  await expect(page.locator('.ev-error[role="alert"]')).toHaveCount(0);
  await page.getByRole("link", { name: "Reset Password" }).click();
  await expect(page.getByRole("heading", { name: "Reset Password", exact: true })).toBeVisible();
  await page.goto("/evaluation/accept");
  await expect(page.getByRole("button", { name: "Send Reset Link" })).toBeDisabled();
  expect(errors).toEqual([]);
});
