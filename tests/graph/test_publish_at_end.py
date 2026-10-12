"""Done pushes the generated repo's HEAD to its GitHub origin (best effort). No network: push_repo is faked.

[PRD › LangGraph flow and shared state]; the Builder pushes only the first commit, so the finished agent, the
scenarios and the fixes would otherwise never reach GitHub.
"""

from __future__ import annotations

import subprocess

import pytest
from graph_helpers import run_graph

from susanoo.graph import stubs
from susanoo.graph.build import Heads
from susanoo.integrations import github

FAST = {"intake_questions": []}
TOKEN = "tok_" + "unit_test_value_123"
ORIGIN = "https://github.com/acme-org/review-agent-abc123.git"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in github.TOKEN_ENVS:
        monkeypatch.delenv(name, raising=False)


def make_repo(tmp_path, origin: str | None = ORIGIN):
    repo = tmp_path / "gen"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    if origin:
        subprocess.run(["git", "remote", "add", "origin", origin], cwd=repo, check=True)
    return repo


@pytest.fixture
def pushes(monkeypatch):
    calls: list[tuple] = []

    async def fake_push(repo_path, remote_url, token, **kw):
        calls.append((str(repo_path), remote_url, token, kw))

    monkeypatch.setattr(github, "push_repo", fake_push)
    return calls


async def go(repo=None):
    """Run the stub graph; the stub Builder is replaced so the run's repo_path is the given repo."""

    async def builder(state):
        update = await stubs.builder(state)
        return {**update, "repo_path": str(repo)} if repo else update

    return await run_graph(dict(FAST), heads=Heads(builder=builder))


def progress(run) -> list[str]:
    return [e.message for e in run.events if e.type == "progress" and "GitHub" in e.message]


async def test_pushes_head_to_origin_when_origin_and_token_exist(tmp_path, monkeypatch, pushes):
    monkeypatch.setenv("GITHUB_ORG_TOKEN", TOKEN)
    repo = make_repo(tmp_path)
    run = await go(repo)
    assert pushes == [(str(repo), ORIGIN, TOKEN, {})]
    assert run.nodes[-1] == "Done" and progress(run) == []


async def test_falls_back_to_the_github_token_variable(tmp_path, monkeypatch, pushes):
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    repo = make_repo(tmp_path)
    await go(repo)
    assert [c[2] for c in pushes] == [TOKEN]


@pytest.mark.parametrize("case", ["no_token", "no_origin", "other_host", "no_repo_path"])
async def test_skipped_silently_when_it_cannot_publish(tmp_path, monkeypatch, pushes, case):
    if case != "no_token":
        monkeypatch.setenv("GITHUB_ORG_TOKEN", TOKEN)
    origin = {"no_origin": None, "other_host": "https://gitlab.com/acme/x.git"}.get(case, ORIGIN)
    run = await go(None if case == "no_repo_path" else make_repo(tmp_path, origin))
    assert pushes == [] and progress(run) == []
    assert run.nodes[-1] == "Done"


async def test_failed_push_emits_one_scrubbed_event_and_the_run_still_ends(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_ORG_TOKEN", TOKEN)

    async def failing_push(repo_path, remote_url, token, **kw):
        raise github.GitHubError(f"git push failed while publishing the repo: bad credentials {TOKEN}")

    monkeypatch.setattr(github, "push_repo", failing_push)
    run = await go(make_repo(tmp_path))
    msgs = progress(run)
    assert len(msgs) == 1 and msgs[0].startswith("Couldn't publish the latest version to GitHub: ")
    assert TOKEN not in msgs[0] and "bad credentials" in msgs[0]
    assert run.nodes[-1] == "Done" and run.events[-1].type == "finished"
