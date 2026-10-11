"""Iteration metrics. [PRD › Contracts (locked) › Scoring rule (decided)], rule 7.

Conventions (pending Muaaz's confirmation; implemented exactly as written here):

* Results are joined to scenarios by ``scenario_id``; a result with an unknown id raises ``ValueError``.
* Counts are pooled (micro-averaged) per split, not averaged per scenario.
* ``recall``, ``precision``, ``f1`` and ``severity_accuracy`` are computed over the ``train`` split.
* ``heldout_f1`` is the F1 over the ``heldout`` split; ``acceptance_pass_rate`` is passed / total over
  the ``acceptance`` split.
* Expected count for a scenario = matched + missed.
* recall = matched / expected; precision = matched / (matched + false_positives);
  f1 = 2PR / (P + R); severity_accuracy = severity_correct / matched.
* Zero-division: recall = 1.0 when expected == 0; precision = 1.0 when matched + fp == 0;
  f1 = 0.0 when P + R == 0; severity_accuracy = 1.0 when matched == 0;
  acceptance_pass_rate = 0.0 when there are no acceptance scenarios (results).
  An empty heldout pool gives recall 1, precision 1, so heldout_f1 = 1.0.
"""

from susanoo.schemas import IterationMetrics, Scenario, ScenarioResult, Split


def f1(matched: int, expected: int, false_positives: int) -> tuple[float, float, float]:
    """Return ``(recall, precision, f1)`` from pooled counts."""
    recall = matched / expected if expected else 1.0
    denom = matched + false_positives
    precision = matched / denom if denom else 1.0
    total = precision + recall
    score = 2 * precision * recall / total if total else 0.0
    return recall, precision, score


def _pool(results: list[ScenarioResult]) -> tuple[int, int, int, int]:
    """Pooled ``(matched, expected, false_positives, severity_correct)``."""
    matched = sum(r.matched for r in results)
    expected = sum(r.matched + r.missed for r in results)
    false_positives = sum(r.false_positives for r in results)
    severity_correct = sum(r.severity_correct for r in results)
    return matched, expected, false_positives, severity_correct


def compute_metrics(
    results: list[ScenarioResult],
    scenarios: list[Scenario],
    *,
    iteration: int,
    cost_usd: float,
) -> IterationMetrics:
    """Aggregate scenario results into the per-iteration metrics."""
    split_of: dict[str, Split] = {s.id: s.split for s in scenarios}
    by_split: dict[Split, list[ScenarioResult]] = {"train": [], "heldout": [], "acceptance": []}
    for result in results:
        if result.scenario_id not in split_of:
            raise ValueError(f"result for unknown scenario_id {result.scenario_id!r}")
        by_split[split_of[result.scenario_id]].append(result)

    matched, expected, fp, sev = _pool(by_split["train"])
    recall, precision, f1_score = f1(matched, expected, fp)
    severity_accuracy = sev / matched if matched else 1.0

    h_matched, h_expected, h_fp, _ = _pool(by_split["heldout"])
    _, _, heldout_f1 = f1(h_matched, h_expected, h_fp)

    acceptance = by_split["acceptance"]
    acceptance_pass_rate = sum(1 for r in acceptance if r.passed) / len(acceptance) if acceptance else 0.0

    return IterationMetrics(
        iteration=iteration,
        recall=recall,
        precision=precision,
        f1=f1_score,
        severity_accuracy=severity_accuracy,
        heldout_f1=heldout_f1,
        acceptance_pass_rate=acceptance_pass_rate,
        cost_usd=cost_usd,
    )
