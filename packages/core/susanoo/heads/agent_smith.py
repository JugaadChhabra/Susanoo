"""Agent Smith: Plan -> the generated app's agent files. [PRD › The eight heads of Orochi] (head 3)

Writes `agent/prompt.md` (model-written, Sonnet), `agent/policy.yaml` (compiled from `Plan.rules` in
code, so rule ids are exact) and `agent/tools.py` (the template's tools plus any connector tools) into
the generated repo, commits only `agent/`, and returns an `AgentBundle` and the new `head_commit`.

`agent/` is the `prompt_policy` + `tool` layer folder the Fixer edits later
[PRD › Repository structure › Generated product repo]. File formats and tool names (`parse_diff`,
`get_file_context`, `lookup_rule`) match `templates/app-template/` so `backend/reviewer.py` keeps working.
Credentials never reach these files [CLAUDE.md §5, PRD › Credential handling].
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
from pathlib import Path, PurePosixPath

import yaml

from susanoo.emit import emit
from susanoo.guards import budget
from susanoo.heads import _connectors
from susanoo.integrations import llm, tracing
from susanoo.schemas import AgentBundle, Plan, Rule
from susanoo.schemas.state import SusanooState

HEAD = "agent_smith"

PROMPT_PATH = "agent/prompt.md"
POLICY_PATH = "agent/policy.yaml"
TOOLS_PATH = "agent/tools.py"
AGENT_DIR = "agent"

# The generated agent's model. The template reads env APP_MODEL and falls back to this.
AGENT_MODEL = llm.SONNET

BASE_TOOL_NAMES = ("parse_diff", "get_file_context", "lookup_rule")
MAX_PROMPT_ATTEMPTS = 2  # first try plus one retry with the problems listed
MIN_PROMPT_CHARS = 400

SYSTEM_PROMPT_FILE = Path(__file__).resolve().parent.parent / "prompts" / f"{HEAD}.md"
# heads/ -> susanoo/ -> core/ -> packages/ -> repo root
TEMPLATE_AGENT_DIR = Path(__file__).resolve().parents[4] / "templates" / "app-template" / "agent"

POLICY_HEADER = (
    "# The team's review rules, compiled from the plain-English rules in the spec.\n"
    "# Every finding the reviewer returns must use one of these ids.\n"
)

_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_WHOLE_FENCE = re.compile(r"\A```[A-Za-z]*\n(.*)\n```\s*\Z", re.DOTALL)
_EXAMPLE_RULE_ID = re.compile(r'"rule_id"\s*:\s*"([^"]*)"')


class AgentSmithError(Exception):
    """The agent files could not be produced. The message is plain English."""


# --- the head ----------------------------------------------------------------------------------------------


async def run(state: SusanooState) -> dict:
    with tracing.span(HEAD, run_id=state["run_id"]):
        await emit(state, HEAD, "started", "Writing the agent's prompt, policy and tools…")
        repo: Path | None = None
        try:
            plan: Plan = state["plan"]
            repo = Path(state["repo_path"])
            if not (repo / ".git").exists():
                raise AgentSmithError("The generated app's folder is not a git repository, so nothing was written.")

            policy_text = compile_policy(plan.rules)
            tools_text, connector_infos = _build_tools_file(repo, _selected_connector_names(state))

            cost = float(state.get("cost_usd", 0.0))
            prompt_text, spent = await _write_prompt(state, plan, repo)
            cost += spent

            head_commit = await _write_and_commit(
                repo,
                {PROMPT_PATH: prompt_text, POLICY_PATH: policy_text, TOOLS_PATH: tools_text},
                plan,
            )

            bundle = AgentBundle(
                prompt_path=PROMPT_PATH, policy_path=POLICY_PATH, tools_path=TOOLS_PATH, model=AGENT_MODEL
            )
            n_rules = len(plan.rules)
            n_tools = _count_connector_tools(tools_text)
            extra = f" and {n_tools} connection tool{'s' if n_tools != 1 else ''}" if n_tools else ""
            pending = [i.name for i in connector_infos if not i.tool_code_available]
            note = (
                f" Tools for the {', '.join(pending)} connection cannot be added to the agent yet." if pending else ""
            )
            await emit(
                state,
                HEAD,
                "finished",
                f"The first version of the agent is ready: it checks {n_rules} rule{'s' if n_rules != 1 else ''}"
                f"{extra}.{note}",
                {"head_commit": head_commit, "connector_env_vars": connector_env_names(connector_infos)},
            )
            return {"agent": bundle, "head_commit": head_commit, "cost_usd": cost}
        except budget.BudgetExceeded:
            raise  # the graph ends the run cleanly
        except Exception as exc:
            await emit(state, HEAD, "error", f"I could not write the agent's files: {tracing.scrub_text(str(exc))}")
            raise


# --- policy.yaml (code, not model) -------------------------------------------------------------------------


def compile_policy(rules: list[Rule]) -> str:
    """Dump `Plan.rules` to policy.yaml in the template's format: `rules: [{id, text, default_severity}]`."""
    if not rules:
        raise AgentSmithError("The plan has no rules, so there is nothing for the agent to enforce.")
    ids = [r.id for r in rules]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise AgentSmithError(f"Two rules share the same id ({', '.join(duplicates)}).")
    body = yaml.safe_dump(
        {"rules": [{"id": r.id, "text": r.text, "default_severity": r.default_severity} for r in rules]},
        sort_keys=False,
        allow_unicode=True,
        width=1_000_000,
    )
    text = POLICY_HEADER + body
    if tracing.looks_secret_shaped(text):
        raise AgentSmithError("A rule appears to contain a secret, so the policy was not written.")
    return text


