"""Simulator head (Haiku). [PRD › The eight heads of Orochi] (head 4); [PRD › Testing environments]

Two parts live here.

1. `draft_cards` (intake): writes 6-10 plain-English scenario cards (a diff plus an answer key) for the user
   to approve. Drafted cards are `split="train"` and `approved_by_user=False`; only the approval step in
   `heads/planner.py` promotes the ones the user approves to `split="acceptance"`.

2. `run` (graph node, P3.1): writes ~20-30 more scenarios (planted, edge, adversarial, clean) with answer keys,
   seeded from the 10 seeds in the template, validates each one (Pydantic model, rule ids from the plan, line
   numbers on added lines, kind matches the answer key, unique ids), drops and reports the invalid ones, and
   splits seeds + generated ~70/30 into `train` / `heldout` (stratified by kind, deterministic per run_id).
   The user-approved acceptance cards in `state["scenarios"]` are kept unchanged.

World state (Level 1 runs). The Scenario contract has no world-state field, so each scenario gets a sidecar in
the generated repo: `scenarios/<split>/<id>.json` is the Scenario, and `scenarios/<split>/<id>.world.json` is
`{"files": {path: full new-file content after the diff}, "notes": "..."}`. Every file the diff touches is in
"files", and for each added line n of the diff, `files[path].splitlines()[n-1]` is that line. Acceptance cards and
seeds get worlds too (written by the model, or rebuilt from the diff alone when the model's version is unusable).

GUARDRAIL for later heads [PRD › Improvement loop and guardrails › Guardrails]: scenarios with `split == "heldout"`
(and the files under `scenarios/heldout/`) must never reach the Diagnostician or Fixer. The Diagnostician and
Fixer must filter them out of whatever they read; this module only labels and writes them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import re
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel, ValidationError

from susanoo.emit import emit
from susanoo.guards import budget
from susanoo.integrations import llm, tracing
from susanoo.schemas import AnswerKey, ExpectedIssue, Plan, Scenario
from susanoo.schemas.state import SusanooState

log = logging.getLogger(__name__)

HEAD = "simulator"
_PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
PROMPT_PATH = _PROMPTS / "simulator_cards.md"
SCENARIO_PROMPT_PATH = _PROMPTS / "simulator.md"
# heads/ -> susanoo/ -> core/ -> packages/ -> repo root
TEMPLATE_SEEDS = Path(__file__).resolve().parents[4] / "templates" / "app-template" / "scenarios" / "seeds"

SCENARIOS_DIR = "scenarios"
SPLIT_DIRS = ("acceptance", "train", "heldout")
COMMIT_MESSAGE = "test: add scenarios"

SCENARIO_COUNT_ENV = "SIMULATOR_SCENARIO_COUNT"
DEFAULT_SCENARIO_COUNT = 24  # new scenarios to ask Haiku for (the PRD wants ~20-30 in total with the seeds)
MIN_SCENARIO_COUNT = 8
MAX_SCENARIO_COUNT = 40
BATCH_SIZE = 6  # scenarios (or worlds) per model call, to keep each answer small
HELDOUT_FRACTION = 0.30
MAX_WORLD_FILE_CHARS = 20_000
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
FILE_HINTS = (
    "app/routes/orders.py",
    "src/billing/invoices.py",
    "services/auth/session.py",
    "app/db/queries.py",
    "lib/notify/email_sender.py",
    "api/v2/customers.py",
    "app/jobs/cleanup.py",
    "core/payments/refunds.py",
    "backend/users/profile.py",
    "app/utils/export_csv.py",
)

MIN_CARDS = 6
MAX_CARDS = 10
_KINDS = ("planted", "edge", "adversarial", "clean")
_RULE_ID = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)+$")
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_FILE_HEADERS = (
    "diff ",
    "index ",
    "--- ",
    "+++ ",
    "new file",
    "deleted file",
    "old mode",
    "new mode",
    "similarity",
    "rename ",
    "copy ",
    "Binary files",
    "\\",
)
# Our own prompt wrappers; user-provided text must not be able to close or open one.
_WRAPPER_TAG = re.compile(
    r"<(/?\s*(?:spec|intake_answers|approved_scenarios|scenario_feedback|template"
    r"|task|rules|seed_examples|requests|scenarios)\b)",
    re.I,
)


class CardDraftError(ValueError):
    """The model's scenario cards could not be used."""

    cost_usd: float = 0.0  # what the model call that produced them cost


