"""GitHub connector: validation, test_connection, tools and dry-run writes. [PRD › Connectors and API budget]"""

import logging

import httpx
import pytest
from pydantic import SecretStr

from susanoo.connectors import Credential, InvalidCredential, registry
from susanoo.connectors.base import install_log_redaction, parse_guide, redact, redact_value
from susanoo.connectors.github import GitHubConnector, normalise_repo

TOKEN = "github_pat_" + "11ABCDEFG0" * 6  # built at runtime so it never looks like a real token in git


def cred(writes: bool = False) -> Credential:
    return Credential(secret=SecretStr(TOKEN), settings={"repo": "acme/payments-api"}, writes_enabled=writes)


def github(routes: dict[tuple[str, str], httpx.Response], seen: list[httpx.Request] | None = None) -> GitHubConnector:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        if seen is not None:
            seen.append(request)
        return routes.get((request.method, request.url.path), httpx.Response(404, json={"message": "Not Found"}))

    return GitHubConnector(transport=httpx.MockTransport(handler))


OK_ROUTES = {
    ("GET", "/repos/acme/payments-api"): httpx.Response(200, json={"full_name": "acme/payments-api"}),
    ("GET", "/repos/acme/payments-api/pulls"): httpx.Response(200, json=[]),
    ("GET", "/repos/acme/payments-api/contents/"): httpx.Response(200, json=[]),
}


@pytest.mark.parametrize(
    "value",
    ["acme/payments-api", " https://github.com/acme/payments-api.git ", "github.com/acme/payments-api/"],
)
def test_normalise_repo_accepts_names_and_urls(value):
    assert normalise_repo(value) == "acme/payments-api"


@pytest.mark.parametrize("value", ["", "acme", "acme/payments/extra", "https://gitlab.com/acme/x", "acme/.."])
def test_normalise_repo_rejects_anything_else(value):
    with pytest.raises(InvalidCredential):
        normalise_repo(value)


def test_validate_splits_secret_from_settings():
    secret, settings = GitHubConnector().validate({"token": SecretStr(f"  {TOKEN} "), "repo": SecretStr("acme/x")})
    assert secret.get_secret_value() == TOKEN
    assert settings == {"repo": "acme/x"}


@pytest.mark.parametrize(
    ("token", "hint"),
    [("", "Paste"), ("ghp_" + "a" * 36, "classic"), ("not-a-token", "github_pat_")],
)
def test_validate_rejects_bad_tokens_without_echoing_them(token, hint):
    with pytest.raises(InvalidCredential) as exc:
        GitHubConnector().validate({"token": SecretStr(token), "repo": SecretStr("acme/x")})
    assert hint in str(exc.value)
    assert not token or token not in str(exc.value)


async def test_test_connection_confirms_read_access():
    result = await github(OK_ROUTES).test_connection(cred())
    assert result.ok
    assert result.message == "Connected to acme/payments-api, read access to pull requests and code confirmed."


async def test_test_connection_accepts_an_empty_repository():
    routes = {**OK_ROUTES, ("GET", "/repos/acme/payments-api/contents/"): httpx.Response(404, json={})}
    assert (await github(routes).test_connection(cred())).ok


@pytest.mark.parametrize(
    ("route", "status", "phrase"),
    [
        ("/repos/acme/payments-api", 401, "didn't accept this token"),
        ("/repos/acme/payments-api", 404, "can't see acme/payments-api"),
        ("/repos/acme/payments-api/pulls", 403, "Pull requests to Read-only"),
        ("/repos/acme/payments-api/contents/", 403, "Contents to Read-only"),
        ("/repos/acme/payments-api", 500, "had a problem"),
    ],
)
async def test_test_connection_explains_each_failure(route, status, phrase):
    routes = {**OK_ROUTES, ("GET", route): httpx.Response(status, json={"message": "nope"})}
    result = await github(routes).test_connection(cred())
    assert not result.ok
    assert phrase in result.message
    assert TOKEN not in result.message


async def test_test_connection_when_github_is_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    result = await GitHubConnector(transport=httpx.MockTransport(handler)).test_connection(cred())
    assert not result.ok and "couldn't reach GitHub" in result.message


async def test_read_tools_return_pull_requests_diffs_and_files():
    routes = {
        ("GET", "/repos/acme/payments-api/pulls"): httpx.Response(
            200, json=[{"number": 7, "title": "Add login", "user": {"login": "dev"}}]
        ),
        ("GET", "/repos/acme/payments-api/contents/app/main.py"): httpx.Response(200, text="print('hi')\n"),
    }
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/repos/acme/payments-api/pulls/7":
            if request.headers["Accept"] == "application/vnd.github.diff":
                return httpx.Response(200, text="diff --git a/x b/x\n")
            return httpx.Response(200, json={"number": 7, "title": "Add login", "body": None, "head": {"sha": "abc"}})
        return routes[(request.method, request.url.path)]

    tools = {t.name: t for t in GitHubConnector(transport=httpx.MockTransport(handler)).tools(cred())}
    assert {name for name, t in tools.items() if t.write} == {"post_review_comment"}

    listed = await tools["list_pull_requests"].run()
    assert listed.data == [{"number": 7, "title": "Add login", "author": "dev"}]
    pr = await tools["get_pull_request"].run(number=7)
    assert pr.data["diff"].startswith("diff --git") and pr.data["head_sha"] == "abc"
    file = await tools["get_file"].run(path="app/main.py", ref="abc")
    assert file.data == "print('hi')\n"
    assert seen[-1].url.params["ref"] == "abc"
    assert not (await tools["get_file"].run(path="../../secrets")).ok


