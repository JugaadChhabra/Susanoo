# Deploying the backend

[PRD › Deployment]. Web deploys from Vercel on push to `main`. This page covers the backend (api, worker, caddy on EC2).

## Live setup (Mon 12 Oct 2026)

- **Server:** one EC2 `t3.large`, Ubuntu 24.04 x86, 40 GB gp3, in the hackathon AWS account: **us-east-1 only, the existing `Hackathon_VPC` only, one instance per user** (a second instance is auto-terminated; terminate before replacing), t2/t3/t3a up to `.large`, EBS ≤ 50 GB. Elastic IP `35.173.112.138`. Security group: 22, 80 and 443 open (22 because GitHub Actions deploys over SSH).
- **Stack:** `/opt/susanoo/` holds `docker-compose.yml`, `Caddyfile` and `.env`; the workflow copies the first two on every deploy.
- **Worker:** has the Docker socket (group `DOCKER_GID`), a named volume for run workdirs (`/data/runs`), and joins `susanoo_sandbox` with Caddy; generated-app containers join only that network.
- **Live checks:** `/health` OK, `/demo/<id>` 200, signed-in routes 401 without a token.
- **Not live yet:** `run-<id>` previews (#31, #32) and passing a project's connections into runs (#28–#30).

## Hostnames

Base domain: `susanoo.jugaadchhabra.dev` (Cloudflare DNS).

| Host | Goes to |
|---|---|
| `app.susanoo.jugaadchhabra.dev` | Vercel (Next.js dashboard) |
| `api.susanoo.jugaadchhabra.dev` | EC2 → Caddy → `api:8000` |
| `run-<id>.susanoo.jugaadchhabra.dev` | EC2 → Caddy → generated app (comes with #31/#32; placeholder in `Caddyfile`) |

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

## Logs

```bash
cd /opt/susanoo
docker compose logs --tail=200 api worker caddy
docker compose logs -f api
docker compose ps
```

## Check it worked

`curl -fsS https://api.susanoo.jugaadchhabra.dev/health`, then the checks in the `deploy-check` skill.

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
