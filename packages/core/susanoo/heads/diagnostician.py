"""Diagnostician head (Opus): find the one layer that caused each cluster of failures.

[PRD › The eight heads of Orochi › Root-cause layers] [PRD › Improvement loop and guardrails]
[PRD › Contracts (locked)] (Diagnosis); IMPLEMENTATION_MUAAZ P4.1. The I/O contract with the model is the
header of prompts/diagnostician.md; this file builds that exact user message and checks the answer.

Guardrails kept here, in code (the prompt asks nicely, the code enforces):
* Only failing results of the CURRENT iteration whose scenario split is "train" and which a human has not
  disputed are looked at. Held-out and approved acceptance scenarios never reach the prompt.
* Files are read only through `layer_allowlist.is_readable_by("diagnostician", path)`, and only from the
  agent files and `backend/`, `web/`, `agent/`, `scenarios/train/`. `scenarios/heldout/` and
  `scenarios/acceptance/` are never listed or opened.
* Every diagnosis is validated: ids, evidence span ids, target files (layer allow-list, repo listing), the
  test-layer rules, hypothesis length and secrets. Anything that does not check out is dropped.
* Rolled-back hypotheses are not repeated.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from susanoo.emit import emit
from susanoo.guards import budget, layer_allowlist
from susanoo.integrations import llm, tracing
from susanoo.integrations.neatlogs_read import NeatlogsReadClient, NeatlogsReadError
from susanoo.sandbox import mocks
from susanoo.schemas import Diagnosis, Scenario, ScenarioResult, SusanooState

log = logging.getLogger(__name__)

HEAD = "diagnostician"
PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "diagnostician.md"
MAX_OUTPUT_TOKENS = 8000
MAX_DIAGNOSES = 4
MAX_FAILURES = 16  # failures shown to the model per round; the rest wait for a later round
MAX_HYPOTHESIS_CHARS = 1200
MAX_REASON_CHARS = 300
MAX_FILE_CHARS = 100_000
MAX_REPO_FILES = 500
MAX_TRACE_SPANS = 60
TRACE_FETCH_CONCURRENCY = 4
REPEAT_SIMILARITY = 0.9  # a rolled-back hypothesis repeated (nearly) word for word

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_REPO_ROOTS = ("backend", "web", "agent", "scenarios/train")
_SKIP_DIRS = frozenset(
    {".git", "node_modules", "__pycache__", ".next", ".venv", "venv", "dist", "build", ".mypy_cache"}
)
_RETRY_NOTE = (
    "\n\nYour previous reply could not be read as one JSON object in the shape described above. "
    "Reply again with only that JSON object."
)

_LAYER_WORDS = {
    "code": "the app's code",
    "tool": "a tool",
    "prompt_policy": "the agent's instructions",
    "test": "a test",
}


class DiagnosticianError(Exception):
    """The model's answer could not be used even after one retry."""


# --- trace source ----------------------------------------------------------------------------------------

TraceSource = Callable[[str], Awaitable[list[dict[str, Any]]]]


class _DefaultTraceSource:
    def __init__(self, client: NeatlogsReadClient | None) -> None:
        self._client = client

    async def __call__(self, trace_id: str) -> list[dict[str, Any]]:
        if not trace_id:
            return []
        local = tracing.recorded_spans(trace_id)
        if local or self._client is None:
            return local
        try:
            return await self._client.trace_spans(trace_id)
        except NeatlogsReadError as exc:
            log.warning("Could not read trace %s from neatlogs: %s", trace_id, exc)
            return []

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()


def default_trace_source() -> TraceSource:
    """Local recorder first, then the neatlogs read API (only if NEATLOGS_TOKEN is set), else no spans."""
    return _DefaultTraceSource(NeatlogsReadClient.from_env())


# --- building the message --------------------------------------------------------------------------------


def _esc(text: str) -> str:
    """Inside a data block "</" can never close the block."""
    return text.replace("</", "<\\/")


def _block(tag: str, text: str) -> str:
    return f"<{tag}>{_esc(text)}</{tag}>"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _off_limits(parts: Sequence[str]) -> bool:
    """True for anything under scenarios/ other than scenarios/train/ (held-out, acceptance, ...), any letter case."""
    folded = [p.casefold() for p in parts]
    return len(folded) >= 2 and folded[0] == "scenarios" and folded[1] != "train"


