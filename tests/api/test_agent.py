"""Agent card and report card, in plain English. [PRD › Non-technical users › Extra API]"""

# ruff: noqa: F811  (fixtures imported from conftest_ai are used as test arguments)

import json
import uuid
from pathlib import Path

import pytest
from conftest_ai import ALICE, BOB, bearer, client, quota_store, store  # noqa: F401

from susanoo.schemas import AgentBundle, IterationMetrics, Plan, Rule, Scenario, ScenarioResult
from susanoo.translate import AgentCard
from susanoo_api.routers import agent as agent_module
from susanoo_api.routers.agent import ReportCards

FIXTURES = Path(__file__).parent.parent / "fixtures"
SCENARIOS = [Scenario(**s) for s in json.loads((FIXTURES / "scenarios.json").read_text())]
METRICS = [IterationMetrics(**m) for m in json.loads((FIXTURES / "iteration_metrics.json").read_text())]
PLAN = Plan(
    product_name="Code Reviewer",
    summary="Reviews code changes and flags the ones that break your rules.",
    rules=[Rule(id="no-raw-sql", text="Never build SQL by gluing text together.", default_severity="block")],
    intake_answers={},
    files_to_generate=["agent/prompt.md"],
    acceptance_criteria=[],
)
CARD = AgentCard(
    what_it_does="It reads code changes and flags risky ones.",
    can_do=["Flags raw SQL."],
    wont_do=["It does not change itself."],
)


@pytest.fixture(autouse=True)
def fresh_cache():
    agent_module._cards.clear()
    yield
    agent_module._cards.clear()


def result(sid: str, iteration: int, *, matched=0, missed=0, fp=0, passed=False) -> ScenarioResult:
    return ScenarioResult(
        scenario_id=sid, iteration=iteration, findings=[], matched=matched, missed=missed, false_positives=fp,
        severity_correct=matched, passed=passed, trace_id="t",
    )  # fmt: skip


def built_run(store, owner=ALICE, status="running", **cols) -> str:
    run = store.add_run(owner, status, **cols)
    store.states[run] = {
        "plan": PLAN,
        "scenarios": SCENARIOS,
        "metrics": METRICS[:2],
        "results": [
            result("sc-planted-raw-sql", 0, missed=1),
            result("sc-clean-rename", 0, matched=0, missed=0, fp=1),
            result("sc-adversarial-comment", 0, missed=1),
            result("sc-planted-raw-sql", 1, matched=1, passed=True),
            result("sc-clean-rename", 1, passed=True),
            result("sc-adversarial-comment", 1, missed=1),
        ],
        "agent": AgentBundle(prompt_path="agent/prompt.md", policy_path="p", tools_path="t", model="m"),
    }
    return run


@pytest.mark.parametrize("suffix", ["agent-card", "report-card"])
def test_agent_routes_need_auth(client, suffix):
    assert client.get(f"/runs/{uuid.uuid4()}/{suffix}").status_code == 401


@pytest.mark.parametrize("suffix", ["agent-card", "report-card"])
def test_someone_elses_run_is_a_404(client, store, suffix):
    run = built_run(store, owner=BOB)
    response = client.get(f"/runs/{run}/{suffix}", headers=bearer(ALICE))
    assert response.status_code == 404 and response.json()["error"]["code"] == "not_found"
    assert client.get(f"/runs/nope/{suffix}", headers=bearer(ALICE)).status_code == 404


# ---------------------------------------------------------------- agent card


def test_agent_card_happy_path_pays_once_and_is_cached(client, store, monkeypatch):
    calls = []

    async def fake_card(plan, prompt, rules, connectors, *, state, on_cost):
        calls.append((plan.product_name, [r.id for r in rules], state))
        on_cost(0.002)
        return CARD

    monkeypatch.setattr(agent_module, "agent_card", fake_card)
    run = built_run(store, cost_usd=0.5)
    first = client.get(f"/runs/{run}/agent-card", headers=bearer(ALICE))
    assert first.status_code == 200 and AgentCard.model_validate(first.json()) == CARD
    assert client.get(f"/runs/{run}/agent-card", headers=bearer(ALICE)).json() == first.json()
    assert calls == [("Code Reviewer", ["no-raw-sql"], {"cost_usd": 0.5, "cost_cap_usd": 2.0})]  # second hit is cached
    assert store.costs == [(run, 0.002)]  # the model call's cost lands on the run