def neutralise_tags(text: str) -> str:
    """Defang `<spec>`, `</spec>`, `</template>` and the other wrapper tags inside user-provided text."""
    return _WRAPPER_TAG.sub(r"&lt;\1", text)


def load_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def added_lines(diff: str, *, include_context: bool = False) -> dict[str, dict[int, str]]:
    """Map each file to {new-file line number: text} for every ADDED line of a unified diff.

    With `include_context=True` the unchanged (context) lines of each hunk are included as well, i.e. every line
    of the new file that the diff shows.

    Strict, mirroring the project's verified parser (tests/scoring/test_seed_scenarios.py): hunks are consumed by
    their declared line counts. Raises CardDraftError when a hunk body is shorter than its header says, has a line
    that is not '+', '-', ' ' (or an empty context line), or when text other than a diff header sits outside a hunk.
    """
    added: dict[str, dict[int, str]] = {}
    lines = diff.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":  # the newline that ends the last line is not an empty context line
        lines.pop()
    path: str | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _HUNK.match(line)
        if line.startswith("+++ "):
            target = line[4:].strip().split("\t")[0]
            path = None if target == "/dev/null" else target.removeprefix("b/")
            if path is not None:
                added.setdefault(path, {})
            i += 1
        elif m:
            if path is None:
                raise CardDraftError("a hunk appears before any '+++ b/file' line")
            old_left = int(m.group(2) if m.group(2) is not None else 1)
            new_left = int(m.group(4) if m.group(4) is not None else 1)
            new_no = int(m.group(3))
            i += 1
            while old_left > 0 or new_left > 0:
                if i >= len(lines):
                    raise CardDraftError(f"a hunk in {path} is shorter than its header says")
                body = lines[i]
                tag, text = body[:1], body[1:]
                if tag == "+":
                    added[path][new_no] = text
                    new_no += 1
                    new_left -= 1
                elif tag == "-":
                    old_left -= 1
                elif tag == " " or body == "":
                    if include_context:
                        added[path][new_no] = text
                    new_no += 1
                    old_left -= 1
                    new_left -= 1
                else:
                    raise CardDraftError(f"a hunk in {path} has a line that does not match its header: {body!r}")
                if old_left < 0 or new_left < 0:
                    raise CardDraftError(f"a hunk in {path} is longer than its header says")
                i += 1
            while i < len(lines) and lines[i].startswith("\\"):  # "\ No newline at end of file"
                i += 1
        elif line == "" or line.startswith(_FILE_HEADERS):
            i += 1
        else:
            raise CardDraftError(f"the diff has stray text outside a hunk: {line!r}")
    return added


MATCH_WINDOW = 3  # the scorer matches a finding within line_start-3 .. line_end+3 [PRD › Scoring rule (decided)]


def _reject_repeated_issues(expected: list[ExpectedIssue]) -> None:
    """Two expected issues with the same file and rule whose match windows overlap or touch cannot be told apart.

    The scorer matches one-to-one, so one correct finding would match only one of them and leave the other
    "missed": a correct review would fail while a duplicated finding would pass.
    """
    for i, a in enumerate(expected):
        for b in expected[i + 1 :]:
            if a.file != b.file or a.rule_id != b.rule_id:
                continue
            touching = (
                a.line_start - MATCH_WINDOW <= b.line_end + MATCH_WINDOW + 1
                and b.line_start - MATCH_WINDOW <= a.line_end + MATCH_WINDOW + 1
            )
            if touching:
                raise CardDraftError(
                    f"expected lists the same problem twice ({a.rule_id} in {a.file} near line {a.line_start})"
                )


