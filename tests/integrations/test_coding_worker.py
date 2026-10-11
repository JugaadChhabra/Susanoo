"""Unit tests for integrations/coding_worker.py. The agent is faked; git is real (temp repos)."""

import asyncio
import subprocess

import pytest

from susanoo.integrations import coding_worker as cw


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


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


# --- hardening: env, tools, path scoping, scrubbed errors -----------------------------------------------


def _options_seen(monkeypatch, repo, *, result=None):
    """Run the real `_run_agent` with `query` faked; returns the ClaudeAgentOptions it built."""
    from claude_agent_sdk import ResultMessage

    seen = {}

    async def fake_query(*, prompt, options):
        seen["options"] = options
        yield result or ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="s",
            total_cost_usd=0.5,
            result="ok",
        )

    monkeypatch.setattr(cw, "query", fake_query)
    cost = asyncio.run(cw._run_agent(repo, "p", "m", 3, 1.0))
    return seen["options"], cost


def test_agent_gets_only_file_tools_a_scrubbed_env_and_path_guards(repo, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-" + "ant-keep-for-auth-0123456789")
    monkeypatch.setenv("GITHUB_ORG_TOKEN", "ghp" + "_hostgithubtoken0123456789abcdef")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "service-role-value-123456")
    monkeypatch.setenv("DATABASE_URL", "postgres://u:pw@db/x")
    monkeypatch.setenv("GITHUB_ORG", "acme")
    options, cost = _options_seen(monkeypatch, repo)
    assert cost == 0.5
    assert options.tools == cw.AGENT_TOOLS == options.allowed_tools
    for tool in ("Bash", "WebFetch", "WebSearch", "Task"):
        assert tool in options.disallowed_tools
    assert options.env["GITHUB_ORG_TOKEN"] == "" and options.env["SUPABASE_SERVICE_KEY"] == ""
    assert options.env["DATABASE_URL"] == ""
    assert "ANTHROPIC_API_KEY" not in options.env  # the SDK needs it to authenticate
    assert "GITHUB_ORG" not in options.env  # not a secret
    assert options.can_use_tool is not None
    assert options.hooks["PreToolUse"][0].hooks


def test_agent_env_keeps_only_what_auth_needs():
    env = {
        "PATH": "/bin",
        "ANTHROPIC_API_KEY": "sk-ant-aaaaaaaaaaaa",
        "ANTHROPIC_AUTH_TOKEN": "tok-aaaaaaaaaaaaaa",
        "AWS_SECRET_ACCESS_KEY": "aws-secret-aaaaaaaa",
        "NEATLOGS_API_KEY": "nl-aaaaaaaaaaaa",
        "ANTHROPIC_MODEL": "claude-sonnet-5-5",
    }
    assert cw.agent_env(env) == {"AWS_SECRET_ACCESS_KEY": "", "NEATLOGS_API_KEY": ""}
    assert set(cw.credential_like_vars(env)) == {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "NEATLOGS_API_KEY",
    }


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("Read", {"file_path": "backend/app.py"}),
        ("Edit", {"file_path": "web/index.html"}),
        ("Write", {"file_path": "backend/new/file.py"}),
        ("Glob", {"pattern": "**/*.py", "path": "backend"}),
        ("Grep", {"pattern": "x", "path": "backend", "glob": "*.py"}),
    ],
)
def test_tool_calls_inside_the_repo_are_allowed(repo, tool, tool_input):
    assert cw.check_tool_use(repo, tool, tool_input) is None


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("Read", {"file_path": "/etc/passwd"}),
        ("Read", {"file_path": "../outside.txt"}),
        ("Write", {"file_path": "backend/../../outside.txt"}),
        ("Read", {"file_path": "~/.ssh/id_rsa"}),
        ("Edit", {"file_path": "scenarios/heldout/case.json"}),
        ("Read", {"file_path": "scenarios/heldout/../heldout/case.json"}),
        ("Glob", {"pattern": "/etc/*"}),
        ("Glob", {"pattern": "../*"}),
        ("Grep", {"pattern": "x", "path": "/"}),
        ("Grep", {"pattern": "x", "glob": "../**"}),
        ("Bash", {"command": "ls"}),
        ("WebFetch", {"url": "https://example.com"}),
    ],
)
def test_tool_calls_outside_the_repo_or_on_heldout_are_denied(repo, tool, tool_input):
    assert cw.check_tool_use(repo, tool, tool_input)


