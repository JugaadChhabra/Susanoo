"""Unit tests for guards/budget.py."""

import pytest

from susanoo.guards import budget


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(budget.RUN_COST_CAP_ENV, raising=False)


def test_default_cap_is_two_dollars():
    assert budget.run_cost_cap_usd() == 2.0


def test_under_the_cap_passes():
    budget.check({"cost_usd": 1.99})


def test_missing_cost_counts_as_zero():
    budget.check({})


def test_at_the_cap_raises():
    with pytest.raises(budget.BudgetExceeded) as exc:
        budget.check({"cost_usd": 2.0})
    assert exc.value.cost_usd == 2.0
    assert exc.value.cap_usd == 2.0


def test_over_the_cap_raises():
    with pytest.raises(budget.BudgetExceeded):
        budget.check({"cost_usd": 5.5})


def test_cap_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv(budget.RUN_COST_CAP_ENV, "0.5")
    budget.check({"cost_usd": 0.49})
    with pytest.raises(budget.BudgetExceeded) as exc:
        budget.check({"cost_usd": 0.5})
    assert exc.value.cap_usd == 0.5


def test_cap_is_read_on_every_check(monkeypatch):
    state = {"cost_usd": 1.0}
    monkeypatch.setenv(budget.RUN_COST_CAP_ENV, "10")
    budget.check(state)
    monkeypatch.setenv(budget.RUN_COST_CAP_ENV, "1")
    with pytest.raises(budget.BudgetExceeded):
        budget.check(state)


def test_blank_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv(budget.RUN_COST_CAP_ENV, "  ")
    assert budget.run_cost_cap_usd() == 2.0


@pytest.mark.parametrize("bad", ["abc", "0", "-1", "nan", "inf"])
def test_bad_cap_fails_loudly(monkeypatch, bad):
    monkeypatch.setenv(budget.RUN_COST_CAP_ENV, bad)
    with pytest.raises(ValueError, match=budget.RUN_COST_CAP_ENV):
        budget.check({"cost_usd": 0.0})


def test_message_is_plain_english():
    with pytest.raises(budget.BudgetExceeded, match=r"reached the \$2\.00 cap"):
        budget.check({"cost_usd": 2.5})