def _check_card(raw: Any) -> tuple[str, str, str, AnswerKey]:
    """Validate one raw card from the model; return (kind, title, diff, answer_key) or raise CardDraftError."""
    if not isinstance(raw, dict):
        raise CardDraftError("card is not an object")
    kind = raw.get("kind")
    if kind not in _KINDS:
        raise CardDraftError(f"unknown kind {kind!r}")
    title = raw.get("title")
    diff = raw.get("diff")
    if not isinstance(title, str) or not title.strip():
        raise CardDraftError("missing title")
    if not isinstance(diff, str) or not diff.strip():
        raise CardDraftError("missing diff")
    try:
        key = AnswerKey(
            expected=[ExpectedIssue.model_validate(e) for e in raw.get("expected") or []],
            must_not_flag=list(raw.get("must_not_flag") or []),
        )
    except Exception as e:  # pydantic.ValidationError, TypeError
        raise CardDraftError(f"bad answer key: {e}") from e

    for rule_id in [e.rule_id for e in key.expected] + key.must_not_flag:
        if not isinstance(rule_id, str) or not _RULE_ID.match(rule_id):
            raise CardDraftError(f"rule id {rule_id!r} is not kebab-case")
    if kind == "clean" and (key.expected or key.must_not_flag):
        raise CardDraftError("a clean card must have no expected issues")
    if kind in ("planted", "adversarial") and not key.expected:
        raise CardDraftError(f"a {kind} card needs at least one expected issue")
    if kind == "edge" and not key.expected and not key.must_not_flag:
        raise CardDraftError("an edge card needs an expected issue or a rule that must stay quiet")

    _reject_repeated_issues(key.expected)
    added = added_lines(diff)  # raises CardDraftError on a malformed diff
    if not added:
        raise CardDraftError("diff has no files")
    for issue in key.expected:
        shown = added.get(issue.file)
        if shown is None:
            raise CardDraftError(f"expected issue names {issue.file!r}, which is not in the diff")
        if issue.line_start > issue.line_end:
            raise CardDraftError("line_start is after line_end")
        if not all(n in shown for n in range(issue.line_start, issue.line_end + 1)):
            raise CardDraftError(
                f"lines {issue.line_start}-{issue.line_end} of {issue.file} are not all added lines of the diff"
            )
    return kind, title.strip(), diff, key


def parse_cards(data: Any) -> tuple[list[Scenario], int]:
    """Turn the model's JSON into Scenarios (ids acc-01, acc-02, ...). Returns (cards, dropped_count).

    Cards that fail validation are dropped; more than MAX_CARDS are cut; fewer than MIN_CARDS valid raises.
    """
    raw_cards = data.get("cards") if isinstance(data, dict) else None
    if not isinstance(raw_cards, list):
        raise CardDraftError("The model did not return a list of scenario cards.")
    good: list[tuple[str, str, str, AnswerKey]] = []
    dropped = 0
    for raw in raw_cards:
        try:
            good.append(_check_card(raw))
        except CardDraftError:
            dropped += 1
    if len(good) < MIN_CARDS:
        raise CardDraftError(
            f"Only {len(good)} usable scenario cards were drafted ({dropped} were dropped as invalid); "
            f"at least {MIN_CARDS} are needed."
        )
    dropped += max(0, len(good) - MAX_CARDS)
    good = good[:MAX_CARDS]
    cards = [
        Scenario(
            id=f"acc-{i:02d}",
            kind=kind,  # type: ignore[arg-type]
            split="train",
            title=title,
            diff=diff,
            answer_key=key,
            approved_by_user=False,
        )
        for i, (kind, title, diff, key) in enumerate(good, start=1)
    ]
    return cards, dropped


async def draft_cards(
    state: Mapping[str, Any],
    *,
    intake_answers: list[dict[str, str]] | None = None,
    on_cost: Callable[[float], None] | None = None,
) -> list[Scenario]:
    """Draft 6-10 scenario cards from the spec and the intake answers (Haiku).

    `intake_answers` is a list of {"id", "question", "answer", "source"}; `on_cost` receives the call's USD cost
    so the calling head can add it to `cost_usd`. Raises `budget.BudgetExceeded` before the call if the run is
    over its cap, and `CardDraftError` if fewer than 6 usable cards come back.
    """
    budget.check(state)
    message = (
        f"<spec>{neutralise_tags(str(state['spec']))}</spec>\n"
        f"<intake_answers>{neutralise_tags(json.dumps(intake_answers or []))}</intake_answers>"
    )
    with tracing.span(HEAD, run_id=state["run_id"], step="cards"):
        result = await llm.call(HEAD, messages=[{"role": "user", "content": message}], system=load_prompt())
        if on_cost is not None:
            on_cost(result.cost_usd)  # before parsing, so a failed draft still reports what it cost
        try:
            cards, _dropped = parse_cards(result.json())
        except ValueError as e:
            if isinstance(e, CardDraftError):
                e.cost_usd = result.cost_usd
            raise
    return cards


# =============================================================================================================
# The full head: train / held-out scenarios and their mocked world (P3.1)
# =============================================================================================================


class SimulatorError(Exception):
    """The scenarios could not be produced. The message is plain English."""


