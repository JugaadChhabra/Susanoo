# E2E smoke test

Playwright walks the signed-in journey: log in → new project → intake → the run view receives events → the agent page loads. A second test checks the public demo replay. [PRD › Team and ownership › Testing]

It runs against a production build of `apps/web`. `stub/server.ts` stands in for Supabase Auth and the Susanoo API, so no keys, network or database are needed:

- **Auth** follows Supabase's magic-link PKCE flow for real. The test reads the "email" from `GET /__e2e/inbox`.
- **The API is in demo mode**: every run streams the saved run in `tests/fixtures/run_events.json`, so the run view is deterministic.

```bash
make e2e                      # or: cd tests/e2e && npx playwright test
npx playwright show-report    # after a failure
```

Locally an already-running stub or web server on ports 54400 / 3100 is reused; CI always starts fresh ones. The test build bakes the stub's URLs into `apps/web/.next`, so run `npm run build` again before `npm run start` against real services.
