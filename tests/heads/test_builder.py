"""Unit tests for heads/builder.py. The coding agent and GitHub are fakes; git is real (temp repos)."""

import asyncio
import base64
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

from susanoo.emit import InMemorySink, use_sink
from susanoo.guards import budget
from susanoo.heads import builder
from susanoo.integrations import coding_worker as cw
from susanoo.integrations import github as gh
from susanoo.integrations import llm
from susanoo.schemas import Plan, Rule

TOKEN = "ghp" + "_BUILDERSECRETTOKEN0123456789abcdef"


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for var in ("GITHUB_ORG", "GITHUB_ORG_TOKEN", "GITHUB_TOKEN", "RUN_COST_CAP_USD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SUSANOO_WORKDIR", str(tmp_path / "work"))
    spans = []

    @contextmanager
    def fake_span(name, **attrs):
        spans.append((name, attrs))
        yield

    monkeypatch.setattr(builder, "_span", fake_span)
    monkeypatch.setattr(builder, "_which", lambda name: None)  # never run the real `entire` unless a test asks
    return spans


def make_plan(**kw):
    base = dict(
        product_name="Python Code Review",
        summary="Reviews pull-request diffs against your team's rules.",
        rules=[Rule(id="no-raw-sql", text="No raw SQL built from strings.", default_severity="block")],
        intake_answers={"q1": "block"},
        files_to_generate=["backend/main.py", "web/index.html"],
        acceptance_criteria=["Flags raw SQL"],
    )
    base.update(kw)
    return Plan(**base)


def make_state(**kw):
    state = {"run_id": "run3fa9c1d2", "spec": "Build a code reviewer", "plan": make_plan(), "cost_usd": 0.1}
    state.update(kw)
    return state


def run_head(state, **kw):
    sink = InMemorySink()
    with use_sink(sink):
        try:
            result = asyncio.run(builder.run(state, **kw))
        except BaseException as exc:
            return sink, exc
    return sink, result


def fake_agent(monkeypatch, edit, cost=0.25):
    """Replace the SDK call inside the real coding worker with `edit(repo)`."""
    seen = {}

    async def _run(repo_path, prompt, model, max_turns, max_budget_usd):
        seen.update(prompt=prompt, model=model, max_budget_usd=max_budget_usd, calls=seen.get("calls", 0) + 1)
        edit(Path(repo_path))
        return cost

    monkeypatch.setattr(cw, "_run_agent", _run)
    return seen


def edit_backend(repo):
    (repo / "backend" / "main.py").write_text("# built for the plan\n")


class FakeGitHub:
    """Stands in for GitHubClient; really pushes to a local bare repo with the real push helper."""

    def __init__(self, bare):
        self.bare = bare
        self.calls = []

    async def create_and_push(self, org, name, repo_path, *, description=""):
        self.calls.append((org, name, Path(repo_path), description))
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.bare)], check=True)
        await gh.push_repo(repo_path, str(self.bare), TOKEN)
        return gh.RepoInfo(f"{org}/{name}", f"https://github.com/{org}/{name}.git", f"https://github.com/{org}/{name}")


# --- copy and commit ---------------------------------------------------------------------------------


def test_copies_the_template_and_records_repo_and_commit(monkeypatch):
    fake_agent(monkeypatch, edit_backend)
    sink, out = run_head(make_state())
    repo = Path(out["repo_path"])
    assert repo == Path(repo).resolve() and repo.name == "repo" and repo.parent.name == "run3fa9c1d2"
    for rel in (
        "backend/main.py",
        "web/index.html",
        "agent/prompt.md",
        "agent/policy.yaml",
        "Dockerfile",
        "tests/test_api.py",
    ):
        assert (repo / rel).exists(), rel
    assert not list(repo.rglob("__pycache__")) and not list(repo.rglob(".pytest_cache"))
    assert out["head_commit"] == git(repo, "rev-parse", "HEAD").strip()
    assert git(repo, "status", "--porcelain") == ""
    assert git(repo, "log", "--format=%s").splitlines() == [
        "feat: build Python Code Review from the plan",
        "chore: start from the app template",
    ]
    assert (repo / "backend" / "main.py").read_text() == "# built for the plan\n"
    assert out["cost_usd"] == pytest.approx(0.35)
    assert set(out) == {"repo_path", "head_commit", "cost_usd"}  # only the changed keys