class World(BaseModel):
    """The mocked files a Level 1 run shows the agent for one scenario: the new content of every touched file."""

    files: dict[str, str]
    notes: str = ""


class ScenarioDrop(Exception):
    """One scenario failed validation. `category` is a key of DROP_LABELS."""

    def __init__(self, category: str, detail: str) -> None:
        super().__init__(detail)
        self.category = category
        self.detail = detail


# Phrases that follow a count: "2 pointing at lines the change does not add".
DROP_LABELS = {
    "shape": "in the wrong format",
    "id": "with an unusable or repeated name",
    "rules": "using a rule that is not in the plan",
    "lines": "pointing at lines the change does not add",
    "kind": "whose kind and answer key disagree",
    "diff": "with a malformed change",
    "world": "whose mocked files disagree with the change",
    "dupes": "listing the same problem twice",
    "title": "with a title that shows code or a rule name",
    "extra": "beyond the number asked for",
}


@dataclass(frozen=True)
class Slot:
    """One scenario the model is asked to write."""

    id: str
    kind: str
    rule_id: str | None
    file_hint: str


@dataclass
class _Spend:
    """Model cost accumulated by this head, so the budget guard sees it before the next call."""

    base: float
    added: float = 0.0

    def check(self, state: Mapping[str, Any]) -> None:
        budget.check({**state, "cost_usd": self.base + self.added})

    @property
    def total(self) -> float:
        return self.base + self.added


def load_scenario_prompt() -> str:
    return _HTML_COMMENT.sub("", SCENARIO_PROMPT_PATH.read_text(encoding="utf-8"), count=1).strip()


def scenario_count() -> int:
    """How many NEW scenarios to ask for: env SIMULATOR_SCENARIO_COUNT, clamped to 8..40, default 24."""
    raw = os.environ.get(SCENARIO_COUNT_ENV, "").strip()
    try:
        n = int(raw) if raw else DEFAULT_SCENARIO_COUNT
    except ValueError:
        n = DEFAULT_SCENARIO_COUNT
    return max(MIN_SCENARIO_COUNT, min(MAX_SCENARIO_COUNT, n))


# --- slots: what to ask for ----------------------------------------------------------------------------------


def make_slots(rule_ids: list[str], count: int) -> list[Slot]:
    """The mix of scenarios to request: every rule gets planted scenarios, plus edge, adversarial and clean."""
    n_adv = max(2, round(0.20 * count))
    n_edge = max(2, round(0.25 * count))
    n_clean = max(2, round(0.15 * count))
    n_planted = max(count - n_adv - n_edge - n_clean, 1)
    kinds = ["planted"] * n_planted + ["edge"] * n_edge + ["adversarial"] * n_adv + ["clean"] * n_clean
    slots: list[Slot] = []
    per_kind: Counter[str] = Counter()
    for i, kind in enumerate(kinds):
        j = per_kind[kind]
        per_kind[kind] += 1
        rule = None if kind == "clean" or not rule_ids else rule_ids[j % len(rule_ids)]
        slots.append(Slot(f"sim-{i + 1:02d}", kind, rule, FILE_HINTS[i % len(FILE_HINTS)]))
    return slots


def batches_of(slots: list[Slot]) -> list[list[Slot]]:
    """Split into batches that each mix the kinds (strided), so one bad answer does not wipe out one kind."""
    n = max(1, math.ceil(len(slots) / BATCH_SIZE))
    return [slots[i::n] for i in range(n)]


# --- validation ----------------------------------------------------------------------------------------------


def _classify(message: str) -> str:
    if "same problem twice" in message:
        return "dupes"
    if "hunk" in message or "stray text" in message or "has no files" in message:
        return "diff"
    if "rule id" in message:
        return "rules"
    if "lines" in message or "not in the diff" in message or "line_start" in message:
        return "lines"
    if "kind" in message or "clean card" in message or "needs at least" in message or "edge card" in message:
        return "kind"
    return "shape"


