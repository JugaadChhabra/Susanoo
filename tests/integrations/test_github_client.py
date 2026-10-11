"""Unit tests for integrations/github.py. No network: httpx.MockTransport and a local bare git repo."""

import asyncio
import base64
import subprocess

import httpx
import pytest

from susanoo.integrations import github as gh

TOKEN = "ghp" + "_SECRETTOKENVALUE0123456789abcdef"


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


def make_client(handler):
    http = httpx.AsyncClient(base_url=gh.GITHUB_API, transport=httpx.MockTransport(handler))
    return gh.GitHubClient(TOKEN, http=http)


def created(request):
    return httpx.Response(
        201,
        json={
            "full_name": "acme/code-review-abc123",
            "clone_url": "https://github.com/acme/code-review-abc123.git",
            "html_url": "https://github.com/acme/code-review-abc123",
        },
    )


# --- names -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "slug"),
    [
        ("Python Code Review", "python-code-review"),
        ("  Hello,   World!! ", "hello-world"),
        ("!!!", "app"),
        ("Ünï côdé", "n-c-d"),
    ],
)
def test_slugify(text, slug):
    assert gh.slugify(text) == slug


def test_slugify_is_capped_and_never_ends_with_a_hyphen():
    slug = gh.slugify("word " * 30)
    assert len(slug) <= 40 and not slug.endswith("-")


def test_repo_name_is_slug_plus_short_run_id():
    assert gh.repo_name("Python Code Review", "3FA9C1D2-xxxx") == "python-code-review-3fa9c1"
    assert gh.repo_name("X", "---") == "x-run"


# --- environment -------------------------------------------------------------------------------------


def test_config_from_env(monkeypatch):
    for var in ("GITHUB_ORG", "GITHUB_ORG_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    assert gh.github_config_from_env() is None
    monkeypatch.setenv("GITHUB_ORG", "acme")
    assert gh.github_config_from_env() is None  # org without token is still local-only
    monkeypatch.setenv("GITHUB_TOKEN", "fallback")
    assert gh.github_config_from_env() == ("acme", "fallback")
    monkeypatch.setenv("GITHUB_ORG_TOKEN", "primary")
    assert gh.github_config_from_env() == ("acme", "primary")


# --- create_repo -------------------------------------------------------------------------------------


def test_create_repo_posts_a_public_repo_and_returns_urls():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = request.read().decode()
        return created(request)

    client = make_client(handler)
    info = asyncio.run(client.create_repo("acme", "code-review-abc123", description="Reviews diffs"))
    assert info == gh.RepoInfo(
        "acme/code-review-abc123",
        "https://github.com/acme/code-review-abc123.git",
        "https://github.com/acme/code-review-abc123",
    )
    assert seen["url"].endswith("/orgs/acme/repos")
    assert seen["auth"] == f"Bearer {TOKEN}"
    assert '"private":false' in seen["body"].replace(" ", "")
    assert TOKEN not in seen["body"]


def test_name_conflict_gives_a_clear_error():
    def handler(request):
        return httpx.Response(
            422,
            json={
                "message": "Repository creation failed.",
                "errors": [{"message": "name already exists on this account"}],
            },
        )

    with pytest.raises(gh.RepoAlreadyExists, match="acme/code-review-abc123"):
        asyncio.run(make_client(handler).create_repo("acme", "code-review-abc123"))


@pytest.mark.parametrize(
    ("status", "needle"),
    [(401, "token"), (403, "permission"), (404, "organisation"), (422, "422"), (500, "500")],
)
def test_other_http_errors_are_plain_english_and_token_free(status, needle):
    client = make_client(lambda request: httpx.Response(status, json={"message": "nope"}))
    with pytest.raises(gh.GitHubError, match=needle) as err:
        asyncio.run(client.create_repo("acme", "x"))
    assert not isinstance(err.value, gh.RepoAlreadyExists)
    assert TOKEN not in str(err.value)


def test_network_failure_is_reported_without_the_token():
    def handler(request):
        raise httpx.ConnectError(f"boom {TOKEN}")

    with pytest.raises(gh.GitHubError, match="Could not reach GitHub") as err:
        asyncio.run(make_client(handler).create_repo("acme", "x"))
    assert TOKEN not in str(err.value)
    assert err.value.__cause__ is None and err.value.__suppress_context__


def test_token_is_never_in_repr_and_is_required():
    assert TOKEN not in repr(gh.GitHubClient(TOKEN))
    with pytest.raises(ValueError):
        gh.GitHubClient("")


# --- push_repo ---------------------------------------------------------------------------------------


@pytest.fixture
def local_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "test")
    git(repo, "config", "user.email", "t@example.com")
    (repo / "a.txt").write_text("hello\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "init")
    return repo


@pytest.fixture
def bare(tmp_path):
    path = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(path)], check=True)
    return path


def tree_text(root):
    """Every byte of every file under root (including .git) as latin-1 text, for secret greps."""
    return "".join(p.read_bytes().decode("latin-1") for p in root.rglob("*") if p.is_file())


def test_push_sends_the_commit_and_persists_no_token(local_repo, bare, tmp_path):
    asyncio.run(gh.push_repo(local_repo, str(bare), TOKEN))
    assert git(bare, "rev-parse", "main").strip() == git(local_repo, "rev-parse", "HEAD").strip()
    basic = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
    everything = tree_text(tmp_path)
    assert TOKEN not in everything and basic not in everything
    assert git(local_repo, "config", "--get", "remote.origin.url").strip() == str(bare)
    assert "extraheader" not in git(local_repo, "config", "--local", "--list").lower()


def test_push_passes_the_token_by_environment_not_argv(local_repo, bare, monkeypatch):
    calls = []
    real = asyncio.create_subprocess_exec

    async def spy(*args, **kw):
        calls.append((args, kw.get("env") or {}))
        return await real(*args, **kw)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    asyncio.run(gh.push_repo(local_repo, str(bare), TOKEN))
    basic = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
    assert calls
    for args, _ in calls:
        assert TOKEN not in " ".join(args) and basic not in " ".join(args)
    push_env = calls[-1][1]
    assert push_env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    assert push_env["GIT_CONFIG_VALUE_0"] == f"Authorization: Basic {basic}"
    assert push_env["GIT_TERMINAL_PROMPT"] == "0"


def test_push_twice_replaces_origin_cleanly(local_repo, bare):
    asyncio.run(gh.push_repo(local_repo, str(bare), TOKEN))
    asyncio.run(gh.push_repo(local_repo, str(bare), TOKEN))


def test_failed_push_raises_a_scrubbed_error(local_repo, tmp_path):
    missing = tmp_path / "does-not-exist.git"
    with pytest.raises(gh.GitHubError, match="git push failed") as err:
        asyncio.run(gh.push_repo(local_repo, str(missing), TOKEN))
    assert TOKEN not in str(err.value)


def test_create_and_push_creates_then_pushes(local_repo, monkeypatch):
    pushed = {}

    async def fake_push(repo, url, token, **kw):
        pushed.update(repo=repo, url=url, token=token)

    monkeypatch.setattr(gh, "push_repo", fake_push)
    client = make_client(created)
    info = asyncio.run(client.create_and_push("acme", "code-review-abc123", local_repo))
    assert info.full_name == "acme/code-review-abc123"
    assert pushed == {"repo": local_repo, "url": info.clone_url, "token": TOKEN}