# --- tools.py ----------------------------------------------------------------------------------------------


def _selected_connector_names(state: SusanooState) -> list[str]:
    # TODO(NEEDS HUMAN/contract): SusanooState and Plan have no field for the connectors the user picked
    # on the New project page. Until one exists, an optional "connectors" list of names may be set on
    # the state; without it the agent gets no connector tools.
    return [str(n) for n in state.get("connectors", [])]  # type: ignore[attr-defined]


def connector_env_names(infos: list[_connectors.ConnectorInfo]) -> dict[str, list[str]]:
    """Env var NAMES (never values) each selected connector sets in the container; the sandbox fills them in."""
    return {i.id: list(i.env_vars) for i in infos if i.env_vars}


def env_var_names_for(connector_names: list[str]) -> dict[str, list[str]]:
    """For the Runner/sandbox: connector id -> env var NAMES to fill from Vault at container start."""
    return _connectors.connector_env_names(connector_names)


def _build_tools_file(repo: Path, connector_names: list[str]) -> tuple[str, list[_connectors.ConnectorInfo]]:
    existing = repo / TOOLS_PATH
    if existing.is_file():
        base = existing.read_text(encoding="utf-8")
    elif (TEMPLATE_AGENT_DIR / "tools.py").is_file():
        base = (TEMPLATE_AGENT_DIR / "tools.py").read_text(encoding="utf-8")
    else:
        raise AgentSmithError("The agent's base tools file is missing from both the app and the template.")

    try:
        connectors = _connectors.load_connectors(connector_names)
        tools = _connectors.collect_tools(connectors)
        infos = _connectors.describe_connectors(connectors, tools)
        text = _connectors.render_tools_file(base, tools, connector_env_names(infos))
    except _connectors.ConnectorError as exc:
        raise AgentSmithError(str(exc)) from exc

    defined = _top_level_names(text)
    missing = [n for n in BASE_TOOL_NAMES if n not in defined]
    if missing:
        raise AgentSmithError(f"The agent's tools file lost its standard tools ({', '.join(missing)}).")
    clash = [t.name for t in tools if t.name in BASE_TOOL_NAMES or _count_defs(text, t.name) != 1]
    if clash:
        raise AgentSmithError(f"A connection tool reuses an existing tool name ({', '.join(clash)}).")
    return text, infos