def _check_world(raw: Any, diff: str) -> World:
    """The world must name every file the diff touches, with the diff's added lines at the right line numbers."""
    try:
        world = World.model_validate(raw)
    except ValidationError as e:
        raise ScenarioDrop("world", "the mocked files are not {files, notes}") from e
    if not world.files:
        raise ScenarioDrop("world", "the mocked world has no files")
    for path, content in world.files.items():
        parts = PurePosixPath(path).parts
        if not path or path.startswith("/") or "\\" in path or ".." in parts:
            raise ScenarioDrop("world", f"unsafe file path {path!r}")
        if len(content) > MAX_WORLD_FILE_CHARS:
            raise ScenarioDrop("world", f"{path} is too long")
    try:
        added = added_lines(diff)
    except CardDraftError as e:
        raise ScenarioDrop("diff", str(e)) from e
    for path, lines in added.items():
        if path not in world.files:
            raise ScenarioDrop("world", f"{path} is changed by the diff but missing from the mocked files")
        content_lines = world.files[path].splitlines()
        for n, text in lines.items():
            if n > len(content_lines) or content_lines[n - 1] != text:
                raise ScenarioDrop("world", f"{path} line {n} does not match the diff")
    return world


def _title_problem(title: str, rule_ids: set[str]) -> str | None:
    """Titles are plain-English cards for non-technical users: no backticks, no policy rule ids."""
    if "`" in title:
        return "the title contains a backtick"
    for rule_id in sorted(rule_ids):
        if re.search(rf"(?<![a-z0-9-]){re.escape(rule_id)}(?![a-z0-9-])", title, re.I):
            return f"the title contains the rule id {rule_id}"
    return None


def validate_scenario(
    raw: Any, rule_ids: set[str], taken: set[str], *, world_required: bool, seed: bool = False
) -> tuple[Scenario, World | None]:
    """Validate one raw scenario (a dict shaped like the prompt's output). Raises ScenarioDrop.

    Checks: id is a safe, unused name; `_check_card` (kind, answer key, diff parses, expected lines are ADDED lines);
    every rule id is in the plan; the answer key does not contradict itself; Scenario.model_validate; and, when
    `world_required`, the world is consistent with the diff.

    `seed=True` is for the hand-written seeds: a rule in `must_not_flag` that the plan lacks is vacuous, so it is
    removed rather than failing the seed, and a clean seed may keep a (trimmed) `must_not_flag` list.
    """
    if not isinstance(raw, dict):
        raise ScenarioDrop("shape", "not an object")
    trimmed: list[str] | None = None
    if seed:
        trimmed = [r for r in raw.get("must_not_flag") or [] if r in rule_ids]
        raw = {**raw, "must_not_flag": [] if raw.get("kind") == "clean" else trimmed}
    sid = raw.get("id")
    if not isinstance(sid, str) or not _ID.match(sid):
        raise ScenarioDrop("id", f"unusable id {sid!r}")
    if sid in taken:
        raise ScenarioDrop("id", f"duplicate id {sid}")
    try:
        kind, title, diff, key = _check_card(raw)
    except CardDraftError as e:
        raise ScenarioDrop(_classify(str(e)), str(e)) from e
    if seed and kind == "clean":
        key = AnswerKey(expected=[], must_not_flag=trimmed or [])
    if not seed:
        bad_title = _title_problem(title, rule_ids)
        if bad_title:
            raise ScenarioDrop("title", f"{sid}: {bad_title}")
    used = {e.rule_id for e in key.expected} | set(key.must_not_flag)
    unknown = sorted(used - rule_ids)
    if unknown:
        raise ScenarioDrop("rules", f"{sid} uses rules that are not in the plan: {', '.join(unknown)}")
    clash = {e.rule_id for e in key.expected} & set(key.must_not_flag)
    if clash:
        raise ScenarioDrop("kind", f"{sid} both expects and forbids {', '.join(sorted(clash))}")
    try:
        scenario = Scenario.model_validate(
            {"id": sid, "kind": kind, "split": "train", "title": title, "diff": diff, "answer_key": key.model_dump()}
        )
    except ValidationError as e:
        raise ScenarioDrop("shape", str(e)) from e
    world = _check_world(raw.get("world"), diff) if world_required else None
    return scenario, world


def _seed_to_raw(sc: Scenario) -> dict[str, Any]:
    return {
        "id": sc.id,
        "kind": sc.kind,
        "title": sc.title,
        "diff": sc.diff,
        "expected": [e.model_dump() for e in sc.answer_key.expected],
        "must_not_flag": list(sc.answer_key.must_not_flag),
    }


def world_from_diff(diff: str) -> World:
    """A world rebuilt from the diff alone: the lines it shows, at their line numbers; other lines blank."""
    files: dict[str, str] = {}
    for path, lines in added_lines(diff, include_context=True).items():
        last = max(lines, default=0)
        files[path] = "\n".join(lines.get(n, "") for n in range(1, last + 1)) + ("\n" if last else "")
    return World(files=files, notes="Only the lines the change shows; every other line of the file is blank.")


