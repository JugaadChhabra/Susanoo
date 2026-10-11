#!/usr/bin/env bash
# Claude Code PreToolUse hook (Bash): blocks git commands that break CLAUDE.md §3.
# Exit code 2 blocks the tool call and shows the reason to Claude.
input="$(cat)"
cmd="$(printf '%s' "$input" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("tool_input",{}).get("command",""))' 2>/dev/null)"
[ -z "$cmd" ] && exit 0
echo "$cmd" | grep -q 'git' || exit 0

block() { echo "Blocked by project rule (CLAUDE.md §3): $1" >&2; exit 2; }

echo "$cmd" | grep -E -q -- '--no-verify'                                   && block "never bypass git hooks (--no-verify)."
echo "$cmd" | grep -E -q 'git[^|;&]*push[^|;&]*(--force|-f( |$)|--force-with-lease)' && block "never force-push."
echo "$cmd" | grep -E -q 'git[^|;&]*commit[^|;&]*--author'                   && block "never set --author; commits must be by MuaazSM or JugaadChhabra."
echo "$cmd" | grep -E -q 'git[^|;&]* -c +user\.(name|email)='               && block "never override the git identity."
echo "$cmd" | grep -E -q 'git +config( +--(global|local|system))? +user\.(name|email) +[^ ]' && block "never change the git identity; the human sets it."
echo "$cmd" | grep -E -i -q 'Co-Authored-By|Claude-Session|Generated with \[?Claude|claude\.ai/code/session' && block "no AI attribution in commit messages or PRs."
echo "$cmd" | grep -E -q 'git[^|;&]*(filter-branch|filter-repo)'             && block "never rewrite history."
exit 0