def _top_level_names(source: str) -> set[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise AgentSmithError("The agent's tools file is not valid Python, so it was not written.") from exc
    return {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}


def _count_defs(source: str, name: str) -> int:
    tree = ast.parse(source)
    return sum(1 for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def _count_connector_tools(tools_text: str) -> int:
    if _connectors.BEGIN_MARKER not in tools_text:
        return 0
    block = tools_text.split(_connectors.BEGIN_MARKER, 1)[1]
    return len(re.findall(r"^(?:async )?def ", block, re.MULTILINE))


# --- prompt.md (model) -------------------------------------------------------------------------------------


def _load_system_prompt() -> str:
    return _HTML_COMMENT.sub("", SYSTEM_PROMPT_FILE.read_text(encoding="utf-8"), count=1).strip()


def _reference_prompt(repo: Path) -> str:
    for candidate in (repo / PROMPT_PATH, TEMPLATE_AGENT_DIR / "prompt.md"):
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    return ""


async def _write_prompt(state: SusanooState, plan: Plan, repo: Path) -> tuple[str, float]:
    """Ask the model for prompt.md, validate it, retry once with the problems listed. Returns (text, cost)."""
    system = _load_system_prompt()
    plan_json = json.dumps(
        plan.model_dump(include={"product_name", "summary", "rules", "intake_answers", "acceptance_criteria"}),
        indent=2,
        ensure_ascii=False,
    )
    base_message = f"<plan>\n{plan_json}\n</plan>\n\n<reference_prompt>\n{_reference_prompt(repo)}\n</reference_prompt>"

    spent = 0.0
    problems: list[str] = []
    for _attempt in range(MAX_PROMPT_ATTEMPTS):
        budget.check({**state, "cost_usd": float(state.get("cost_usd", 0.0)) + spent})  # before EVERY model call
        message = base_message
        if problems:
            listed = "\n".join(f"- {p}" for p in problems)
            message += f"\n\n<fix_these_problems>\n{listed}\n</fix_these_problems>"
        result = await llm.call(HEAD, messages=[{"role": "user", "content": message}], system=system)
        spent += result.cost_usd
        text = _clean_model_text(result.text)
        problems = validate_prompt(text, plan.rules)
        if not problems:
            return text + "\n", spent
    raise AgentSmithError(f"The written prompt did not pass the checks ({'; '.join(problems)}).")


def _clean_model_text(text: str) -> str:
    body = text.strip()
    fenced = _WHOLE_FENCE.match(body)
    return (fenced.group(1) if fenced else body).strip()


def validate_prompt(text: str, rules: list[Rule]) -> list[str]:
    """What is wrong with a candidate prompt.md; empty means it is acceptable."""
    problems: list[str] = []
    if len(text) < MIN_PROMPT_CHARS:
        problems.append("the prompt is too short to be a complete reviewer prompt")
    if "<diff>" not in text:
        problems.append("it never says the diff arrives inside <diff> tags")
    if "untrusted" not in text.lower():
        problems.append("it does not tell the reviewer to treat the diff as untrusted data")
    if "Rules in force" not in text:
        problems.append('it does not refer to the "Rules in force" section')
    if re.search(r"^#+\s*Rules in force\b", text, re.MULTILINE | re.IGNORECASE):
        problems.append('it contains its own "Rules in force" heading, but the app appends that section')
    if '{"findings": []}' not in text:
        problems.append('it does not show the empty answer {"findings": []}')
    plan_ids = {r.id for r in rules}
    for rule_id in sorted(plan_ids):
        if f"`{rule_id}`" not in text:
            problems.append(f"rule `{rule_id}` is not described (write its id in backticks)")
    unknown = sorted({m for m in _EXAMPLE_RULE_ID.findall(text) if m not in plan_ids})
    if unknown:
        problems.append(f"an example uses rule ids that are not in the plan ({', '.join(unknown)})")
    if tracing.looks_secret_shaped(text):
        problems.append("it contains something that looks like a secret or credential; remove it")
    return problems


# --- writing and committing --------------------------------------------------------------------------------


async def _write_and_commit(repo: Path, files: dict[str, str], plan: Plan) -> str:
    """Write the files, check only agent/ changed, commit, and return the new HEAD sha."""
    for rel in files:
        _assert_in_agent(rel)
    try:
        for rel, content in files.items():
            target = repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        outside = [p for p in await _changed_paths(repo) if not _in_agent(p)]
        if outside:
            raise AgentSmithError(
                f"The app folder has other uncommitted changes ({', '.join(sorted(outside))}); "
                "only the agent's files may change here."
            )
        await _git(repo, "add", "--", AGENT_DIR)
        staged = [p for p in (await _git(repo, "diff", "--cached", "--name-only")).splitlines() if p]
        if any(not _in_agent(p) for p in staged):
            raise AgentSmithError("Something outside the agent folder was staged, so nothing was committed.")
        if staged:
            await _git(repo, "commit", "-m", _commit_message(plan))
    except BaseException:
        await _restore_agent_dir(repo)
        raise
    return (await _git(repo, "rev-parse", "HEAD")).strip()


def _commit_message(plan: Plan) -> str:
    n = len(plan.rules)
    return f"feat(agent): prompt, policy and tools for {plan.product_name} ({n} rule{'s' if n != 1 else ''})"


def _in_agent(path: str) -> bool:
    return path == AGENT_DIR or path.startswith(AGENT_DIR + "/")


def _assert_in_agent(rel: str) -> None:
    path = PurePosixPath(rel)
    if path.is_absolute() or ".." in path.parts or not _in_agent(str(path)):
        raise AgentSmithError(f"Refusing to write {rel!r}: Agent Smith may only write inside agent/.")


async def _changed_paths(repo: Path) -> list[str]:
    raw = await _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    entries = raw.split("\0")
    paths: list[str] = []
    i = 0
    while i < len(entries) and entries[i]:
        status, path = entries[i][:2], entries[i][3:]
        paths.append(path)
        if "R" in status or "C" in status:  # -z lists a rename's source as the next entry
            i += 1
            paths.append(entries[i])
        i += 1
    return paths


async def _restore_agent_dir(repo: Path) -> None:
    """Best effort: put agent/ back to HEAD so a failed run leaves no half-written files."""
    try:
        await _git(repo, "restore", "--source=HEAD", "--staged", "--worktree", "--", AGENT_DIR)
        await _git(repo, "clean", "-fd", "--", AGENT_DIR)
    except Exception:  # never mask the original error
        pass


async def _git(repo: Path, *args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-c",
        "core.fsmonitor=false",
        *args,
        cwd=repo,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise AgentSmithError(f"git {args[0]} failed: {tracing.scrub_text(err.decode().strip())}")
    return out.decode()