# --- splitting -----------------------------------------------------------------------------------------------


def split_scenarios(
    pool: list[Scenario], run_id: str, fraction: float = HELDOUT_FRACTION
) -> tuple[list[Scenario], list[Scenario]]:
    """Return (train, heldout). ~`fraction` of the pool goes to held-out, stratified by kind.

    Every kind with 2+ scenarios appears in both splits. Deterministic for a given run_id and pool.
    """
    rng = random.Random(run_id)
    train: list[Scenario] = []
    heldout: list[Scenario] = []
    carry = 0.0
    for kind in _KINDS:
        group = sorted((s for s in pool if s.kind == kind), key=lambda s: s.id)
        if not group:
            continue
        rng.shuffle(group)
        n = len(group)
        target = n * fraction + carry
        k = int(target + 0.5)
        k = min(max(k, 1), n - 1) if n >= 2 else 0
        carry = target - k
        heldout += group[:k]
        train += group[k:]
    key = lambda s: s.id  # noqa: E731
    return (
        [s.model_copy(update={"split": "train"}) for s in sorted(train, key=key)],
        [s.model_copy(update={"split": "heldout"}) for s in sorted(heldout, key=key)],
    )


# --- model calls ---------------------------------------------------------------------------------------------


async def _ask(state: Mapping[str, Any], spend: _Spend, message: str) -> llm.LLMResult:
    spend.check(state)  # before EVERY model call
    result = await llm.call(HEAD, messages=[{"role": "user", "content": message}], system=load_scenario_prompt())
    spend.added += result.cost_usd
    return result


def _data(label: str, value: Any) -> str:
    return f"<{label}>{neutralise_tags(json.dumps(value, ensure_ascii=False))}</{label}>"


def load_seeds(repo: Path) -> tuple[list[Scenario], int]:
    """The 10 seeds from the repo's scenarios/seeds/ (else the template's). Returns (seeds, unreadable files)."""
    folder = repo / SCENARIOS_DIR / "seeds"
    if not folder.is_dir():
        folder = TEMPLATE_SEEDS
    seeds: list[Scenario] = []
    unreadable = 0
    for path in sorted(folder.glob("*.json")):
        try:
            seeds.append(Scenario.model_validate_json(path.read_text(encoding="utf-8")))
        except (ValidationError, OSError, ValueError):
            unreadable += 1
    return seeds, unreadable


async def _generate_batch(
    state: Mapping[str, Any],
    spend: _Spend,
    plan: Plan,
    seeds: list[Scenario],
    batch: list[Slot],
) -> list[Any]:
    """One model call for one batch of slots; returns the raw scenarios. Raises ValueError if unreadable."""
    message = "\n".join(
        [
            "<task>scenarios</task>",
            f"<spec>{neutralise_tags(str(state.get('spec', '')) or plan.summary)}</spec>",
            _data("intake_answers", plan.intake_answers),
            _data("rules", [r.model_dump() for r in plan.rules]),
            _data(
                "seed_examples", [s.model_dump(include={"id", "kind", "title", "diff", "answer_key"}) for s in seeds]
            ),
            _data("requests", [asdict(s) for s in batch]),
        ]
    )
    result = await _ask(state, spend, message)
    data = result.json()
    items = data.get("scenarios") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError("no scenarios list")
    return items


async def _generate_worlds(
    state: Mapping[str, Any], spend: _Spend, plan: Plan, scenarios: list[Scenario]
) -> tuple[dict[str, World], int]:
    """A world for each scenario that has none: from the model when it checks out, else rebuilt from the diff.

    Returns (worlds by id, how many were rebuilt from the diff). A scenario whose diff cannot be parsed gets none.
    """
    worlds: dict[str, World] = {}
    rebuilt = 0
    for i in range(0, len(scenarios), BATCH_SIZE):
        chunk = scenarios[i : i + BATCH_SIZE]
        message = "\n".join(
            [
                "<task>worlds</task>",
                _data("rules", [r.model_dump() for r in plan.rules]),
                _data("scenarios", [{"id": s.id, "diff": s.diff} for s in chunk]),
            ]
        )
        answers: Any = {}
        try:
            data = (await _ask(state, spend, message)).json()
            answers = data.get("worlds") if isinstance(data, dict) else {}
        except ValueError:
            answers = {}
        if not isinstance(answers, dict):
            answers = {}
        for sc in chunk:
            try:
                worlds[sc.id] = _check_world(answers.get(sc.id), sc.diff)
                continue
            except ScenarioDrop as e:
                log.info("world for %s rebuilt from the diff: %s", sc.id, e.detail)
            try:
                worlds[sc.id] = world_from_diff(sc.diff)
                rebuilt += 1
            except CardDraftError as e:
                log.warning("no world for %s: %s", sc.id, e)
    return worlds, rebuilt


