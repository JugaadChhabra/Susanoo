"""Read-only tools the reviewer uses: parse_diff, get_file_context, lookup_rule.

Layer: `tool`. These are pure functions over the diff text and the policy; they never touch
the network, the disk outside agent/, or the host.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

Severity = Literal["block", "warn", "nit"]

POLICY_PATH = Path(__file__).with_name("policy.yaml")


class Rule(BaseModel):
    """Mirrors susanoo.schemas.core.Rule field for field (this repo is standalone)."""

    id: str
    text: str
    default_severity: Severity


@dataclass
class DiffLine:
    kind: Literal["add", "context", "remove"]
    text: str
    new_line: int | None  # new-file line number; None for removed lines


@dataclass
class FileDiff:
    path: str
    lines: list[DiffLine] = field(default_factory=list)

    @property
    def added(self) -> list[DiffLine]:
        return [ln for ln in self.lines if ln.kind == "add"]


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_HEADER_PREFIXES = (
    "diff ",
    "@@",
    "--- ",
    "+++ ",
    "index ",
    "new file",
    "deleted file",
    "old mode",
    "new mode",
    "similarity",
    "rename ",
    "copy ",
    "Binary files",
)


def _strip_prefix(path: str) -> str:
    path = path.strip().split("\t")[0]
    if path.startswith(('"a/', '"b/')):
        path = path[1:]
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path.rstrip('"')


def parse_diff(diff: str) -> list[FileDiff]:
    """Parse a unified diff into files with new-file line numbers.

    Tolerant of hand-written or model-written diffs whose hunk counts are wrong: line numbers
    come from the `+N` start of each hunk header and then count up one per added or context line.
    """
    files: list[FileDiff] = []
    current: FileDiff | None = None
    old_path: str | None = None
    new_remaining = old_remaining = 0
    new_no = 0
    in_hunk = False

    for raw in diff.replace("\r\n", "\n").split("\n"):
        counts_left = in_hunk and (new_remaining > 0 or old_remaining > 0)

        if not counts_left and raw.startswith("diff --git "):
            current, old_path, in_hunk = None, None, False
            parts = raw.split(" b/", 1)
            if len(parts) == 2:
                current = FileDiff(path=_strip_prefix(parts[1]))
                files.append(current)
            continue

        if not counts_left and raw.startswith("--- "):
            old_path = _strip_prefix(raw[4:])
            in_hunk = False
            continue

        if not counts_left and raw.startswith("+++ "):
            new_path = _strip_prefix(raw[4:])
            in_hunk = False
            if new_path == "/dev/null":  # deleted file: nothing exists in the new version
                new_path = old_path or ""
            if current is None or (current.path != new_path and current.lines):
                current = FileDiff(path=new_path)
                files.append(current)
            else:
                current.path = new_path
            continue

        match = _HUNK_RE.match(raw) if not counts_left else None
        if match:
            old_remaining = int(match.group(2) if match.group(2) is not None else 1)
            new_remaining = int(match.group(4) if match.group(4) is not None else 1)
            new_no = int(match.group(3))
            in_hunk = True
            if current is None:
                current = FileDiff(path=old_path or "")
                files.append(current)
            continue

        if not in_hunk or current is None:
            continue
        if not counts_left and raw.startswith(_HEADER_PREFIXES):
            in_hunk = False
            continue
        if raw.startswith("\\"):  # "\ No newline at end of file"
            continue

        if raw.startswith("+"):
            current.lines.append(DiffLine("add", raw[1:], new_no))
            new_no += 1
            new_remaining -= 1
        elif raw.startswith("-"):
            current.lines.append(DiffLine("remove", raw[1:], None))
            old_remaining -= 1
        elif raw.startswith(" ") or raw == "":
            current.lines.append(DiffLine("context", raw[1:], new_no))
            new_no += 1
            new_remaining -= 1
            old_remaining -= 1
        else:
            in_hunk = False

    return [f for f in files if f.lines]


def annotate_diff(files: list[FileDiff]) -> str:
    """Render files with explicit new-file line numbers so the model never has to count."""
    out: list[str] = []
    for f in files:
        out.append(f"=== FILE: {f.path} ===")
        for ln in f.lines:
            if ln.kind == "remove":
                out.append(f"{'':>6} - {ln.text}")
            else:
                marker = "+" if ln.kind == "add" else " "
                out.append(f"{ln.new_line:>6} {marker} {ln.text}")
        out.append("")
    return "\n".join(out)


def get_file_context(files: list[FileDiff], path: str, line: int, radius: int = 3) -> list[DiffLine]:
    """Return the diff lines within `radius` new-file lines of `line` in `path` (what the diff shows of the file)."""
    for f in files:
        if f.path == path:
            return [ln for ln in f.lines if ln.new_line is not None and abs(ln.new_line - line) <= radius]
    return []


def load_rules(path: Path = POLICY_PATH) -> list[Rule]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rules = [Rule(**item) for item in data.get("rules", [])]
    ids = [r.id for r in rules]
    if len(ids) != len(set(ids)):
        raise ValueError("policy.yaml has duplicate rule ids")
    return rules


def lookup_rule(rule_id: str, rules: list[Rule] | None = None) -> Rule | None:
    """Return the policy rule with this id, or None if it is not a policy rule."""
    for rule in rules if rules is not None else load_rules():
        if rule.id == rule_id:
            return rule
    return None