async def test_post_review_comment_is_a_dry_run_by_default():
    seen: list[httpx.Request] = []
    tools = {t.name: t for t in github({}, seen).tools(cred(writes=False))}
    result = await tools["post_review_comment"].run(number=7, body="Looks risky.")
    assert result.ok and result.dry_run
    assert result.message == "Dry run: would have posted a review comment on pull request #7."
    assert seen == []  # nothing was sent to GitHub


async def test_post_review_comment_posts_when_writes_are_on():
    routes = {("POST", "/repos/acme/payments-api/pulls/7/reviews"): httpx.Response(200, json={"id": 1})}
    seen: list[httpx.Request] = []
    tools = {t.name: t for t in github(routes, seen).tools(cred(writes=True))}
    result = await tools["post_review_comment"].run(number=7, body="Looks risky.")
    assert result.ok and not result.dry_run
    assert len(seen) == 1 and seen[0].method == "POST"


def test_env_is_what_the_container_gets():
    env = GitHubConnector().env(cred())
    assert env == {"GITHUB_TOKEN": TOKEN, "GITHUB_REPO": "acme/payments-api", "GITHUB_WRITES_ENABLED": "false"}


def test_guide_has_numbered_steps_and_hides_maintainer_notes():
    guide = registry()["github"].guide()
    assert len(guide.steps) >= 4
    assert "Fine-grained tokens" in guide.steps[0]
    assert all("Click-paths a human must check" not in s for s in guide.steps + guide.notes)
    assert any("dry" in n.lower() or "would have posted" in n for n in guide.notes)


def test_parse_guide_joins_indented_continuations():
    guide = parse_guide("# Title\n\n1. First\n   still first\n2. Second\n\nA note\non two lines.\n")
    assert guide.steps == ["First still first", "Second"]
    assert guide.notes == ["A note on two lines."]


def test_redact_hides_known_secrets_and_token_shapes():
    text = f"token={TOKEN} key=sk-ant-api03-{'x' * 30} other=hunter2"
    out = redact(text, secrets=["hunter2"])
    assert TOKEN not in out and "sk-ant-" not in out and "hunter2" not in out
    assert out.count("[redacted]") == 3


def test_redact_value_cleans_span_attributes():
    span = {
        "headers": {"Authorization": "Bearer x"},
        "github_token": "abc",
        "args": [TOKEN, 3],
        "secret": SecretStr("s"),
    }
    assert redact_value(span) == {
        "headers": {"Authorization": "[redacted]"},
        "github_token": "[redacted]",
        "args": ["[redacted]", 3],
        "secret": "[redacted]",
    }


def test_log_records_are_redacted(caplog):
    install_log_redaction()
    install_log_redaction()  # idempotent
    with caplog.at_level(logging.INFO):
        logging.getLogger("anything").info("calling GitHub with %s", TOKEN)
    assert TOKEN not in caplog.text
    assert "calling GitHub with [redacted]" in caplog.text


@pytest.mark.parametrize("path", ["../../user", "a/./b", "", "/"])
async def test_get_file_refuses_paths_outside_the_repository(path):
    seen: list[httpx.Request] = []
    [get_file] = [t for t in github({}, seen).tools(cred()) if t.name == "get_file"]
    assert not (await get_file.run(path=path)).ok
    assert seen == []  # nothing was sent


async def test_get_file_sends_encoded_dots_as_literal_names():
    seen: list[httpx.Request] = []
    [get_file] = [t for t in github({}, seen).tools(cred()) if t.name == "get_file"]
    await get_file.run(path="%2e%2e/%2e%2e/user")
    assert seen[0].url.raw_path == b"/repos/acme/payments-api/contents/%252e%252e/%252e%252e/user"


async def test_get_file_escapes_query_and_fragment_characters():
    seen: list[httpx.Request] = []
    [get_file] = [t for t in github({}, seen).tools(cred()) if t.name == "get_file"]
    await get_file.run(path="x?ref=other#frag")
    assert seen[0].url.raw_path == b"/repos/acme/payments-api/contents/x%3Fref%3Dother%23frag"
    assert "ref" not in seen[0].url.params


async def test_tools_answer_failures_instead_of_raising():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    tools = {t.name: t for t in GitHubConnector(transport=httpx.MockTransport(handler)).tools(cred(writes=True))}
    timed_out = await tools["get_pull_request"].run(number=7)
    assert not timed_out.ok and "couldn't reach GitHub" in timed_out.message
    bad = await tools["get_pull_request"].run(number="abc")
    assert not bad.ok and "arguments" in bad.message
    assert not (await tools["list_pull_requests"].run(limit="many")).ok
    assert not (await tools["post_review_comment"].run(number=None, body="x")).ok


async def test_a_connection_without_a_repo_fails_its_test_cleanly():
    broken = Credential(secret=SecretStr(TOKEN), settings={})
    result = await GitHubConnector().test_connection(broken)
    assert not result.ok and "no repository" in result.message
    with pytest.raises(InvalidCredential):
        GitHubConnector().tools(broken)
