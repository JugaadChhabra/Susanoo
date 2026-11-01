# Private Platform Operations

This is the expanded control plane, not the customer execution host. Existing
hackathon routes remain available. The local UI is `/evaluation`; no customer
runner, evaluator, sample business backend or successful business outcome is seeded.
The outcome/preflight runtime bridge is now available separately; see
[customer runner operations](RUNNER_OPERATIONS.md) for local approval, registration,
execution and evidence-only recovery. The API still never hosts customer code.

## Native Development

Install the locked Python and web dependencies (`uv sync --frozen`, `npm ci` in
`apps/web`, and `npm ci` in `tests/e2e`). Supply a private PostgreSQL URL in
`PLATFORM_DATABASE_URL`, or use the already provisioned ignored native PostgreSQL
tools/data for this workspace. Bootstrap never downloads a database automatically.

```powershell
uv run python -m susanoo_api.platform.cli bootstrap
powershell -NoProfile -File scripts/platform-dev.ps1 up
powershell -NoProfile -File scripts/platform-dev.ps1 status
```

Bootstrap generates private `.env.platform` and randomized synthetic owner,
reviewer and viewer accounts under `.platform/demo-credentials-*.json`. These files
are ignored and must not be committed. The development script starts loopback-only
API, internal worker and web processes, logs under `.platform`, and records only
their PIDs. `down` stops only matching recorded process trees, not the database or
data. It refuses to replace existing listeners. It does not supervise customer code.

The script also starts the daily encrypted backup worker. On Windows, generated
private paths use current-user/SYSTEM DACLs, not just Unix-style chmod. Keep existing
children inheriting the protected parent when provisioning native tools; do not
apply directory-only ACL flags recursively to files.

If the separately provisioned native PostgreSQL is stopped, start it with its
`pg_ctl` and the existing data directory/password; never reinitialize an existing
database. The current native test deployment uses port 55432. A supplied PostgreSQL
URL is preferable for a clean machine.

## Tests

```powershell
uv run python scripts/test_platform.py -q
uv run ruff check .
uv run python scripts/export_platform_contracts.py
```

`test_platform.py` creates a new UUID-named local database and drops only that
database in `finally`. It refuses nonlocal administrator URLs. It never truncates
the demo or a production database. Set `PLATFORM_TEST_ADMIN_URL` when the test
administrator URL differs from the local platform URL.

Run `npm run typecheck`, `npm run lint`, `npm test` and `npm run build` in
`apps/web`. Stop `next dev` before a production build. In `tests/e2e`, set
`PLATFORM_BROWSER_CREDENTIALS` to the ignored local credentials path and run
`npx playwright test --config platform.config.ts`. The workflow browser test uses
real API/PostgreSQL/storage/jobs and an explicitly labeled protocol test client;
it does not establish business-runtime correctness or live-provider qualification.

