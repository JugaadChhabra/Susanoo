"""Feedback: thumbs down + "it should have..." becomes a new test. [PRD › Non-technical users]"""

# ruff: noqa: F811  (fixtures imported from conftest_ai are used as test arguments)

import uuid

import pytest
from conftest_ai import ALICE, BOB, bearer, client, quota_store, store  # noqa: F401

from susanoo.schemas import AgentBundle, AnswerKey, ExpectedIssue, Plan, Rule, Scenario
from susanoo.translate import FeedbackRejected
from susanoo_api.routers import feedback as feedback_module
from susanoo_api.routers.feedback import FeedbackOut

DIFF = '--- a/app/db.py\n+++ b/app/db.py\n@@ -1,1 +1,2 @@\n x = 1\n+cur.execute("SELECT " + name)\n'
PLAN = Plan(
    product_name="Code Reviewer",
    summary="Reviews code changes.",
    rules=[Rule(id="no-raw-sql", text="Never build SQL by gluing text together.", default_severity="block")],
    intake_answers={},
    files_to_generate=[],
    acceptance_criteria=[],
)
BODY = {"thumbs_down": True, "expectation": "It should have flagged the raw SQL.", "diff": DIFF, "findings": []}


def planned_run(store, owner=ALICE, **cols) -> str:
    run = store.add_run(owner, "done", **cols)
    store.states[run] = {
        "plan": PLAN,
        "agent": AgentBundle(prompt_path="p", policy_path="q", tools_path="t", model="m"),
    }
    return run


def post(client, run, user=ALICE, **overrides):
    return client.post(f"/runs/{run}/feedback", headers=bearer(user), json={**BODY, **overrides})


def make_scenario(sid: str) -> Scenario:
    return Scenario(
        id=sid, kind="planted", split="train", title="The agent missed a query built from user input.", diff=DIFF,
        answer_key=AnswerKey(
            expected=[ExpectedIssue(file="app/db.py", line_start=2, line_end=2, rule_id="no-raw-sql", severity="block")]
        ),
    )  # fmt: skip


def test_feedback_needs_auth(client):
    assert client.post(f"/runs/{uuid.uuid4()}/feedback", json=BODY).status_code == 401


def test_someone_elses_run_is_a_404_and_nothing_is_written(client, store):
    run = planned_run(store, owner=BOB)
    response = post(client, run)
    assert response.status_code == 404 and response.json()["error"]["code"] == "not_found"
    assert not store.saved and not store.costs


def test_a_good_thumbs_down_becomes_a_train_scenario_and_is_saved(client, store, monkeypatch):
    seen = {}

    async def fake(diff, findings, thumbs_down, expectation, rules, *, scenario_id, state, on_cost):
        seen.update(
            diff=diff, thumbs_down=thumbs_down, expectation=expectation, rules=[r.id for r in rules], state=state
        )
        on_cost(0.004)
        return make_scenario(scenario_id)

    monkeypatch.setattr(feedback_module, "feedback_to_scenario", fake)
    run = planned_run(store, cost_usd=0.25)
    response = post(client, run)
    assert response.status_code == 200
    body = FeedbackOut.model_validate(response.json())
    assert body.message == "Saved as a new check for this agent. It will be used from the next round."
    assert body.accepted and body.code is None and body.title == "The agent missed a query built from user input."
    assert seen == {
        "diff": DIFF, "thumbs_down": True, "expectation": "It should have flagged the raw SQL.",
        "rules": ["no-raw-sql"], "state": {"cost_usd": 0.25, "cost_cap_usd": 2.0},
    }  # fmt: skip
    [(saved_run, scenario)] = store.saved
    assert saved_run == run and scenario.id == body.scenario_id and scenario.split == "train"
    assert not scenario.approved_by_user  # feedback never joins the locked acceptance set
    assert uuid.UUID(scenario.id)
    assert store.costs == [(run, 0.004)]


def test_a_rejection_is_a_friendly_200_that_saves_nothing(client, store, monkeypatch):
    async def fake(*args, scenario_id, state, on_cost, **kwargs):
        on_cost(0.001)
        return FeedbackRejected(code="unclear", reason="Which part of the change should it have flagged?")

    monkeypatch.setattr(feedback_module, "feedback_to_scenario", fake)
    run = planned_run(store)
    body = FeedbackOut.model_validate(post(client, run).json())
    assert not body.accepted and body.code == "unclear" and body.message.startswith("Which part")
    assert body.scenario_id is None and not store.saved
    assert store.costs == [(run, 0.001)]  # even a rejected try cost something


def test_a_thumbs_up_needs_no_test_and_costs_nothing(client, store):
    run = planned_run(store)
    body = FeedbackOut.model_validate(post(client, run, thumbs_down=False).json())
    assert not body.accepted and body.code == "thumbs_up" and not store.saved and not store.costs


def test_an_empty_expectation_is_asked_for_again(client, store):
    body = FeedbackOut.model_validate(post(client, planned_run(store), expectation="   ").json())
    assert not body.accepted and body.code == "no_expectation" and "It should have" in body.message


def test_a_spent_budget_is_a_plain_rejection_without_a_model_call(client, store):
    run = planned_run(store, cost_usd=2.0, cost_cap_usd=2.0)
    body = FeedbackOut.model_validate(post(client, run).json())
    assert not body.accepted and body.code == "budget" and "budget" in body.message
    assert not store.saved


def test_feedback_before_the_plan_exists_is_a_plain_409(client, store):
    run = store.add_run(ALICE, "running")
    response = post(client, run)
    assert response.status_code == 409 and "isn't planned yet" in response.json()["error"]["message"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"thumbs_down": "sure"},
        {"thumbs_down": 1},
        {"thumbs_down": "true"},
        {"expectation": "x" * 2001},
        {"diff": "x" * 50_001},
        {"findings": [{"file": "a.py"}]},
        {"diff": None},
    ],
)
def test_malformed_feedback_is_a_422(client, store, overrides):
    response = post(client, planned_run(store), **overrides)
    assert response.status_code == 422 and response.json()["error"]["code"] == "validation_error"
    assert not store.saved


def test_feedback_respects_the_global_daily_cap_without_a_model_call(client, store, quota_store, monkeypatch):
    async def never(*args, **kwargs):
        raise AssertionError("the model must not be called while the platform is at its daily cap")

    monkeypatch.setattr(feedback_module, "feedback_to_scenario", never)
    quota_store.spend_usd = 50.0
    run = planned_run(store)
    body = FeedbackOut.model_validate(post(client, run).json())
    assert not body.accepted and body.code == "budget" and "spending limit" in body.message
    assert not store.saved and not store.costs
