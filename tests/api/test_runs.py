"""Runs: start (quota, enqueue), status, cancel. [PRD › Frontend and API], [PRD › Tech stack] (Postgres job queue)"""

# ruff: noqa: F811  (fixtures imported from conftest_ai are used as test arguments)

import asyncio
import json
import uuid
from pathlib import Path

import pytest
from conftest_ai import ALICE, BOB, SECRET, FakeRunStore, bearer, client, pg, pg_server, quota_store, store  # noqa: F401

from susanoo.schemas import IterationMetrics
from susanoo_api import main
from susanoo_api.routers import runs as runs_module
from susanoo_api.routers.runs import PostgresRunStore, QuotaRefused, RunRow, RunStatus, _TransactionQuotaStore

METRICS = json.loads((Path(__file__).parent.parent / "fixtures" / "iteration_metrics.json").read_text())
MISSING = str(uuid.uuid4())


# ---------------------------------------------------------------- auth


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", f"/projects/{MISSING}/runs"),
        ("get", f"/runs/{MISSING}"),
        ("post", f"/runs/{MISSING}/cancel"),
    ],
)
def test_every_runs_route_needs_auth(client, method, path):
    response = client.request(method, path)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


# ---------------------------------------------------------------- start


def test_start_a_run_enqueues_it_for_the_worker(client, store):
    project = store.add_project(ALICE)
    response = client.post(f"/projects/{project}/runs", headers=bearer(ALICE))
    assert response.status_code == 201
    run = RunStatus.model_validate(response.json())
    assert run.status == "queued" and run.project_id == project
    assert run.cost_usd == 0 and run.cost_cap_usd == 2.0  # the per-run cap, from RUN_COST_CAP_USD or its default
    assert "worker" in run.message
    assert store.rows[run.id][0] == ALICE and store.rows[run.id][1].status == "queued"


def test_start_uses_the_configured_cost_cap(client, store, monkeypatch):
    monkeypatch.setenv("RUN_COST_CAP_USD", "1.5")
    project = store.add_project(ALICE)
    assert client.post(f"/projects/{project}/runs", headers=bearer(ALICE)).json()["cost_cap_usd"] == 1.5


def test_start_on_someone_elses_project_is_a_404(client, store):
    project = store.add_project(BOB)
    response = client.post(f"/projects/{project}/runs", headers=bearer(ALICE))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert not store.rows


def test_start_on_a_malformed_project_id_is_a_404(client):
    assert client.post("/projects/not-a-uuid/runs", headers=bearer(ALICE)).status_code == 404


def test_fourth_run_of_the_day_is_a_plain_english_429(client, store, quota_store):
    project = store.add_project(ALICE)
    quota_store.runs_today[ALICE] = 3
    response = client.post(f"/projects/{project}/runs", headers=bearer(ALICE))
    assert response.status_code == 429
    error = response.json()["error"]
    assert error["code"] == "rate_limited" and "3 runs" in error["message"] and "{" not in error["message"]
    assert not store.rows  # refused before anything was enqueued


def test_start_is_refused_while_the_global_cap_is_reached(client, store, quota_store):
    project = store.add_project(BOB)
    quota_store.spend_usd = 50.0
    response = client.post(f"/projects/{project}/runs", headers=bearer(BOB))
    assert response.status_code == 429 and "paused" in response.json()["error"]["message"]
    assert not store.rows


def test_start_that_loses_the_quota_race_is_the_same_plain_english_429(client, store):
    project = store.add_project(ALICE)
    store.refuse_start = "You've used all 3 runs for today."  # the pre-check passed; the in-transaction recheck did not
    response = client.post(f"/projects/{project}/runs", headers=bearer(ALICE))
    assert response.status_code == 429
    assert response.json()["error"] == {"code": "rate_limited", "message": "You've used all 3 runs for today."}


def test_start_with_a_broken_cost_cap_setting_refuses_plainly(client, store, monkeypatch):
    monkeypatch.setenv("RUN_COST_CAP_USD", "lots")
    project = store.add_project(ALICE)
    response = client.post(f"/projects/{project}/runs", headers=bearer(ALICE))
    assert response.status_code == 503 and "switched off" in response.json()["error"]["message"]


def test_start_when_the_database_is_down_is_a_plain_503(client, store):
    project = store.add_project(ALICE)
    store.broken = True
    response = client.post(f"/projects/{project}/runs", headers=bearer(ALICE))
    assert response.status_code == 503 and "try again" in response.json()["error"]["message"]


# ---------------------------------------------------------------- status


