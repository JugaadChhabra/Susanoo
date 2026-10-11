"""Unit tests for integrations/coding_worker.py. The agent is faked; git is real (temp repos)."""

import asyncio
import subprocess

import pytest

from susanoo.integrations import coding_worker as cw


def git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.name", "test")
    git(tmp_path, "config", "user.email", "test@example.com")
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "app.py").write_text("x = 1\n")
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "prompt.md").write_text("hello\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def fake_agent(monkeypatch, edit, cost=0.25):
    """Replace the SDK call with `edit(repo)`; records the prompt it was given."""
    seen = {}

    async def _run(repo_path, prompt, model, max_turns, max_budget_usd):
        seen.update(prompt=prompt, model=model, max_turns=max_turns, max_budget_usd=max_budget_usd)
        edit(repo_path)
        return cost

    monkeypatch.setattr(cw, "_run_agent", _run)
    return seen


def apply(repo, allowed=("backend/",), **kw):
    return asyncio.run(cw.run_change(repo, "Set x to 2", list(allowed), **kw))


def test_commits_the_change_and_returns_sha_and_cost(repo, monkeypatch):
    fake_agent(monkeypatch, lambda r: (r / "backend" / "app.py").write_text("x = 2\n"))
    result = apply(repo)
    assert result.commit_sha == git(repo, "rev-parse", "HEAD").strip()
    assert result.cost_usd == 0.25
    assert git(repo, "show", "HEAD:backend/app.py") == "x = 2\n"
    assert git(repo, "status", "--porcelain") == ""
    assert git(repo, "log", "-1", "--format=%s").strip() == "agent: Set x to 2"


def test_apply_change_returns_just_the_sha(repo, monkeypatch):
    fake_agent(monkeypatch, lambda r: (r / "backend" / "app.py").write_text("x = 2\n"))
    sha = asyncio.run(cw.apply_change(repo, "Set x to 2", ["backend"]))
    assert sha == git(repo, "rev-parse", "HEAD").strip()


def test_new_files_inside_allowed_paths_are_committed(repo, monkeypatch):
    fake_agent(monkeypatch, lambda r: (r / "backend" / "new.py").write_text("y = 1\n"))
    apply(repo)
    assert "backend/new.py" in git(repo, "show", "--name-only", "--format=", "HEAD")


def test_allowed_path_can_be_a_single_file(repo, monkeypatch):
    fake_agent(monkeypatch, lambda r: (r / "agent" / "prompt.md").write_text("bye\n"))
    apply(repo, allowed=["agent/prompt.md"])


def test_prompt_lists_allowed_paths_and_options_are_forwarded(repo, monkeypatch):
    seen = fake_agent(monkeypatch, lambda r: (r / "backend" / "app.py").write_text("x = 2\n"))
    apply(repo, allowed=["backend/", "agent/prompt.md"], model="m", max_turns=3, max_budget_usd=0.1)
    assert "- backend" in seen["prompt"]
    assert "- agent/prompt.md" in seen["prompt"]
    assert seen["prompt"].startswith("Set x to 2")
    assert (seen["model"], seen["max_turns"], seen["max_budget_usd"]) == ("m", 3, 0.1)


def test_custom_commit_message(repo, monkeypatch):
    fake_agent(monkeypatch, lambda r: (r / "backend" / "app.py").write_text("x = 2\n"))
    apply(repo, commit_message="fix(heads): bump x")
    assert git(repo, "log", "-1", "--format=%s").strip() == "fix(heads): bump x"


def test_entire_checkpoint_trailer_is_returned(repo, monkeypatch):
    hook = repo / ".git" / "hooks" / "commit-msg"
    hook.write_text('#!/bin/sh\nprintf "\\nEntire-Checkpoint: 01ABC\\n" >> "$1"\n')
    hook.chmod(0o755)
    fake_agent(monkeypatch, lambda r: (r / "backend" / "app.py").write_text("x = 2\n"))
    assert apply(repo).checkpoint_id == "01ABC"


def test_no_checkpoint_when_entire_is_not_enabled(repo, monkeypatch):
    fake_agent(monkeypatch, lambda r: (r / "backend" / "app.py").write_text("x = 2\n"))
    assert apply(repo).checkpoint_id is None


def test_change_outside_allowed_paths_is_reverted(repo, monkeypatch):
    def edit(r):
        (r / "backend" / "app.py").write_text("x = 2\n")  # allowed
        (r / "agent" / "prompt.md").write_text("changed\n")  # not allowed
        (r / "stray.txt").write_text("new\n")  # not allowed, untracked

    fake_agent(monkeypatch, edit)
    before = git(repo, "rev-parse", "HEAD")
    with pytest.raises(cw.OutsideAllowedPaths, match="agent/prompt.md"):
        apply(repo)
    assert git(repo, "rev-parse", "HEAD") == before
    assert git(repo, "status", "--porcelain") == ""
    assert (repo / "backend" / "app.py").read_text() == "x = 1\n"
    assert not (repo / "stray.txt").exists()


def test_deleting_a_file_outside_allowed_paths_is_blocked(repo, monkeypatch):
    fake_agent(monkeypatch, lambda r: (r / "agent" / "prompt.md").unlink())
    with pytest.raises(cw.OutsideAllowedPaths):
        apply(repo)
    assert (repo / "agent" / "prompt.md").exists()


def test_folder_prefix_does_not_match_sibling_names(repo, monkeypatch):
    (repo / "backend2").mkdir()
    fake_agent(monkeypatch, lambda r: (r / "backend2" / "x.py").write_text("z\n"))
    with pytest.raises(cw.OutsideAllowedPaths):
        apply(repo, allowed=["backend"])


def test_no_change_raises_and_makes_no_commit(repo, monkeypatch):
    fake_agent(monkeypatch, lambda r: None)
    before = git(repo, "rev-parse", "HEAD")
    with pytest.raises(cw.NoChange):
        apply(repo)
    assert git(repo, "rev-parse", "HEAD") == before


def test_dirty_repo_is_rejected_before_the_agent_runs(repo, monkeypatch):
    seen = fake_agent(monkeypatch, lambda r: None)
    (repo / "backend" / "app.py").write_text("dirty\n")
    with pytest.raises(cw.CodingWorkerError, match="uncommitted"):
        apply(repo)
    assert seen == {}


@pytest.mark.parametrize("bad", [[], ["/etc"], ["../x"], ["backend/../../x"], ["."], [""]])
def test_bad_allowed_paths_are_rejected(repo, monkeypatch, bad):
    seen = fake_agent(monkeypatch, lambda r: None)
    with pytest.raises(ValueError):
        apply(repo, allowed=bad)
    assert seen == {}


def test_agent_failure_propagates_and_leaves_a_clean_repo(repo, monkeypatch):
    async def _fail(repo_path, *a):
        (repo_path / "backend" / "app.py").write_text("half done\n")
        (repo_path / "backend" / "partial.py").write_text("...\n")
        raise cw.CodingWorkerError("The coding agent failed (error_max_turns): ...")

    monkeypatch.setattr(cw, "_run_agent", _fail)
    before = git(repo, "rev-parse", "HEAD")
    with pytest.raises(cw.CodingWorkerError, match="failed"):
        apply(repo)
    assert git(repo, "rev-parse", "HEAD") == before
    assert git(repo, "status", "--porcelain") == ""
