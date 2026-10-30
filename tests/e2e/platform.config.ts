import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./platform",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 60000,
  expect: { timeout: 15000 },
  outputDir: "../../.platform/browser-results",
  reporter: "list",
  use: { baseURL: process.env.PLATFORM_WEB_URL ?? "http://localhost:3000", trace: "retain-on-failure", actionTimeout: 15000 },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 1000 } } },
    { name: "mobile", use: { ...devices["iPhone 13"], defaultBrowserType: "chromium" } },
  ],
});
