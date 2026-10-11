"""Level 1 sandbox: mocked tools that answer from the scenario's world state. [PRD › Testing environments]

Level 1 runs in-process in the worker. The agent's prompt, policy and model calls are real; its tools are
MOCKED here. The generated repo's `agent/tools.py` and backend code are never imported or executed on the
host: these are Susanoo-side re-implementations of the template's tool wiring (`TOOL_SCHEMAS`, `call_tool` for
`parse_diff`, `get_file_context`, `lookup_rule`). Their outputs are byte-identical to the shipped app's: they read
ONLY the submitted diff and the policy text, so Level 1 is never more capable than the deployed agent.

World state lives in the generated repo at `scenarios/<split>/<scenario_id>.world.json`:
`{"files": {"<path>": "<full new-file content>"}, "notes": "<one line>"}`. `WorldState` / `load_world` are kept for
future connector-backed tools and Level 2 context; they are NOT fed into the three tools above.

Side-effecting tools do nothing and answer "Dry run: would have ..." (none exist in the hero product).
"""

from __future__ import annotations

import copy
import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from susanoo.schemas import Rule, Scenario

log = logging.getLogger(__name__)

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


class WorldState(BaseModel):
    """What the mocked world looks like for one scenario: full new-file contents by path."""

    files: dict[str, str] = Field(default_factory=dict)
    notes: str = ""


