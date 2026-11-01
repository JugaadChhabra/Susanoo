"""Runner capacity benchmark: measured p95 and resource use at a stated size/concurrency [Spec §23; T40].

Numbers are measurements from this host, never targets. The report states suite, concurrency, repetitions and
host so a reader can tell what was actually exercised.
"""

from __future__ import annotations

import asyncio
import math
import os
import platform
try:
    import resource
except ImportError:
    resource = None
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict
from susanoo_sdk import AgentAdapter

from .journal import Journal
from .local import RunReport, run_suite
from .suite import LocalSuite


class CapacityReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    measured_at: datetime
    host: str
    python: str
    suite_id: str
    cases_per_run: int
    concurrency: int
    repetitions: int
    attempts: int
    passed: int
    not_passed: int
    execution_errors: int
    wall_seconds: float
    attempts_per_second: float
    attempt_p50_seconds: float
    attempt_p95_seconds: float
    attempt_max_seconds: float
    peak_rss_mb: float
    journal_bytes: int
    gates: list[str]


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile; no interpolation so the value is one that actually occurred."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, min(len(ordered), math.ceil(fraction * len(ordered))))
    return ordered[rank - 1]


def peak_rss_mb() -> float:
    if resource is None:
        import psutil
        return psutil.Process().memory_info().peak_wset / (1024 * 1024)
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


async def benchmark(
    suite: LocalSuite,
    *,
    agent_factory: Callable[[], AgentAdapter],
    simulator_url: str,
    simulator_admin_key: str,
    journal_path: Path,
    concurrency: int = 3,
    repetitions: int = 1,
    case_ids: list[str] | None = None,
) -> CapacityReport:
    """Run ``concurrency`` suite runs at once, ``repetitions`` times, against one shared journal."""
    if concurrency < 1 or repetitions < 1:
        raise ValueError("concurrency and repetitions must be at least 1")
    journal = Journal(journal_path)
    reports: list[RunReport] = []
    started = time.perf_counter()
    try:
        for _ in range(repetitions):
            batch = await asyncio.gather(
                *(
                    run_suite(
                        suite,
                        agent_factory=agent_factory,
                        agent_label="capacity",
                        journal=journal,
                        simulator_url=simulator_url,
                        simulator_admin_key=simulator_admin_key,
                        case_ids=case_ids,
                    )
                    for _ in range(concurrency)
                )
            )
            reports.extend(batch)
    finally:
        journal.close()
    wall = time.perf_counter() - started
    attempts = [a for report in reports for a in report.attempts]
    durations = [a.duration_seconds for a in attempts]
    passed = sum(1 for a in attempts if a.business_verdict == "PASS")
    return CapacityReport(
        measured_at=datetime.now(UTC),
        host=f"{platform.system()} {platform.machine()} ({os.cpu_count()} CPU)",
        python=platform.python_version(),
        suite_id=suite.id,
        cases_per_run=len(reports[0].attempts) if reports else 0,
        concurrency=concurrency,
        repetitions=repetitions,
        attempts=len(attempts),
        passed=passed,
        not_passed=len(attempts) - passed,
        execution_errors=sum(1 for a in attempts if a.execution_state != "completed"),
        wall_seconds=round(wall, 3),
        attempts_per_second=round(len(attempts) / wall, 3) if wall else 0.0,
        attempt_p50_seconds=round(percentile(durations, 0.50), 4),
        attempt_p95_seconds=round(percentile(durations, 0.95), 4),
        attempt_max_seconds=round(max(durations, default=0.0), 4),
        peak_rss_mb=round(peak_rss_mb(), 1),
        journal_bytes=sum(p.stat().st_size for p in journal_path.parent.glob(journal_path.name + "*")),
        gates=[report.gate for report in reports],
    )
