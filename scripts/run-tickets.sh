#!/usr/bin/env bash
# Work Jugaad's ticket frontier with Claude Code, one fresh headless session per ticket.
#
# For each open `ready-for-agent` issue with no open blockers (lowest number first):
#   1. branch ticket/<n> off an up-to-date main
#   2. new `claude -p` session: /mattpocock-skills:implement + the ticket's prompt from PROMPTBOOK_JUGAAD.md
#      (implement = TDD, typecheck, tests, /code-review, commit; plus the CLAUDE.md rules)
#   3. verify: commits exist, tree clean, identity + no AI attribution (CLAUDE.md §3), make lint + make test
#   4. rebase on origin/main, fast-forward main, push (hooks run), close the issue with the session's summary
# Stops at the first failure and leaves the ticket branch for you to inspect.
#
# Usage:
#   scripts/run-tickets.sh                 # work the frontier until nothing is unblocked
#   scripts/run-tickets.sh 5 6             # only these issues, in this order
#   DRY_RUN=1 scripts/run-tickets.sh       # print the frontier and each prompt; change nothing
#   MAX_TICKETS=3 scripts/run-tickets.sh   # stop after 3 merged tickets
#
# Env: PERMISSION_MODE (default bypassPermissions — the session runs unattended),
#      CLAUDE_MODEL (optional), LOG_DIR (default .git/ticket-runs, never tracked).
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
REPO="JugaadChhabra/Susanoo"
PROMPTBOOK="PROMPTBOOK_JUGAAD.md"
PERMISSION_MODE="${PERMISSION_MODE:-bypassPermissions}"
LOG_DIR="${LOG_DIR:-$(git rev-parse --git-dir)/ticket-runs}"
MAX_TICKETS="${MAX_TICKETS:-0}"
DRY_RUN="${DRY_RUN:-0}"
ALLOWED_IDENTITIES="JugaadChhabra|MuaazSM"
ATTRIBUTION_RE='Co-Authored-By|Claude-Session|claude\.ai/code|Generated with|🤖'
# Belt and braces: CLAUDE.md §3.2 forbids AI attribution in commits.
SESSION_SETTINGS='{"includeCoAuthoredBy":false,"attribution":{"commit":"","pr":""}}'

die() { echo "✖ $*" >&2; exit 1; }
log() { echo "→ $*"; }

# ---------- preflight ----------
for tool in git gh jq claude make; do command -v "$tool" >/dev/null || die "$tool not found"; done
gh auth status >/dev/null 2>&1 || die "gh is not logged in (gh auth login)"
[[ "$(git config user.name)" =~ ^($ALLOWED_IDENTITIES)$ ]] \
  || die "git user.name is '$(git config user.name)'; it must be JugaadChhabra (see CLAUDE.md §3). Not changing it for you."
[ "$(git branch --show-current)" = main ] || die "check out main first"
git diff --quiet && git diff --cached --quiet || die "working tree has uncommitted changes; commit or stash them first"
[ -f "$PROMPTBOOK" ] || die "$PROMPTBOOK missing"
mkdir -p "$LOG_DIR"
PRE_UNTRACKED="$(git ls-files --others --exclude-standard)"   # e.g. your .vscode/; not the session's doing

