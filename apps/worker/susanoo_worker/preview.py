"""Keep a finished run's agent up at its preview address. [PRD › Deployment › Sandbox limits]; P6 (#32).

Once a run is recorded 'done', the worker builds the generated repo as it stands (its HEAD is the final kept
version: the Verifier reverts every rolled-back patch) and starts it through the same sandbox start the Level 2
check uses, so it gets the same CPU, memory, pids and no-mounts limits. When the app answers `/health`, the worker
records the run's preview_url, which is also what lets Caddy get the preview's certificate. The sandbox's idle
reaper stops the app after its timeout (counted from the start: nothing reports visits); the agent page then says
the preview is asleep.

This runs after the 'done' write, so a cancel or a lost claim can never turn a finished run into anything else.
A preview that can't start leaves the run 'done' with no preview_url, and a plain-English event says so in general
terms (exception text can hold credentials, so it's never echoed). Env values are never logged.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

import httpx
from susanoo_worker import store

from susanoo.db import emit_event
from susanoo.sandbox.docker import DEFAULT_IDLE_TIMEOUT_S

log = logging.getLogger("susanoo_worker")

PREVIEW_READY = (
    "Your agent is live at {url}. "
    f"Previews go to sleep about {DEFAULT_IDLE_TIMEOUT_S // 60} minutes after they start, to keep the server healthy."
)
PREVIEW_FAILED = "Your agent is finished, but its live preview couldn't be started, so it has no web address for now."
READY_TIMEOUT_S = 60.0
READY_POLL_S = 1.0


class PreviewSandbox(Protocol):
    """The slice of `susanoo.sandbox.docker` used here (the module itself satisfies it); tests pass a fake."""

    def build_image(self, repo_path: str | Path, tag: str) -> str: ...
    def start_app(self, run_id: str, image: str, *, env: Mapping[str, str]) -> Any: ...
    def stop_app(self, handle: Any) -> None: ...


async def _app_env(
    run_id: str, connectors: list[str], connection_env: Callable[[str], Awaitable[Mapping[str, str]]] | None
) -> dict[str, str]:
    """The same env the Level 2 check starts the app with: the app key plus the run's picked connections."""
    from susanoo.heads.runner import APP_KEY_ENV, level2_env

    resolved: Mapping[str, str] = {}
    if connectors:
        from susanoo.connectors import runtime_env  # lazy: reads Vault

        resolved = await (connection_env or runtime_env.run_connection_env)(run_id)
    env = level2_env(connectors, {**resolved, APP_KEY_ENV: os.environ.get(APP_KEY_ENV, "")})
    if APP_KEY_ENV not in env:
        raise RuntimeError(f"{APP_KEY_ENV} is not set, so the agent's app would have no key")
    return env


async def wait_until_ready(internal_url: str, timeout_s: float = READY_TIMEOUT_S) -> None:
    """Poll the app's /health on the sandbox network until it answers 200. Raises TimeoutError."""
    async with httpx.AsyncClient(timeout=5.0) as http:
        async with asyncio.timeout(timeout_s):
            while True:
                try:
                    if (await http.get(f"{internal_url.rstrip('/')}/health")).status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(READY_POLL_S)


async def _say(run_id: str, message: str, payload: dict[str, Any]) -> None:
    try:
        await emit_event(run_id, "system", "progress", message, payload)
    except Exception as exc:  # the run is already done; a lost note must not look like a failed run
        log.warning("Run %s: could not record the preview event (%s)", run_id, type(exc).__name__)


async def keep_preview(
    run_id: str,
    state: Mapping[str, Any],
    sandbox: PreviewSandbox,
    *,
    connection_env: Callable[[str], Awaitable[Mapping[str, str]]] | None = None,
) -> str | None:
    """Start the finished run's final app and record its preview URL. Returns the URL, or None. Never raises."""
    handle: Any = None
    try:
        repo = state.get("repo_path")
        if not repo:
            raise RuntimeError("the run has no generated repo")
        env = await _app_env(run_id, list(state.get("connectors") or []), connection_env)
        # Same tag as the Level 2 check: the build is mostly cached and each run keeps one image, not two.
        image = await asyncio.to_thread(sandbox.build_image, repo, f"susanoo-app-{run_id}:level2")
        log.info("Run %s: starting its preview (env names: %s)", run_id, sorted(env))
        handle = await asyncio.to_thread(sandbox.start_app, run_id, image, env=env)
        await wait_until_ready(str(handle.internal_url))
        url = str(handle.url)
        if not await store.set_preview_url(run_id, url):
            raise RuntimeError("the run is no longer done")
    except Exception as exc:
        log.warning("Run %s: its preview could not be started (%s)", run_id, type(exc).__name__)
        if handle is not None:
            try:
                await asyncio.to_thread(sandbox.stop_app, handle)
            except Exception as stop_exc:  # the idle reaper still removes it
                log.warning("Run %s: could not stop the unused preview (%s)", run_id, type(stop_exc).__name__)
        await _say(run_id, PREVIEW_FAILED, {"preview_url": None})
        return None
    await _say(run_id, PREVIEW_READY.format(url=url), {"preview_url": url})
    return url
