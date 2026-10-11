"""Patch size cap. [PRD › Improvement loop and guardrails]

"Patch size cap: diffs above a line limit are rejected and re-diagnosed." The PRD gives no
number, so the default is MAX_LINES_CHANGED, overridable with PATCH_MAX_LINES.
Lines changed = lines added + lines removed between two commits (`git diff --numstat`).
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

PATCH_MAX_LINES_ENV = "PATCH_MAX_LINES"
MAX_LINES_CHANGED = 60
BINARY_FILE_LINES = 1000  # numstat prints "-" for binary files; count each as this many lines


class PatchTooLarge(Exception):
    def __init__(self, lines: int, cap: int) -> None:
        self.lines = lines
        self.cap = cap
        super().__init__(
            f"The fix changes {lines} lines, more than the {cap}-line limit for one patch. It needs a smaller change."
        )


class GitDiffError(Exception):
    """git could not produce the diff."""


def patch_max_lines() -> int:
    """The cap, read from the environment on every call."""
    raw = os.environ.get(PATCH_MAX_LINES_ENV)
    if raw is None or raw.strip() == "":
        return MAX_LINES_CHANGED
    try:
        cap = int(raw)
    except ValueError:
        raise ValueError(f"{PATCH_MAX_LINES_ENV} must be a whole number, got {raw!r}") from None
    if cap <= 0:
        raise ValueError(f"{PATCH_MAX_LINES_ENV} must be positive, got {raw!r}")
    return cap


def count_from_numstat(text: str) -> int:
    """Sum added + removed from `git diff --numstat` output (renames and binary files included).

    Lines look like "<added>\\t<removed>\\t<path>"; a pure rename is "0\\t0\\told => new";
    a binary file is "-\\t-\\t<path>".
    """
    total = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 2)
        if len(parts) < 3:
            raise ValueError(f"Unrecognised numstat line: {line!r}")
        added, removed = parts[0], parts[1]
        if added == "-" and removed == "-":
            total += BINARY_FILE_LINES
            continue
        try:
            total += int(added) + int(removed)
        except ValueError:
            raise ValueError(f"Unrecognised numstat line: {line!r}") from None
    return total


_COMMIT_ID = re.compile(r"[0-9a-f]{7,64}")


def count_lines_changed(repo_path: str | Path, base: str, head: str) -> int:
    """Lines added + removed between commits `base` and `head` in `repo_path`."""
    for ref in (base, head):  # refs come from our own commits; never let one be read as a git option
        if not _COMMIT_ID.fullmatch(ref):
            raise GitDiffError(f"Could not measure the patch: {ref!r} is not a commit id.")
    cmd = [
        "git",
        "-c",
        "core.fsmonitor=false",
        "-C",
        str(repo_path),
        "diff",
        "--numstat",
        "--no-ext-diff",
        "-M",
        base,
        head,
        "--",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise GitDiffError(f"Could not measure the patch: {proc.stderr.strip() or 'git diff failed'}")
    return count_from_numstat(proc.stdout)


def check(repo_path: str | Path, base: str, head: str, max_lines: int | None = None) -> int:
    """Return the lines changed, or raise PatchTooLarge if above the cap (== cap is fine)."""
    cap = max_lines if max_lines is not None else patch_max_lines()
    lines = count_lines_changed(repo_path, base, head)
    if lines > cap:
        raise PatchTooLarge(lines, cap)
    return lines
