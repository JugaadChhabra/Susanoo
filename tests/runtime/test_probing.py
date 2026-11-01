"""T36: probe budget holds under concurrent claims; baseline and priority use equal-budget accounting [Spec §21]."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from susanoo_runner.journal import Journal
from susanoo_runner.probing import (
    ProbeCandidate,
    ProbeOutcome,
    account,
    compare,
    plan_probes,
    run_probes,
    score_inputs,
)

CHECKER = "checker-sha256:abc123"
SEVERITIES = ["critical", "high", "medium", "low"]


def pool(n: int = 12) -> list[ProbeCandidate]:
    return [
        ProbeCandidate(
            id=f"P{i:02d}",
            transition=f"op{i % 4}:state{i % 3}",
            severity=SEVERITIES[i % 4],
            times_tested=i % 3,
            recently_changed=i % 5 == 0,
            uncertainty=(i % 10) / 10,
            expected_cost=1 + i % 3,
            max_retries=i % 2,
        )
        for i in range(n)
    ]


# Ground truth lives in the probe, the same for every strategy: critical/high untested probes find defects.
DEFECTIVE = {c.id for c in pool() if c.severity in {"critical", "high"} and c.times_tested == 0}


def probe(candidate: ProbeCandidate, attempt: int) -> ProbeOutcome:
    if candidate.id == "P05" and attempt == 0:
        return ProbeOutcome(status="error", detail="reference timeout")
    if candidate.id == "P07":
        return ProbeOutcome(status="unknown", detail="reference returned ambiguous state")
    if candidate.id in DEFECTIVE:
        return ProbeOutcome(status="finding", finding_id=f"F-{candidate.id}")
    return ProbeOutcome(status="no_finding")


@pytest.fixture
def journal(tmp_path):
    j = Journal(tmp_path / "journal.sqlite")
    yield j
    j.close()


def test_plans_are_deterministic_for_a_seed_and_persist_every_input():
    a = plan_probes(pool(), strategy="priority", seed=7, budget_cap=20, checker_digest=CHECKER)
    b = plan_probes(list(reversed(pool())), strategy="priority", seed=7, budget_cap=20, checker_digest=CHECKER)
    assert a == b and a.plan_id == b.plan_id
    assert len(a.candidate_pool) == 12 and len(a.score_inputs) == 12 and a.seed == 7
    assert a.exploration_fraction == 0.2 and set(a.selected) == {c.id for c in pool()}
    assert set(a.exploration_picks) <= set(a.selected)
    uniform = plan_probes(pool(), strategy="uniform", seed=7, budget_cap=20, checker_digest=CHECKER)
    assert uniform.score_inputs == [] and uniform.exploration_fraction == 0.0
    assert (
        uniform.selected
        == plan_probes(pool(), strategy="uniform", seed=7, budget_cap=20, checker_digest=CHECKER).selected
    )
    assert (
        uniform.selected
        != plan_probes(pool(), strategy="uniform", seed=8, budget_cap=20, checker_digest=CHECKER).selected
    )


def test_priority_score_is_transparent():
    critical = ProbeCandidate(id="C", transition="t", severity="critical", recently_changed=True, uncertainty=1.0)
    cheap_low = ProbeCandidate(id="L", transition="t", severity="low", times_tested=3, uncertainty=0.0)
    s = score_inputs(critical, {"severity": 3.0, "untested": 2.0, "recent_change": 1.5, "uncertainty": 1.0})
    assert (s.severity, s.untested, s.recent_change, s.uncertainty) == (1.0, 1.0, 1.0, 1.0) and s.score == 7.5
    pure = plan_probes(
        [critical, cheap_low],
        strategy="priority",
        seed=1,
        budget_cap=5,
        checker_digest=CHECKER,
        exploration_fraction=0.0,
    )
    assert pure.selected == ["C", "L"] and pure.exploration_picks == []
    costly = critical.model_copy(update={"expected_cost": 100})
    assert score_inputs(costly, pure.weights).score < score_inputs(cheap_low, pure.weights).score


def test_reservation_covers_retries_and_cleanup_and_stops_before_cap(journal):
    plan = plan_probes(pool(), strategy="priority", seed=3, budget_cap=15, checker_digest=CHECKER, cleanup_allowance=1)
    run = run_probes(plan, probe, journal=journal, scope="ref:staging", cleanup=lambda c: 5)
    assert run.stopped_reason == "budget_exhausted" and run.not_run
    assert run.budget_reserved <= 15 and run.remaining_budget == 15 - run.budget_reserved
    for execution in run.executions:
        candidate = next(c for c in pool() if c.id == execution.candidate_id)
        assert execution.reserved == candidate.expected_cost * (1 + candidate.max_retries) + 1
        assert execution.cost + execution.cleanup_cost <= execution.reserved
        assert execution.cleanup_cost <= 1  # cleanup is capped at its allowance
    assert run.cost_used <= run.budget_reserved
    # The next ranked probe would not have fitted: execution stopped before exceeding the cap.
    nxt = next(c for c in pool() if c.id == run.not_run[0])
    assert run.budget_reserved + nxt.reservation(1) > 15
    stored = journal.evidence("ref:staging", "probe_run")
    assert stored and stored[0]["run_id"] == run.run_id
    assert journal.evidence("ref:staging", "probe_plan")[0]["seed"] == 3


def test_retry_then_unknown_and_crash_are_never_findings(journal):
    plan = plan_probes(pool(), strategy="uniform", seed=1, budget_cap=1000, checker_digest=CHECKER)

    def crashing(candidate, attempt):
        if candidate.id == "P02":
            raise RuntimeError("socket closed")
        return probe(candidate, attempt)

    run = run_probes(plan, crashing, journal=journal, scope="s")
    by_id = {e.candidate_id: e for e in run.executions}
    assert run.stopped_reason == "completed" and not run.not_run
    assert by_id["P05"].retries == 1 and by_id["P05"].calls == 2
    assert by_id["P07"].status == "unknown" and by_id["P02"].status == "unknown"
    assert by_id["P02"].finding_id is None


def test_rerunning_a_plan_in_the_same_scope_draws_new_budget(journal):
    plan = plan_probes(pool(4), strategy="uniform", seed=1, budget_cap=12, checker_digest=CHECKER)
    first = run_probes(plan, probe, journal=journal, scope="s")
    second = run_probes(plan, probe, journal=journal, scope="s")
    assert first.run_id != second.run_id
    assert second.budget_reserved <= 12 and len(first.executions) + len(second.executions) < 8


def test_concurrent_workers_and_runs_never_exceed_the_cap(tmp_path):
    path = tmp_path / "shared.sqlite"
    plan_a = plan_probes(pool(40), strategy="priority", seed=1, budget_cap=37, checker_digest=CHECKER)
    plan_b = plan_probes(pool(40), strategy="uniform", seed=2, budget_cap=37, checker_digest=CHECKER)
    sent = []
    guard = threading.Lock()

    def counting(candidate, attempt):
        with guard:
            sent.append(candidate.id)
        return ProbeOutcome(status="no_finding")

    journals = [Journal(path) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool_:
        runs = list(
            pool_.map(
                lambda args: run_probes(args[0], counting, journal=args[1], scope="ref", workers=8),
                [(plan_a, journals[0]), (plan_b, journals[1])],
            )
        )
    cap, used = journals[0].budget("ref", "reference")
    assert cap == 37 and used <= 37
    assert sum(sum(e.cost + e.cleanup_cost for e in r.executions) for r in runs) == used
    assert len(sent) == sum(e.calls for r in runs for e in r.executions)
    for j in journals:
        j.close()


@settings(max_examples=40, deadline=None)
@given(
    costs=st.lists(st.integers(1, 9), min_size=1, max_size=20),
    cap=st.integers(0, 60),
    threads=st.integers(1, 8),
)
def test_raw_reservations_are_atomic_across_connections(tmp_path_factory, costs, cap, threads):
    path = tmp_path_factory.mktemp("budget") / "j.sqlite"
    journals = [Journal(path) for _ in range(threads)]
    journals[0].set_budget("scope", "reference", cap)
    granted = []
    lock = threading.Lock()

    def claim(index):
        ok = journals[index % threads].reserve("scope", "reference", f"op-{index}", costs[index])
        if ok:
            with lock:
                granted.append(costs[index])

    with ThreadPoolExecutor(max_workers=threads) as pool_:
        list(pool_.map(claim, range(len(costs))))
    _, used = journals[0].budget("scope", "reference")
    assert used == sum(granted) <= cap
    for j in journals:
        j.close()


@settings(max_examples=25, deadline=None)
@given(
    seed=st.integers(0, 2**32),
    cap=st.integers(0, 50),
    fraction=st.floats(0, 1),
    workers=st.integers(1, 6),
)
def test_budget_never_exceeded_and_plans_repeat(tmp_path_factory, seed, cap, fraction, workers):
    plan = plan_probes(
        pool(), strategy="priority", seed=seed, budget_cap=cap, checker_digest=CHECKER, exploration_fraction=fraction
    )
    assert plan == plan_probes(
        pool(), strategy="priority", seed=seed, budget_cap=cap, checker_digest=CHECKER, exploration_fraction=fraction
    )
    journal = Journal(tmp_path_factory.mktemp("p") / "j.sqlite")
    run = run_probes(plan, probe, journal=journal, scope="s", workers=workers, cleanup=lambda c: 1)
    assert run.budget_reserved <= cap and run.cost_used <= run.budget_reserved
    assert set(run.not_run) | {e.candidate_id for e in run.executions} == set(plan.selected)
    journal.close()


def test_strategies_compare_only_on_equal_scope_budget_and_checker(tmp_path):
    runs = {}
    for strategy, seed in (("uniform", 11), ("priority", 11)):
        plan = plan_probes(pool(), strategy=strategy, seed=seed, budget_cap=18, checker_digest=CHECKER)
        journal = Journal(tmp_path / f"{strategy}.sqlite")
        runs[strategy] = run_probes(plan, probe, journal=journal, scope="ref")
        journal.close()
    accepted = {f"F-{cid}" for cid in DEFECTIVE} - {"F-P04"}  # owner rejected one finding
    base, prio = account(runs["uniform"], accepted), account(runs["priority"], accepted)
    for item in (base, prio):
        assert item.budget_cap == 18 and item.budget_reserved <= 18
        assert item.review_effort == item.accepted_findings + item.rejected_findings + item.unknowns
        if item.reference_calls:
            assert item.accepted_per_reference_call == round(item.accepted_findings / item.reference_calls, 6)
    result = compare(base, prio)
    assert result.accepted_delta == prio.accepted_findings - base.accepted_findings
    assert "do not establish" in result.note
    other = plan_probes(pool(), strategy="priority", seed=11, budget_cap=30, checker_digest=CHECKER)
    journal = Journal(tmp_path / "other.sqlite")
    bigger = account(run_probes(other, probe, journal=journal, scope="ref"), accepted)
    journal.close()
    with pytest.raises(ValueError, match="budget_cap"):
        compare(base, bigger)
    swapped = prio.model_copy(update={"checker_digest": "checker-sha256:lenient"})
    with pytest.raises(ValueError, match="checker_digest"):
        compare(base, swapped)


def test_selection_never_changes_the_checker(tmp_path):
    plans = [
        plan_probes(pool(), strategy=s, seed=n, budget_cap=1000, checker_digest=CHECKER)
        for s in ("uniform", "priority")
        for n in range(3)
    ]
    assert {p.checker_digest for p in plans} == {CHECKER}
    verdicts = []
    for index, plan in enumerate(plans):
        journal = Journal(tmp_path / f"{index}.sqlite")
        run = run_probes(plan, probe, journal=journal, scope="ref")
        journal.close()
        verdicts.append({e.candidate_id: (e.status, e.finding_id) for e in run.executions})
    # Order differs between strategies; the judgement of each probe is identical.
    assert all(v == verdicts[0] for v in verdicts)