def test_symlink_escape_is_denied(repo, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret.txt").write_text("x")
    (repo / "backend" / "link").symlink_to(outside)
    assert cw.check_tool_use(repo, "Read", {"file_path": "backend/link/secret.txt"})


def test_searching_a_parent_of_nonempty_heldout_is_denied(repo):
    (repo / "scenarios" / "heldout").mkdir(parents=True)
    (repo / "scenarios" / "heldout" / ".gitkeep").write_text("")
    assert cw.check_tool_use(repo, "Grep", {"pattern": "x"}) is None  # only a .gitkeep in there
    (repo / "scenarios" / "heldout" / "case.json").write_text("{}")
    assert cw.check_tool_use(repo, "Grep", {"pattern": "x"})
    assert cw.check_tool_use(repo, "Glob", {"pattern": "**/*.json", "path": "scenarios"})
    assert cw.check_tool_use(repo, "Grep", {"pattern": "x", "path": "backend"}) is None


@pytest.mark.parametrize("folder", ["acceptance", "Acceptance", "ACCEPTANCE"])
@pytest.mark.parametrize("tool", ["Read", "Write", "Edit", "MultiEdit", "NotebookEdit"])
def test_the_acceptance_folder_is_off_limits_to_every_tool(repo, tool, folder):
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    for rel in (f"scenarios/{folder}/ac-1.json", f"scenarios/{folder}/../{folder}/ac-1.json", f"scenarios/{folder}"):
        assert "approved scenarios are off limits" in cw.check_tool_use(repo, tool, {key: rel})
        assert cw.check_tool_use(repo, tool, {key: rel}, ["scenarios/"])


def test_searching_inside_acceptance_is_denied_even_with_an_empty_looking_path(repo):
    (repo / "scenarios" / "acceptance").mkdir(parents=True)
    (repo / "scenarios" / "acceptance" / "ac-1.json").write_text("{}")
    assert cw.check_tool_use(repo, "Grep", {"pattern": "x", "path": "scenarios/acceptance"})
    assert cw.check_tool_use(repo, "Glob", {"pattern": "*.json", "path": "scenarios/Acceptance"})


def test_searching_a_parent_of_nonempty_acceptance_is_denied(repo):
    (repo / "scenarios" / "acceptance").mkdir(parents=True)
    (repo / "scenarios" / "acceptance" / ".gitkeep").write_text("")
    assert cw.check_tool_use(repo, "Grep", {"pattern": "x"}) is None  # only a .gitkeep in there
    (repo / "scenarios" / "acceptance" / "ac-1.json").write_text("{}")
    assert "approved scenarios" in cw.check_tool_use(repo, "Grep", {"pattern": "x"})
    assert cw.check_tool_use(repo, "Glob", {"pattern": "**/*.json", "path": "scenarios"})
    assert cw.check_tool_use(repo, "Grep", {"pattern": "x", "path": "backend"}) is None
    (repo / "scenarios" / "train").mkdir()
    assert cw.check_tool_use(repo, "Grep", {"pattern": "x", "path": "scenarios/train"}) is None


def test_a_symlink_into_acceptance_is_denied(repo):
    (repo / "scenarios" / "acceptance").mkdir(parents=True)
    (repo / "scenarios" / "acceptance" / "ac-1.json").write_text("{}")
    (repo / "backend" / "peek").symlink_to(repo / "scenarios" / "acceptance", target_is_directory=True)
    assert cw.check_tool_use(repo, "Read", {"file_path": "backend/peek/ac-1.json"})


def test_the_hook_and_can_use_tool_callbacks_deny_the_same_calls(repo):
    can_use_tool, hooks = cw._tool_guards(repo)
    hook = hooks["PreToolUse"][0].hooks[0]

    async def go():
        bad = {"tool_name": "Read", "tool_input": {"file_path": "/etc/passwd"}}
        good = {"tool_name": "Read", "tool_input": {"file_path": "backend/app.py"}}
        denied = await hook(bad, "id", None)
        assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert await hook(good, "id", None) == {}
        assert (await can_use_tool("Read", bad["tool_input"], None)).behavior == "deny"
        assert (await can_use_tool("Read", good["tool_input"], None)).behavior == "allow"

    asyncio.run(go())


def test_agent_result_text_is_scrubbed_before_it_is_raised(repo, monkeypatch):
    from claude_agent_sdk import ResultMessage

    secret = "sk-" + "ant-api03-LEAKEDLEAKEDLEAKED1234"
    bad = ResultMessage(
        subtype="error_during_execution",
        duration_ms=1,
        duration_api_ms=1,
        is_error=True,
        num_turns=1,
        session_id="s",
        result=f"failed with key {secret}",
    )
    with pytest.raises(cw.CodingWorkerError) as err:
        _options_seen(monkeypatch, repo, result=bad)
    assert secret not in str(err.value) and "[REDACTED]" in str(err.value)


# --- writes confined during the run, .git off limits, case-insensitive deny lists ---------------------------

ALLOWED = ["backend/", "web/"]


@pytest.mark.parametrize("tool", ["Write", "Edit"])
@pytest.mark.parametrize("rel", ["backend/x.py", "backend/new/dir/x.py", "web/index.html"])
def test_writes_inside_allowed_paths_are_allowed(repo, tool, rel):
    assert cw.check_tool_use(repo, tool, {"file_path": rel}, ALLOWED) is None


@pytest.mark.parametrize("tool", ["Write", "Edit", "NotebookEdit"])
@pytest.mark.parametrize(
    "rel",
    [
        "agent/x.py",
        "tests/x.py",
        "Dockerfile",
        ".git/config",
        ".git/hooks/pre-commit",
        ".claude/settings.json",
        "../x",
        "backend/../agent/x.py",
        "scenarios/heldout/case.json",
        "/etc/hosts",
    ],
)
def test_writes_outside_allowed_paths_are_denied(repo, tool, rel):
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    assert cw.check_tool_use(repo, tool, {key: rel}, ALLOWED)


def test_a_write_with_no_path_is_denied(repo):
    assert cw.check_tool_use(repo, "Write", {"content": "x"}, ALLOWED)


def test_reads_stay_allowed_except_git_internals_and_heldout(repo):
    assert cw.check_tool_use(repo, "Read", {"file_path": "agent/prompt.md"}, ALLOWED) is None
    assert cw.check_tool_use(repo, "Read", {"file_path": ".claude/settings.json"}, ALLOWED) is None
    assert cw.check_tool_use(repo, "Read", {"file_path": ".git/config"}, ALLOWED)
    assert cw.check_tool_use(repo, "Grep", {"pattern": "x", "path": ".git"}, ALLOWED)


@pytest.mark.parametrize("rel", ["Scenarios/HeldOut/case.json", "SCENARIOS/heldout", "scenarios/HELDOUT/a/b.json"])
def test_case_variants_of_heldout_are_denied_for_reads(repo, rel):
    assert cw.check_tool_use(repo, "Read", {"file_path": rel}, ALLOWED)


@pytest.mark.parametrize("rel", [".GIT/config", ".Git/hooks/pre-commit"])
def test_case_variants_of_dot_git_are_denied(repo, rel):
    assert cw.check_tool_use(repo, "Read", {"file_path": rel}, ALLOWED)
    assert cw.check_tool_use(repo, "Write", {"file_path": rel}, ALLOWED)


def test_a_symlink_inside_backend_pointing_outside_is_denied_for_writes(repo, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (repo / "backend" / "out").symlink_to(outside, target_is_directory=True)
    (repo / "backend" / "cfg").symlink_to(repo / ".git" / "config")
    (repo / "backend" / "other").symlink_to(repo / "agent", target_is_directory=True)
    for rel in ("backend/out/x.py", "backend/cfg", "backend/other/x.py"):
        assert cw.check_tool_use(repo, "Write", {"file_path": rel}, ALLOWED), rel


def test_a_symlinked_allowed_folder_cannot_redirect_writes(repo, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside2")
    (repo / "web").symlink_to(outside, target_is_directory=True)
    assert cw.check_tool_use(repo, "Write", {"file_path": "web/x.html"}, ALLOWED)


def test_the_hook_passes_allowed_paths_through(repo):
    can_use_tool, hooks = cw._tool_guards(repo, ALLOWED)
    hook = hooks["PreToolUse"][0].hooks[0]

    async def go():
        bad = {"tool_name": "Write", "tool_input": {"file_path": ".git/config"}}
        assert (await hook(bad, "id", None))["hookSpecificOutput"]["permissionDecision"] == "deny"
        good = {"tool_name": "Write", "tool_input": {"file_path": "backend/new.py"}}
        assert await hook(good, "id", None) == {}
        assert (await can_use_tool("Write", {"file_path": "agent/x.py"}, None)).behavior == "deny"

    asyncio.run(go())


def test_run_agent_hands_the_allowed_paths_to_the_guards(repo, monkeypatch):
    seen = {}
    real = cw._tool_guards

    def spy(r, allowed=None):
        seen["allowed"] = allowed
        return real(r, allowed)

    monkeypatch.setattr(cw, "_tool_guards", spy)
    token = cw._ALLOWED.set(["backend", "web"])
    try:
        _options_seen(monkeypatch, repo)
    finally:
        cw._ALLOWED.reset(token)
    assert seen["allowed"] == ["backend", "web"]


# --- .git/config and hooks cannot run code on the host --------------------------------------------------------


def tamper_config(proof):
    def edit(repo_path):
        cfg = repo_path / ".git" / "config"
        cfg.write_text(cfg.read_text() + f'[core]\n\tfsmonitor = "printenv GITHUB_TOKEN > {proof}; false"\n')
        (repo_path / "backend" / "app.py").write_text("x = 2\n")

    return edit


def tamper_hook(proof, name="pre-commit"):
    def edit(repo_path):
        hook = repo_path / ".git" / "hooks" / name
        hook.write_text(f"#!/bin/sh\nprintenv GITHUB_TOKEN > {proof}\n")
        hook.chmod(0o755)
        (repo_path / "backend" / "app.py").write_text("x = 2\n")

    return edit


@pytest.mark.parametrize("tamper", [tamper_config, tamper_hook])
def test_tampering_with_git_config_or_hooks_fails_closed(repo, monkeypatch, tmp_path_factory, tamper):
    proof = tmp_path_factory.mktemp("proof") / "proof.txt"
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "Q" * 36)
    before_config = (repo / ".git" / "config").read_bytes()
    before_head = git(repo, "rev-parse", "HEAD")
    fake_agent(monkeypatch, tamper(proof))
    with pytest.raises(cw.GitStateTampered, match="git's own settings"):
        apply(repo)
    assert not proof.exists()  # git never ran the planted command
    assert (repo / ".git" / "config").read_bytes() == before_config
    assert not (repo / ".git" / "hooks" / "pre-commit").exists()
    assert git(repo, "rev-parse", "HEAD") == before_head
    assert git(repo, "status", "--porcelain") == ""
    assert (repo / "backend" / "app.py").read_text() == "x = 1\n"


def test_tampering_by_an_agent_that_then_fails_is_still_cleaned_up(repo, monkeypatch, tmp_path_factory):
    proof = tmp_path_factory.mktemp("proof") / "proof.txt"
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "Q" * 36)

    async def failing(repo_path, *a):
        tamper_config(proof)(repo_path)
        raise cw.CodingWorkerError("The coding agent failed (error_max_turns)")

    monkeypatch.setattr(cw, "_run_agent", failing)
    with pytest.raises(cw.CodingWorkerError, match="failed"):
        apply(repo)
    assert not proof.exists()
    assert "fsmonitor" not in (repo / ".git" / "config").read_text()
    assert git(repo, "status", "--porcelain") == ""


def test_a_modified_existing_hook_is_restored_exactly(repo, monkeypatch):
    hook = repo / ".git" / "hooks" / "post-commit"
    hook.write_text("#!/bin/sh\nexit 0\n")
    hook.chmod(0o755)
    fake_agent(
        monkeypatch,
        lambda r: (hook.write_text("#!/bin/sh\necho pwned\n"), (r / "backend" / "app.py").write_text("y\n")),
    )
    with pytest.raises(cw.GitStateTampered):
        apply(repo)
    assert hook.read_text() == "#!/bin/sh\nexit 0\n" and hook.stat().st_mode & 0o777 == 0o755


def test_git_commands_after_the_run_disable_fsmonitor_and_pin_the_hooks_dir(repo, monkeypatch):
    calls = []
    real = asyncio.create_subprocess_exec

    async def spy(*args, **kw):
        calls.append(args)
        return await real(*args, **kw)

    fake_agent(monkeypatch, lambda r: (r / "backend" / "app.py").write_text("x = 2\n"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    apply(repo)
    hooks = str(repo.resolve() / ".git" / "hooks")
    assert len(calls) > 3  # the first call is the clean-tree check, before the agent ran
    for args in calls[1:]:
        assert args[:5] == ("git", "-c", "core.fsmonitor=false", "-c", f"core.hooksPath={hooks}"), args


def test_untampered_runs_still_commit_and_hooks_still_run(repo, monkeypatch, tmp_path_factory):
    marker = tmp_path_factory.mktemp("marker") / "hook-ran"
    hook = repo / ".git" / "hooks" / "post-commit"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")  # trusted hook installed BEFORE the run, like Entire's
    hook.chmod(0o755)
    fake_agent(monkeypatch, lambda r: (r / "backend" / "app.py").write_text("x = 2\n"))
    result = apply(repo)
    assert result.commit_sha == git(repo, "rev-parse", "HEAD").strip()
    assert marker.exists()


def test_more_credential_shapes_are_blanked_and_scanned():
    env = {
        "DB_PW": "pw-value-0123456789",
        "APP_PWD": "pwd-value-0123456789",
        "ADMIN_PASS": "pass-value-0123456789",
        "REDIS_URI": "redis://u:p@h:6379",
        "ANALYTICS_DB_URL": "postgres://u:p@h/db",
        "POSTGRES_PASSWORD": "pgpw-value-0123456789",
        "POSTGRES_HOST": "db.internal.example.com",
        "PWD": "/home/me",
        "OLDPWD": "/home",
        "HOME": "/home/me",
    }
    blanked = cw.agent_env(env)
    assert set(blanked) == {
        "DB_PW",
        "APP_PWD",
        "ADMIN_PASS",
        "REDIS_URI",
        "ANALYTICS_DB_URL",
        "POSTGRES_PASSWORD",
        "POSTGRES_HOST",
    }
    assert set(cw.credential_like_vars(env)) == set(blanked)