def _readable(repo: Path, rel: str) -> Path | None:
    """The file at `rel` inside `repo`, if the Diagnostician may read it; else None. Never reads held-out files.

    The path must pass the allow-list check, stay inside `repo` after symlinks are resolved, and must neither
    look like nor resolve into `scenarios/heldout/` or `scenarios/acceptance/` (compared case-insensitively).
    """
    if not layer_allowlist.is_readable_by(HEAD, rel):
        return None
    posix = rel.replace("\\", "/")
    if _off_limits(Path(posix).parts):
        return None  # heldout, acceptance, or anything else outside train
    path = repo / posix
    try:
        resolved = path.resolve()
        root = repo.resolve()
        if path.is_symlink() or not path.is_file() or not resolved.is_relative_to(root):
            return None
        inside = resolved.relative_to(root)
        if _off_limits(inside.parts) or layer_allowlist.is_heldout(inside.as_posix()):
            return None
    except OSError:
        return None
    return path


def _read_text(repo: Path, rel: str) -> str:
    path = _readable(repo, rel)
    if path is None:
        return "(this file could not be read)"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(this file could not be read)"
    return text if len(text) <= MAX_FILE_CHARS else text[:MAX_FILE_CHARS] + "\n…[shortened]"


def _list_repo_files(repo: Path, exclude_ids: frozenset[str] = frozenset()) -> list[str]:
    """Paths under backend/, web/, agent/ and scenarios/train/, repo-relative, sorted. Nothing else is walked."""
    hidden = {f"scenarios/train/{i}{ext}" for i in exclude_ids for ext in (".json", ".world.json")}
    found: list[str] = []
    for root in _REPO_ROOTS:
        base = repo / root
        if not base.is_dir() or base.is_symlink():
            continue
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
            for name in sorted(filenames):
                rel = (Path(dirpath) / name).relative_to(repo).as_posix()
                if rel not in hidden and _readable(repo, rel) is not None:
                    found.append(rel)
    return sorted(set(found))[:MAX_REPO_FILES]


@dataclass
class _Failure:
    scenario: Scenario
    result: ScenarioResult
    trace: list[dict[str, Any]] = field(default_factory=list)

    @property
    def span_ids(self) -> set[str]:
        return {str(s.get("span_id")) for s in self.trace}


def shared_ids(state: Mapping[str, Any]) -> frozenset[str]:
    """Ids that cannot be trusted as plain train scenarios: also used by a non-train scenario, or user-approved."""
    scenarios = state.get("scenarios", [])
    non_train = {s.id for s in scenarios if s.split != "train"}
    approved = {s.id for s in scenarios if s.approved_by_user}
    return frozenset(non_train | approved)


def select_failures(state: Mapping[str, Any]) -> tuple[list[tuple[Scenario, ScenarioResult]], int]:
    """The only failures the Diagnostician may see, and how many were skipped for sharing an id with a hidden check.

    Kept: current iteration, failing, train split, not approved by the user, not disputed, and an id that no
    scenario of another split uses. Scenarios with a shared id are excluded entirely.
    """
    iteration = state.get("iteration", 0)
    disputed = set(state.get("disputed_scenario_ids", []))
    scenarios = state.get("scenarios", [])
    by_id = {s.id: s for s in scenarios if s.split == "train"}
    tainted = shared_ids(state)
    non_train = {s.id for s in scenarios if s.split != "train"}
    chosen: list[tuple[Scenario, ScenarioResult]] = []
    seen: set[str] = set()
    skipped: set[str] = set()
    for r in state.get("results", []):
        scenario = by_id.get(r.scenario_id)
        if r.iteration != iteration or r.passed or r.scenario_id in disputed or r.scenario_id in seen:
            continue
        if scenario is None:
            continue
        if r.scenario_id in tainted:
            if r.scenario_id in non_train:  # shared with a hidden check; approved-only ones are just left out
                skipped.add(r.scenario_id)
            seen.add(r.scenario_id)
            continue
        seen.add(r.scenario_id)
        chosen.append((scenario, r))
    return chosen, len(skipped)