Hosted Ubuntu 24.04 checks install the matching PostgreSQL 17 client from the
[official signed PGDG repository](https://www.postgresql.org/download/linux/ubuntu/),
not Ubuntu's default older client. The container image carries matching 17 tools.

Equivalent expanded commands deliberately use `platform-` names to preserve the
hackathon Make targets: `platform-bootstrap`, `platform-migrate`, `platform-seed`,
`platform-compose`, `platform-test`, `platform-e2e`, `platform-benchmark` and
`platform-acceptance`. `acceptance` is the full fail-closed gate. `platform-seed`
creates only synthetic identities/project, not approved fake business outcomes.
For export-demo, select a stored authorized run in the console and request its
four formats; reuse the first returned snapshot artifact for a matching snapshot.
`make runtime-demo` runs the installed synthetic collections example locally. This is not
the same as seeding a successful platform run; platform execution needs approved revisions,
local connection approval, registration and an actual customer-side worker.

## Private Compose

```powershell
uv run python -m susanoo_api.platform.cli compose-env
docker compose --env-file .env.platform.compose -f deploy/platform.compose.yml config --quiet
docker compose --env-file .env.platform.compose -f deploy/platform.compose.yml up --build -d
docker compose --env-file .env.platform.compose -f deploy/platform.compose.yml run --rm api /app/.venv/bin/python -m susanoo_api.platform.cli seed
```

The development stack includes digest-pinned PostgreSQL, API, internal worker,
backup worker, Next web, local mail and a private-CA Caddy proxy. No Docker socket
is mounted into API/web. Ports bind to loopback. Use the private CA deliberately;
do not disable certificate verification for a production client. Development mail
is inspectable at `http://localhost:8025`; do not send real customer secrets there.

The default private object store is a protected persistent filesystem. S3-compatible
storage is implemented through `PLATFORM_S3_BUCKET`, endpoint and injected AWS
credentials. On an explicitly provisioned bucket, `storage-init` applies public
access blocking, AES-256 default encryption, 30-day expiration/noncurrent expiration
and one-day abandoned multipart cleanup. This is a real configuration command, not
a dry run; use it only on the dedicated authorized bucket. Application deletion also
removes exact-key versions. Object-store/provider execution still needs qualification
on the chosen deployment.

**Distribution deviation:** the plan named MinIO images. Their former Docker Hub
repositories returned authorization/unavailable responses during this build, so no
unverified third-party mirror or fabricated digest was substituted. The maintained
release decision must be reviewed before enabling a MinIO-specific deployment.
Local private storage and the explicit S3 boundary are usable independently.

The sample runner/simulator/reference/agent containers are supplied by Muaz and are
not fake services in this Compose file. Add them only after his runtime contract,
image, network restrictions and health checks are reviewed. Native process mode is
not suitable for untrusted customer code.

## First Owner and Recovery

Synthetic seed is rejected outside explicit development mode. For a private release,
inject `PLATFORM_BOOTSTRAP_EMAIL` and `PLATFORM_BOOTSTRAP_PASSWORD`, then run
`init-owner` once after migrations. It refuses to overwrite an existing owner.
Normal additions use owner invitations. Roles are sets; reviewer and hidden grants
are independent. The last workspace owner cannot be removed.

For an administrator recovery with server/database authority, inject
`PLATFORM_RECOVERY_EMAIL`, `PLATFORM_RECOVERY_PASSWORD`, and a genuine
`PLATFORM_OPERATOR_ID`; run `recover-user`, then remove those environment variables.
It changes only that enabled identity's password, revokes its credentials and audits
the local administrative action. It does not create memberships or bypass MFA on
external systems.

## Credentials, Integrations and Uncertainty

Issue/rotate runner registration and CLI credentials through Settings; revoke the
old credential explicitly. Revoke installations independently of runner tokens.
Configure GitHub App ID/private key/webhook secret only as service secrets. Read
source access and draft-PR publication are separate permissions. Register the App
webhook at `/api/v1/callbacks/github/app` and configure the integration's secret
reference as `PLATFORM_GITHUB_WEBHOOK_SECRET`. Fork triggers cannot receive test
credentials. Draft PRs require exact patch/base validation and separate approval;
publication uses a stable marker/branch and never merges or deploys.

An expired lease means stop dispatching, preserve the local journal, perform bounded
read-back and append reconciliation. Never resubmit an uncertain write. A reviewer
releases an exclusive quarantined fixture only after complete read-back evidence.
The original result remains unchanged; reproduce with a fresh attempt/namespace.
Revocation failures remain visible and bounded by provider token expiry; inspect
project health and retry/reconcile rather than assuming revocation succeeded.

## Backup, Restore and Retention

```powershell
uv run python -m susanoo_api.platform.cli backup --path .platform/backups/manual.susanoobak
uv run python -m susanoo_api.platform.cli expire-backups --path .platform/backups
```

Use a separate strong `PLATFORM_BACKUP_KEY` in production. Backups include a
consistent private-schema PostgreSQL snapshot and verified ready objects, encrypted
as one authenticated archive. The deletion journal is separately encrypted and must
be backed up independently and kept current. The worker enforces 30-day backup
expiration; invalid/unrecognized files are not deleted. A backup over the bounded
512 MiB private-tool limit requires a separately qualified storage-native process.
This does not back up unrelated legacy schemas, the host OS or customer-local data.

Restore only during an authorized maintenance procedure into a different **empty**
database and isolated artifact location. Quiesce the source before taking the final
deletion journal; do not use the old journal embedded in an old backup. Inject the
target URL as `PLATFORM_RESTORE_DATABASE_URL` and run:

```powershell
uv run python -m susanoo_api.platform.cli restore --path .platform/backups/manual.susanoobak --confirm-database EXACT_TARGET_DATABASE --deletion-journal CURRENT_JOURNAL_PATH
```

Restore refuses an existing/nonempty target and expired archives, verifies objects,
reapplies current deletions, revokes sessions/runners/integrations, queues source-token
revocation, and disables schedules/internal mutations. Reconcile uncertain attempts,
verify the newest journal again, rotate credentials and review installation scope
before explicitly restarting services. No automatic production cutover occurs.

Hosted artifacts/evidence/events expire under project policy. Project deletion
immediately revokes access and ingestion; the worker purges active payloads and
keeps a minimal tombstone. Staged unreferenced uploads are swept after 24 hours.
Customer downloads and customer-runner local retention are separate ownership.

## Upgrade and Readiness

Back up first. Stop new dispatch, reconcile active local actions, update the exact
release/lockfiles and apply additive migrations with `migrate`. The private migration
ledger and advisory lock make reruns safe; no destructive down-migration is run.
Rolling the application back retains expanded compatible schema/evidence. Runtime
adapter upgrades require Muaz's conformance evidence, new pinned revisions and
preflight; do not rewrite a previously accepted result.

`production-check` is a read-only configuration check and never deploys. Production
also needs actual TLS trust, protected/encrypted persistent volumes and database,
private storage, working SMTP, backup-key custody, restore drills, monitoring and the
full matching runtime acceptance report. Configuration flags and protocol fixtures
do not establish those facts. No SSO, certification, HA or live qualification is claimed.
