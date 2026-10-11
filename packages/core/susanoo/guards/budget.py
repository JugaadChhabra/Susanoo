"""Per-run cost guard. [PRD › Connectors and API budget › Anthropic budget]

Every head calls `check(state)` before each model call. When the run has spent the
cap, it raises BudgetExceeded; the graph catches it, sets `status`, and ends cleanly
with a partial report.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from typing import Any

RUN_COST_CAP_ENV = "RUN_COST_CAP_USD"
DEFAULT_RUN_COST_CAP_USD = 2.0


class BudgetExceeded(Exception):
    def __init__(self, cost_usd: float, cap_usd: float) -> None:
        self.cost_usd = cost_usd
        self.cap_usd = cap_usd
        super().__init__(f"Run cost ${cost_usd:.4f} reached the ${cap_usd:.2f} cap")


def run_cost_cap_usd() -> float:
    """The per-run cap in USD, read from the environment on every call."""
    raw = os.environ.get(RUN_COST_CAP_ENV)
    if raw is None or raw.strip() == "":
        return DEFAULT_RUN_COST_CAP_USD
    try:
        cap = float(raw)
    except ValueError:
        raise ValueError(f"{RUN_COST_CAP_ENV} must be a number, got {raw!r}") from None
    if not math.isfinite(cap) or cap <= 0:
        raise ValueError(f"{RUN_COST_CAP_ENV} must be a positive number, got {raw!r}")
    return cap


def check(state: Mapping[str, Any]) -> None:
    """Raise BudgetExceeded if `state["cost_usd"]` has reached the per-run cap."""
    cap = run_cost_cap_usd()
    cost = float(state.get("cost_usd", 0.0))
    if cost >= cap:
        raise BudgetExceeded(cost, cap)