def test_heldout_content_is_never_copied(tmp_path, monkeypatch):
    template = tmp_path / "tpl"
    (template / "backend").mkdir(parents=True)
    (template / "backend" / "main.py").write_text("x = 1\n")
    (template / "web").mkdir()
    (template / "web" / "index.html").write_text("<p>hi</p>\n")
    (template / "scenarios" / "heldout").mkdir(parents=True)
    (template / "scenarios" / "heldout" / "secret-case.json").write_text('{"answer": "leak"}')
    (template / ".git").mkdir()
    (template / ".git" / "HEAD").write_text("ref: nope\n")
    fake_agent(monkeypatch, edit_backend)
    _, out = run_head(make_state(), template_dir=template)
    repo = Path(out["repo_path"])
    assert sorted(p.name for p in (repo / "scenarios" / "heldout").iterdir()) == [".gitkeep"]
    assert "secret-case" not in git(repo, "ls-files")
    assert "leak" not in "".join(
        p.read_text(errors="ignore") for p in repo.rglob("*") if p.is_file() and ".git/" not in str(p)
    )
    assert git(repo, "log", "--oneline").count("\n") == 2  # the template's own .git was not copied


def test_a_retried_run_starts_from_a_clean_copy(monkeypatch):
    fake_agent(monkeypatch, lambda r: (r / "backend" / "extra.py").write_text("y = 2\n"))
    first = run_head(make_state())[1]["repo_path"]
    second = run_head(make_state())[1]["repo_path"]
    assert first == second and git(second, "log", "--oneline").count("\n") == 2


@pytest.mark.parametrize("bad", ["../escape", "a/b", "", ".hidden", "x" * 200])
def test_unsafe_run_ids_are_refused(bad):
    sink, err = run_head(make_state(run_id=bad))
    assert isinstance(err, ValueError)
    assert sink.events[-1].type == "error"


# --- coding worker contract ----------------------------------------------------------------------------


def test_worker_gets_backend_and_web_only_the_sonnet_model_and_a_plan_instruction():
    calls = []

    async def worker(repo, instruction, allowed, **kw):
        calls.append((repo, instruction, allowed, kw))
        (Path(repo) / "web" / "index.html").write_text("<p>built</p>\n")
        return cw.ChangeResult("abc123", 0.4, "cp-1")

    _, out = run_head(make_state(), worker=worker)
    ((repo, instruction, allowed, kw),) = calls
    assert allowed == ["backend/", "web/"]
    assert kw["model"] == llm.HEAD_MODELS["builder"] == "claude-sonnet-5-5"
    assert "Python Code Review" in instruction and "no-raw-sql" in instruction
    assert "- backend/main.py" in instruction and "- web/index.html" in instruction
    assert "{{" not in instruction and "<!--" not in instruction
    assert out["head_commit"] == "abc123"


def test_real_worker_receives_the_allowed_folders_in_the_agent_prompt(monkeypatch):
    seen = fake_agent(monkeypatch, edit_backend)
    run_head(make_state())
    assert "- backend\n- web" in seen["prompt"]
    assert seen["model"] == "claude-sonnet-5-5"


def test_plan_text_cannot_inject_template_placeholders(monkeypatch):
    plan = make_plan(product_name="Evil {{plan}}", summary="{{files}}")
    seen = fake_agent(monkeypatch, edit_backend)
    run_head(make_state(plan=plan))
    assert seen["prompt"].count("<plan>") == 1


def test_worker_budget_is_capped_by_what_the_run_has_left(monkeypatch):
    seen = fake_agent(monkeypatch, edit_backend)
    monkeypatch.setenv("RUN_COST_CAP_USD", "2.0")
    run_head(make_state(cost_usd=1.7))
    assert seen["max_budget_usd"] == pytest.approx(0.3)
    run_head(make_state(cost_usd=0.0))
    assert seen["max_budget_usd"] == 1.0


