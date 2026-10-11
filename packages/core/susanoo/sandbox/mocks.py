"""Level 1 sandbox: mocked tools that answer from the scenario's world state. [PRD › Testing environments]

Level 1 runs in-process in the worker. The agent's prompt, policy and model calls are real; its tools are
MOCKED here. The generated repo's `agent/tools.py` and backend code are never imported or executed on the
host: these are Susanoo-side re-implementations of the template's tool names (`parse_diff`, `get_file_context`,
`lookup_rule`) that answer from the scenario's diff, its world state file and the policy text.

World state lives in the generated repo at `scenarios/<split>/<scenario_id>.world.json`:
`{"files": {"<path>": "<full new-file content>"}, "notes": "<one line>"}`.

Side-effecting tools do nothing and answer "Dry run: would have ..." (none exist in the hero product).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from susanoo.schemas import Rule, Scenario

log = logging.getLogger(__name__)

MAX_CONTEXT_LINES = 200  # one get_file_context call never returns more than this many lines
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


class WorldState(BaseModel):
    """What the mocked world looks like for one scenario: full new-file contents by path."""

    files: dict[str, str] = Field(default_factory=dict)
    notes: str = ""


# --------------------------------------------------------------------------------------------- diff helpers


@dataclass(frozen=True)
class _DiffLine:
    kind: str  # "add" | "context" | "remove"
    text: str
    new_line: int | None


def _clean_path(path: str) -> str:
    path = path.strip().strip('"').split("\t")[0]
    for prefix in ("a/", "b/", "./"):
        if path.startswith(prefix):
            path = path[len(prefix) :]
    return path


def _parse(diff: str) -> dict[str, list[_DiffLine]]:
    """Parse a unified diff into {new-file path: lines}. Tolerant of wrong hunk counts (numbers come from `+N`)."""
    files: dict[str, list[_DiffLine]] = {}
    current: list[_DiffLine] | None = None
    new_no = 0
    in_hunk = False
    for raw in diff.replace("\r\n", "\n").split("\n"):
        if raw.startswith("diff --git "):
            current, in_hunk = None, False
            continue
        if raw.startswith("+++ ") and not in_hunk:
            path = _clean_path(raw[4:])
            if path and path != "/dev/null":
                current = files.setdefault(path, [])
            continue
        if raw.startswith("--- ") and not in_hunk:
            continue
        match = _HUNK.match(raw)
        if match:
            new_no, in_hunk = int(match.group(1)), True
            continue
        if not in_hunk or current is None or raw.startswith("\\"):
            continue
        if raw.startswith("+"):
            current.append(_DiffLine("add", raw[1:], new_no))
            new_no += 1
        elif raw.startswith("-"):
            current.append(_DiffLine("remove", raw[1:], None))
        else:
            current.append(_DiffLine("context", raw[1:], new_no))
            new_no += 1
    return {path: lines for path, lines in files.items() if lines}


def annotate_diff(diff: str) -> str:
    """Render the diff with explicit new-file line numbers so the model never has to count."""
    out: list[str] = []
    for path, lines in _parse(diff).items():
        out.append(f"=== FILE: {path} ===")
        for ln in lines:
            if ln.kind == "remove":
                out.append(f"{'':>6} - {ln.text}")
            else:
                out.append(f"{ln.new_line:>6} {'+' if ln.kind == 'add' else ' '} {ln.text}")
        out.append("")
    return "\n".join(out)


def _world_from_diff(diff: str) -> WorldState:
    """Fallback world: each file holds only the diff's added lines, at their new-file line numbers."""
    files: dict[str, str] = {}
    for path, lines in _parse(diff).items():
        added = {ln.new_line: ln.text for ln in lines if ln.kind == "add" and ln.new_line is not None}
        if added:
            files[path] = "\n".join(added.get(n, "") for n in range(1, max(added) + 1))
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

# Anthropic tool-use schemas, named like the template's tools.
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "parse_diff",
        "description": (
            "Show the pull request diff with explicit new-file line numbers. "
            "'+' marks added lines, '-' removed lines. Use these numbers when you report a line."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_file_context",
        "description": "Read lines line_start..line_end (1-based, inclusive) of a file as it is after the change.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path as shown in the diff"},
                "line_start": {"type": "integer", "minimum": 1},
                "line_end": {"type": "integer", "minimum": 1},
            },
            "required": ["path", "line_start", "line_end"],
            "additionalProperties": False,
        },
    },
    {
        "name": "lookup_rule",
        "description": "Look up one policy rule by id: its plain-English text and default severity.",
        "input_schema": {
            "type": "object",
            "properties": {"rule_id": {"type": "string"}},
            "required": ["rule_id"],
            "additionalProperties": False,
        },
    },
]


def rules_from_policy(policy_text: str) -> list[Rule]:
    """Parse `agent/policy.yaml` TEXT (data only) into rules. Raises ValueError on malformed policy."""
    import yaml

    try:
        data = yaml.safe_load(policy_text) or {}
        return [Rule(**item) for item in data.get("rules", [])]
    except (yaml.YAMLError, TypeError, ValidationError, AttributeError):
        # Plain message only: YAML and validation errors quote the file's content.
        raise ValueError(
            "agent/policy.yaml could not be read: it is not valid YAML or its rules are malformed"
        ) from None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


class MockTools:
    """The mocked toolbox for one scenario. `call_tool(name, args)` always returns text for the model."""

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
        self._handlers: dict[str, Callable[[dict[str, Any]], str]] = {
            "parse_diff": self._parse_diff,
            "get_file_context": self._get_file_context,
            "lookup_rule": self._lookup_rule,
        }

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return [dict(s) for s in TOOL_SCHEMAS]

    def call_tool(self, name: str, args: dict[str, Any] | None = None) -> str:
        args = args if isinstance(args, dict) else {}
        if name in self.side_effect_tools:
            shown = ", ".join(f"{k}={v!r}" for k, v in sorted(args.items()))
            return f"Dry run: would have run {name}({shown}). Nothing was changed."
        handler = self._handlers.get(name)
        if handler is None:
            return f"Unknown tool {name!r}. Available tools: {', '.join(sorted(self._handlers))}."
        return handler(args)

    # -- handlers

    def _parse_diff(self, _args: dict[str, Any]) -> str:
        return annotate_diff(self.diff) or "The diff is empty."

    def _get_file_context(self, args: dict[str, Any]) -> str:
        path = _clean_path(str(args.get("path", "")))
        content = self.world.files.get(path)
        if content is None:
            return "file not found in this scenario"
        start, end = _as_int(args.get("line_start")), _as_int(args.get("line_end"))
        if start is None or end is None:
            return "line_start and line_end must be whole numbers"
        lines = content.split("\n")
        total = len(lines)
        start, end = max(1, start), min(total, end)
        if start > end:
            return f"{path} has {total} lines; nothing at that range"
        end = min(end, start + MAX_CONTEXT_LINES - 1)
        return "\n".join(f"{n:>6} | {lines[n - 1]}" for n in range(start, end + 1))

    def _lookup_rule(self, args: dict[str, Any]) -> str:
        rule_id = str(args.get("rule_id", "")).strip()
        for rule in self.rules:
            if rule.id == rule_id:
                return f"{rule.id}: {rule.text} (default severity: {rule.default_severity})"
        known = ", ".join(r.id for r in self.rules) or "none"
        return f"No policy rule with id {rule_id!r}. Known rule ids: {known}."


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
