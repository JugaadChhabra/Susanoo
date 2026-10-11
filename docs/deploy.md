# Deploying the backend

[PRD › Deployment]. Web deploys from Vercel on push to `main`. This page covers the backend (api, worker, caddy on EC2).

## Hostnames

Base domain: `susanoo.jugaadchhabra.dev` (Cloudflare DNS).

| Host | Goes to |
|---|---|
| `app.susanoo.jugaadchhabra.dev` | Vercel (Next.js dashboard) |
| `api.susanoo.jugaadchhabra.dev` | EC2 → Caddy → `api:8000` |
| `run-<id>.susanoo.jugaadchhabra.dev` | EC2 → Caddy → generated app (comes with #4; placeholder in `Caddyfile`) |

The `api` record must point at the EC2 IP and be **DNS only** (grey cloud) so Caddy can get its own certificate.

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
3. Create `/opt/susanoo/.env` with at least `API_DOMAIN=api.susanoo.jugaadchhabra.dev`, plus the other variables the api and worker need.
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