# --- writing -------------------------------------------------------------------------------------------------


def _dump(obj: BaseModel) -> str:
    return json.dumps(obj.model_dump(), indent=2, ensure_ascii=False) + "\n"


def write_scenarios(repo: Path, scenarios: list[Scenario], worlds: Mapping[str, World]) -> None:
    """Write scenarios/<split>/<id>.json and <id>.world.json. Replaces the .json files of any earlier run."""
    for sc in scenarios:
        if not _ID.match(sc.id):
            raise SimulatorError(f"The scenario name {sc.id!r} cannot be used as a file name.")
    for split in SPLIT_DIRS:
        folder = repo / SCENARIOS_DIR / split
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.glob("*.json"):
            old.unlink()
    for sc in scenarios:
        folder = repo / SCENARIOS_DIR / sc.split
        (folder / f"{sc.id}.json").write_text(_dump(sc), encoding="utf-8")
        if sc.id in worlds:
            (folder / f"{sc.id}.world.json").write_text(_dump(worlds[sc.id]), encoding="utf-8")


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
        raise SimulatorError(f"git {args[0]} failed: {tracing.scrub_text(err.decode().strip())}")
    return out.decode()


async def commit_scenarios(repo: Path) -> str:
    """Commit only scenarios/ inside the generated repo; return the HEAD sha."""
    await _git(repo, "add", "--", SCENARIOS_DIR)
    staged = (await _git(repo, "diff", "--cached", "--name-only", "--", SCENARIOS_DIR)).split()
    if staged:
        await _git(repo, "commit", "-m", COMMIT_MESSAGE, "--", SCENARIOS_DIR)
    return (await _git(repo, "rev-parse", "HEAD")).strip()


# --- the head ------------------------------------------------------------------------------------------------


def _reasons(drops: Counter[str]) -> str:
    return "; ".join(f"{n} {DROP_LABELS[c]}" for c, n in sorted(drops.items(), key=lambda kv: (-kv[1], kv[0])))


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


async def run(state: SusanooState) -> dict:
    """Graph node: write, validate, split and commit the train / held-out scenarios. Returns changed keys only.

    Returns {"scenarios": acceptance + train + heldout, "cost_usd", "head_commit"}. The Diagnostician and Fixer
    must filter out `split == "heldout"` themselves (see the module docstring).
    """
    with tracing.span(HEAD, run_id=state["run_id"]):
        await emit(state, HEAD, "started", "Writing test scenarios for the agent…")
        try:
            return await _run(state)
        except budget.BudgetExceeded:
            raise  # the graph ends the run cleanly
        except Exception as exc:
            await emit(state, HEAD, "error", f"I could not write the test scenarios: {tracing.scrub_text(str(exc))}")
            raise


