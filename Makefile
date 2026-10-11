# Susanoo dev shortcuts. Targets for code that doesn't exist yet will start working
# once the scaffold from IMPLEMENTATION_JUGAAD.md P0 lands.
.PHONY: setup hooks dev api worker web test lint types types-check check-identity

setup: hooks
	bash scripts/setup-dev.sh
	uv sync
	cd apps/web && npm install

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

lint:
	uv run ruff check .
	cd apps/web && npx tsc --noEmit && npm run lint

types:
	uv run python scripts/export_types.py

types-check: types
	@test -z "$$(git status --porcelain -- apps/web/types/generated.ts)" || (echo "✖ apps/web/types/generated.ts is stale or uncommitted; run make types and commit it"; exit 1)

check-identity:
	@. .githooks/_lib.sh; is_allowed_identity "$$(git config user.name)" "$$(git config user.email)" && echo "✓ identity ok" || (echo "✖ set git user.name to MuaazSM or JugaadChhabra"; exit 1)
