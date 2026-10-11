/** Ports, URLs and ids shared by the Playwright config, the stub server and the specs. */

export const WEB_PORT = 3100;
export const STUB_PORT = 54400;

// Different hosts on purpose: the web app's cookies stay on localhost, like a real deploy.
export const WEB_URL = `http://localhost:${WEB_PORT}`;
export const STUB_URL = `http://127.0.0.1:${STUB_PORT}`;

/** Not a secret: the stub accepts any anon key, the app just needs one to turn sign-in on. */
export const ANON_KEY = "e2e-anon-key";

/** The saved run the stub replays (tests/fixtures/run_events.json). */
export const DEMO_RUN_ID = "7d3f0c5e-2b8a-4e61-9a47-c1d5e8f20b36";

export const TEST_EMAIL = "smoke@example.com";
