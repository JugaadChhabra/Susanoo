---
name: deploy-check
description: Deploy Susanoo or verify the live deployment after a push — Vercel web, EC2 Docker Compose (api, worker, caddy), Supabase, Cloudflare DNS, generated-app preview containers. Use after pushes touching api/worker/web/infra, or when the live site misbehaves.
---

# Deploy and check

Owner: Jugaad (infra); both use the checks. Sources: **[PRD › Deployment]**, **[PRD › Tech stack]**, **[PRD › Connectors and API budget › Anthropic budget]**.

## How deploys happen
- **Web:** Vercel deploys on push to `main`.
- **Backend:** `.github/workflows/deploy-backend.yml` SSHes to EC2 and runs `docker compose pull && docker compose up -d` (api, worker, caddy).
- **Database:** Supabase migrations in `supabase/migrations/`, applied deliberately, never edited after they've run. A change means a new numbered migration.
- **Never deploy by hand-editing files on the server.** If a hotfix is unavoidable, commit the same change right after.

## After a push, check
1. GitHub Actions: `ci`, `commit-guard` and `deploy-backend` are green.
2. `curl -fsS https://api.<domain>/health` returns OK and reads from the database.
3. `https://app.<domain>` loads; logging in works; `(app)` routes redirect when logged out.
4. The demo replay `/demo` works without login. This is the safety net.
5. If the worker changed: start a small run and watch events arrive over SSE in the run view.
6. If the sandbox changed: a generated app is reachable at `https://run-<id>.<domain>`; the container has CPU and memory caps and no host mounts (`docker inspect`); idle containers get reaped.

## Safety checks before going public
- [ ] Every API route needs auth except `/health` and `/demo/*`
- [ ] Quota (3 runs per user per day) and the global daily cap are enforced
- [ ] Per-run cost cap is set (`RUN_COST_CAP_USD`)
- [ ] Anthropic Console spend limit is set, as a backstop
- [ ] Secrets only in Vercel and EC2 env vars; `.env` never committed
- [ ] Generated apps use the separate `ANTHROPIC_APP_API_KEY`

## When something is broken
- **Roll back by reverting the commit** (`git revert <sha>`, then push), never with a force push.
- **Check logs:** `docker compose logs --tail=200 api worker caddy` on EC2, and the Vercel build logs.
- **If live runs are failing near the demo, switch the marketing CTA to demo mode** and keep fixing.
