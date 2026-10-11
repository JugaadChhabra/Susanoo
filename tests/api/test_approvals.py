"""Approvals: a test-layer change waits for a human. [PRD › Improvement loop and guardrails]"""

# ruff: noqa: F811  (fixtures imported from conftest_ai are used as test arguments)

import uuid

import pytest
from conftest_ai import ALICE, BOB, TOKEN, bearer, client, quota_store, store  # noqa: F401

from susanoo_api.routers.approvals import ApprovalOut

GATE = {"diagnosis_id": "dx-1", "layer": "test", "hypothesis": "The expected line is wrong.", "scenario_ids": ["tr-1"]}


def gated(store, owner=ALICE) -> str:
    run = store.add_run(owner)
    store.pause(run, "gate", GATE)
    return run


def decide(client, run, patch="dx-1", user=ALICE, **body):
    return client.post(f"/runs/{run}/approvals/{patch}", headers=bearer(user), json=body)


def test_approvals_need_auth(client):
    response = client.post(f"/runs/{uuid.uuid4()}/approvals/dx-1", json={"pause_token": TOKEN, "approved": True})
    assert response.status_code == 401


def test_someone_elses_run_is_a_404_and_nothing_is_stored(client, store):
    run = gated(store, owner=BOB)
    response = decide(client, run, pause_token=TOKEN, approved=True)
    assert response.status_code == 404 and response.json()["error"]["code"] == "not_found"
    assert not store.answers


def test_approve_resumes_the_gate(client, store):
    run = gated(store)
    response = decide(client, run, pause_token=TOKEN, approved=True)
    assert response.status_code == 200
    body = ApprovalOut.model_validate(response.json())
    assert body.ok and body.approved and "approved" in body.message
    assert store.answers == [(run, {"approved": True}, TOKEN)]
    assert store.rows[run][1].status == "queued"


def test_reject_resumes_the_gate_with_a_no(client, store):
    run = gated(store)
    body = ApprovalOut.model_validate(decide(client, run, pause_token=TOKEN, approved=False).json())
    assert body.ok and not body.approved and "rejected" in body.message
    assert store.answers == [(run, {"approved": False}, TOKEN)]


@pytest.mark.parametrize(
    "body",
    [
        {"pause_token": TOKEN},
        {"approved": True},
        {"pause_token": TOKEN, "approved": "maybe"},
        {"pause_token": TOKEN, "approved": "true"},
        {"pause_token": TOKEN, "approved": 1},
        {"pause_token": TOKEN, "approved": "yes"},
        {"pause_token": TOKEN, "approved": None},
        {"pause_token": "", "approved": True},
    ],
)
def test_malformed_decisions_are_422s(client, store, body):
    run = gated(store)
    response = decide(client, run, **body)
    assert response.status_code == 422 and response.json()["error"]["code"] == "validation_error"
    assert not store.answers


def test_the_id_must_match_the_gate_that_is_waiting(client, store):
    run = gated(store)
    response = decide(client, run, patch="dx-other", pause_token=TOKEN, approved=True)
    assert response.status_code == 409
    assert not store.answers and store.rows[run][1].status == "awaiting_approval"


def test_a_stale_token_is_a_409(client, store):
    run = gated(store)
    response = decide(client, run, pause_token="chk-0", approved=True)
    assert response.status_code == 409
    assert response.json()["error"]["message"] == "This question was already answered or has moved on; please reload."
    assert not store.answers


def test_deciding_twice_is_a_409(client, store):
    run = gated(store)
    assert decide(client, run, pause_token=TOKEN, approved=True).status_code == 200
    assert decide(client, run, pause_token=TOKEN, approved=False).status_code == 409
    assert store.answers == [(run, {"approved": True}, TOKEN)]  # the first decision stands


def test_a_run_that_is_not_at_a_gate_is_a_409(client, store):
    running = store.add_run(ALICE, "running")
    assert decide(client, running, pause_token=TOKEN, approved=True).status_code == 409
    questions = store.add_run(ALICE)
    store.pause(questions, "questions", {"questions": []})
    assert decide(client, questions, pause_token=TOKEN, approved=True).status_code == 409
    assert not store.answers
