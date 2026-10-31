"""Run a whole suite locally against the simulator, with no control plane.

This is the runtime half of the M2 vertical slice: correct write -> PASS, false completion -> FAIL,
observer outage -> UNKNOWN, using real processes, a real HTTP simulator and the durable journal.
Platform integration (claims, events, finalize over HTTP) is a separate gate.
"""

from __future__ import annotations

import asyncio
import secrets
import socket
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import uvicorn
from pydantic import BaseModel, ConfigDict, Field
from susanoo_evaluator import AttemptSummary, evaluator_digest, gate_blockers, run_gate
from susanoo_sdk import AgentAdapter
from susanoo_simulator.adapter import SimulatorFixtureAdapter
from susanoo_simulator.service import create_app

from .executor import AttemptExecutor, AttemptResult, CancelToken
from .journal import Journal
from .suite import LocalProfile, LocalSuite


class SimulatorServer:
    """The simulator HTTP service on a loopback socket in a background thread."""

    def __init__(self, root: Path | None = None):
        self._tmp = tempfile.TemporaryDirectory(prefix="susanoo-sim-") if root is None else None
        self.root = Path(self._tmp.name) if self._tmp else Path(root)
        self.admin_key = secrets.token_urlsafe(24)
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self.base_url = f"http://127.0.0.1:{self._socket.getsockname()[1]}"
        config = uvicorn.Config(create_app(self.root, self.admin_key), log_level="warning", lifespan="off")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, kwargs={"sockets": [self._socket]}, daemon=True)

    def __enter__(self) -> SimulatorServer:
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("Simulator service did not start")
            time.sleep(0.01)
        return self

    def __exit__(self, *_: Any) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)
        self._socket.close()
        if self._tmp:
            self._tmp.cleanup()


class RunCounts(BaseModel):
    """Overlapping categories, each with a fixed definition (Spec §13)."""

    scheduled: int = Field(description="cases in the approved suite")
    attempted: int = Field(description="attempts whose fixture was prepared")
    completed: int = Field(description="attempts whose execution finished normally")
    passed: int = Field(description="business verdict PASS")
    failed: int = Field(description="business verdict FAIL")
    unknown: int = Field(description="business verdict UNKNOWN")
    execution_errors: int = Field(description="error, timeout, cancellation or interruption")
    excluded: int = Field(default=0, description="excluded with a recorded reason")


class RunReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    suite_id: str
    suite_hash: str
    synthetic: bool
    approval_kind: str
    agent: str
    evaluator_digest: str
    started_at: datetime
    finished_at: datetime
    gate: Literal["IN_PROGRESS", "BLOCKED", "NEEDS_REVIEW", "READY_FOR_ACCEPTANCE"]
    blockers: list[str]
    counts: RunCounts
    attempts: list[AttemptResult]

    def view(self, audience: Literal["operator", "engineer"] = "engineer") -> dict[str, Any]:
        """Engineer view hides held-out case content; the operator (host admin) sees everything locally."""
        data = self.model_dump(mode="json")
        if audience == "engineer":
            for attempt in data["attempts"]:
                if attempt["visibility"] == "hidden":
                    attempt["outcomes"] = []
                    attempt["turns"] = []
                    attempt["reconciliations"] = []
                    attempt["title"] = "Held-out case"
        return data


def summarize(attempts: list[AttemptResult]) -> tuple[str, list[str], RunCounts]:
    summaries = [
        AttemptSummary(
            required=a.required,
            execution_state=a.execution_state,
            business_verdict=a.business_verdict,
            cleanup_state=a.cleanup_state,
            reconciliation_state=a.reconciliation_state,
        )
        for a in attempts
    ]
    blockers = sorted({f"{a.case_id}:{b}" for a, s in zip(attempts, summaries, strict=True) for b in gate_blockers(s)})
    counts = RunCounts(
        scheduled=len(attempts),
        attempted=sum(1 for a in attempts if a.turns or a.outcomes),
        completed=sum(1 for a in attempts if a.execution_state == "completed"),
        passed=sum(1 for a in attempts if a.business_verdict == "PASS"),
        failed=sum(1 for a in attempts if a.business_verdict == "FAIL"),
        unknown=sum(1 for a in attempts if a.business_verdict == "UNKNOWN"),
        execution_errors=sum(1 for a in attempts if a.execution_state != "completed"),
    )
    return run_gate(summaries), blockers, counts


async def run_suite(
    suite: LocalSuite,
    *,
    agent_factory: Callable[[], AgentAdapter],
    agent_label: str,
    journal: Journal,
    simulator_url: str,
    simulator_admin_key: str,
    profile: LocalProfile | None = None,
    observer_outage: bool = False,
    cleanup_failure: bool = False,
    cancel: CancelToken | None = None,
    case_ids: list[str] | None = None,
) -> RunReport:
    profile = profile or LocalProfile()
    limits = profile.narrow(suite.limits)
    run_id = str(uuid.uuid4())
    started = datetime.now(UTC)
    fixtures = SimulatorFixtureAdapter(
        simulator_url, simulator_admin_key, observer_outage=observer_outage, cleanup_failure=cleanup_failure
    )
    executor = AttemptExecutor(journal, run_id=run_id, limits=limits, agent_factory=agent_factory, fixtures=fixtures)
    attempts = []
    try:
        for case in suite.cases:
            if case_ids and case.id not in case_ids:
                continue
            refused = profile.admit(case)
            if refused:
                raise PermissionError(f"Case {case.id} exceeds the local profile: {'; '.join(refused)}")
            attempts.append(await executor.run(case, cancel))
    finally:
        await fixtures.aclose()
    gate, blockers, counts = summarize(attempts)
    return RunReport(
        run_id=run_id,
        suite_id=suite.id,
        suite_hash=suite.content_hash,
        synthetic=suite.synthetic,
        approval_kind=str(suite.approval.get("kind", "unspecified")),
        agent=agent_label,
        evaluator_digest=evaluator_digest(),
        started_at=started,
        finished_at=datetime.now(UTC),
        gate=gate,
        blockers=blockers,
        counts=counts,
        attempts=attempts,
    )


def run_suite_sync(suite: LocalSuite, **kwargs: Any) -> RunReport:
    return asyncio.run(run_suite(suite, **kwargs))