async def _run(state: SusanooState) -> dict:
    plan: Plan = state["plan"]
    repo = Path(state["repo_path"])
    if not (repo / ".git").exists():
        raise SimulatorError("The generated app's folder is not a git repository, so nothing was written.")
    rule_ids = {r.id for r in plan.rules}
    if not rule_ids:
        raise SimulatorError("The plan has no rules, so there is nothing to write scenarios about.")
    spend = _Spend(float(state.get("cost_usd", 0.0)))

    acceptance = [s for s in state.get("scenarios", []) if s.split == "acceptance"]
    taken = {s.id for s in acceptance}

    # seeds: the same checks as generated ones (a seed about a rule this plan lacks cannot be scored fairly)
    raw_seeds, unreadable = load_seeds(repo)
    seed_drops: Counter[str] = Counter()
    if unreadable:
        seed_drops["shape"] += unreadable
    good_seeds: list[Scenario] = []
    for seed in raw_seeds:
        try:
            checked, _ = validate_scenario(_seed_to_raw(seed), rule_ids, taken, world_required=False, seed=True)
        except ScenarioDrop as e:
            seed_drops[e.category] += 1
            log.warning("dropped seed %s: %s (%s)", seed.id, e.category, e.detail)
            continue
        taken.add(checked.id)
        good_seeds.append(checked)

    count = scenario_count()
    slots = make_slots(list(dict.fromkeys(r.id for r in plan.rules)), count)
    await emit(
        state,
        HEAD,
        "progress",
        f"Asking for {count} new scenarios: planted bugs, tricky cases, attacks and clean changes.",
    )

    # generated scenarios
    generated: list[Scenario] = []
    worlds: dict[str, World] = {}
    drops: Counter[str] = Counter()
    unreadable_batches = 0
    for batch in batches_of(slots):
        try:
            items = await _generate_batch(state, spend, plan, raw_seeds, batch)
        except ValueError:
            unreadable_batches += 1
            log.warning("a scenario batch of %d was unreadable", len(batch))
            continue
        accepted_in_batch = 0
        for raw in items:
            try:
                sc, world = validate_scenario(raw, rule_ids, taken, world_required=True)
                if accepted_in_batch >= len(batch):  # more than were asked for
                    raise ScenarioDrop("extra", f"{sc.id}: more scenarios than the {len(batch)} requested")
            except ScenarioDrop as e:
                drops[e.category] += 1
                log.warning(
                    "dropped generated scenario %s: %s (%s)",
                    raw.get("id") if isinstance(raw, dict) else "?",
                    e.category,
                    e.detail,
                )
                continue
            assert world is not None
            accepted_in_batch += 1
            taken.add(sc.id)
            generated.append(sc)
            worlds[sc.id] = world
    requested = len(slots)
    n_dropped = sum(drops.values())
    parts: list[str] = []
    if n_dropped or unreadable_batches:
        gen_bits = []
        if n_dropped:
            gen_bits.append(
                f"Dropped {n_dropped} of {requested} new scenarios that did not check out ({_reasons(drops)})."
            )
        if unreadable_batches:
            verb = "was" if unreadable_batches == 1 else "were"
            gen_bits.append(
                f"{_plural(unreadable_batches, 'group')} of new scenarios came back unreadable and {verb} skipped."
            )
        parts.extend(gen_bits)
    if seed_drops:
        parts.append(
            f"Skipped {sum(seed_drops.values())} of {len(raw_seeds) + unreadable} starter examples "
            f"({_reasons(seed_drops)})."
        )
    if parts:
        await emit(
            state,
            HEAD,
            "progress",
            " ".join(parts),
            {"dropped_generated": n_dropped, "dropped_seeds": sum(seed_drops.values())},
        )

    pool = good_seeds + generated
    if len(pool) < 2:
        raise SimulatorError("Too few usable test scenarios were written (fewer than 2), so the run cannot continue.")
    train, heldout = split_scenarios(pool, state["run_id"])
    pool_kinds = {s.kind for s in pool}
    for name, part in (("held-out", heldout), ("practice", train)):
        missing = [k for k in _KINDS if k in pool_kinds and k not in {s.kind for s in part}]
        if missing:
            await emit(
                state,
                HEAD,
                "progress",
                f"Warning: the {name} scenarios have no {', '.join(missing)} case, because only one usable "
                "scenario of that kind was left to share between practice and held-out.",
                {"split": name, "missing_kinds": missing},
            )

    # worlds for the scenarios that have none yet: seeds and the approved acceptance cards
    need = [s for s in good_seeds + acceptance if s.id not in worlds]
    more, rebuilt = await _generate_worlds(state, spend, plan, need)
    worlds.update(more)
    if rebuilt:
        await emit(
            state,
            HEAD,
            "progress",
            f"For {_plural(rebuilt, 'scenario')}, the mocked files were rebuilt from the change itself, "
            "so they show only the lines it touches.",
        )

    final = [*acceptance, *train, *heldout]
    write_scenarios(repo, final, worlds)
    head_commit = await commit_scenarios(repo)

    await emit(
        state,
        HEAD,
        "finished",
        f"{_plural(len(final), 'scenario')} ready: {len(train)} to practise on, {len(heldout)} held back for the final "
        f"check, and {len(acceptance)} "
        + (
            "you approved."
            if acceptance and all(s.approved_by_user for s in acceptance)
            else "example scenarios from intake."
        ),
        {
            "train": len(train),
            "heldout": len(heldout),
            "acceptance": len(acceptance),
            "dropped": n_dropped + sum(seed_drops.values()),
            "head_commit": head_commit,
        },
    )
    return {"scenarios": final, "cost_usd": spend.total, "head_commit": head_commit}
