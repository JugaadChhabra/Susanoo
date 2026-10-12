"""Try it: a pasted diff goes to the run's live preview and comes back as findings. [PRD › Non-technical users]

The preview is faked with an httpx mock transport, so nothing leaves the test.
"""

# ruff: noqa: F811  (fixtures imported from conftest_ai are used as test arguments)

import uuid

import httpx
import pytest
from conftest_ai import ALICE, BOB, bearer, client, quota_store, store  # noqa: F401

from susanoo_api.models import TryResult
from susanoo_api.routers import try_it

DIFF = '--- a/app/db.py\n+++ b/app/db.py\n@@ -1,1 +1,2 @@\n x = 1\n+cur.execute("SELECT " + name)\n'
FINDING = {"file": "app/db.py", "line": 2, "rule_id": "no-raw-sql", "severity": "block", "message": "Raw SQL."}


PREVIEW = "https://run-abc123.example.dev"


def live_run(store, owner=ALICE, preview_url=PREVIEW) -> str:
    return store.add_run(owner, "done", preview_url=preview_url)


@pytest.fixture
def preview(monkeypatch):
    """The fake preview: records each request and answers with whatever `reply` holds."""
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
    assert str(request.url) == f"{PREVIEW}/review"
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


def test_a_pr_is_a_422_until_prs_are_supported(client, store, preview):
    response = client.post(
        f"/runs/{live_run(store)}/try", headers=bearer(ALICE), json={"kind": "pr", "pr": "owner/repo#12"}
    )
    assert response.status_code == 422 and "diff" in response.json()["error"]["message"]
    assert not preview["seen"]


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


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(502, json={"detail": "The model failed."}),
        httpx.Response(503, text="asleep"),
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


def test_a_non_https_preview_url_is_never_called(client, store, preview):
    response = post(client, live_run(store, preview_url="http://169.254.169.254/latest"))
    assert response.status_code == 503 and not preview["seen"]