def test_get_run_happy_path_with_metrics(client, store):
    run = store.add_run(ALICE, "running", cost_usd=0.83, preview_url="https://run-1.example.com")
    store.states[run] = {"metrics": [IterationMetrics(**m) for m in reversed(METRICS)]}
    response = client.get(f"/runs/{run}", headers=bearer(ALICE))
    assert response.status_code == 200
    body = RunStatus.model_validate(response.json())
    assert body.status == "running" and body.preview_url == "https://run-1.example.com"
    assert [m.iteration for m in body.metrics] == sorted(m["iteration"] for m in METRICS)  # oldest first
    assert body.cost_usd == 0.83


def test_get_run_does_not_leak_internals(client, store):
    run = store.add_run(ALICE, "awaiting_approval")
    store.pause(run, "questions", {"questions": []})
    text = client.get(f"/runs/{run}", headers=bearer(ALICE)).text
    for hidden in ("resume_value", "claimed_by", "paused_checkpoint_id", "pause_token", "heartbeat"):
        assert hidden not in text


def test_a_failed_run_says_why_in_plain_english(client, store):
    run = store.add_run(ALICE, "failed", error="The run reached its cost limit before the first test finished.")
    assert "cost limit" in client.get(f"/runs/{run}", headers=bearer(ALICE)).json()["message"]


def test_get_someone_elses_run_is_a_404(client, store):
    run = store.add_run(BOB)
    response = client.get(f"/runs/{run}", headers=bearer(ALICE))
    assert response.status_code == 404 and response.json()["error"]["code"] == "not_found"


def test_get_unknown_and_malformed_run_ids_are_404s(client):
    assert client.get(f"/runs/{MISSING}", headers=bearer(ALICE)).status_code == 404
    assert client.get("/runs/nope", headers=bearer(ALICE)).status_code == 404


def test_get_run_still_answers_when_the_checkpoint_cannot_be_read(client, store, monkeypatch):
    run = store.add_run(ALICE, "running")

    async def boom(run_id):
        raise RuntimeError("no checkpoint")

    monkeypatch.setattr(store, "load_state", boom)
    response = client.get(f"/runs/{run}", headers=bearer(ALICE))
    assert response.status_code == 200 and response.json()["metrics"] == []


# ---------------------------------------------------------------- cancel


def test_cancel_asks_the_worker_to_stop_the_run(client, store):
    run = store.add_run(ALICE, "running")
    response = client.post(f"/runs/{run}/cancel", headers=bearer(ALICE))
    assert response.status_code == 200
    body = RunStatus.model_validate(response.json())
    assert body.cancel_requested and "Cancelling" in body.message
    assert store.rows[run][1].cancel_requested


def test_cancel_someone_elses_run_is_a_404_and_changes_nothing(client, store):
    run = store.add_run(BOB, "running")
    assert client.post(f"/runs/{run}/cancel", headers=bearer(ALICE)).status_code == 404
    assert not store.rows[run][1].cancel_requested


@pytest.mark.parametrize("status", ["done", "failed", "cancelled"])
def test_cancel_a_finished_run_is_a_409(client, store, status):
    run = store.add_run(ALICE, status)
    response = client.post(f"/runs/{run}/cancel", headers=bearer(ALICE))
    assert response.status_code == 409 and "already finished" in response.json()["error"]["message"]


# ---------------------------------------------------------------- the real SQL (throwaway Postgres; needs Docker)


def _user_project(pg, status: str | None = None, **cols):
    uid = uuid.uuid4()
    pg.execute("insert into auth.users (id, email) values (%s, %s)", (uid, f"{uid}@example.com"))
    pid = pg.execute(
        "insert into public.projects (user_id, name, spec) values (%s, 'p', 's') returning id", (uid,)
    ).fetchone()["id"]
    if status is None:
        return str(uid), str(pid), None
    extra = "".join(f", {name}" for name in cols)
    marks = "".join(", %s" for _ in cols)
    rid = pg.execute(
        f"insert into public.runs (project_id, status{extra}) values (%s, %s{marks}) returning id",
        (pid, status, *cols.values()),
    ).fetchone()["id"]
    return str(uid), str(pid), str(rid)


async def test_sql_create_get_and_cancel_are_scoped_to_the_owner(pg):
    store = PostgresRunStore()
    alice, project, _ = _user_project(pg)
    bob, _, _ = _user_project(pg)

    assert await store.create_run(bob, project, 2.0) is None  # not Bob's project
    run = await store.create_run(alice, project, 1.25)
    assert run is not None and run.status == "queued" and run.cost_cap_usd == 1.25 and not run.cancel_requested
    assert await store.get_run(bob, run.id) is None
    assert (await store.get_run(alice, run.id)).id == run.id

    assert await store.request_cancel(bob, run.id) is None
    assert (
        pg.execute("select cancel_requested from public.runs where id = %s", (run.id,)).fetchone()["cancel_requested"]
        is False
    )
    cancelled = await store.request_cancel(alice, run.id)
    assert cancelled is not None and cancelled.cancel_requested and cancelled.status == "queued"