def test_outside_allowed_paths_is_an_error_event_and_is_reraised(monkeypatch):
    def sneaky(repo):
        (repo / "backend" / "main.py").write_text("# fine\n")
        (repo / "agent" / "prompt.md").write_text("# changed the agent\n")

    fake_agent(monkeypatch, sneaky)
    sink, err = run_head(make_state())
    assert isinstance(err, cw.OutsideAllowedPaths)
    last = sink.events[-1]
    assert (last.head, last.type) == ("builder", "error")
    assert "not allowed" in last.message and "undid" in last.message
    assert "agent/prompt.md" in last.payload["detail"]
    # the worker reverted the repo, so no half-edits remain
    repo = next(Path(builder.os.environ["SUSANOO_WORKDIR"]).glob("*/repo"))
    assert git(repo, "status", "--porcelain") == ""
    assert (repo / "agent" / "prompt.md").read_text() != "# changed the agent\n"
    assert git(repo, "log", "--oneline").count("\n") == 1


def test_other_worker_failures_become_an_error_event_and_reraise(monkeypatch):
    async def worker(*a, **kw):
        raise cw.CodingWorkerError("the coding agent failed (error_max_turns)")

    sink, err = run_head(make_state(), worker=worker)
    assert isinstance(err, cw.CodingWorkerError)
    assert sink.events[-1].type == "error" and "Building the app failed" in sink.events[-1].message
    assert [e.type for e in sink.events].count("finished") == 0


# --- budget ----------------------------------------------------------------------------------------------


def test_budget_exceeded_stops_before_any_model_call_and_is_reraised(monkeypatch):
    seen = fake_agent(monkeypatch, edit_backend)
    monkeypatch.setenv("RUN_COST_CAP_USD", "1.0")
    sink, err = run_head(make_state(cost_usd=1.0))
    assert isinstance(err, budget.BudgetExceeded)
    assert "calls" not in seen
    assert [e.type for e in sink.events] == ["started"]  # no error event: the graph ends the run cleanly


def test_budget_is_rechecked_right_before_the_worker(monkeypatch):
    seen = fake_agent(monkeypatch, edit_backend)
    checks = []
    real = budget.check

    def check(state):
        checks.append(float(state.get("cost_usd", 0.0)))
        real(state)

    monkeypatch.setattr(builder.budget, "check", check)
    run_head(make_state())
    assert len(checks) == 2 and seen["calls"] == 1


# --- GitHub -----------------------------------------------------------------------------------------------


def test_remote_is_skipped_with_a_plain_message_when_env_is_unset(monkeypatch):
    fake_agent(monkeypatch, edit_backend)
    sink, out = run_head(make_state())
    assert "repo_path" in out  # did not crash
    messages = [e.message for e in sink.events]
    assert any("GitHub is not set up" in m for m in messages)
    assert "kept locally" in messages[-1]
    assert sink.events[-1].payload["remote"] is None


def test_org_without_token_is_also_local_only(monkeypatch):
    fake_agent(monkeypatch, edit_backend)
    monkeypatch.setenv("GITHUB_ORG", "acme")
    sink, out = run_head(make_state())
    assert "repo_path" in out and sink.events[-1].payload["remote"] is None


def test_repo_is_created_in_the_org_and_pushed(tmp_path, monkeypatch):
    fake_agent(monkeypatch, edit_backend)
    monkeypatch.setenv("GITHUB_ORG", "acme")
    monkeypatch.setenv("GITHUB_ORG_TOKEN", TOKEN)
    client = FakeGitHub(tmp_path / "remote.git")
    sink, out = run_head(make_state(), github_client=client)
    ((org, name, repo, description),) = client.calls
    assert (org, name) == ("acme", "python-code-review-run3fa")
    assert repo == Path(out["repo_path"]) and description.startswith("Reviews pull-request diffs")
    assert git(client.bare, "rev-parse", "main").strip() == out["head_commit"]
    finished = sink.events[-1]
    assert finished.type == "finished"
    assert finished.payload["remote"] == "https://github.com/acme/python-code-review-run3fa"
    assert "https://github.com/acme/python-code-review-run3fa" in finished.message


def test_name_conflict_surfaces_as_an_error_event_and_reraises(monkeypatch):
    fake_agent(monkeypatch, edit_backend)
    monkeypatch.setenv("GITHUB_ORG", "acme")

    class Taken:
        async def create_and_push(self, *a, **kw):
            raise gh.RepoAlreadyExists("A repo named acme/x already exists. Pick another product name.")

    sink, err = run_head(make_state(), github_client=Taken())
    assert isinstance(err, gh.RepoAlreadyExists)
    assert sink.events[-1].type == "error" and "already exists" in sink.events[-1].message