# ---------- helpers ----------
# First fenced block under the heading "### #<n> — ..." in the promptbook.
ticket_prompt() {
  awk -v h="### #$1 " '
    index($0, h) == 1 { found = 1; next }
    found && /^#/ && !inblock { exit }
    found && /^```/ { if (inblock) exit; inblock = 1; next }
    found && inblock { print }
  ' "$PROMPTBOOK"
}

# GitHub's issue list and dependency summaries lag a few seconds behind a close, so:
# tickets merged in this run are remembered in DONE, and every candidate is re-read directly.
SKIPPED=" "
DONE=" "
open_blockers() {
  local b count=0
  for b in $(gh api "repos/$REPO/issues/$1/dependencies/blocked_by" --jq '.[] | select(.state == "open") | .number'); do
    [[ "$DONE" == *" $b "* ]] || count=$((count + 1))
  done
  echo "$count"
}

next_ticket() {
  local n
  for n in $(gh issue list -R "$REPO" --state open --label ready-for-agent --limit 100 \
               --json number --jq '.[].number' | sort -n); do
    [[ "$SKIPPED$DONE" == *" $n "* ]] && continue
    [ "$(gh api "repos/$REPO/issues/$n" --jq .state)" = open ] || continue
    [ "$(open_blockers "$n")" = 0 ] && { echo "$n"; return; }
  done
}

build_prompt() {
  local n="$1" body
  body="$(ticket_prompt "$n")"
  [ -n "$body" ] || die "no prompt for #$n in $PROMPTBOOK (expected a '### #$n — ...' heading with a fenced block)"
  cat <<EOF
/mattpocock-skills:implement GitHub issue #$n in $REPO.

You are running headless: no human will answer questions, so make reasonable decisions within the PRD and note them in your final report.
1. Read CLAUDE.md and follow it exactly (Jugaad's lane, frozen contracts, no secrets, commit format, NO AI attribution lines, never --no-verify).
2. Read the full ticket with \`gh issue view $n --comments\`; its acceptance criteria are the definition of done. Use the prd-lookup skill for any PRD section it cites.
3. Ticket prompt from $PROMPTBOOK:
---
$body
---
4. Before finishing: make lint and make test pass, run /code-review on your changes and fix what it finds, and for any screen check the "Definition of done for every screen" list in IMPLEMENTATION_JUGAAD.md §7.
5. Commit everything to the current branch (ticket/$n) in small commits, format <type>(<area>): <what changed>, with a body line citing the PRD section. Do not push, merge, switch branches or close the issue; the runner script does that.
6. If an external blocker isn't met (e.g. something Muaaz owns doesn't exist yet), or you cannot meet the acceptance criteria, don't fake it: commit nothing partial that breaks main, and end your final message with a line "TICKET_BLOCKED: <reason>".
   Otherwise end your final message with a short plain-English summary (what was built, decisions made, manual steps for Jugaad) followed by a line "TICKET_DONE".
EOF
}

# ---------- main loop ----------
QUEUE=("$@")
merged=0
while :; do
  if [ ${#QUEUE[@]} -gt 0 ]; then
    n="${QUEUE[0]}"; QUEUE=("${QUEUE[@]:1}")
    [ "$(open_blockers "$n")" = 0 ] || die "#$n still has open blockers"
  elif [ $# -gt 0 ]; then
    break                                   # explicit list finished
  else
    n="$(next_ticket)"
    [ -n "$n" ] || { log "No unblocked ready-for-agent tickets left."; break; }
  fi
  title="$(gh issue view "$n" -R "$REPO" --json title --jq .title)"
  echo; echo "━━━ #$n — $title ━━━"

  prompt="$(build_prompt "$n")"
  if [ "$DRY_RUN" = 1 ]; then
    echo "$prompt"; SKIPPED+="$n "; continue
  fi

  branch="ticket/$n"
  git fetch -q origin main
  git merge -q --ff-only origin/main
  git rev-parse --verify -q "$branch" >/dev/null && die "branch $branch already exists; inspect or delete it first"
  git switch -q -c "$branch"
  base="$(git rev-parse HEAD)"

  log "Running a fresh Claude Code session (log: $LOG_DIR/$n.json)"
  claude_args=(-p "$prompt" --output-format json --permission-mode "$PERMISSION_MODE" --settings "$SESSION_SETTINGS")
  [ -n "${CLAUDE_MODEL:-}" ] && claude_args+=(--model "$CLAUDE_MODEL")
  claude "${claude_args[@]}" > "$LOG_DIR/$n.json" || die "claude session failed for #$n (branch $branch left in place)"
  result="$(jq -r '.result // ""' "$LOG_DIR/$n.json")"
  echo "$result" > "$LOG_DIR/$n.md"

  if grep -q '^TICKET_BLOCKED:' <<<"$result"; then
    reason="$(grep '^TICKET_BLOCKED:' <<<"$result" | head -1)"
    log "#$n blocked — $reason"
    gh issue comment "$n" -R "$REPO" --body "Automated run stopped: ${reason#TICKET_BLOCKED: }" >/dev/null
    if [ "$(git rev-parse HEAD)" = "$base" ] && git diff --quiet && git diff --cached --quiet; then
      git switch -q main && git branch -q -D "$branch"
    else
      git switch -q main 2>/dev/null || true
      log "Kept $branch: it has work in it."
    fi
    SKIPPED+="$n "; continue
  fi
  grep -q 'TICKET_DONE' <<<"$result" || die "#$n: session ended without TICKET_DONE; see $LOG_DIR/$n.md (branch $branch left in place)"

  # ---------- verification (CLAUDE.md §3 + §6) ----------
  [ "$(git branch --show-current)" = "$branch" ] || die "#$n: session left $branch"
  git diff --quiet && git diff --cached --quiet || die "#$n: uncommitted changes left on $branch"
  [ "$(git ls-files --others --exclude-standard)" = "$PRE_UNTRACKED" ] || die "#$n: untracked files left on $branch"
  [ "$(git rev-list --count "$base..HEAD")" -gt 0 ] || die "#$n: no commits on $branch"
  bad_ids="$(git log "$base..HEAD" --format='%an%n%cn' | grep -vE "^($ALLOWED_IDENTITIES)\$" || true)"
  [ -z "$bad_ids" ] || die "#$n: commits by a non-allowed identity: $bad_ids"
  ! git log "$base..HEAD" --format=%B | grep -qiE "$ATTRIBUTION_RE" \
    || die "#$n: AI attribution found in commit messages on $branch; reword them before merging"
  git diff --name-only "$base..HEAD" | grep -qE '(^|/)\.env$' && die "#$n: a .env file was committed"

  log "make lint"; make lint
  log "make test"; make test

  # ---------- merge to main (trunk-based, fast-forward only) ----------
  git fetch -q origin main
  if [ "$(git merge-base HEAD origin/main)" != "$(git rev-parse origin/main)" ]; then
    log "main moved; rebasing $branch"
    git rebase -q origin/main || { git rebase --abort; die "#$n: rebase conflict; resolve on $branch by hand"; }
    make lint && make test
  fi
  git switch -q main
  git merge -q --ff-only "$branch"
  git push -q origin main
  range="$(git rev-parse --short "$base")..$(git rev-parse --short HEAD)"

  summary="$(sed '/^TICKET_DONE/d' "$LOG_DIR/$n.md")"
  gh issue close "$n" -R "$REPO" --comment "$(printf 'Merged to main (%s).\n\n%s\n' "$range" "$summary")" >/dev/null
  git branch -q -d "$branch"
  log "#$n merged ($range) and closed."

  merged=$((merged + 1)); DONE+="$n "
  [ "$MAX_TICKETS" -gt 0 ] && [ "$merged" -ge "$MAX_TICKETS" ] && { log "Reached MAX_TICKETS=$MAX_TICKETS."; break; }
done

echo
log "Merged $merged ticket(s)."
[ "$SKIPPED" = " " ] || log "Skipped or blocked this run:$SKIPPED"
