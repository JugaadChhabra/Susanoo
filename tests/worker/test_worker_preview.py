"""A finished run keeps its final app running at its preview URL (#32). [PRD › Deployment › Sandbox limits]"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from helpers import FAST, Ledger, answer_until_terminal, instrumented
from susanoo_worker.main import Worker
from susanoo_worker.preview import PREVIEW_FAILED


class FakeSandbox:
    """Records builds and starts; a started app stays in `running` until something stops it."""

    def __init__(self, *, fail_build: bool = False) -> None:
        self.fail_build = fail_build
        self.builds: list[tuple[str, str]] = []
        self.running: dict[str, dict[str, str]] = {}  # run id -> env it started with

    def build_image(self, repo_path: Any, tag: str) -> str:
        if self.fail_build:
            raise RuntimeError("build broke near sk-secret-value")
        self.builds.append((str(repo_path), tag))
        return tag

    def start_app(self, run_id: str, image: str, *, env: Any) -> Any:
        self.running[run_id] = dict(env)
        return SimpleNamespace(run_id=run_id, url=f"https://run-{run_id}.preview.test")

    def stop_app(self, handle: Any) -> None:
        self.running.pop(handle.run_id, None)


async def run_to_end(db, sandbox: FakeSandbox) -> tuple[str, dict]:
    run_id = db.make_run()
    worker = Worker(FAST, instrumented(Ledger()), worker_id="w1", preview_sandbox=sandbox)
    task = asyncio.create_task(worker.run())
    try:
        final = await answer_until_terminal(db, run_id)
    finally:
        worker.request_stop()
        await asyncio.wait_for(task, 30)
    return run_id, final


async def test_a_finished_run_records_its_preview_and_leaves_the_app_running(db, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_APP_API_KEY", "sk-app-test")
    sandbox = FakeSandbox()
    run_id, final = await run_to_end(db, sandbox)

    assert final["status"] == "done"
    assert final["preview_url"] == f"https://run-{run_id}.preview.test"
    assert run_id in sandbox.running  # still up after the run; the idle reaper stops it later
    assert sandbox.running[run_id]["ANTHROPIC_APP_API_KEY"] == "sk-app-test"
    assert sandbox.builds == [(f"runs/{run_id}/repo", f"susanoo-app-{run_id}:preview")]  # the repo as it ended
    live = [e for e in db.events(run_id) if e["payload"].get("preview_url")]
    assert len(live) == 1 and final["preview_url"] in live[0]["message"]


async def test_a_preview_that_cannot_start_leaves_the_run_done_and_says_so_plainly(db, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_APP_API_KEY", "sk-app-test")
    sandbox = FakeSandbox(fail_build=True)
    run_id, final = await run_to_end(db, sandbox)

    assert final["status"] == "done" and final["error"] is None
    assert final["preview_url"] is None and sandbox.running == {}
    messages = [e["message"] for e in db.events(run_id)]
    assert PREVIEW_FAILED in messages
    assert not any("sk-secret-value" in m for m in messages)


async def test_without_an_app_key_no_preview_is_started(db, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_APP_API_KEY", raising=False)
    sandbox = FakeSandbox()
    run_id, final = await run_to_end(db, sandbox)

    assert final["status"] == "done" and final["preview_url"] is None and sandbox.running == {}