def test_no_credentials_end_up_in_the_generated_tree_or_git_config(tmp_path, monkeypatch):
    fake_agent(monkeypatch, edit_backend)
    monkeypatch.setenv("GITHUB_ORG", "acme")
    monkeypatch.setenv("GITHUB_ORG_TOKEN", TOKEN)
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    client = FakeGitHub(tmp_path / "remote.git")
    sink, out = run_head(make_state(), github_client=client)
    repo = Path(out["repo_path"])
    basic = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
    blob = "".join(p.read_bytes().decode("latin-1") for p in repo.rglob("*") if p.is_file())  # includes .git
    assert TOKEN not in blob and basic not in blob
    assert not any(TOKEN in e.message or TOKEN in str(e.payload) for e in sink.events)
    assert not list(repo.rglob(".env*"))


# --- events and tracing ------------------------------------------------------------------------------------


def test_events_start_and_finish_in_plain_english(monkeypatch):
    fake_agent(monkeypatch, edit_backend)
    sink, _ = run_head(make_state())
    assert [(e.head, e.type) for e in sink.events][0] == ("builder", "started")
    assert [(e.head, e.type) for e in sink.events][-1] == ("builder", "finished")
    assert all(e.head == "builder" and e.run_id == "run3fa9c1d2" for e in sink.events)
    for event in sink.events:
        assert not event.message.lstrip().startswith(("{", "["))
        assert "Traceback" not in event.message


def test_head_runs_inside_a_builder_span(clean_env, monkeypatch):
    fake_agent(monkeypatch, edit_backend)
    run_head(make_state())
    assert clean_env == [("builder", {"run_id": "run3fa9c1d2"})]


# --- secrets in generated code -----------------------------------------------------------------------------

LEAKED_KEY = "sk-" + "ant-api03-ABCDEFGHIJKLMNOP1234567890"


def write_settings(content):
    def edit(repo):
        (repo / "backend" / "settings.py").write_text(content)

    return edit


def assert_refused(sink, err, repo_glob_root, secret, *, github_calls=None):
    assert isinstance(err, builder.SecretInGeneratedCode)
    assert err.files == ["backend/settings.py"]
    last = sink.events[-1]
    assert (last.head, last.type) == ("builder", "error")
    assert "backend/settings.py" in last.message and "did not upload" in last.message
    for event in sink.events:
        assert secret not in event.message and secret not in str(event.payload)
    assert secret not in str(err)
    repo = next(Path(repo_glob_root).glob("*/repo"))
    assert not (repo / "backend" / "settings.py").exists()  # reset to the pre-worker commit
    assert git(repo, "status", "--porcelain") == ""
    assert git(repo, "log", "--format=%s").splitlines() == ["chore: start from the app template"]
    if github_calls is not None:
        assert github_calls == []


def test_reviewer_repro_api_key_in_backend_settings_is_never_pushed(tmp_path, monkeypatch):
    fake_agent(monkeypatch, write_settings(f'ANTHROPIC_API_KEY = "{LEAKED_KEY}"\n'))
    monkeypatch.setenv("GITHUB_ORG", "acme")
    monkeypatch.setenv("GITHUB_ORG_TOKEN", TOKEN)
    client = FakeGitHub(tmp_path / "remote.git")
    sink, err = run_head(make_state(), github_client=client)
    assert_refused(sink, err, builder.os.environ["SUSANOO_WORKDIR"], LEAKED_KEY, github_calls=client.calls)
    assert not client.bare.exists()


def test_uncommitted_secret_left_by_a_worker_is_also_caught_and_removed(monkeypatch):
    async def worker(repo, instruction, allowed, **kw):
        (Path(repo) / "backend" / "settings.py").write_text(f"K = '{LEAKED_KEY}'\n")  # never committed
        (Path(repo) / "backend" / "main.py").write_text("# fine\n")
        return cw.ChangeResult("deadbeef", 0.1, None)

    sink, err = run_head(make_state(), worker=worker)
    assert_refused(sink, err, builder.os.environ["SUSANOO_WORKDIR"], LEAKED_KEY)
    repo = next(Path(builder.os.environ["SUSANOO_WORKDIR"]).glob("*/repo"))
    assert (repo / "backend" / "main.py").read_text() != "# fine\n"


