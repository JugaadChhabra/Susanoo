"""Intake: what a paused run asks, and the answers. [PRD › Intake: questions and approved scenarios]"""

# ruff: noqa: F811  (fixtures imported from conftest_ai are used as test arguments)

import uuid

import pytest
from conftest_ai import ALICE, BOB, TOKEN, bearer, client, quota_store, store  # noqa: F401

from susanoo_api.routers.intake import IntakeAccepted, IntakeOut

QUESTIONS = [
    {"id": "health-auth", "text": "Block or allow a missing login check on /health?", "options": ["block", "allow"],
     "default": "allow", "why": "Health checks are usually public."},
    {"id": "tone", "text": "How blunt should reviews be?", "options": [], "default": "short", "why": "Sets wording."},
]  # fmt: skip
CARDS = [
    {"id": "sc-1", "title": "A developer logs a customer email."},
    {"id": "sc-2", "title": "A rename; nothing to flag."},
]


def waiting_on_questions(store, owner=ALICE) -> str:
    run = store.add_run(owner)
    store.pause(run, "questions", {"questions": QUESTIONS})
    return run


def waiting_on_scenarios(store, owner=ALICE) -> str:
    run = store.add_run(owner)
    store.pause(run, "scenarios", {"scenarios": CARDS})
    return run


# ---------------------------------------------------------------- auth and ownership


@pytest.mark.parametrize(
    ("method", "suffix"), [("get", "intake"), ("post", "intake/answers"), ("post", "intake/scenarios")]
)
def test_every_intake_route_needs_auth(client, method, suffix):
    response = client.request(method, f"/runs/{uuid.uuid4()}/{suffix}", json={})
    assert response.status_code == 401


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("get", "intake", None),
        ("post", "intake/answers", {"pause_token": TOKEN, "answers": {"tone": "short"}}),
        ("post", "intake/scenarios", {"pause_token": TOKEN, "approved_ids": ["sc-1"]}),
    ],
)
def test_someone_elses_run_is_a_404_and_nothing_is_stored(client, store, method, suffix, body):
    run = waiting_on_questions(store, owner=BOB)
    response = client.request(method, f"/runs/{run}/{suffix}", headers=bearer(ALICE), json=body)
    assert response.status_code == 404 and response.json()["error"]["code"] == "not_found"
    assert not store.answers


# ---------------------------------------------------------------- GET


def test_get_intake_returns_the_questions_and_a_pause_token(client, store):
    run = waiting_on_questions(store)
    response = client.get(f"/runs/{run}/intake", headers=bearer(ALICE))
    assert response.status_code == 200
    body = IntakeOut.model_validate(response.json())
    assert body.waiting and body.kind == "questions" and body.pause_token == TOKEN
    assert [q.id for q in body.questions] == ["health-auth", "tone"]
    assert body.questions[0].options == ["block", "allow"] and body.questions[0].default == "allow"
    assert body.scenarios == [] and body.gate is None and "{" not in body.message


def test_get_intake_returns_the_scenario_cards(client, store):
    body = IntakeOut.model_validate(
        client.get(f"/runs/{waiting_on_scenarios(store)}/intake", headers=bearer(ALICE)).json()
    )
    assert body.kind == "scenarios" and [c.title for c in body.scenarios] == [c["title"] for c in CARDS]


def test_get_intake_returns_a_pending_gate_for_the_approval_card(client, store):
    run = store.add_run(ALICE)
    gate = {
        "diagnosis_id": "dx-1",
        "layer": "test",
        "hypothesis": "The expected line is wrong.",
        "scenario_ids": ["tr-1"],
    }
    store.pause(run, "gate", gate)
    body = IntakeOut.model_validate(client.get(f"/runs/{run}/intake", headers=bearer(ALICE)).json())
    assert body.kind == "gate" and body.pause_token == TOKEN
    assert body.gate is not None and body.gate.approval_id == "dx-1" and body.gate.hypothesis == gate["hypothesis"]


