"""Try it: a pasted diff goes to the run's live preview and comes back as findings. [PRD › Non-technical users]

The preview is faked with an httpx mock transport, so nothing leaves the test.
"""

# ruff: noqa: F811  (fixtures imported from conftest_ai are used as test arguments)

import uuid
from dataclasses import replace

import httpx
import pytest
from conftest_ai import ALICE, BOB, bearer, client, quota_store, store  # noqa: F401

from susanoo_api.models import TryResult
from susanoo_api.routers import try_it

DIFF = '--- a/app/db.py\n+++ b/app/db.py\n@@ -1,1 +1,2 @@\n x = 1\n+cur.execute("SELECT " + name)\n'
FINDING = {"file": "app/db.py", "line": 2, "rule_id": "no-raw-sql", "severity": "block", "message": "Raw SQL."}


def live_run(store, owner=ALICE, preview_url: str | None = None) -> str:
    run = store.add_run(owner, "done")
    owner_, row = store.rows[run]
    store.rows[run] = (owner_, replace(row, preview_url=preview_url or f"https://run-{run}.example.dev"))
    return run


@pytest.fixture
def preview(monkeypatch):
    """The fake preview: records each request and answers with whatever `reply` holds."""
    monkeypatch.setenv("PREVIEW_DOMAIN", "example.dev")
    seen: list[httpx.Request] = []
    review = {"id": "r1", "created_at": "now", "findings": [FINDING]}
    state: dict = {"reply": lambda _: httpx.Response(200, json=review)}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return state["reply"](request)

    monkeypatch.setattr(try_it, "_transport", httpx.MockTransport(handler))
    state["seen"] = seen
    return state


def post(client, run, user=ALICE, **body):
    return client.post(f"/runs/{run}/try", headers=bearer(user), json={"kind": "diff", "diff": DIFF, **body})


def test_try_needs_auth(client):
    assert client.post(f"/runs/{uuid.uuid4()}/try", json={"kind": "diff", "diff": DIFF}).status_code == 401


def test_a_diff_comes_back_with_the_agents_findings(client, store, quota_store, preview):
    run = live_run(store)
    response = post(client, run)
    assert response.status_code == 200
    body = TryResult.model_validate(response.json())
    assert [f.model_dump() for f in body.findings] == [FINDING]
    assert body.summary == "Found 1 possible problem, and it should block the change."
    (request,) = preview["seen"]
    assert str(request.url) == f"https://run-{run}.example.dev/review"
    assert request.method == "POST" and httpx.Response(200, content=request.content).json() == {"diff": DIFF.strip()}
    assert not store.costs and quota_store.runs_today == {}  # no quota cost


def test_no_findings_says_so_in_plain_english(client, store, preview):
    preview["reply"] = lambda _: httpx.Response(200, json={"id": "r1", "created_at": "now", "findings": []})
    body = post(client, live_run(store)).json()
    assert body == {"summary": "No problems found in this change.", "findings": []}


def test_someone_elses_run_is_a_404_and_the_preview_is_never_called(client, store, preview):
    response = post(client, live_run(store, owner=BOB))
    assert response.status_code == 404 and response.json()["error"]["code"] == "not_found"
    assert response.json()["error"]["message"] == "We couldn't find that run."
    assert not preview["seen"]


def test_a_diff_over_20000_characters_is_a_422(client, store, preview):
    response = post(client, live_run(store), diff="+" + "x" * 20_000)
    assert response.status_code == 422
    assert response.json()["error"]["message"] == "That change is too long. Keep it under 20,000 characters."
    assert not preview["seen"]


def test_a_blank_diff_is_a_422(client, store, preview):
    response = post(client, live_run(store), diff="   \n")
    assert response.status_code == 422 and not preview["seen"]


def test_a_preview_that_does_not_answer_is_a_503(client, store, preview):
    def down(request):
        raise httpx.ConnectError("refused", request=request)

    preview["reply"] = down
    response = post(client, live_run(store))
    assert response.status_code == 503
    assert response.json()["error"]["message"].startswith("Your agent's preview isn't answering")