def _rejected(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    rolled_back = {p.diagnosis_id for p in state.get("patches", []) if p.verdict == "rolled_back"}
    return [
        {
            "layer": d.layer,
            "scenario_ids": d.scenario_ids,
            "hypothesis": d.hypothesis,
            "target_files": d.target_files,
        }
        for d in state.get("failure_ledger", [])
        if d.id in rolled_back
    ]


def _failure_block(f: _Failure) -> str:
    s, r = f.scenario, f.result
    diff = mocks.annotate_diff(s.diff) or s.diff
    score = {
        "matched": r.matched,
        "missed": r.missed,
        "false_positives": r.false_positives,
        "severity_correct": r.severity_correct,
        "error": tracing.scrub_text(r.error) if r.error else None,
    }
    return (
        f'<failure scenario_id="{s.id}" kind="{s.kind}" split="{s.split}" diff_chars="{len(s.diff)}">\n'
        f"{_block('title', s.title)}\n"
        f"{_block('diff', diff)}\n"
        f"{_block('answer_key', s.answer_key.model_dump_json())}\n"
        f"{_block('findings', _json([x.model_dump() for x in r.findings]))}\n"
        f"{_block('score', _json(score))}\n"
        f"{_block('trace', _json(f.trace[:MAX_TRACE_SPANS]))}\n"
        "</failure>"
    )


def build_user_message(
    state: Mapping[str, Any], failures: Sequence[_Failure], repo_files: Sequence[str], agent_text: Mapping[str, str]
) -> str:
    rules = [r.model_dump() for r in state["plan"].rules]
    parts = [
        _block("iteration", str(state.get("iteration", 0))),
        _block("user_rules", _json(rules)),
        _block("agent_prompt", agent_text["prompt"]),
        _block("agent_policy", agent_text["policy"]),
        _block("agent_tools", agent_text["tools"]),
        _block("repo_files", "\n".join(repo_files)),
        _block("rejected_hypotheses", _json(_rejected(state))),
        "<failures>\n" + "\n".join(_failure_block(f) for f in failures) + "\n</failures>",
    ]
    return "\n".join(parts)


# --- checking the answer ---------------------------------------------------------------------------------


def _norm(text: str) -> str:
    return " ".join(text.casefold().split())


def _is_repeat(layer: str, files: set[str], hypothesis: str, rejected: Sequence[Mapping[str, Any]]) -> bool:
    for old in rejected:
        if old["layer"] != layer or set(old["target_files"]) != files:
            continue
        a, b = _norm(hypothesis), _norm(str(old["hypothesis"]))
        if a == b or difflib.SequenceMatcher(None, a, b).ratio() >= REPEAT_SIMILARITY:
            return True
    return False


def _str_list(value: Any) -> list[str] | None:
    if not isinstance(value, list) or not value or not all(isinstance(x, str) and x for x in value):
        return None
    return list(dict.fromkeys(value))


def _short_hypothesis(text: str) -> str:
    text = tracing.scrub_text(text.strip())
    if len(text) <= MAX_HYPOTHESIS_CHARS:
        return text
    cut = text[: MAX_HYPOTHESIS_CHARS - 1].rsplit(" ", 1)[0].rstrip(" ,;:")
    return cut + "…"


def _validate_one(
    raw: Any,
    *,
    by_id: Mapping[str, _Failure],
    claimed: set[str],
    repo_files: set[str],
    rejected: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any] | None, str]:
    """(clean diagnosis fields, "") or (None, why it was dropped)."""
    if not isinstance(raw, dict):
        return None, "not an object"
    layer = raw.get("layer")
    if layer not in layer_allowlist.ALLOWED:
        return None, "unknown layer"
    ids = _str_list(raw.get("scenario_ids"))
    if ids is None:
        return None, "no scenario ids"
    if any(i not in by_id for i in ids):
        return None, "names a scenario that was not a failure shown"
    ids = [i for i in ids if i not in claimed]  # a scenario belongs to one diagnosis only
    if not ids:
        return None, "its scenarios are already in an earlier diagnosis"
    spans = set().union(*(by_id[i].span_ids for i in ids))
    evidence = _str_list(raw.get("evidence_span_ids"))
    evidence = [e for e in evidence if e in spans] if evidence else []
    if not evidence:
        return None, "no evidence span id from its own traces"
    files = _str_list(raw.get("target_files"))
    if files is None:
        return None, "no target files"
    for path in files:
        try:
            layer_allowlist.check_paths(layer, [path])
        except layer_allowlist.OutsideLayer:
            return None, "a target file is outside the layer"
        if path not in repo_files or not layer_allowlist.is_readable_by(HEAD, path):
            return None, "a target file is not in the repository listing"
    if layer == "test":
        for i in ids:
            f = by_id[i]
            if f.scenario.split != "train" or f.scenario.approved_by_user:
                return None, "test change on an approved or non-train scenario"
        allowed = {f"scenarios/train/{i}{ext}" for i in ids for ext in (".json", ".world.json")}
        if any(p not in allowed for p in files):
            return None, "test files do not belong to its own scenarios"
    hypothesis = raw.get("hypothesis")
    if not isinstance(hypothesis, str) or not hypothesis.strip():
        return None, "empty hypothesis"
    hypothesis = _short_hypothesis(hypothesis)
    if _is_repeat(layer, set(files), hypothesis, rejected):
        return None, "repeats a rolled-back hypothesis"
    return {
        "scenario_ids": ids,
        "layer": layer,
        "hypothesis": hypothesis,
        "evidence_span_ids": evidence,
        "target_files": files,
    }, ""


