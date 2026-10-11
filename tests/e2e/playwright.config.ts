import { defineConfig, devices } from "@playwright/test";
import { ANON_KEY, DEMO_RUN_ID, STUB_URL, WEB_PORT, WEB_URL } from "./constants.ts";

const CI = Boolean(process.env.CI);

/**
 * Smoke test of the signed-in journey against a production build of apps/web, with Supabase Auth
 * and the API replaced by stub/server.ts in demo mode. [PRD › Team and ownership › Testing]
 */
export default defineConfig({
  testDir: "./specs",
  fullyParallel: false,
  workers: 1, // the stub keeps one in-memory world
  forbidOnly: CI,
  retries: 0, // the stub makes runs deterministic, so a retry would only hide a real failure
  reporter: CI ? [["list"], ["html", { open: "never" }]] : "list",
  timeout: 60_000,
  expect: { timeout: 10_000 },
  use: {
    baseURL: WEB_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command: "node stub/server.ts",
      url: `${STUB_URL}/health`,
      reuseExistingServer: !CI,
      stdout: "pipe",
    },
    {
      // A production build, so dev-only fixture shortcuts are off and the real data paths run.
      command: `npm run build && npm run start -- --port ${WEB_PORT}`,
      cwd: "../../apps/web",
      url: WEB_URL,
      reuseExistingServer: !CI,
      timeout: 300_000,
      env: {
        NEXT_PUBLIC_SUPABASE_URL: STUB_URL,
        NEXT_PUBLIC_SUPABASE_ANON_KEY: ANON_KEY,
        NEXT_PUBLIC_API_URL: STUB_URL,
        NEXT_PUBLIC_DEMO_RUN_ID: DEMO_RUN_ID,
        NEXT_PUBLIC_SITE_URL: WEB_URL,
        NEXT_TELEMETRY_DISABLED: "1",
      },
    },
  ],
});
