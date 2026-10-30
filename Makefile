# Susanoo dev shortcuts. Targets for code that doesn't exist yet will start working
# once the scaffold from IMPLEMENTATION_JUGAAD.md P0 lands.
.PHONY: setup hooks dev api worker web test e2e lint types types-check check-identity

setup: hooks
	bash scripts/setup-dev.sh
	uv sync
	cd apps/web && npm install
	cd tests/e2e && npm install && npx playwright install chromium

hooks:
	git config core.hooksPath .githooks
	chmod +x .githooks/* .claude/hooks/*.sh

dev:
	$(MAKE) -j3 api worker web

api:
	uv run uvicorn susanoo_api.main:app --reload --port 8000

worker:
	uv run python -m susanoo_worker.main

web:
	cd apps/web && npm run dev

test:
	uv run pytest -q
	cd apps/web && npm test

# Playwright smoke test: builds apps/web and runs it against a stub of Supabase Auth and the API.
e2e:
	cd tests/e2e && npx playwright test

lint:
	uv run ruff check .
	cd apps/web && npx tsc --noEmit && npm run lint
	cd tests/e2e && npx tsc --noEmit

types:
	uv run python scripts/export_types.py

types-check: types
	@test -z "$$(git status --porcelain -- apps/web/types/generated.ts)" || (echo "✖ apps/web/types/generated.ts is stale or uncommitted; run make types and commit it"; exit 1)

check-identity:
	@. .githooks/_lib.sh; is_allowed_identity "$$(git config user.name)" "$$(git config user.email)" && echo "✓ identity ok" || (echo "✖ set git user.name to MuaazSM or JugaadChhabra"; exit 1)

.PHONY: platform-bootstrap platform-api platform-worker platform-test platform-types platform-compose production-check
platform-bootstrap:
	uv run python -m susanoo_api.platform.cli bootstrap
platform-api:
	uv run python -m susanoo_api.platform.cli serve
platform-worker:
	uv run python -m susanoo_api.platform.cli worker
platform-migrate:
	uv run python -m susanoo_api.platform.cli migrate
platform-seed:
	uv run python -m susanoo_api.platform.cli seed
platform-test:
	uv run python scripts/test_platform.py -q
platform-e2e:
	cd tests/e2e && npx playwright test --config platform.config.ts
platform-benchmark:
	uv run python scripts/test_platform.py -q -k "capacity or twenty_case_protocol"
platform-types:
	uv run python scripts/export_platform_contracts.py
platform-compose:
	docker compose --env-file .env.platform.compose -f deploy/platform.compose.yml up --build
production-check:
	uv run python -m susanoo_api.platform.cli production-check

.PHONY: acceptance platform-acceptance
.PHONY: platform-migrate platform-seed platform-e2e platform-benchmark
acceptance:
	uv run python scripts/platform_acceptance.py
platform-acceptance:
	uv run python scripts/platform_acceptance.py --platform-only