def test_base64_of_a_host_credential_is_caught(monkeypatch):
    value = "service-role-value-0123456789"
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", value)
    encoded = base64.b64encode(value.encode()).decode()
    fake_agent(monkeypatch, write_settings(f'BLOB = "{encoded}"\n'))
    sink, err = run_head(make_state())
    assert_refused(sink, err, builder.os.environ["SUSANOO_WORKDIR"], encoded)


def test_unpadded_urlsafe_base64_of_a_host_credential_is_caught(monkeypatch):
    value = "tok?>>>-value-with-odd-bytes~~"
    monkeypatch.setenv("NEATLOGS_API_KEY", value)
    encoded = base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")
    fake_agent(monkeypatch, write_settings(f"BLOB = '{encoded}'\n"))
    sink, err = run_head(make_state())
    assert isinstance(err, builder.SecretInGeneratedCode)


def test_exact_env_value_of_a_host_credential_is_caught(monkeypatch):
    value = "an-arbitrary-looking-but-private-value"
    monkeypatch.setenv("DATABASE_URL", value)
    fake_agent(monkeypatch, write_settings(f"URL = '{value}'\n"))
    sink, err = run_head(make_state())
    assert_refused(sink, err, builder.os.environ["SUSANOO_WORKDIR"], value)


def test_the_github_token_value_is_caught_even_when_not_secret_shaped(monkeypatch):
    value = "plain-words-token-value"
    monkeypatch.setenv("GITHUB_ORG_TOKEN", value)
    fake_agent(monkeypatch, write_settings(f"T = '{value}'\n"))
    sink, err = run_head(make_state())
    assert isinstance(err, builder.SecretInGeneratedCode)


@pytest.mark.parametrize(
    "content",
    [
        "---" + "--BEGIN RSA PRIVATE KEY-----\nMIIB\n-----END RSA PRIVATE KEY-----\n",
        "KEY = 'AKIAABCDEFGHIJKLMNOP'\n",
        "T = 'ghp" + "_abcdefghijklmnopqrstuvwxyz0123'\n",
        "J = 'eyJhbGciOiJI.eyJzdWIiOiIx.SflKxwRJSMeKKF2QT4'\n",
        "DB = 'postgres://app:hunter2hunter2@db.example.com/prod'\n",
    ],
)
def test_secret_shaped_values_are_caught_without_any_env(monkeypatch, content):
    fake_agent(monkeypatch, write_settings(content))
    sink, err = run_head(make_state())
    assert isinstance(err, builder.SecretInGeneratedCode)


def test_a_secret_in_a_commit_message_is_caught():
    state = make_state()
    sink = InMemorySink()

    async def worker(repo, instruction, allowed, **kw):
        edit_backend(Path(repo))
        git(repo, "commit", "-qam", f"use key {LEAKED_KEY}")
        return cw.ChangeResult(git(repo, "rev-parse", "HEAD").strip(), 0.1, None)

    with use_sink(sink):
        with pytest.raises(builder.SecretInGeneratedCode) as err:
            asyncio.run(builder.run(state, worker=worker))
    assert err.value.files == ["(a commit message)"]
    assert LEAKED_KEY not in str(err.value) and LEAKED_KEY not in sink.events[-1].message


