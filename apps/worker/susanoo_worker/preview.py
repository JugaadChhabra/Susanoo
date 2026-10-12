"""Keep a finished run's agent up at its preview address. [PRD › Deployment › Sandbox limits]; P6 (#32).

When the graph reaches Done with status 'done', the worker builds the generated repo as it stands (its HEAD is the
final kept version: the Verifier reverts every rolled-back patch) and starts it through the same sandbox start the
Level 2 check uses, so it gets the same CPU, memory, pids and no-mounts limits. Then it records the run's
preview_url, which is also what lets Caddy get the preview's certificate. The sandbox's idle reaper stops the app
later; the agent page then says the preview is asleep.

A preview that can't start never fails the run: the run stays 'done' with no preview_url, and a plain-English event
says so in general terms (exception text can hold credentials, so it's never echoed). Env values are never logged.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from susanoo_worker import store

from susanoo.db import emit_event

log = logging.getLogger("susanoo_worker")

PREVIEW_READY = "Your agent is live at {url}. It goes to sleep after a while with no visitors."
PREVIEW_FAILED = "Your agent is finished, but its live preview couldn't be started, so it has no web address for now."


class PreviewSandbox(Protocol):
    """The slice of `susanoo.sandbox.docker` used here (the module itself satisfies it); tests pass a fake."""

    def build_image(self, repo_path: str | Path, tag: str) -> str: ...
    def start_app(self, run_id: str, image: str, *, env: Mapping[str, str]) -> Any: ...


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


async def keep_preview(
    run_id: str,
    worker_id: str,
    state: Mapping[str, Any],
    sandbox: PreviewSandbox,
    *,
    connection_env: Callable[[str], Awaitable[Mapping[str, str]]] | None = None,
) -> str | None:
    """Start the final app and record its preview URL. Returns the URL, or None if there's no preview."""
    try:
        repo = state.get("repo_path")
        if not repo:
            raise RuntimeError("the run has no generated repo")
        env = await _app_env(run_id, list(state.get("connectors") or []), connection_env)
        image = await asyncio.to_thread(sandbox.build_image, repo, f"susanoo-app-{run_id}:preview")
        log.info("Run %s: starting its preview (env names: %s)", run_id, sorted(env))
        handle = await asyncio.to_thread(sandbox.start_app, run_id, image, env=env)
        url = str(handle.url)
        if not await store.set_preview_url(run_id, worker_id, url):
            return None  # another worker owns the run now; the reaper still stops this container
    except Exception as exc:
        log.warning("Run %s: its preview could not be started (%s)", run_id, type(exc).__name__)
        await emit_event(run_id, "system", "progress", PREVIEW_FAILED, {"preview_url": None})
        return None
    await emit_event(run_id, "system", "progress", PREVIEW_READY.format(url=url), {"preview_url": url})
    return url