async def test_sql_cancel_leaves_a_finished_run_alone(pg):
    store = PostgresRunStore()
    alice, _, run = _user_project(pg, "done")
    row = await store.request_cancel(alice, run)
    assert row is not None and row.status == "done" and not row.cancel_requested


async def test_sql_events_resume_by_seq_and_are_scoped(pg):
    from susanoo.db import emit_event

    store = PostgresRunStore()
    alice, _, run = _user_project(pg, "running")
    bob, _, _ = _user_project(pg)
    for i in range(3):
        await emit_event(run, "planner", "progress", f"step {i}")
    batch = await store.poll_events(alice, run, 0, 200)
    assert batch is not None and batch.status == "running" and [e.seq for e in batch.events] == [1, 2, 3]
    assert [e.seq for e in (await store.poll_events(alice, run, 1, 200)).events] == [2, 3]
    assert [e.seq for e in (await store.poll_events(alice, run, 0, 2)).events] == [1, 2]
    assert await store.poll_events(bob, run, 0, 200) is None


async def test_sql_pending_and_answer_are_bound_to_the_pause_token(pg):
    from susanoo.db import emit_event

    store = PostgresRunStore()
    alice, _, run = _user_project(pg, "awaiting_approval", paused_checkpoint_id="chk-1")
    bob, _, _ = _user_project(pg)
    payload = {"kind": "questions", "questions": [{"id": "q1", "text": "Block or allow?"}]}
    await emit_event(run, "planner", "question_needed", "Please answer.", payload)

    pending = await store.pending(alice, run)
    assert pending is not None and pending.waiting and pending.kind == "questions"
    assert pending.pause_token == "chk-1" and pending.payload == payload
    assert await store.pending(bob, run) is None

    value = {"answers": {"q1": "block"}}
    assert not await store.store_answer(alice, run, value, "chk-0")  # a stale token stores nothing
    assert not await store.store_answer(bob, run, value, "chk-1")  # someone else's run stores nothing
    row = pg.execute("select status, resume_value from public.runs where id = %s", (run,)).fetchone()
    assert row["status"] == "awaiting_approval" and row["resume_value"] is None

    assert await store.store_answer(alice, run, value, "chk-1")
    row = pg.execute("select status, resume_value from public.runs where id = %s", (run,)).fetchone()
    assert row["status"] == "queued" and row["resume_value"] == value
    assert not await store.store_answer(alice, run, value, "chk-1")  # a double click is refused
    assert (await store.pending(alice, run)).waiting is False


async def test_sql_cost_and_feedback_scenario(pg):
    from susanoo.schemas import AnswerKey, Scenario

    store = PostgresRunStore()
    alice, _, run = _user_project(pg, "done", cost_usd=0.5)
    await store.add_cost(run, 0.0123)
    assert float(
        pg.execute("select cost_usd from public.runs where id = %s", (run,)).fetchone()["cost_usd"]
    ) == pytest.approx(0.5123)

    scenario = Scenario(
        id=str(uuid.uuid4()), kind="clean", split="train", title="A rename changes nothing", diff="+x = 1\n",
        answer_key=AnswerKey(expected=[]),
    )  # fmt: skip
    await store.save_feedback_scenario(run, scenario)
    row = pg.execute("select split, approved_by_user, title from public.scenarios where run_id = %s", (run,)).fetchone()
    assert row == {"split": "train", "approved_by_user": False, "title": "A rename changes nothing"}
    event = pg.execute("select head, type, message from public.run_events where run_id = %s", (run,)).fetchone()
    assert event["type"] == "progress" and event["message"] == "Saved as a new check for this agent."
    assert alice  # the owner exists; the scenario belongs to their run


# ---------------------------------------------------------------- the quota race (real concurrent connections)


def _runs_today(pg, user_id: str) -> int:
    return pg.execute(
        "select count(*) n from public.runs r join public.projects p on p.id = r.project_id where p.user_id = %s",
        (user_id,),
    ).fetchone()["n"]