# --------------------------------------------------------------------------------------------- diff helpers


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

    A line-for-line re-implementation of `templates/app-template/agent/tools.py:parse_diff` (the generated
    agent's own parser), so Level 1 shows the model exactly what the deployed app would. tests/sandbox checks the
    two stay identical. Keep them in step: change the template and this together.
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


def annotate_files(files: list[FileDiff]) -> str:
    """Render files with explicit new-file line numbers (identical to the template's `annotate_diff(files)`)."""
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


def annotate_diff(diff: str) -> str:
    """The template's `annotate_diff(parse_diff(diff))`; "" when no file could be parsed (callers show the raw diff)."""
    return annotate_files(parse_diff(diff))


def _world_from_diff(diff: str) -> WorldState:
    """Fallback world: each file holds only the diff's added lines, at their new-file line numbers."""
    files: dict[str, str] = {}
    for f in parse_diff(diff):
        added = {ln.new_line: ln.text for ln in f.added if ln.new_line is not None}
        if added:
            files[f.path] = "\n".join(added.get(n, "") for n in range(1, max(added) + 1))
    return WorldState(files=files, notes="Built from the diff's added lines only (no world file).")


def _inside(path: Path, repo_path: str | Path) -> bool:
    """True if `path` (after resolving symlinks) is inside `repo_path`."""
    try:
        return path.resolve().is_relative_to(Path(repo_path).resolve())
    except (OSError, RuntimeError):
        return False


def world_path(repo_path: str | Path, scenario: Scenario) -> Path:
    """Where the scenario's world file lives. Ids are validated so a scenario can never point outside the repo."""
    if not _SAFE_ID.match(scenario.id):
        raise ValueError(f"unsafe scenario id {scenario.id!r}")
    return Path(repo_path) / "scenarios" / scenario.split / f"{scenario.id}.world.json"


def load_world(repo_path: str | Path, scenario: Scenario) -> WorldState:
    """Read the scenario's world file (data only, never executed). Missing or unreadable -> diff-only world."""
    path = world_path(repo_path, scenario)
    try:
        if not _inside(path, repo_path):
            log.warning("World file for scenario %s resolves outside the repo; refusing it", scenario.id)
            return _world_from_diff(scenario.diff)
        return WorldState.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        log.warning("No world file for scenario %s (%s); using the diff's added lines only", scenario.id, path.name)
    except (OSError, ValueError, ValidationError) as exc:
        # Type name only: validation and JSON errors can quote the file's content.
        log.warning("Unreadable world file %s (%s); using the diff's added lines only", path.name, type(exc).__name__)
    return _world_from_diff(scenario.diff)


# ------------------------------------------------------------------------------------------------ the tools
#
# Re-implementation of the template's tool wiring (`agent/tools.py`: TOOL_SCHEMAS, call_tool). Level 1 must not be
# more capable than the shipped app, so outputs are byte-identical to the template's `call_tool`; tests/sandbox
# loads the template and compares. Change the template and this block together.

MAX_TOOL_ROUNDS = 6
TOOL_ERROR_PREFIX = "Error: "

TOOL_SCHEMAS: list[dict[str, Any]] = [
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


def rules_from_policy(policy_text: str) -> list[Rule]:
    """Parse `agent/policy.yaml` TEXT (data only) into rules. Raises ValueError on malformed policy."""
    import yaml

    try:
        data = yaml.safe_load(policy_text) or {}
        rules = [Rule(**item) for item in data.get("rules", [])]
    except (yaml.YAMLError, TypeError, ValidationError, AttributeError):
        # Plain message only: YAML and validation errors quote the file's content.
        raise ValueError(
            "agent/policy.yaml could not be read: it is not valid YAML or its rules are malformed"
        ) from None
    if len({r.id for r in rules}) != len(rules):  # the app refuses to start on this, so Level 1 does too
        raise ValueError("agent/policy.yaml has duplicate rule ids")
    return rules


def _context_lines(files: list[FileDiff], path: str, line: int, radius: int) -> list[DiffLine]:
    for f in files:
        if f.path == path:
            return [ln for ln in f.lines if ln.new_line is not None and abs(ln.new_line - line) <= radius]
    return []


def _format_lines(lines: list[DiffLine]) -> str:
    out = []
    for ln in lines:
        if ln.kind == "remove":
            out.append(f"{'':>6} - {ln.text}")
        else:
            out.append(f"{ln.new_line:>6} {'+' if ln.kind == 'add' else ' '} {ln.text}")
    return "\n".join(out)


def call_tool(name: str, args: dict[str, Any] | None, *, diff: str, policy: list[Rule]) -> str:
    """Answer one read-only tool call exactly as the generated app would. Never raises."""
    args = args if isinstance(args, dict) else {}
    try:
        if name == "parse_diff":
            files = parse_diff(diff)
            wanted = args.get("file")
            if wanted:
                files = [f for f in files if f.path == wanted]
            return annotate_files(files) if files else "Not shown in this change."
        if name == "get_file_context":
            path, line = args.get("file"), args.get("line")
            if not isinstance(path, str) or isinstance(line, bool) or not isinstance(line, int):
                return f"{TOOL_ERROR_PREFIX}get_file_context needs a file (text) and a line (whole number)."
            radius = args.get("radius", 3)
            radius = radius if isinstance(radius, int) and not isinstance(radius, bool) else 3
            lines = _context_lines(parse_diff(diff), path, line, max(0, min(radius, 20)))
            return _format_lines(lines) if lines else "Not shown in this change."
        if name == "lookup_rule":
            rule_id = str(args.get("rule_id", ""))
            rule = next((r for r in policy if r.id == rule_id), None)
            if rule is None:
                return "No rule with that id. Valid ids: " + ", ".join(r.id for r in policy)
            return json.dumps(rule.model_dump())
    except Exception as exc:  # a tool bug must not crash the review
        return f"{TOOL_ERROR_PREFIX}{name} failed ({type(exc).__name__})."
    return f"{TOOL_ERROR_PREFIX}unknown tool '{name}'. Available tools: " + ", ".join(t["name"] for t in TOOL_SCHEMAS)


class MockTools:
    """The mocked toolbox for one scenario. `call_tool(name, args)` always returns text for the model.

    parse_diff, get_file_context and lookup_rule answer from the submitted diff and the policy only, like the app.
    `world` is carried for future connector-backed tools and Level 2 context; it is never fed into those three.
    """

    def __init__(
        self,
        *,
        diff: str,
        world: WorldState,
        rules: Iterable[Rule] = (),
        side_effect_tools: Iterable[str] = (),
    ) -> None:
        self.diff = diff
        self.world = world
        self.rules = list(rules)
        self.side_effect_tools = frozenset(side_effect_tools)

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return copy.deepcopy(TOOL_SCHEMAS)

    def call_tool(self, name: str, args: dict[str, Any] | None = None) -> str:
        if name in self.side_effect_tools:
            shown = ", ".join(f"{k}={v!r}" for k, v in sorted((args if isinstance(args, dict) else {}).items()))
            return f"Dry run: would have run {name}({shown}). Nothing was changed."
        return call_tool(name, args, diff=self.diff, policy=self.rules)


def build_mock_tools(
    repo_path: str | Path,
    scenario: Scenario,
    *,
    policy_text: str | None = None,
    side_effect_tools: Iterable[str] = (),
) -> MockTools:
    """Build the toolbox for one scenario. Reads the world file and `agent/policy.yaml` as text only."""
    if policy_text is None:
        policy_file = Path(repo_path) / "agent" / "policy.yaml"
        if policy_file.exists() and not _inside(policy_file, repo_path):
            raise ValueError("agent/policy.yaml resolves outside the generated repo, so it was refused")
        policy_text = policy_file.read_text(encoding="utf-8") if policy_file.exists() else ""
    return MockTools(
        diff=scenario.diff,
        world=load_world(repo_path, scenario),
        rules=rules_from_policy(policy_text),
        side_effect_tools=side_effect_tools,
    )