@pytest.mark.parametrize(("status", "fragment"), [("running", "Nothing is waiting"), ("done", "finished")])
def test_get_intake_when_nothing_is_waiting(client, store, status, fragment):
    run = store.add_run(ALICE, status)
    body = IntakeOut.model_validate(client.get(f"/runs/{run}/intake", headers=bearer(ALICE)).json())
    assert not body.waiting and body.kind is None and body.pause_token is None and fragment in body.message


# ---------------------------------------------------------------- answers


def test_answers_are_validated_stored_and_queue_the_run(client, store):
    run = waiting_on_questions(store)
    response = client.post(
        f"/runs/{run}/intake/answers",
        headers=bearer(ALICE),
        json={"pause_token": TOKEN, "answers": {"health-auth": " block ", "tone": "short"}},
    )
    assert response.status_code == 200
    assert IntakeAccepted.model_validate(response.json()).ok
    assert store.answers == [(run, {"answers": {"health-auth": "block", "tone": "short"}}, TOKEN)]
    assert store.rows[run][1].status == "queued"


def test_a_partial_answer_is_fine_because_defaults_fill_the_rest(client, store):
    run = waiting_on_questions(store)
    response = client.post(
        f"/runs/{run}/intake/answers", headers=bearer(ALICE), json={"pause_token": TOKEN, "answers": {"tone": "long"}}
    )
    assert response.status_code == 200


def test_answers_for_unknown_questions_are_a_422_in_plain_english(client, store):
    run = waiting_on_questions(store)
    response = client.post(
        f"/runs/{run}/intake/answers", headers=bearer(ALICE), json={"pause_token": TOKEN, "answers": {"nope": "x"}}
    )
    assert response.status_code == 422 and "unknown questions: nope" in response.json()["error"]["message"]
    assert not store.answers and store.rows[run][1].status == "awaiting_approval"


@pytest.mark.parametrize(
    "body",
    [
        {"pause_token": TOKEN},
        {"answers": {"tone": "short"}},
        {"pause_token": TOKEN, "answers": ["tone"]},
        {"pause_token": TOKEN, "answers": {"tone": {"deep": 1}}},
        {"pause_token": "", "answers": {"tone": "short"}},
        {"pause_token": TOKEN, "answers": {"tone": "x" * 501}},
    ],
)
def test_malformed_answer_bodies_are_422s(client, store, body):
    run = waiting_on_questions(store)
    response = client.post(f"/runs/{run}/intake/answers", headers=bearer(ALICE), json=body)
    assert response.status_code == 422 and response.json()["error"]["code"] == "validation_error"
    assert not store.answers


def test_an_empty_answers_object_means_use_the_defaults(client, store):
    run = waiting_on_questions(store)
    response = client.post(
        f"/runs/{run}/intake/answers", headers=bearer(ALICE), json={"pause_token": TOKEN, "answers": {}}
    )
    assert response.status_code == 200 and store.answers[0][1] == {"answers": {}}


def test_a_stale_token_is_a_409_and_nothing_is_stored(client, store):
    run = waiting_on_questions(store)
    response = client.post(
        f"/runs/{run}/intake/answers",
        headers=bearer(ALICE),
        json={"pause_token": "chk-0", "answers": {"tone": "short"}},
    )
    assert response.status_code == 409
    assert response.json()["error"]["message"] == "This question was already answered or has moved on; please reload."
    assert not store.answers and store.rows[run][1].status == "awaiting_approval"


def test_answering_twice_is_a_409(client, store):
    run = waiting_on_questions(store)
    body = {"pause_token": TOKEN, "answers": {"tone": "short"}}
    assert client.post(f"/runs/{run}/intake/answers", headers=bearer(ALICE), json=body).status_code == 200
    again = client.post(f"/runs/{run}/intake/answers", headers=bearer(ALICE), json=body)
    assert again.status_code == 409 and "already answered" in again.json()["error"]["message"]
    assert len(store.answers) == 1