def test_ordinary_code_that_reads_secrets_from_the_environment_is_not_flagged(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-" + "ant-host-key-0123456789abcdef")
    code = (
        "import os\n"
        'api_key = os.environ["ANTHROPIC_APP_API_KEY"]\n'
        "password = get_password()\n"
        'headers = {"Authorization": f"Bearer {token}"}\n'
        "# send the diff to the model and return findings\n"
    )
    fake_agent(monkeypatch, write_settings(code))
    sink, out = run_head(make_state())
    assert "head_commit" in out and sink.events[-1].type == "finished"


def test_error_text_from_unexpected_failures_is_scrubbed(monkeypatch):
    async def worker(*a, **kw):
        raise RuntimeError(f"boom with {LEAKED_KEY}")

    sink, err = run_head(make_state(), worker=worker)
    assert isinstance(err, RuntimeError)
    assert LEAKED_KEY not in sink.events[-1].message and "[REDACTED]" in sink.events[-1].message


# --- Entire ---------------------------------------------------------------------------------------------------


class FakeEntire:
    """Command runner that pretends to be `entire enable`: writes its files, records the call."""

    def __init__(self, fail=False, write=True):
        self.calls = []
        self.fail = fail
        self.write = write

    async def __call__(self, args, cwd):
        self.calls.append((list(args), Path(cwd)))
        if self.write:
            (cwd / ".claude").mkdir(exist_ok=True)
            (cwd / ".claude" / "settings.json").write_text("{}\n")
            (cwd / ".entire").mkdir(exist_ok=True)
            (cwd / ".entire" / "settings.json").write_text('{"enabled": true}\n')
        if self.fail:
            raise RuntimeError("entire exited with 1: boom")


def test_entire_is_enabled_and_committed_before_the_worker_runs(monkeypatch):
    monkeypatch.setattr(builder, "_which", lambda name: "/usr/bin/entire")
    runner = FakeEntire()
    seen = {}

    async def worker(repo, instruction, allowed, **kw):
        seen["log"] = git(repo, "log", "--format=%s").splitlines()
        seen["clean"] = git(repo, "status", "--porcelain") == ""
        edit_backend(Path(repo))
        git(repo, "commit", "-qam", "built")
        return cw.ChangeResult(git(repo, "rev-parse", "HEAD").strip(), 0.1, "cp-1")

    sink, out = run_head(make_state(), worker=worker, command_runner=runner)
    ((args, cwd),) = runner.calls
    assert args[:4] == ["entire", "enable", "--agent", "claude-code"]
    assert cwd == Path(out["repo_path"])
    assert seen["log"] == ["chore: enable Entire", "chore: start from the app template"]
    assert seen["clean"]
    repo = Path(out["repo_path"])
    assert "chore: enable Entire" in git(repo, "log", "--format=%s")
    assert git(repo, "ls-files", ".claude", ".entire").split() == [".claude/settings.json", ".entire/settings.json"]
    assert any("Entire is switched on" in e.message for e in sink.events)
    assert sink.events[-1].payload["checkpoint_id"] == "cp-1"


def test_entire_files_are_not_mistaken_for_worker_changes_by_the_secret_scan(monkeypatch):
    monkeypatch.setattr(builder, "_which", lambda name: "/usr/bin/entire")
    fake_agent(monkeypatch, edit_backend)
    sink, out = run_head(make_state(), command_runner=FakeEntire())
    assert sink.events[-1].type == "finished"


def test_missing_entire_binary_is_a_plain_progress_note(monkeypatch):
    runner = FakeEntire()
    fake_agent(monkeypatch, edit_backend)
    sink, out = run_head(make_state(), command_runner=runner)
    assert runner.calls == []
    notes = [e.message for e in sink.events if e.type == "progress"]
    assert "Entire isn't installed here, so fixes won't have checkpoints." in notes
    assert "head_commit" in out


def test_entire_failure_is_survived_and_leaves_a_clean_repo(monkeypatch):
    monkeypatch.setattr(builder, "_which", lambda name: "/usr/bin/entire")
    fake_agent(monkeypatch, edit_backend)
    sink, out = run_head(make_state(), command_runner=FakeEntire(fail=True))
    assert "head_commit" in out
    repo = Path(out["repo_path"])
    assert not (repo / ".entire").exists()
    assert "enable Entire" not in git(repo, "log", "--format=%s")
    assert any("Entire could not be set up" in e.message and "checkpoints" in e.message for e in sink.events)


def test_entire_that_writes_nothing_makes_no_extra_commit(monkeypatch):
    monkeypatch.setattr(builder, "_which", lambda name: "/usr/bin/entire")
    fake_agent(monkeypatch, edit_backend)
    sink, out = run_head(make_state(), command_runner=FakeEntire(write=False))
    assert "enable Entire" not in git(Path(out["repo_path"]), "log", "--format=%s")


@pytest.mark.skipif(shutil.which("entire") is None, reason="the entire CLI is not installed")
def test_real_entire_enable_in_a_temporary_repo(monkeypatch):
    monkeypatch.setattr(builder, "_which", shutil.which)  # real binary, real runner; cwd is the temp workdir only
    fake_agent(monkeypatch, edit_backend)
    sink, out = run_head(make_state())
    assert not isinstance(out, BaseException), out
    repo = Path(out["repo_path"])
    assert repo.is_relative_to(Path(builder.os.environ["SUSANOO_WORKDIR"]).resolve())
    files = git(repo, "ls-files", ".claude", ".entire").split()
    assert ".claude/settings.json" in files and ".entire/settings.json" in files
    assert "chore: enable Entire" in git(repo, "log", "--format=%s")
    assert (repo / ".git" / "hooks" / "post-commit").exists()
    assert git(repo, "status", "--porcelain") == ""


# --- .git config and hooks cannot run code on the host ----------------------------------------------------------


def assert_failed_closed(sink, err, proof, remote):
    assert isinstance(err, cw.GitStateTampered)
    assert not proof.exists()  # the planted command never ran on the host
    last = sink.events[-1]
    assert (last.head, last.type) == ("builder", "error")
    assert "git's own settings" in last.message and "did not upload" in last.message
    assert not remote.exists()  # nothing pushed
    repo = next(Path(builder.os.environ["SUSANOO_WORKDIR"]).glob("*/repo"))
    assert "fsmonitor" not in (repo / ".git" / "config").read_text()
    assert not (repo / ".git" / "hooks" / "pre-commit").exists()
    assert git(repo, "status", "--porcelain") == ""


def test_reviewer_repro_fsmonitor_in_git_config_fails_closed(tmp_path, monkeypatch):
    proof = tmp_path / "proof.txt"
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "Q" * 36)
    monkeypatch.setenv("GITHUB_ORG", "acme")
    monkeypatch.setenv("GITHUB_ORG_TOKEN", TOKEN)

    def edit(repo):
        cfg = repo / ".git" / "config"
        cfg.write_text(cfg.read_text() + f'[core]\n\tfsmonitor = "printenv GITHUB_TOKEN > {proof}; false"\n')
        (repo / "backend" / "main.py").write_text("# ok\n")

    fake_agent(monkeypatch, edit)
    client = FakeGitHub(tmp_path / "remote.git")
    sink, err = run_head(make_state(), github_client=client)
    assert_failed_closed(sink, err, proof, client.bare)
    assert client.calls == []


