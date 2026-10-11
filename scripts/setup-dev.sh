#!/usr/bin/env bash
# One-time setup after cloning. Usage: bash scripts/setup-dev.sh
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

echo "→ Installing git hooks (.githooks)"
git config core.hooksPath .githooks
chmod +x .githooks/* .claude/hooks/*.sh

name="$(git config user.name || true)"
. .githooks/_lib.sh
if ! is_allowed_identity "$name" "$(git config user.email || true)"; then
  echo "✖ git user.name is '$name'. Set it to your GitHub username in this repo:"
  echo "    git config user.name \"MuaazSM\"        # Muaaz"
  echo "    git config user.name \"JugaadChhabra\"  # Jugaad"
  echo "  and use the email linked to that GitHub account (git config user.email ...)."
  exit 1
fi
echo "✓ Committing as $name"

for tool in uv node npm docker; do
  if command -v "$tool" >/dev/null; then echo "✓ $tool"; else echo "• $tool not found (install it before 'make dev')"; fi
done

[ -f .env ] || { cp .env.example .env; echo "→ Created .env from .env.example; fill in your values (never commit it)."; }

cat <<'MSG'

Next:
  1. Turn on Entire for this repo (checkpoints from the first commit) — see https://entire.io
  2. Make sure neatlogs keys are in .env so the first run is traced.
  3. Open Claude Code in the repo; it reads CLAUDE.md automatically.
  4. Open your PROMPTBOOK_*.md and start at Phase 0.
MSG