def validate_answer(
    data: Any,
    *,
    iteration: int,
    failures: Sequence[_Failure],
    repo_files: Sequence[str],
    rejected: Sequence[Mapping[str, Any]],
) -> tuple[list[Diagnosis], list[dict[str, str]]]:
    """Check the model's JSON. Returns (valid diagnoses in priority order, model-reported undiagnosed)."""
    if not isinstance(data, dict) or not isinstance(data.get("diagnoses"), list):
        raise DiagnosticianError("the answer has no 'diagnoses' list")
    by_id = {f.scenario.id: f for f in failures}
    files = set(repo_files)
    claimed: set[str] = set()
    kept: list[Diagnosis] = []
    for raw in data["diagnoses"]:
        if len(kept) >= MAX_DIAGNOSES:
            break
        fields, why = _validate_one(raw, by_id=by_id, claimed=claimed, repo_files=files, rejected=rejected)
        if fields is None:
            log.info("Dropped a diagnosis: %s.", why)
            continue
        claimed.update(fields["scenario_ids"])
        kept.append(Diagnosis(id=f"diag-i{iteration}-{len(kept) + 1}", **fields))
    undiagnosed: list[dict[str, str]] = []
    listed = data.get("undiagnosed")
    for item in listed if isinstance(listed, list) else []:
        if isinstance(item, dict) and item.get("scenario_id") in by_id and item["scenario_id"] not in claimed:
            reason = item.get("reason")
            text = tracing.scrub_text(reason.strip())[:MAX_REASON_CHARS] if isinstance(reason, str) else ""
            undiagnosed.append({"scenario_id": item["scenario_id"], "reason": text or "No cause could be shown."})
    return kept, undiagnosed


# --- the head --------------------------------------------------------------------------------------------


def _summary(diagnoses: Sequence[Diagnosis], unexplained: int) -> str:
    counts: dict[str, int] = {}
    for d in diagnoses:
        counts[d.layer] = counts.get(d.layer, 0) + 1
    parts = ", ".join(f"{_LAYER_WORDS[layer]} ({n})" for layer, n in counts.items())
    n = len(diagnoses)
    text = f"Found {n} cause{'s' if n != 1 else ''}: {parts}."
    if unexplained:
        text += f" {unexplained} failing scenario{'s' if unexplained != 1 else ''} could not be explained."
    return text


async def _gather_traces(failures: list[_Failure], source: TraceSource) -> None:
    sem = asyncio.Semaphore(TRACE_FETCH_CONCURRENCY)

    async def one(f: _Failure) -> None:
        async with sem:
            try:
                f.trace = await source(f.result.trace_id)
            except Exception as exc:  # a trace we cannot read makes that failure undiagnosed, not the whole round
                log.warning("Could not read the trace of %s: %s", f.scenario.id, type(exc).__name__)
                f.trace = []

    await asyncio.gather(*(one(f) for f in failures))


async def _ask(state: Mapping[str, Any], user: str, system: str) -> tuple[Any, float]:
    """Call the model; on unreadable JSON retry once. Returns (answer, cost). Budget is checked before each call."""
    cost = 0.0
    for attempt in (0, 1):
        budget.check({**state, "cost_usd": float(state.get("cost_usd", 0.0)) + cost})
        result = await llm.call(
            HEAD,
            [{"role": "user", "content": user + (_RETRY_NOTE if attempt else "")}],
            system=system,
            max_tokens=MAX_OUTPUT_TOKENS,
        )
        cost += result.cost_usd
        try:
            data = result.json()
        except ValueError:
            data = None
        if isinstance(data, dict) and isinstance(data.get("diagnoses"), list):
            return data, cost
    raise DiagnosticianError("The model's answer could not be read as the expected JSON, even after one retry.")


