"""Worker settings, read from the environment. [PRD > Tech stack] (job queue = Postgres table, polled by the worker)"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None
    if value < 0:
        raise ValueError(f"{name} must not be negative, got {raw!r}")
    return value


@dataclass(frozen=True)
class WorkerConfig:
    poll_seconds: float = 2.0  # how often an idle worker looks for a queued run
    heartbeat_seconds: float = 10.0  # how often a running run proves its worker is alive (also the cancel latency)
    stale_seconds: float = 60.0  # a running run with no heartbeat for this long is treated as crashed and resumed
    concurrency: int = 2  # runs one worker executes at once
    max_attempts: int = 3  # claims without reaching a pause or the end before a run is given up on
    shutdown_grace_seconds: float = 20.0  # on SIGTERM, how long in-flight runs get before they are handed back
    reaper_interval_seconds: float = 60.0  # idle generated-app containers are stopped on this interval

    def __post_init__(self) -> None:
        if self.concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.heartbeat_seconds <= 0 or self.poll_seconds <= 0:
            raise ValueError("heartbeat_seconds and poll_seconds must be positive")
        if self.stale_seconds < 2 * self.heartbeat_seconds:
            # a live worker must be able to miss one beat without losing its run
            raise ValueError("stale_seconds must be at least twice heartbeat_seconds")

    @classmethod
    def from_env(cls) -> WorkerConfig:
        d = cls()
        return cls(
            poll_seconds=_env_float("WORKER_POLL_SECONDS", d.poll_seconds),
            heartbeat_seconds=_env_float("WORKER_HEARTBEAT_SECONDS", d.heartbeat_seconds),
            stale_seconds=_env_float("WORKER_STALE_SECONDS", d.stale_seconds),
            concurrency=int(_env_float("WORKER_CONCURRENCY", d.concurrency)),
            max_attempts=int(_env_float("WORKER_MAX_ATTEMPTS", d.max_attempts)),
            shutdown_grace_seconds=_env_float("WORKER_SHUTDOWN_GRACE_SECONDS", d.shutdown_grace_seconds),
            reaper_interval_seconds=_env_float("WORKER_REAPER_INTERVAL_SECONDS", d.reaper_interval_seconds),
        )


SUPABASE_POOLER_PORT = 6543


def require_session_connection(database_url: str) -> None:
    """Refuse Supabase's transaction pooler (port 6543). The worker's startup takes a session-level advisory lock
    and LangGraph's setup() runs CREATE INDEX CONCURRENTLY; both need a direct or session-mode connection
    (port 5432), because the transaction pooler hands every statement to a different backend."""
    from urllib.parse import urlparse

    try:
        port = urlparse(database_url).port
    except ValueError:
        port = None
    if port == SUPABASE_POOLER_PORT:
        raise RuntimeError(
            "DATABASE_URL points at Supabase's transaction pooler (port 6543). The worker needs a direct or "
            "session-mode connection (port 5432) because of its advisory lock and checkpoint table setup. "
            "Change the port in DATABASE_URL to 5432."
        )