def test_agent_card_at_the_global_daily_cap_is_the_plain_card_and_not_cached(client, store, quota_store, monkeypatch):
    async def never(*args, **kwargs):
        raise AssertionError("the model must not be called while the platform is at its daily cap")

    monkeypatch.setattr(agent_module, "agent_card", never)
    quota_store.spend_usd = 50.0
    run = built_run(store)
    response = client.get(f"/runs/{run}/agent-card", headers=bearer(ALICE))
    assert response.status_code == 200 and AgentCard.model_validate(response.json()).can_do
    assert not store.costs and not agent_module._cards  # nothing cached, so the real card comes once spending resumes


def test_agent_card_before_the_plan_exists_is_a_plain_409(client, store):
    run = store.add_run(ALICE, "running")
    response = client.get(f"/runs/{run}/agent-card", headers=bearer(ALICE))
    assert response.status_code == 409 and "isn't planned yet" in response.json()["error"]["message"]


def test_agent_card_over_budget_uses_the_deterministic_card_without_a_model(client, store):
    run = built_run(store, cost_usd=2.0, cost_cap_usd=2.0)  # spent: translate falls back before any model call
    response = client.get(f"/runs/{run}/agent-card", headers=bearer(ALICE))
    assert response.status_code == 200
    card = AgentCard.model_validate(response.json())
    assert "no-raw-sql" not in json.dumps(response.json())  # plain sentences, never rule ids
    assert any("SQL" in line for line in card.can_do)
    assert store.costs == []


def test_agent_card_reads_the_prompt_only_from_inside_the_repo(client, store, monkeypatch, tmp_path):
    seen = []

    async def fake_card(plan, prompt, rules, connectors, *, state, on_cost):
        seen.append(prompt)
        return CARD

    monkeypatch.setattr(agent_module, "agent_card", fake_card)
    repo = tmp_path / "repo"
    (repo / "agent").mkdir(parents=True)
    (repo / "agent" / "prompt.md").write_text("Be kind.")
    run = built_run(store)
    store.states[run]["repo_path"] = str(repo)
    assert client.get(f"/runs/{run}/agent-card", headers=bearer(ALICE)).status_code == 200
    assert seen == ["Be kind."]

    (tmp_path / "secret.txt").write_text("outside")
    other = built_run(store)
    store.states[other]["repo_path"] = str(repo)
    store.states[other]["agent"] = AgentBundle(prompt_path="../secret.txt", policy_path="p", tools_path="t", model="m")
    assert client.get(f"/runs/{other}/agent-card", headers=bearer(ALICE)).status_code == 200
    assert seen[-1] == ""  # a path that escapes the repo is never read


def test_agent_card_when_the_checkpoint_is_unreadable_is_a_plain_503(client, store):
    run = built_run(store)
    store.broken = True  # get_run fails first
    assert client.get(f"/runs/{run}/agent-card", headers=bearer(ALICE)).status_code == 503


# ---------------------------------------------------------------- report card


def test_report_card_gives_plain_sentences_per_iteration(client, store):
    run = built_run(store)
    response = client.get(f"/runs/{run}/report-card", headers=bearer(ALICE))
    assert response.status_code == 200
    body = ReportCards.model_validate(response.json())
    assert [c.iteration for c in body.history] == [0, 1] and body.latest == body.history[-1]
    assert body.message == body.latest.headline and body.latest.headline
    assert body.latest.comparison is not None and body.latest.comparison.startswith("Before:")
    assert body.history[0].comparison is None


def test_report_card_carries_numbers_only_never_scenario_content(client, store):
    run = built_run(store)
    text = client.get(f"/runs/{run}/report-card", headers=bearer(ALICE)).text
    for scenario in SCENARIOS:
        assert scenario.diff not in text and scenario.title not in text and scenario.id not in text
    assert "answer_key" not in text and "SELECT" not in text


def test_report_card_before_any_scoring_is_an_empty_but_friendly_200(client, store):
    run = store.add_run(ALICE, "running")
    body = ReportCards.model_validate(client.get(f"/runs/{run}/report-card", headers=bearer(ALICE)).json())
    assert body.latest is None and body.history == [] and "No examples have been scored yet" in body.message


def test_report_card_with_unreadable_results_is_a_plain_503(client, store):
    run = built_run(store)
    store.states[run]["results"] = [{"not": "a result"}]
    response = client.get(f"/runs/{run}/report-card", headers=bearer(ALICE))
    assert response.status_code == 503 and "try again" in response.json()["error"]["message"]