def test_reviewer_repro_pre_commit_hook_fails_closed(tmp_path, monkeypatch):
    proof = tmp_path / "proof.txt"
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "Q" * 36)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-role-" + "z" * 30)

    def edit(repo):
        hook = repo / ".git" / "hooks" / "pre-commit"
        hook.write_text(f"#!/bin/sh\nprintenv GITHUB_TOKEN > {proof}\nprintenv SUPABASE_SERVICE_ROLE_KEY >> {proof}\n")
        hook.chmod(0o755)
        (repo / "backend" / "main.py").write_text("# ok\n")

    fake_agent(monkeypatch, edit)
    sink, err = run_head(make_state())
    assert_failed_closed(sink, err, proof, tmp_path / "remote.git")


def test_a_directly_injected_worker_that_tampers_is_caught_by_the_builder_too(tmp_path, monkeypatch):
    proof = tmp_path / "proof.txt"
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "Q" * 36)

    async def worker(repo, instruction, allowed, **kw):
        repo = Path(repo)
        cfg = repo / ".git" / "config"
        cfg.write_text(cfg.read_text() + f'[core]\n\tfsmonitor = "printenv GITHUB_TOKEN > {proof}; false"\n')
        edit_backend(repo)
        return cw.ChangeResult("deadbeef", 0.1, None)  # pretends it committed

    sink, err = run_head(make_state(), worker=worker)
    assert_failed_closed(sink, err, proof, tmp_path / "remote.git")


def test_a_db_password_shaped_env_value_in_generated_code_is_caught(monkeypatch):
    value = "pg-pass-value-0123456789"
    monkeypatch.setenv("POSTGRES_PASSWORD", value)
    monkeypatch.setenv("ADMIN_PW", "adminpw-value-0123456789")
    fake_agent(monkeypatch, write_settings(f"X = '{value}'\n"))
    sink, err = run_head(make_state())
    assert isinstance(err, builder.SecretInGeneratedCode)
    fake_agent(monkeypatch, write_settings("X = 'adminpw-value-0123456789'\n"))
    sink, err = run_head(make_state())
    assert isinstance(err, builder.SecretInGeneratedCode)