async def test_sql_concurrent_starts_at_two_of_three_let_exactly_one_in(pg):
    store = PostgresRunStore()
    alice, project, _ = _user_project(pg)
    for _ in range(2):
        pg.execute("insert into public.runs (project_id, status) values (%s, 'done')", (project,))

    results = await asyncio.gather(*[store.create_run(alice, project, 2.0) for _ in range(10)])
    assert sum(isinstance(r, RunRow) for r in results) == 1
    refused = [r for r in results if isinstance(r, QuotaRefused)]
    assert len(refused) == 9 and all("all 3 runs" in r.message for r in refused)
    assert _runs_today(pg, alice) == 3


async def test_http_concurrent_posts_at_two_of_three_give_one_201_and_nine_429s(pg, monkeypatch):
    """The reviewer's repro through the whole app: Jugaad's real quota dependency plus the real store."""
    import httpx

    monkeypatch.setenv("SUPABASE_JWT_SECRET", SECRET)
    app = main.create_app()
    app.include_router(runs_module.router)
    alice, project, _ = _user_project(pg)
    for _ in range(2):
        pg.execute("insert into public.runs (project_id, status) values (%s, 'done')", (project,))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        responses = await asyncio.gather(
            *[http.post(f"/projects/{project}/runs", headers=bearer(alice)) for _ in range(10)]
        )
    assert sorted(r.status_code for r in responses) == [201] + [429] * 9
    assert _runs_today(pg, alice) == 3


async def test_sql_one_users_runs_do_not_use_up_another_users_quota(pg):
    store = PostgresRunStore()
    alice, a_project, _ = _user_project(pg)
    bob, b_project, _ = _user_project(pg)
    for _ in range(3):
        assert isinstance(await store.create_run(alice, a_project, 2.0), RunRow)
    assert isinstance(await store.create_run(alice, a_project, 2.0), QuotaRefused)
    assert isinstance(await store.create_run(bob, b_project, 2.0), RunRow)


async def test_sql_the_global_daily_cap_is_respected_inside_the_transaction(pg, monkeypatch):
    store = PostgresRunStore()
    monkeypatch.setenv("GLOBAL_DAILY_CAP_USD", "50")
    alice, project, _ = _user_project(pg)
    _user_project(pg, "running", cost_cap_usd=50)  # someone else's in-flight run counts at its cap: 50 >= 50
    refused = await store.create_run(alice, project, 2.0)
    assert isinstance(refused, QuotaRefused) and "spending limit" in refused.message
    assert _runs_today(pg, alice) == 0
    monkeypatch.setenv("GLOBAL_DAILY_CAP_USD", "51")
    assert isinstance(await store.create_run(alice, project, 2.0), RunRow)


async def test_sql_a_refused_start_inserts_nothing_and_unknown_project_is_still_none(pg):
    store = PostgresRunStore()
    alice, project, _ = _user_project(pg)
    bob, _, _ = _user_project(pg)
    assert await store.create_run(bob, project, 2.0) is None  # under quota, but not Bob's project
    assert _runs_today(pg, alice) == 0


async def test_sql_the_in_transaction_counts_match_jugaads_quota_store(pg):
    """The recheck copies quota.PostgresQuotaStore's query so it can run on our connection; this keeps them equal."""
    from datetime import UTC, datetime

    from susanoo.db import session
    from susanoo_api import quota

    alice, project, _ = _user_project(pg)
    bob, bob_project, _ = _user_project(pg)
    for status, cost in [("done", 0.4), ("running", 0.1), ("awaiting_approval", 0.0), ("queued", 0.0), ("failed", 1.5)]:
        pg.execute(
            "insert into public.runs (project_id, status, cost_usd, cost_cap_usd) values (%s, %s, %s, 2)",
            (project, status, cost),
        )
    pg.execute("insert into public.runs (project_id, status, cost_usd) values (%s, 'done', 0.7)", (bob_project,))
    pg.execute(
        "insert into public.runs (project_id, status, created_at) values (%s, 'done', now() - interval '3 days')",
        (project,),
    )
    since = quota._start_of_day(datetime.now(UTC))
    for user in (alice, bob):
        theirs = await quota.PostgresQuotaStore().usage_since(user, since)
        async with session() as conn:
            ours = await _TransactionQuotaStore(conn).usage_since(user, since)
        assert ours == theirs


async def test_sql_concurrent_cost_updates_add_up(pg):
    store = PostgresRunStore()
    _, _, run = _user_project(pg, "running", cost_usd=1)
    await asyncio.gather(*[store.add_cost(run, 0.01) for _ in range(20)])
    cost = pg.execute("select cost_usd from public.runs where id = %s", (run,)).fetchone()["cost_usd"]
    assert float(cost) == pytest.approx(1.2)  # an in-database `cost_usd + x`, never read-then-write
