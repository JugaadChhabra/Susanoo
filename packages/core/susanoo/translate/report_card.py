"""Report card: plain-language scores, e.g. "Caught 9 of 10 problems, 1 false alarm." [PRD › Non-technical users]

Pure code, no model: every number comes from the scorer's own counts (`ScenarioResult.matched / missed /
false_positives`, pooled per split exactly as `susanoo.scoring.compute_metrics` pools them), so the card can never
disagree with the real scoring. The headline set is the held-out one [PRD › Contracts (locked) › Scoring rule]:
the examples the Diagnostician and Fixer never see. Only when a run has no held-out results does the card fall back
to the practice (train) set, and it says so in plain words.

Wording: "Caught 9 of 10 problems, 1 false alarm." then "Last week: 6 of 10." when an earlier score is given.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from susanoo.schemas import IterationMetrics, Scenario, ScenarioResult, Split

Basis = Literal["heldout", "train", "none"]
Trend = Literal["better", "same", "worse", "new"]

BASIS_TEXT: dict[Basis, str] = {
    "heldout": "These numbers come from the hidden examples the agent has never practised on.",
    "train": "These numbers come from the practice examples, because there are no hidden examples for this run yet.",
    "none": "No examples have been scored yet.",
}


class SetScore(BaseModel):
    """What one scored set looked like. Store it with the run to compare later runs against it."""

    basis: Basis
    caught: int  # problems the agent found (the scorer's `matched`)
    problems: int  # problems it should have found (matched + missed)
    false_alarms: int


class ReportCard(BaseModel):
    iteration: int
    basis: Basis
    score: SetScore
    score_percent: int  # 0-100, the scorer's F1 for the headline set (held-out F1 when basis is "heldout")
    headline: str  # "Caught 9 of 10 problems, 1 false alarm."
    basis_note: str  # which set the numbers come from, in words
    comparison: str | None  # "Last week: 6 of 10."
    trend: Trend
    acceptance: str | None  # "Passed 4 of 5 of the examples you approved."
    unscored: str | None  # "1 example could not be checked."
    lines: list[str]  # every sentence above in reading order


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _split_results(
    results: Sequence[ScenarioResult], scenarios: Sequence[Scenario]
) -> dict[Split, list[ScenarioResult]]:
    split_of = {s.id: s.split for s in scenarios}
    by_split: dict[Split, list[ScenarioResult]] = {"train": [], "heldout": [], "acceptance": []}
    for result in results:
        if result.scenario_id not in split_of:  # same convention as scoring.compute_metrics
            raise ValueError(f"result for unknown scenario_id {result.scenario_id!r}")
        by_split[split_of[result.scenario_id]].append(result)
    return by_split


def _pool(results: Sequence[ScenarioResult]) -> tuple[int, int, int]:
    matched = sum(r.matched for r in results)
    expected = sum(r.matched + r.missed for r in results)
    return matched, expected, sum(r.false_positives for r in results)


def score_from_results(results: Sequence[ScenarioResult], scenarios: Sequence[Scenario]) -> SetScore:
    """The headline-set score for one iteration's results: held-out if there is any, else train, else none."""
    by_split = _split_results(results, scenarios)
    basis: Basis = "heldout" if by_split["heldout"] else "train" if by_split["train"] else "none"
    matched, expected, fp = _pool(by_split[basis]) if basis != "none" else (0, 0, 0)
    return SetScore(basis=basis, caught=matched, problems=expected, false_alarms=fp)


def previous_label(then: datetime, now: datetime) -> str:
    """Plain wording for how long ago an earlier score was taken: "Last week", "Yesterday", ..."""
    days = (now - then).total_seconds() / 86_400
    if days < 1:
        return "Earlier today"
    if days < 2:
        return "Yesterday"
    if days < 7:
        return f"{int(days)} days ago"
    if days < 14:
        return "Last week"
    return "Earlier"


def _fraction(score: SetScore) -> float:
    return score.caught / score.problems if score.problems else 1.0


def _trend(now: SetScore, before: SetScore | None) -> Trend:
    if before is None or before.basis != now.basis:
        return "new"
    if (_fraction(now), -now.false_alarms) > (_fraction(before), -before.false_alarms):
        return "better"
    if (_fraction(now), -now.false_alarms) < (_fraction(before), -before.false_alarms):
        return "worse"
    return "same"


def _headline(score: SetScore) -> str:
    if score.basis == "none":
        return "Nothing has been scored yet."
    alarms = "no false alarms" if score.false_alarms == 0 else _plural(score.false_alarms, "false alarm")
    if score.problems == 0:
        return f"There were no problems to catch, and {alarms}."
    return f"Caught {score.caught} of {_plural(score.problems, 'problem')}, {alarms}."


def _comparison(label: str, before: SetScore) -> str:
    if before.problems == 0:
        return f"{label}: no problems to catch."
    return f"{label}: {before.caught} of {before.problems}."


def report_card(
    metrics_now: IterationMetrics,
    results_now: Sequence[ScenarioResult],
    scenarios: Sequence[Scenario],
    previous: SetScore | ReportCard | None = None,
    *,
    previous_label_text: str = "Before",
) -> ReportCard:
    """Build the plain-language report card for one iteration.

    `results_now` are the ScenarioResults of iteration `metrics_now.iteration` (all splits), `scenarios` the
    scenarios they were scored against (for each one's split). `previous` is an earlier `SetScore` or
    `ReportCard` for the same run or project; the comparison line is shown only when it is about the same set.
    `previous_label_text` names it in the comparison ("Last week", see `previous_label`).
    """
    by_split = _split_results(results_now, scenarios)
    score = score_from_results(results_now, scenarios)
    before = previous.score if isinstance(previous, ReportCard) else previous

    if score.basis == "heldout":
        percent = metrics_now.heldout_f1
    elif score.basis == "train":
        percent = metrics_now.f1
    else:
        percent = 0.0
    comparable = before is not None and before.basis == score.basis
    comparison = _comparison(previous_label_text, before) if before is not None and comparable else None

    acceptance = by_split["acceptance"]
    acceptance_line = (
        f"Passed {sum(1 for r in acceptance if r.passed)} of {len(acceptance)} of the examples you approved."
        if acceptance
        else None
    )
    errors = sum(1 for r in results_now if r.error is not None)
    unscored = (
        f"{_plural(errors, 'example')} could not be checked because the agent's answer was unreadable or it failed."
        if errors
        else None
    )

    headline = _headline(score)
    note = BASIS_TEXT[score.basis]
    lines = [headline, note, *(x for x in (comparison, acceptance_line, unscored) if x)]
    return ReportCard(
        iteration=metrics_now.iteration,
        basis=score.basis,
        score=score,
        score_percent=round(percent * 100),
        headline=headline,
        basis_note=note,
        comparison=comparison,
        trend=_trend(score, before),
        acceptance=acceptance_line,
        unscored=unscored,
        lines=lines,
    )
