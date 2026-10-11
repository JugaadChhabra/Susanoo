"""Simulator head, card-drafting part only. [PRD › Intake: questions and approved scenarios]

`draft_cards` writes 6-10 plain-English scenario cards (a diff plus an answer key) for the user to approve
at intake. The full Simulator head (train and held-out scenarios, P3.1) will extend this file.

Drafted cards are `split="train"` and `approved_by_user=False`; only the approval step in
`heads/planner.py` promotes the ones the user approves to `split="acceptance"`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from susanoo.guards import budget
from susanoo.integrations import llm, tracing
from susanoo.schemas import AnswerKey, ExpectedIssue, Scenario

HEAD = "simulator"
PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "simulator_cards.md"

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
_WRAPPER_TAG = re.compile(r"<(/?\s*(?:spec|intake_answers|approved_scenarios|scenario_feedback|template)\b)", re.I)


class CardDraftError(ValueError):
    """The model's scenario cards could not be used."""

    cost_usd: float = 0.0  # what the model call that produced them cost


def neutralise_tags(text: str) -> str:
    """Defang `<spec>`, `</spec>`, `</template>` and the other wrapper tags inside user-provided text."""
    return _WRAPPER_TAG.sub(r"&lt;\1", text)


def load_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def added_lines(diff: str) -> dict[str, dict[int, str]]:
    """Map each file to {new-file line number: text} for every ADDED line of a unified diff.

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