def test_a_preview_that_times_out_is_a_503(client, store, preview):
    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    preview["reply"] = slow
    response = post(client, live_run(store))
    assert response.status_code == 503 and "took too long" in response.json()["error"]["message"]


def test_an_asleep_preview_behind_caddy_says_it_may_be_asleep(client, store, preview):
    preview["reply"] = lambda _: httpx.Response(502, text="")  # Caddy, with the container stopped
    response = post(client, live_run(store))
    assert response.status_code == 503
    assert response.json()["error"]["message"].startswith("Your agent's preview isn't answering")


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(502, json={"detail": "The model failed."}),
        httpx.Response(503, json={"detail": "No Anthropic key."}),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"findings": [{"file": "a.py"}]}),
        httpx.Response(200, json={"findings": [{**FINDING, "severity": "critical"}]}),
    ],
)
def test_a_failing_preview_is_a_503_and_its_body_is_not_echoed(client, store, preview, reply):
    preview["reply"] = lambda _: reply
    response = post(client, live_run(store))
    assert response.status_code == 503
    message = response.json()["error"]["message"]
    assert message.startswith("Your agent couldn't review this change") and "model failed" not in message


@pytest.mark.parametrize("status", ["running", "done"])
def test_a_run_without_a_preview_is_a_503(client, store, preview, status):
    run = store.add_run(ALICE, status)
    response = post(client, run)
    assert response.status_code == 503 and not preview["seen"]


@pytest.mark.parametrize(
    "url", ["http://169.254.169.254/latest", "https://169.254.169.254", "https://run-other.example.dev"]
)
def test_a_preview_url_that_is_not_the_runs_own_address_is_never_called(client, store, preview, url):
    response = post(client, live_run(store, preview_url=url))
    assert response.status_code == 503 and not preview["seen"]


def test_an_unknown_kind_is_a_422(client, store, preview):
    response = post(client, live_run(store), kind="patch")
    assert response.status_code == 422 and not preview["seen"]


# ---------------------------------------------------------------- a pull request from the connected repo

TOKEN = "github_pat_" + "11ABCDEFG0" * 6  # built at runtime so it never looks like a real token in git


@pytest.fixture
def github(monkeypatch):
    """The fake GitHub and the run's resolved connections (the #30 Vault resolution, faked per run)."""
    seen: list[httpx.Request] = []
    state: dict = {
        "env": {"GITHUB_TOKEN": TOKEN, "GITHUB_REPO": "acme/payments-api", "GITHUB_WRITES_ENABLED": "false"},
        "reply": lambda _: httpx.Response(200, text=DIFF),
        "seen": seen,
        "asked": [],
    }

    async def run_connection_env(run_id: str) -> dict[str, str]:
        state["asked"].append(run_id)
        if isinstance(state["env"], Exception):
            raise state["env"]
        return dict(state["env"])

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return state["reply"](request)

    monkeypatch.setattr(try_it, "run_connection_env", run_connection_env)
    monkeypatch.setattr(try_it, "_github_transport", httpx.MockTransport(handler))
    return state


def post_pr(client, run, pr="acme/payments-api#12", user=ALICE):
    return client.post(f"/runs/{run}/try", headers=bearer(user), json={"kind": "pr", "pr": pr})


@pytest.mark.parametrize(
    "pr", ["acme/payments-api#12", "https://github.com/acme/payments-api/pull/12", "Acme/Payments-API#12"]
)
def test_a_pr_from_the_connected_repo_comes_back_with_findings(client, store, preview, github, caplog, pr):
    run = live_run(store)
    caplog.set_level("DEBUG")
    response = post_pr(client, run, pr=pr)
    assert response.status_code == 200
    body = TryResult.model_validate(response.json())
    assert [f.model_dump() for f in body.findings] == [FINDING]
    assert github["asked"] == [run]
    (fetch,) = github["seen"]
    assert str(fetch.url) == "https://api.github.com/repos/acme/payments-api/pulls/12"
    assert fetch.headers["Authorization"] == f"Bearer {TOKEN}"
    assert fetch.headers["Accept"] == "application/vnd.github.diff"
    (review,) = preview["seen"]  # the same path as a pasted diff
    assert httpx.Response(200, content=review.content).json() == {"diff": DIFF.strip()}
    assert TOKEN not in response.text and TOKEN not in review.content.decode() and TOKEN not in caplog.text


