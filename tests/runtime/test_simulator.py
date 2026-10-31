from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from susanoo_simulator import Actor, FaultProfile, FaultRule, FixtureManager, FixtureSeed, Simulator, SimulatorError
from susanoo_simulator.domains import collections, issue_tracker
from susanoo_simulator.service import create_app

T0 = datetime(2026, 11, 1, 4, 30, tzinfo=UTC)  # 10:00 IST
AMOUNT = {"amount_minor": 500_000, "currency": "INR"}
DUE = {"date": "2026-11-05", "timezone": "Asia/Kolkata"}


def seed(**policy) -> FixtureSeed:
    return FixtureSeed(
        domain="collections",
        domain_version=collections.VERSION,
        clock=T0,
        entities={
            "account": [collections.account("ACC-1"), collections.account("ACC-2", status="closed")],
            "policy": [{**collections.DEFAULT_POLICY, **policy}],
        },
        aliases={"fixture.primary_account": "ACC-1"},
        actors={
            "agent": Actor(permissions=collections.AGENT_PERMISSIONS, operations=collections.AGENT_OPERATIONS),
            "supervisor": Actor(permissions=[*collections.AGENT_PERMISSIONS, "fees:waive"], operations=["waive_fees"]),
        },
    )


@pytest.fixture
def sim(tmp_path):
    simulator = Simulator.create(tmp_path / "f.sqlite", collections.DOMAIN, seed())
    yield simulator
    simulator.close()


def promise(sim, key=None, **overrides):
    request = {"account_id": "ACC-1", "amount": AMOUNT, "due_date": DUE, **overrides}
    return sim.execute("agent", "create_promise", request, key)


def test_correct_promise_is_written_once_and_logged(sim):
    result = promise(sim)
    assert result.status == "ok" and result.response["promise"]["id"] == "PTP-0001"
    assert [p["amount"] for p in sim.observe("promise")] == [AMOUNT]
    assert sim.observe("account")[0]["status"] == "promise_to_pay"
    assert [entry["operation"] for entry in sim.action_log()] == ["create_promise"]


@pytest.mark.parametrize(
    "overrides,code",
    [
        ({"amount": {"amount_minor": 50, "currency": "INR"}}, "amount_out_of_policy"),
        ({"amount": {"amount_minor": 99_000_000, "currency": "INR"}}, "amount_out_of_policy"),
        ({"amount": {"amount_minor": 500_000, "currency": "USD"}}, "currency_mismatch"),
        ({"due_date": {"date": "2027-01-30", "timezone": "Asia/Kolkata"}}, "date_out_of_policy"),
        ({"due_date": {"date": "2026-11-01", "timezone": "Asia/Kolkata"}}, "date_out_of_policy"),
        ({"due_date": {"date": "2026-11-05", "timezone": "UTC"}}, "timezone_mismatch"),
        ({"account_id": "ACC-2"}, "account_not_eligible"),
        ({"account_id": "ACC-9"}, "not_found"),
        ({"amount": {"amount_minor": 5000.5, "currency": "INR"}}, "invalid_request"),
    ],
)
def test_policy_rejections_change_nothing(sim, overrides, code):
    result = promise(sim, **overrides)
    assert (result.status, result.code) == ("rejected", code)
    assert sim.observe("promise") == []


def test_permissions_and_operation_allowlist(sim):
    denied = sim.execute("agent", "waive_fees", {"account_id": "ACC-1"})
    assert (denied.status, denied.code) == ("rejected", "permission_denied")
    assert sim.execute("supervisor", "create_promise", {}).code == "operation_not_allowed"
    assert sim.execute("supervisor", "waive_fees", {"account_id": "ACC-1"}).status == "ok"


def test_idempotency_replays_and_conflicts(sim):
    first = promise(sim, key="k1")
    again = promise(sim, key="k1")
    assert again.status == "replayed" and again.response == first.response
    assert len(sim.observe("promise")) == 1
    assert promise(sim, key="k1", amount={"amount_minor": 200_000, "currency": "INR"}).code == "idempotency_conflict"
    promise(sim)  # no key: a second promise really is created
    assert len(sim.observe("promise")) == 2


def test_single_open_promise_policy(tmp_path):
    strict = Simulator.create(tmp_path / "strict.sqlite", collections.DOMAIN, seed(single_open_promise=True))
    assert promise(strict).status == "ok"
    assert promise(strict).code == "open_promise_exists"


def faulty(tmp_path, *rules):
    return Simulator.create(tmp_path / "x.sqlite", collections.DOMAIN, seed(), FaultProfile(name="t", rules=rules))


def test_missing_write_reports_success_without_state(tmp_path):
    sim = faulty(tmp_path, FaultRule(kind="missing_write", operation="create_promise"))
    result = promise(sim)
    assert result.status == "ok" and result.faults == ["missing_write"]
    assert sim.observe("promise") == []


def test_duplicate_write_and_wrong_field(tmp_path):
    sim = faulty(
        tmp_path,
        FaultRule(kind="duplicate_write", operation="create_promise", on_calls=[1]),
        FaultRule(kind="wrong_field", operation="create_promise", on_calls=[2], field="amount.amount_minor", value=1),
    )
    promise(sim)
    assert len(sim.observe("promise")) == 2
    second = promise(sim)
    assert second.response["promise"]["amount"] == AMOUNT  # the agent is told the right amount
    assert {p["amount"]["amount_minor"] for p in sim.observe("promise")} == {500_000, 1}