async def run(state: SusanooState) -> dict:
    with tracing.span(HEAD, run_id=state["run_id"]):
        await emit(state, HEAD, "started", "Reading the failing traces to find the cause…")
        try:
            return await _run(state)
        except budget.BudgetExceeded:
            raise  # the graph ends the run cleanly
        except Exception as exc:
            await emit(state, HEAD, "error", f"The diagnosis step failed: {tracing.scrub_text(str(exc))[:300]}")
            raise


async def _run(state: Mapping[str, Any]) -> dict:
    iteration = state.get("iteration", 0)
    ledger = list(state.get("failure_ledger", []))
    spent_before = float(state.get("cost_usd", 0.0))

    chosen, skipped = select_failures(state)
    if skipped:
        one = skipped == 1
        await emit(
            state,
            HEAD,
            "progress",
            f"{skipped} failing check{'' if one else 's'} {'was' if one else 'were'} skipped because "
            f"{'its id is' if one else 'their ids are'} shared with a hidden check.",
        )
    failures: list[_Failure] = []
    undiagnosed: list[dict[str, str]] = []
    for scenario, result in chosen:
        if not _SAFE_ID.match(scenario.id):
            undiagnosed.append({"scenario_id": scenario.id, "reason": "Its id cannot be used safely."})
        elif len(failures) >= MAX_FAILURES:
            undiagnosed.append(
                {"scenario_id": scenario.id, "reason": "Left for a later round to keep this one focused."}
            )
        else:
            failures.append(_Failure(scenario, result))
    source = default_trace_source()
    try:
        await _gather_traces(failures, source)
    finally:
        close = getattr(source, "aclose", None)
        if close is not None:
            await close()
    readable = []
    for f in failures:
        if f.trace:
            readable.append(f)
        else:
            undiagnosed.append({"scenario_id": f.scenario.id, "reason": "No trace of this run could be found."})
    failures = readable

    diagnoses: list[Diagnosis] = []
    cost = spent_before
    if failures:
        repo = Path(state["repo_path"])
        agent = state["agent"]
        agent_text = await asyncio.to_thread(
            lambda: {
                "prompt": _read_text(repo, agent.prompt_path),
                "policy": _read_text(repo, agent.policy_path),
                "tools": _read_text(repo, agent.tools_path),
            }
        )
        repo_files = await asyncio.to_thread(_list_repo_files, repo, shared_ids(state))
        user = build_user_message(state, failures, repo_files, agent_text)
        system = PROMPT_PATH.read_text(encoding="utf-8")
        data, spent = await _ask(state, user, system)
        cost = round(spent_before + spent, 6)
        diagnoses, reported = validate_answer(
            data, iteration=iteration, failures=failures, repo_files=repo_files, rejected=_rejected(state)
        )
        undiagnosed.extend(reported)

    claimed = {i for d in diagnoses for i in d.scenario_ids}
    reported_ids = {u["scenario_id"] for u in undiagnosed}
    for f in failures:
        if f.scenario.id not in claimed and f.scenario.id not in reported_ids:
            undiagnosed.append({"scenario_id": f.scenario.id, "reason": "No usable cause was found for it."})

    payload = {"diagnosis_ids": [d.id for d in diagnoses], "undiagnosed": undiagnosed}
    update: dict[str, Any] = {"cost_usd": cost}
    if not diagnoses:
        update["stop_reason"] = "no_diagnosis"  # the graph ends the run cleanly instead of looping on nothing
        n = len(undiagnosed)
        await emit(
            state,
            HEAD,
            "finished",
            (
                f"I could not pin down a cause for the {n} failing scenario{'s' if n != 1 else ''}, "
                "so nothing was changed."
                if n
                else "There were no failing scenarios to explain."
            ),
            payload,
        )
        return update
    await emit(state, HEAD, "finished", _summary(diagnoses, len(undiagnosed)), payload)
    # The graph acts on failure_ledger[-1] (routing and the Fixer), so the top priority diagnosis goes last.
    update["failure_ledger"] = ledger + list(reversed(diagnoses))
    if diagnoses[0].layer == "test":
        update["status"] = "awaiting_approval"  # the next node, HumanGate, pauses
    return update


__all__ = [
    "HEAD",
    "DiagnosticianError",
    "build_user_message",
    "default_trace_source",
    "run",
    "select_failures",
    "validate_answer",
]