def test_a_pr_without_a_github_connection_is_a_422(client, store, preview, github):
    github["env"] = {"SLACK_BOT_TOKEN": "xoxb-1"}
    response = post_pr(client, live_run(store))
    assert response.status_code == 422
    assert response.json()["error"]["message"] == "Connect GitHub to try a PR."
    assert not github["seen"] and not preview["seen"]


def test_a_pr_when_a_connection_is_not_yours_is_a_422(client, store, preview, github):
    from susanoo.connectors.runtime_env import ConnectionNotYours

    github["env"] = ConnectionNotYours("A connection picked for this run belongs to someone else, so none were used.")
    response = post_pr(client, live_run(store))
    assert response.status_code == 422 and "Connect GitHub" in response.json()["error"]["message"]
    assert not github["seen"] and not preview["seen"]


def test_a_pr_from_another_repo_is_a_422_and_github_is_never_asked(client, store, preview, github):
    response = post_pr(client, live_run(store), pr="someone/else#12")
    assert response.status_code == 422
    message = response.json()["error"]["message"]
    assert message == "Your GitHub connection is for acme/payments-api, so it can only try pull requests from there."
    assert not github["seen"] and not preview["seen"]


def test_a_pr_that_does_not_exist_is_a_422(client, store, preview, github, caplog):
    github["reply"] = lambda _: httpx.Response(404, json={"message": "Not Found"})
    response = post_pr(client, live_run(store), pr="acme/payments-api#999")
    assert response.status_code == 422
    assert response.json()["error"]["message"] == "We couldn't find pull request #999 in acme/payments-api."
    assert not preview["seen"] and TOKEN not in caplog.text


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_token_is_a_422_that_names_no_token(client, store, preview, github, status):
    github["reply"] = lambda _: httpx.Response(status, json={"message": f"Bad credentials {TOKEN}"})
    response = post_pr(client, live_run(store))
    assert response.status_code == 422
    assert "GitHub didn't let your connection read" in response.json()["error"]["message"]
    assert TOKEN not in response.text and not preview["seen"]


def test_github_being_unreachable_is_a_503(client, store, preview, github):
    def down(request):
        raise httpx.ConnectError(f"refused {TOKEN}", request=request)

    github["reply"] = down
    response = post_pr(client, live_run(store))
    assert response.status_code == 503 and "reach GitHub" in response.json()["error"]["message"]
    assert TOKEN not in response.text and not preview["seen"]


def test_a_pr_with_too_many_changes_is_a_422(client, store, preview, github):
    github["reply"] = lambda _: httpx.Response(200, text="+" + "x" * 20_000)
    response = post_pr(client, live_run(store))
    assert response.status_code == 422 and "pull request's changes are too long" in response.json()["error"]["message"]
    assert not preview["seen"]


def test_an_empty_pr_is_a_422_that_talks_about_the_pr(client, store, preview, github):
    github["reply"] = lambda _: httpx.Response(200, text="")
    response = post_pr(client, live_run(store))
    assert response.status_code == 422
    assert response.json()["error"]["message"] == "That pull request has no code changes to review."
    assert not preview["seen"]


@pytest.mark.parametrize("pr", ["", "acme/payments-api", "acme/payments-api#x", "https://evil.dev/a/b/pull/1"])
def test_a_pr_that_is_not_a_pr_reference_is_a_422(client, store, preview, github, pr):
    response = post_pr(client, live_run(store), pr=pr)
    assert response.status_code == 422 and not github["asked"] and not github["seen"]


def test_a_pr_on_a_run_without_a_preview_never_reads_github(client, store, preview, github):
    response = post_pr(client, store.add_run(ALICE, "running"))
    assert response.status_code == 503 and not github["asked"] and not github["seen"]


def test_a_pr_on_someone_elses_run_is_a_404_and_reads_nothing(client, store, preview, github):
    response = post_pr(client, live_run(store, owner=BOB))
    assert response.status_code == 404 and not github["asked"] and not github["seen"]
