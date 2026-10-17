#!/usr/bin/env bash
# Shared helpers for Susanoo git hooks.
ROOT="$(git rev-parse --show-toplevel)"
ALLOWED_FILE="$ROOT/.devkit/allowed-identities"
BANNED_FILE="$ROOT/.devkit/banned-trailers"

# Kept to POSIX sh so it can also be sourced from make (/bin/sh) and macOS bash 3.2.

lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

# is_allowed_identity <name> <email>  -> exit 0 if allowed
is_allowed_identity() {
  local name="$1" email="$2" line kind val
  while IFS= read -r line; do
    line="${line%%#*}"; line="$(echo "$line" | xargs)"
    [ -z "$line" ] && continue
    kind="${line%%:*}"; val="${line#*:}"
    if [ "$kind" = "name" ] && [ "$name" = "$val" ]; then return 0; fi
    if [ "$kind" = "email" ] && [ "$(lower "$email")" = "$(lower "$val")" ]; then return 0; fi
  done < "$ALLOWED_FILE"
  return 1
}

# banned_patterns -> prints one regex per line
banned_patterns() { grep -v '^[[:space:]]*#' "$BANNED_FILE" | sed '/^[[:space:]]*$/d'; }

# has_banned_lines <file> -> exit 0 if the file contains a banned line
has_banned_lines() {
  local f="$1" p
  while IFS= read -r p; do
    if grep -E -i -q -- "$p" "$f"; then return 0; fi
  done <<EOF
$(banned_patterns)
EOF
  return 1
}

allowed_names() { grep -E '^name:' "$ALLOWED_FILE" | cut -d: -f2 | paste -sd ' ' -; }
