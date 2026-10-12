# Deploying the backend

[PRD › Deployment]. Web deploys from Vercel on push to `main`. This page covers the backend (api, worker, caddy on EC2).

## Live setup (Mon 12 Oct 2026)

- **Server:** one EC2 `t3.large`, Ubuntu 24.04 x86, 40 GB gp3, in the hackathon AWS account: **us-east-1 only, the existing `Hackathon_VPC` only, one instance per user** (a second instance is auto-terminated; terminate before replacing), t2/t3/t3a up to `.large`, EBS ≤ 50 GB. Elastic IP `35.173.112.138`. Security group: 22, 80 and 443 open (22 because GitHub Actions deploys over SSH).
- **Stack:** `/opt/susanoo/` holds `docker-compose.yml`, `Caddyfile` and `.env`; the workflow copies the first two on every deploy.
- **Worker:** has the Docker socket (group `DOCKER_GID`), a named volume for run workdirs (`/data/runs`), and joins `susanoo_sandbox` with Caddy; generated-app containers join only that network.
- **Live checks:** `/health` OK, `/demo/<id>` 200, signed-in routes 401 without a token.
- **Previews (#31):** Caddy serves `run-<id>.<PREVIEW_DOMAIN>` with a certificate per host, obtained on first visit only after `http://api:8000/internal/preview-allowed` confirms the run exists with a `preview_url` recorded. It proxies to `susanoo-run-<id>:8000` on `susanoo_sandbox`. Needs `PREVIEW_DOMAIN` in `/opt/susanoo/.env`. Keeping a finished run's app up and writing its `preview_url` is #32.

## Hostnames

Base domain: `susanoo.jugaadchhabra.dev` (Cloudflare DNS).

| Host | Goes to |
|---|---|
| `app.susanoo.jugaadchhabra.dev` | Vercel (Next.js dashboard) |
| `api.susanoo.jugaadchhabra.dev` | EC2 → Caddy → `api:8000` |
| `run-<id>.susanoo.jugaadchhabra.dev` | EC2 → Caddy → `susanoo-run-<id>:8000` on `susanoo_sandbox` (on-demand TLS) |

Cloudflare records `api.susanoo` and `*.susanoo` are A records to the Elastic IP, **DNS only** (grey cloud) so Caddy gets its own certificates; `app.susanoo` and `susanoo` point at Vercel and win over the wildcard.

## Where each secret lives

| Secret | Lives in | Used by |
|---|---|---|
| `EC2_HOST`, `EC2_USER`, `EC2_SSH_KEY` | GitHub repo secrets | `deploy-backend.yml` (scp + ssh) |
| `GITHUB_TOKEN` | provided by GitHub Actions | pushes images to GHCR, and logs the server in to pull them |
| `API_DOMAIN`, Supabase keys, Anthropic keys, `RUN_COST_CAP_USD`, etc. | `/opt/susanoo/.env` on EC2 (never committed) | api, worker, caddy via `env_file` |
| Web env vars | Vercel project settings | `apps/web` |

`.env.example` lists names only.

## One-time server setup

1. Install Docker and the compose plugin; open ports 80 and 443 in the security group.
2. `sudo mkdir -p /opt/susanoo && sudo chown $USER /opt/susanoo`
3. Create `/opt/susanoo/.env` (`chmod 600`). Names only here; values come from Supabase, Anthropic, neatlogs and GitHub:
   - Supabase: `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_JWT_SECRET`, `DATABASE_URL` (**session pooler, port 5432**; the worker refuses 6543)
   - Domains: `APP_DOMAIN`, `API_DOMAIN`, `PREVIEW_DOMAIN`
   - Limits: `RUNS_PER_USER_PER_DAY`, `RUN_COST_CAP_USD`, `GLOBAL_DAILY_CAP_USD` (must be a number, or every new run is refused)
   - Models and tracing: `ANTHROPIC_API_KEY` (heads), `ANTHROPIC_APP_API_KEY` (generated apps, a separate key), `NEATLOGS_API_KEY`, `NEATLOGS_TRACE_URL_TEMPLATE` (`https://app.neatlogs.com/traces/{trace_id}?orgId=…&projectId=…`)
   - Demo: `DEMO_RUN_IDS` (the saved hero-2 run)
   - Generated repos: `GITHUB_ORG=susanoo-apps`, `GITHUB_ORG_TOKEN` (fine-grained, org owner, Administration + Contents read/write)
   - Host: `DOCKER_GID` (`getent group docker | cut -d: -f3`)

   After editing: `cd /opt/susanoo && docker compose up -d`.
4. Make the GHCR packages readable by the server (the workflow logs in with `GITHUB_TOKEN` each deploy, which works while the workflow runs).

## Redeploy

Push to `main` touching `apps/api`, `apps/worker`, `packages`, `docker-compose.yml`, `Caddyfile` or the workflow. Or run **deploy-backend** by hand from the Actions tab. By hand on the server:

```bash
cd /opt/susanoo && docker compose pull && docker compose up -d
```

**Migrations first.** Nothing applies `supabase/migrations/` automatically. Before a push whose api or worker code reads a new column reaches the server, run the new file(s) in the Supabase SQL editor (or `supabase db push`), oldest first. `007_project_connections.sql` and `008_run_connectors.sql` (Mon 12 Oct, #28/#29) must be applied before that deploy, or starting and claiming runs fails on the missing `projects.connection_ids` / `runs.connectors` columns.

## Logs

```bash
cd /opt/susanoo
docker compose logs --tail=200 api worker caddy
docker compose logs -f api
docker compose ps
```

## Check it worked

`curl -fsS https://api.susanoo.jugaadchhabra.dev/health`, then the checks in the `deploy-check` skill.

### Check a run preview by hand (#31)

On the server, in `/opt/susanoo`, with a real run id (`select id from public.runs limit 1;` in Supabase):

```bash
ID=<run uuid, lowercase>
# 1. A hello-world app named and labelled like a run, on the sandbox network only.
docker run -d --rm --name susanoo-run-$ID --network susanoo_sandbox \
  --label susanoo.managed=true --label susanoo.run_id=$ID --label susanoo.port=8000 \
  --cpus 0.5 --memory 128m python:3.12-slim python -m http.server 8000
# 2. Before a preview is recorded the ask says no (run from inside the compose network):
docker compose exec caddy wget -qSO- "http://api:8000/internal/preview-allowed?domain=run-$ID.susanoo.jugaadchhabra.dev"  # 404
```

3. Record the preview in the Supabase SQL editor: `update public.runs set preview_url = 'https://run-<ID>.susanoo.jugaadchhabra.dev' where id = '<ID>';`
4. Repeat step 2: now 200. From outside, `curl -I https://api.susanoo.jugaadchhabra.dev/internal/preview-allowed?domain=run-<ID>.susanoo.jugaadchhabra.dev` is 404 (forwarded requests are refused).
5. Open `https://run-<ID>.susanoo.jugaadchhabra.dev` in a browser: a directory listing over HTTPS with no warning. `docker compose logs caddy | grep -i obtain` shows the certificate for that one host.
6. A made-up host (`https://run-nope.susanoo.jugaadchhabra.dev`) fails the TLS handshake: no certificate is issued.
7. Clean up: `docker rm -f susanoo-run-$ID` and set `preview_url` back to `null`.

## Rollback

Revert the commit and push; never force-push.

```bash
git revert <sha>
git pull --rebase origin main && git push origin HEAD:main
```

That rebuilds and redeploys. Every image is also tagged with its commit SHA, so for an emergency on the server you can pin `image:` to `…:<sha>` temporarily, then commit the real fix.

## Local check

```bash
docker build -f apps/api/Dockerfile -t susanoo-api:test .
docker build -f apps/worker/Dockerfile -t susanoo-worker:test .
```

Build context is the repo root (the uv workspace needs `packages/core` and `uv.lock`).
