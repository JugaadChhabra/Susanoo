"""Credential-free connector tool code for generated repos. [PRD › Connectors and API budget › Connector contract]"""

import ast
import io
import json
import subprocess
import sys
import textwrap
import urllib.error
import urllib.request

import pytest
from pydantic import SecretStr

from susanoo.connectors import Credential, registry
from susanoo.connectors import github_tools
from susanoo.connectors.base import redact
from susanoo.heads import _connectors
from susanoo.integrations import tracing

TOKEN = "github_pat_" + "11ABCDEFG0" * 6  # built at runtime so it never looks like a real token in git
PLACEHOLDER = "placeholder-not-a-credential"


def placeholder(connector) -> Credential:
    settings = {f.name: PLACEHOLDER for f in connector.spec.fields if not f.secret}
    return Credential(secret=SecretStr(PLACEHOLDER), settings=settings)


def all_sources() -> list[tuple[str, str]]:
    return [(cid, src) for cid, c in registry().items() for src in c.tool_sources()]


def function_name(source: str) -> str:
    tree = ast.parse(source)
    assert len(tree.body) == 1 and isinstance(tree.body[0], ast.FunctionDef)
    return tree.body[0].name


def test_every_connector_still_describes_itself_with_a_placeholder_credential():
    for connector in registry().values():
        cred = placeholder(connector)
        assert isinstance(connector.env(cred), dict)
        assert isinstance(connector.tools(cred), list)


def test_tool_sources_match_the_tools_agent_smith_reads_schemas_from():
    for connector in registry().values():
        names = [t.name for t in connector.tools(placeholder(connector))]
        assert [function_name(s) for s in connector.tool_sources()] == names


def test_github_ships_its_four_tools():
    assert [function_name(s) for s in registry()["github"].tool_sources()] == [
        "list_pull_requests",
        "get_pull_request",
        "get_file",
        "post_review_comment",
    ]


@pytest.mark.parametrize(("cid", "source"), all_sources())
def test_each_source_compiles_and_defines_its_function_without_susanoo(cid, source):
    # A fresh, isolated interpreter that cannot see the susanoo package, like the generated app's container.
    script = "import sys\nns = {}\nexec(compile(sys.stdin.read(), 'tool.py', 'exec'), ns)\n"
    script += f"assert callable(ns[{function_name(source)!r}])\nassert 'susanoo' not in sys.modules\n"
    done = subprocess.run([sys.executable, "-I", "-S", "-c", script], input=source, text=True, capture_output=True)
    assert done.returncode == 0, done.stderr
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in getattr(node, "names", [])
    } | {(node.module or "").split(".")[0] for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ImportFrom)}
    assert "susanoo" not in imported
    assert imported <= set(sys.stdlib_module_names), "tool code must run with the standard library only"


@pytest.mark.parametrize(("cid", "source"), all_sources())
def test_each_source_contains_nothing_secret_shaped(cid, source):
    assert not tracing.looks_secret_shaped(source)
    assert redact(source) == source


@pytest.mark.parametrize(("cid", "source"), all_sources())
def test_each_source_reads_only_the_env_vars_env_sets(cid, source):
    connector = registry()[cid]
    allowed = set(connector.env(placeholder(connector)))
    read: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Subscript) and ast.unparse(node.value) == "os.environ":
            read.add(ast.literal_eval(node.slice))
        elif isinstance(node, ast.Call) and ast.unparse(node.func) in ("os.environ.get", "os.getenv"):
            read.add(ast.literal_eval(node.args[0]))
    assert read and read <= allowed


@pytest.mark.parametrize("fn", github_tools.TOOLS, ids=lambda fn: fn.__name__)
def test_each_github_tool_passes_agent_smiths_vetting(fn):
    assert _connectors._tool_source("github", fn.__name__, fn) in registry()["github"].tool_sources()


# --- behaviour, run from the shipped source with the network faked --------------------------------------------


class FakeGitHub:
    def __init__(self, routes: dict[tuple[str, str], tuple[int, str]]) -> None:
        self.routes = routes
        self.requests: list[urllib.request.Request] = []

    def __call__(self, request: urllib.request.Request, timeout: float = 0):
        self.requests.append(request)
        path = request.full_url.removeprefix("https://api.github.com")
        status, body = self.routes.get((request.get_method(), path), (404, '{"message": "Not Found"}'))
        if status >= 400:
            raise urllib.error.HTTPError(request.full_url, status, "error", {}, io.BytesIO(body.encode()))
        response = io.BytesIO(body.encode())
        response.status = status  # type: ignore[attr-defined]
        return response


def shipped(name: str):
    """The tool as the generated app would run it: exec'd from tool_sources(), not imported from susanoo."""
    ns: dict = {}
    for source in registry()["github"].tool_sources():
        exec(compile(source, "tools.py", "exec"), ns)
    return ns[name]


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("GITHUB_REPO", "acme/payments-api")
    monkeypatch.delenv("GITHUB_WRITES_ENABLED", raising=False)
    return monkeypatch