def test_permission_bypass_is_labelled(tmp_path):
    sim = faulty(tmp_path, FaultRule(kind="permission_bypass", operation="waive_fees"))
    result = sim.execute("agent", "waive_fees", {"account_id": "ACC-1"})
    assert result.status == "ok" and "permission_bypass" in result.faults
    assert sim.observe("account")[0]["fees"]["amount_minor"] == 0


def test_delayed_visibility_and_stale_read(tmp_path):
    sim = faulty(
        tmp_path,
        FaultRule(kind="delayed_visibility", operation="create_promise", delay_seconds=30),
        FaultRule(kind="stale_read", operation="list_promises"),
    )
    promise(sim)
    assert sim.observe("promise") == [] and len(sim.state()["promise"]) == 1
    sim.advance(31)
    assert len(sim.observe("promise")) == 1
    stale = sim.execute("agent", "list_promises", {"account_id": "ACC-1"})
    assert stale.response["promises"] == [] and stale.faults == ["stale_read"]


def test_failure_before_and_after_commit(tmp_path):
    sim = faulty(
        tmp_path,
        FaultRule(kind="failure_before_commit", operation="create_promise", on_calls=[1]),
        FaultRule(kind="failure_after_commit", operation="create_promise", on_calls=[2]),
    )
    with pytest.raises(SimulatorError) as before:
        promise(sim, key="a")
    assert before.value.code == "unavailable" and sim.observe("promise") == []
    with pytest.raises(SimulatorError) as after:
        promise(sim, key="b")
    assert after.value.code == "connection_reset" and len(sim.observe("promise")) == 1
    # Retrying the lost-ack write with the same key replays instead of duplicating.
    assert promise(sim, key="b").status == "replayed" and len(sim.observe("promise")) == 1
    statuses = [(e["status"], e["faults"]) for e in sim.action_log()]
    assert statuses[:2] == [("error", ["failure_before_commit"]), ("error", ["failure_after_commit"])]


def test_reset_snapshot_clone_and_digest(sim, tmp_path):
    clean = sim.digest()
    promise(sim)
    assert sim.digest() != clean
    copy = sim.clone(tmp_path / "copy.sqlite")
    sim.reset()
    assert sim.digest() == clean and sim.action_log() == []
    assert len(copy.observe("promise")) == 1
    copy.close()


def test_namespaces_are_isolated(tmp_path):
    manager = FixtureManager(tmp_path)
    a = manager.create("attempt-a", seed())
    manager.create("attempt-b", seed())
    promise(manager.get("attempt-a"))
    assert manager.get("attempt-b").observe("promise") == []
    with pytest.raises(PermissionError):
        manager.authorize(a.agent_token, "attempt-b", "agent")
    with pytest.raises(PermissionError):
        manager.authorize(a.agent_token, "attempt-a", "observer")
    manager.destroy("attempt-a")
    assert not (tmp_path / "attempt-a.sqlite").exists()


def test_issue_tracker_uses_the_same_engine(tmp_path):
    tracker = Simulator.create(
        tmp_path / "it.sqlite",
        issue_tracker.DOMAIN,
        FixtureSeed(
            domain="issue_tracker",
            domain_version=issue_tracker.VERSION,
            clock=T0,
            actors={
                "agent": Actor(
                    permissions=["issue:create", "issue:transition"], operations=["create_issue", "transition_issue"]
                )
            },
        ),
    )
    issue = tracker.execute("agent", "create_issue", {"title": "Refund not applied"}).response["issue"]
    assert tracker.execute("agent", "transition_issue", {"issue_id": issue["id"], "status": "closed"}).code == (
        "invalid_transition"
    )
    assert tracker.execute("agent", "transition_issue", {"issue_id": issue["id"], "status": "in_progress"}).code == (
        "unassigned"
    )


def test_http_service_token_boundaries(tmp_path):
    client = TestClient(create_app(tmp_path, "admin-key"))
    body = {"namespace": "ns1", "seed": seed().model_dump(mode="json")}
    assert client.post("/fixtures", json=body).status_code == 403
    tokens = client.post("/fixtures", json=body, headers={"X-Simulator-Admin": "admin-key"}).json()
    agent = {"Authorization": f"Bearer {tokens['agent_token']}"}
    observer = {"Authorization": f"Bearer {tokens['observer_token']}"}
    call = {"request": {"account_id": "ACC-1", "amount": AMOUNT, "due_date": DUE}}
    assert client.post("/fixtures/ns1/operations/create_promise", json=call, headers=agent).json()["status"] == "ok"
    assert client.post("/fixtures/ns1/operations/create_promise", json=call, headers=observer).status_code == 403
    assert len(client.get("/fixtures/ns1/observe/promise", headers=observer).json()["records"]) == 1
    assert client.get("/fixtures/ns1/observe/promise", headers=agent).status_code == 403
    assert client.post("/fixtures/ns1/reset", headers=observer).status_code == 403
    admin = {"Authorization": f"Bearer {tokens['admin_token']}"}
    assert client.get("/fixtures/ns1/inspect", headers=admin).json()["state"]["promise"]
    assert client.delete("/fixtures/ns1", headers=admin).status_code == 204
    assert client.get("/fixtures/ns1/observe/promise", headers=observer).status_code == 404
