"""Read-only tools the reviewer uses: parse_diff, get_file_context, lookup_rule.

Layer: `tool`. These are pure functions over the diff text and the policy; they never touch
the network, the disk outside agent/, or the host.
"""

from __future__ import annotations

import json
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


# --- Tool-use wiring: what the model is offered and how its calls are answered ---------------

MAX_TOOL_ROUNDS = 6
TOOL_ERROR_PREFIX = "Error: "

TOOL_SCHEMAS: list[dict] = [
    {
        "name": "parse_diff",
        "description": (
            "Show the submitted diff again, split by file, with the new-file line number printed next to every "
            "added (+), unchanged and removed (-) line. Use it to re-check a line number."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Optional. Only show this file path."},
            },
        },
    },
    {
        "name": "get_file_context",
        "description": (
            "View the lines around a line of a file, as far as the submitted diff shows them (new-file line "
            "numbers). Lines the diff does not show are reported as not shown in this change."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "File path exactly as shown after '=== FILE:'."},
                "line": {"type": "integer", "description": "New-file line number to centre on."},
                "radius": {"type": "integer", "description": "How many lines either side to show (default 3, max 20)."},
            },
            "required": ["file", "line"],
        },
    },
    {
        "name": "lookup_rule",
        "description": "Get the exact text and default severity of one team rule by its id.",
        "input_schema": {
            "type": "object",
            "properties": {"rule_id": {"type": "string", "description": "A rule id from the rules in force."}},
            "required": ["rule_id"],
        },
    },
]



def all_tool_schemas() -> list[dict]:
    """The base tools plus any connector tools Agent Smith appended below (copies; base names win)."""
    schemas = [dict(t) for t in TOOL_SCHEMAS]
    taken = {t["name"] for t in schemas}
    for extra in globals().get("CONNECTOR_TOOL_SCHEMAS", None) or []:
        if isinstance(extra, dict) and extra.get("name") not in taken:
            taken.add(extra["name"])
            schemas.append(dict(extra))
    return schemas


def _format_lines(lines: list[DiffLine]) -> str:
    out = []
    for ln in lines:
        if ln.kind == "remove":
            out.append(f"{'':>6} - {ln.text}")
        else:
            out.append(f"{ln.new_line:>6} {'+' if ln.kind == 'add' else ' '} {ln.text}")
    return "\n".join(out)


def call_tool(name: str, args: dict | None, *, diff: str, policy: list[Rule]) -> str:
    """Run one read-only tool for the model and return its result as text.

    Never raises: problems come back as a string starting with TOOL_ERROR_PREFIX so the model can recover.
    """
    args = args if isinstance(args, dict) else {}
    try:
        if name == "parse_diff":
            files = parse_diff(diff)
            wanted = args.get("file")
            if wanted:
                files = [f for f in files if f.path == wanted]
            return annotate_diff(files) if files else "Not shown in this change."
        if name == "get_file_context":
            path, line = args.get("file"), args.get("line")
            if not isinstance(path, str) or isinstance(line, bool) or not isinstance(line, int):
                return f"{TOOL_ERROR_PREFIX}get_file_context needs a file (text) and a line (whole number)."
            radius = args.get("radius", 3)
            radius = radius if isinstance(radius, int) and not isinstance(radius, bool) else 3
            lines = get_file_context(parse_diff(diff), path, line, radius=max(0, min(radius, 20)))
            return _format_lines(lines) if lines else "Not shown in this change."
        if name == "lookup_rule":
            rule = lookup_rule(str(args.get("rule_id", "")), policy)
            if rule is None:
                return "No rule with that id. Valid ids: " + ", ".join(r.id for r in policy)
            return json.dumps(rule.model_dump())
        connector_tools = globals().get("CONNECTOR_TOOLS", None) or {}
        if name in connector_tools:
            return _call_connector_tool(name, connector_tools[name], args)
    except Exception as exc:  # a tool bug must not crash the review
        return f"{TOOL_ERROR_PREFIX}{name} failed ({type(exc).__name__})."
    return f"{TOOL_ERROR_PREFIX}unknown tool '{name}'. Available tools: " + ", ".join(
        t["name"] for t in all_tool_schemas()
    )


def _call_connector_tool(name: str, fn: object, args: dict) -> str:
    """Run one connector tool. Errors become a plain message; exception text is never echoed (it could hold a token)."""
    try:
        result = fn(**args)  # type: ignore[operator]
    except TypeError:
        return f"{TOOL_ERROR_PREFIX}{name} was called with arguments it does not accept."
    except Exception as exc:
        return f"{TOOL_ERROR_PREFIX}{name} failed ({type(exc).__name__})."
    try:
        return json.dumps(result, default=str)
    except Exception:
        return f"{TOOL_ERROR_PREFIX}{name} returned a result that could not be read."