def test_a_run_that_lost_the_race_between_check_and_store_is_a_409(client, store, monkeypatch):
    run = waiting_on_questions(store)

    async def lost(user_id, run_id, value, token):
        return False  # the UPDATE matched nothing: another request or a cancel got there first

    monkeypatch.setattr(store, "store_answer", lost)
    response = client.post(
        f"/runs/{run}/intake/answers", headers=bearer(ALICE), json={"pause_token": TOKEN, "answers": {"tone": "short"}}
    )
    assert response.status_code == 409


def test_answers_to_a_scenario_pause_are_a_409(client, store):
    run = waiting_on_scenarios(store)
    response = client.post(
        f"/runs/{run}/intake/answers", headers=bearer(ALICE), json={"pause_token": TOKEN, "answers": {"tone": "short"}}
    )
    assert response.status_code == 409 and not store.answers


# ---------------------------------------------------------------- scenarios


def test_scenario_review_approve_edit_reject(client, store):
    run = waiting_on_scenarios(store)
    response = client.post(
        f"/runs/{run}/intake/scenarios",
        headers=bearer(ALICE),
        json={
            "pause_token": TOKEN,
            "approved_ids": ["sc-1"],
            "rejected_ids": ["sc-2"],
            "edits": {"sc-1": {"title": "  A developer logs a customer's email address.  "}},
        },
    )
    assert response.status_code == 200 and IntakeAccepted.model_validate(response.json()).ok
    assert store.answers == [
        (
            run,
            {
                "approved_ids": ["sc-1"],
                "rejected_ids": ["sc-2"],
                "edits": {"sc-1": {"title": "A developer logs a customer's email address."}},
            },
            TOKEN,
        )
    ]
    assert store.rows[run][1].status == "queued"


def test_approving_nothing_is_allowed_but_the_list_is_required(client, store):
    run = waiting_on_scenarios(store)
    url = f"/runs/{run}/intake/scenarios"
    assert client.post(url, headers=bearer(ALICE), json={"pause_token": TOKEN}).status_code == 422
    assert client.post(url, headers=bearer(ALICE), json={"pause_token": TOKEN, "approved_ids": []}).status_code == 200


def test_unknown_scenario_ids_are_a_422_in_plain_english(client, store):
    run = waiting_on_scenarios(store)
    response = client.post(
        f"/runs/{run}/intake/scenarios", headers=bearer(ALICE), json={"pause_token": TOKEN, "approved_ids": ["sc-9"]}
    )
    assert response.status_code == 422 and "Unknown scenarios: sc-9" in response.json()["error"]["message"]
    assert not store.answers


@pytest.mark.parametrize(
    "body",
    [
        {"pause_token": TOKEN, "approved_ids": "sc-1"},
        {"pause_token": TOKEN, "approved_ids": [1]},
        {"pause_token": TOKEN, "approved_ids": ["sc-1"], "edits": {"sc-1": {"title": ""}}},
        {"pause_token": TOKEN, "approved_ids": ["sc-1"], "edits": {"sc-1": "new title"}},
    ],
)
def test_malformed_scenario_bodies_are_422s(client, store, body):
    run = waiting_on_scenarios(store)
    assert client.post(f"/runs/{run}/intake/scenarios", headers=bearer(ALICE), json=body).status_code == 422
    assert not store.answers


def test_scenario_review_with_a_stale_token_is_a_409(client, store):
    run = waiting_on_scenarios(store)
    response = client.post(
        f"/runs/{run}/intake/scenarios", headers=bearer(ALICE), json={"pause_token": "chk-0", "approved_ids": ["sc-1"]}
    )
    assert response.status_code == 409 and "please reload" in response.json()["error"]["message"]
    assert not store.answers


def test_scenario_review_of_a_run_that_moved_on_is_a_409(client, store):
    run = store.add_run(ALICE, "running")
    response = client.post(
        f"/runs/{run}/intake/scenarios", headers=bearer(ALICE), json={"pause_token": TOKEN, "approved_ids": ["sc-1"]}
    )
    assert response.status_code == 409