def fake(monkeypatch, routes) -> FakeGitHub:
    gh = FakeGitHub(routes)
    monkeypatch.setattr(urllib.request, "urlopen", gh)
    return gh


def test_list_pull_requests_reads_the_repo_and_token_from_env(env):
    gh = fake(
        env,
        {
            ("GET", "/repos/acme/payments-api/pulls?state=open&per_page=10"): (
                200,
                json.dumps([{"number": 7, "title": "Add refunds", "user": {"login": "sam"}}]),
            )
        },
    )
    result = shipped("list_pull_requests")()
    assert result == {
        "ok": True,
        "message": "Found 1 pull requests in acme/payments-api.",
        "data": [{"number": 7, "title": "Add refunds", "author": "sam"}],
        "dry_run": False,
    }
    assert gh.requests[0].get_header("Authorization") == f"Bearer {TOKEN}"


def test_get_pull_request_returns_title_body_and_diff(env):
    pr = {"number": 7, "title": "Add refunds", "body": None, "head": {"sha": "abc123"}}
    gh = fake(env, {("GET", "/repos/acme/payments-api/pulls/7"): (200, json.dumps(pr))})
    original = gh.__call__

    def route(request, timeout=0):
        if request.get_header("Accept") == "application/vnd.github.diff":
            gh.requests.append(request)
            response = io.BytesIO(b"diff --git a/x b/x\n")
            response.status = 200  # type: ignore[attr-defined]
            return response
        return original(request, timeout)

    env.setattr(urllib.request, "urlopen", route)
    result = shipped("get_pull_request")(7)
    assert result["ok"] is True
    assert result["data"] == {
        "number": 7,
        "title": "Add refunds",
        "body": "",
        "head_sha": "abc123",
        "diff": "diff --git a/x b/x\n",
        "diff_truncated": False,
    }


def test_get_file_escapes_the_path_and_refuses_traversal(env):
    gh = fake(env, {("GET", "/repos/acme/payments-api/contents/app/a%3Fb.py?ref=main"): (200, "print(1)\n")})
    assert shipped("get_file")("app/a?b.py", ref="main")["data"] == "print(1)\n"
    assert gh.requests[0].get_header("Accept") == "application/vnd.github.raw+json"
    refused = shipped("get_file")("../secrets")
    assert refused["ok"] is False and "inside the repository" in refused["message"]


def test_post_review_comment_is_a_dry_run_unless_writes_are_on(env):
    gh = fake(env, {("POST", "/repos/acme/payments-api/pulls/7/reviews"): (200, "{}")})
    result = shipped("post_review_comment")(7, "Looks risky")
    assert result == {
        "ok": True,
        "message": "Dry run: would have posted a review comment on pull request #7.",
        "data": None,
        "dry_run": True,
    }
    assert gh.requests == []

    env.setenv("GITHUB_WRITES_ENABLED", "false")
    assert shipped("post_review_comment")(7, "Looks risky")["dry_run"] is True
    assert gh.requests == []

    env.setenv("GITHUB_WRITES_ENABLED", "true")
    posted = shipped("post_review_comment")(7, "Looks risky")
    assert posted["ok"] is True and posted["dry_run"] is False
    assert json.loads(gh.requests[0].data) == {"body": "Looks risky", "event": "COMMENT"}


def test_failures_are_plain_english_and_never_echo_the_token(env):
    fake(env, {("GET", "/repos/acme/payments-api/pulls/9"): (401, json.dumps({"message": f"bad {TOKEN}"}))})
    result = shipped("get_pull_request")(9)
    assert result["ok"] is False
    assert result["message"] == "Couldn't read pull request #9: the token was rejected."
    assert TOKEN not in repr(result)


def test_unreachable_github_and_bad_arguments_never_raise(env):
    def down(request, timeout=0):
        raise urllib.error.URLError("no route")

    env.setattr(urllib.request, "urlopen", down)
    assert shipped("list_pull_requests")()["message"] == "We couldn't reach GitHub. Try again in a moment."
    assert shipped("get_pull_request")("seven")["ok"] is False
    assert shipped("list_pull_requests")(state="merged")["message"] == "state must be open, closed or all."


def test_an_unreadable_answer_is_not_blamed_on_the_arguments(env):
    fake(env, {("GET", "/repos/acme/payments-api/pulls?state=open&per_page=10"): (200, "<html>proxy error</html>")})
    result = shipped("list_pull_requests")()
    assert result["message"] == "GitHub sent back something we couldn't read. Try again in a moment."


def test_a_missing_connection_is_reported_not_raised(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_REPO", raising=False)
    result = shipped("list_pull_requests")()
    assert result["ok"] is False
    assert "GitHub isn't connected" in result["message"]


def test_the_source_is_what_the_module_defines():
    assert registry()["github"].tool_sources() == [
        textwrap.dedent(__import__("inspect").getsource(fn)).rstrip() + "\n" for fn in github_tools.TOOLS
    ]
