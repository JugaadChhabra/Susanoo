"""Verifier head: keep rule, rollback by `git revert`, level choice, bookkeeping, budget and stop reporting.

No model, no Docker: `runner.run_level1` / `runner.run_level2` are replaced by fakes, and the git repo is a
throwaway one under tmp_path. [PRD › Improvement loop and guardrails]; IMPLEMENTATION_MUAAZ P4.3.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from susanoo.emit import InMemorySink, use_sink
from susanoo.graph.routing import after_verifier, current_failures
from susanoo.heads import diagnostician, runner, verifier
from susanoo.sandbox.docker import SandboxError
from susanoo.schemas import (
    AgentBundle,
    AnswerKey,
    Diagnosis,
    ExpectedIssue,
    IterationMetrics,
    Patch,
    Scenario,
    ScenarioResult,
)
from susanoo.scoring import compute_metrics

FILE = "app/db/queries.py"
HELD_OUT = ("ho-secret-1", "ho-secret-2")
HELD_OUT_DIFF_MARK = "HELDOUT_DIFF_MARKER"


# --- helpers -------------------------------------------------------------------------------------------------


def scenario(sid: str, split: str, *, planted: bool = True) -> Scenario:
    expected = [ExpectedIssue(file=FILE, line_start=12, line_end=12, rule_id="no-raw-sql", severity="block")]
    return Scenario(
        id=sid,
        kind="planted" if planted else "clean",
        split=split,  # type: ignore[arg-type]
        title=f"Title of {sid}",
        diff=f"--- a/{FILE}\n+++ b/{FILE}\n+ {HELD_OUT_DIFF_MARK if split == 'heldout' else 'x'} {sid}\n",
        answer_key=AnswerKey(expected=expected if planted else []),
        approved_by_user=split == "acceptance",
    )


SCENARIOS = [
    scenario("tr-1", "train"),
    scenario("tr-2", "train"),
    scenario(HELD_OUT[0], "heldout"),
    scenario(HELD_OUT[1], "heldout", planted=False),
    scenario("acc-1", "acceptance"),
]


def result(sid: str, passed: bool, iteration: int = 0, *, planted: bool = True) -> ScenarioResult:
    planted = planted and sid != HELD_OUT[1]
    return ScenarioResult(
        scenario_id=sid,
        iteration=iteration,
        findings=[],
        matched=1 if (passed and planted) else 0,
        missed=1 if (not passed and planted) else 0,
        false_positives=0,
        severity_correct=1 if (passed and planted) else 0,
        passed=passed,
        trace_id=f"trace-{sid}-{iteration}",
    )


def results_for(failing: set[str], iteration: int = 0) -> list[ScenarioResult]:
    return [result(s.id, s.id not in failing, iteration) for s in SCENARIOS]


def git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return out.stdout.strip()


def build_repo(root: Path, *, identity: bool = True) -> tuple[Path, str]:
    """A git repo with a base commit and one patch commit (policy change); returns (path, patch sha).

    It holds a train scenario file (unchanged by the patch) and a junk held-out file the Verifier must not read.
    With `identity=False` no user is configured anywhere; the test commits use environment variables only.
    """
    (root / "agent").mkdir(parents=True)
    (root / "scenarios" / "train").mkdir(parents=True)
    (root / "scenarios" / "heldout").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    if identity:
        git(root, "config", "user.name", "Test")
        git(root, "config", "user.email", "test@example.com")
    git(root, "config", "commit.gpgsign", "false")

    def commit(*args: str) -> None:
        subprocess.run(["git", "-C", str(root), "commit", "-q", *args], check=True, env=env)

    (root / "agent" / "policy.yaml").write_text("rules: []\n")
    (root / "scenarios" / "train" / "tr-1.json").write_text(SCENARIOS[0].model_dump_json())
    (root / "scenarios" / "heldout" / "junk.json").write_text("this is not json and must never be read")
    git(root, "add", ".")
    commit("-m", "base")
    (root / "agent" / "policy.yaml").write_text("rules: [patched]\n")
    commit("-am", "patch")
    return root, git(root, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    return build_repo(tmp_path / "gen")


def commit_scenario_edit(info: tuple[Path, str], title: str = "EDITED title") -> tuple[Path, str]:
    """Another patch commit that edits scenarios/train/tr-1.json (a test-layer fix)."""
    root, _ = info
    edited = SCENARIOS[0].model_copy(update={"title": title})
    (root / "scenarios" / "train" / "tr-1.json").write_text(edited.model_dump_json())
    git(root, "commit", "-q", "-am", "edit scenario")
    return root, git(root, "rev-parse", "HEAD")


def make_state(repo_info: tuple[Path, str], *, layer: str = "prompt_policy", **extra: Any) -> dict[str, Any]:
    root, sha = repo_info
    before = results_for({"tr-1"})
    state: dict[str, Any] = {
        "run_id": "run-1",
        "spec": "a code reviewer",
        "repo_path": str(root),
        "head_commit": sha,
        "agent": AgentBundle(
            prompt_path="agent/prompt.md", policy_path="agent/policy.yaml", tools_path="agent/tools.py", model="m"
        ),
        "scenarios": list(SCENARIOS),
        "results": before,
        "failure_ledger": [
            Diagnosis(
                id="dx-1",
                scenario_ids=["tr-1"],
                layer=layer,  # type: ignore[arg-type]
                hypothesis="The policy misses raw SQL.",
                evidence_span_ids=["s1"],
                target_files=["agent/policy.yaml"],
            )
        ],
        "patches": [
            Patch(
                id="patch-1",
                diagnosis_id="dx-1",
                commit_sha=sha,
                checkpoint_id=None,
                lines_changed=3,
                verdict="pending",
            )
        ],
        "metrics": [compute_metrics(before, SCENARIOS, iteration=0, cost_usd=0.1)],
        "iteration": 0,
        "cost_usd": 0.1,
        "status": "running",
    }
    state.update(extra)
    return state


class Fakes:
    """Replaces the Runner's two entry points; records which level was used and what it saw."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[int, list[str]]] = []
        self.scenarios_seen: list[list[Scenario]] = []
        self.repos: list[str] = []  # repo_path of each Level 2 call
        self.policies: list[str] = []  # policy.yaml as it was at each Level 2 call
        self.failing: set[str] = set()  # fails after the patch (both levels)
        self.baseline_failing: set[str] = {"tr-1"}  # Level 2 results of the pre-patch app
        self.cost = 0.05
        self.l2_error: Exception | None = None
        self.l1_error: Exception | None = None
        monkeypatch.setattr(runner, "run_level1", self.level1)
        monkeypatch.setattr(runner, "run_level2", self.level2)

    @staticmethod
    def _results(failing, scenarios, iteration):
        wanted = {s.id for s in scenarios}
        return [r for r in results_for(failing, iteration) if r.scenario_id in wanted]

    async def level1(self, state, scenarios, *, iteration=None):
        self.calls.append((1, [s.id for s in scenarios]))
        self.scenarios_seen.append(list(scenarios))
        if self.l1_error:
            raise self.l1_error
        return self._results(self.failing, scenarios, iteration), self.cost

    async def level2(self, state, scenarios, *, iteration=None, **_):
        self.calls.append((2, [s.id for s in scenarios]))
        self.scenarios_seen.append(list(scenarios))
        self.repos.append(state["repo_path"])
        self.policies.append((Path(state["repo_path"]) / "agent" / "policy.yaml").read_text())
        if self.l2_error:
            raise self.l2_error
        is_baseline = state["repo_path"] != self.main_repo
        return self._results(self.baseline_failing if is_baseline else self.failing, scenarios, iteration)

    main_repo = ""


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch, repo: tuple[Path, str]) -> Fakes:
    f = Fakes(monkeypatch)
    f.main_repo = str(repo[0])
    return f


async def run_verifier(state: dict[str, Any]) -> tuple[dict, InMemorySink]:
    sink = InMemorySink()
    with use_sink(sink):
        update = await verifier.run(state)  # type: ignore[arg-type]
    return update, sink


def commits(root: Path) -> list[str]:
    return git(root, "log", "--format=%s").splitlines()


def assert_no_heldout_leak(sink: InMemorySink) -> None:
    text = json.dumps([(e.message, e.payload) for e in sink.events])
    for ident in HELD_OUT:
        assert ident not in text
    assert HELD_OUT_DIFF_MARK not in text


# --- keep rule -----------------------------------------------------------------------------------------------


async def test_keeps_a_patch_that_fixes_its_target_without_regressions(repo, fakes):
    update, sink = await run_verifier(make_state(repo))
    assert update["patches"][-1].verdict == "kept"
    assert update["iteration"] == 1
    assert "head_commit" not in update  # unchanged: the patch commit stays the head
    assert commits(repo[0]) == ["patch", "base"]  # nothing reverted
    assert update["cost_usd"] == pytest.approx(0.15)
    assert [r.iteration for r in update["results"]] == [1] * len(SCENARIOS)
    types = [(e.head, e.type) for e in sink.events]
    assert types == [("verifier", "started"), ("verifier", "metrics"), ("verifier", "finished")]
    assert sink.events[-1].message == "The fix helped, so I kept it (round 1)."
    assert sink.events[-1].payload["verdict"] == "kept"
    assert_no_heldout_leak(sink)


async def test_rolls_back_when_the_target_is_still_failing(repo, fakes):
    fakes.failing = {"tr-1"}
    update, sink = await run_verifier(make_state(repo))
    assert update["patches"][-1].verdict == "rolled_back"
    assert "problem it was meant to fix is still there" in sink.events[-1].message
    assert "tr-1" in sink.events[-1].message
    assert sink.events[-1].message.startswith("The fix did not help, so I rolled it back (round 1): ")
    assert sink.events[-1].payload["checks"] == {
        "fixed_targets": False,
        "no_regressions": True,
        "heldout_not_lower": True,
    }
    assert_no_heldout_leak(sink)


async def test_rolls_back_on_a_regression_in_train(repo, fakes):
    fakes.failing = {"tr-2"}  # tr-1 fixed, but tr-2 (passing before) now fails
    update, sink = await run_verifier(make_state(repo))
    assert update["patches"][-1].verdict == "rolled_back"
    assert "broke something that worked before" in sink.events[-1].message
    assert "tr-2" in sink.events[-1].message
    assert sink.events[-1].payload["checks"] == {
        "fixed_targets": True,
        "no_regressions": False,
        "heldout_not_lower": True,
    }
    assert sink.events[-1].payload["regressed"] == ["tr-2"]


async def test_rolls_back_on_an_acceptance_regression_without_naming_it_as_train(repo, fakes):
    fakes.failing = {"acc-1"}
    update, sink = await run_verifier(make_state(repo))
    assert update["patches"][-1].verdict == "rolled_back"
    assert "1 approved example" in sink.events[-1].message
    assert sink.events[-1].payload["regressed"] == []  # only train ids are listed in the payload


async def test_rolls_back_when_the_heldout_score_drops(repo, fakes):
    fakes.failing = {HELD_OUT[0]}  # the hidden check that passed before now fails; train is all fine
    update, sink = await run_verifier(make_state(repo))
    assert update["patches"][-1].verdict == "rolled_back"
    message = sink.events[-1].message
    assert "hidden-check score dropped from 1.00 to 0.00" in message
    assert sink.events[-1].payload["checks"] == {
        "fixed_targets": True,
        "no_regressions": True,
        "heldout_not_lower": False,
    }
    assert_no_heldout_leak(sink)


async def test_heldout_not_lower_is_enough_when_equal(repo, fakes):
    # before and after are both 1.0 on the held-out split: "doesn't drop" means equal is kept
    state = make_state(repo)
    assert state["metrics"][0].heldout_f1 == 1.0
    update, _ = await run_verifier(state)
    assert update["patches"][-1].verdict == "kept"
    assert update["metrics"][-1].heldout_f1 == 1.0


async def test_heldout_gain_is_kept(repo, fakes):
    before = results_for({"tr-1", HELD_OUT[0]})
    state = make_state(repo, results=before, metrics=[compute_metrics(before, SCENARIOS, iteration=0, cost_usd=0.1)])
    assert state["metrics"][0].heldout_f1 == 0.0
    update, _ = await run_verifier(state)
    assert update["patches"][-1].verdict == "kept"
    assert update["metrics"][-1].heldout_f1 == 1.0


# --- rollback mechanics --------------------------------------------------------------------------------------


async def test_rollback_creates_a_revert_commit_and_updates_head_commit(repo, fakes):
    root, sha = repo
    fakes.failing = {"tr-1"}
    update, _ = await run_verifier(make_state(repo))
    assert commits(root)[0].startswith("Revert")
    assert len(commits(root)) == 3  # history kept, not rewritten
    assert update["head_commit"] == git(root, "rev-parse", "HEAD")
    assert update["head_commit"] != sha
    assert (root / "agent" / "policy.yaml").read_text() == "rules: []\n"  # patch undone in the working tree


async def test_rollback_restamps_the_restored_agents_results_and_scores(repo, fakes):
    fakes.failing = {"tr-1", HELD_OUT[0]}
    state = make_state(repo)
    update, sink = await run_verifier(state)
    new = update["metrics"][-1]
    assert new.iteration == 1
    assert new.heldout_f1 == state["metrics"][0].heldout_f1  # the restored agent's score, not the failed patch's
    assert new.cost_usd == pytest.approx(0.15)
    assert sink.events[1].payload == new.model_dump()
    # the results are the pre-patch ones (tr-1 failing, held-out passing), stamped with the new iteration
    assert {r.iteration for r in update["results"]} == {1}
    assert {r.scenario_id: r.passed for r in update["results"]} == {r.scenario_id: r.passed for r in state["results"]}


async def test_P2_after_a_rollback_the_diagnostician_still_sees_the_failures(repo, fakes):
    fakes.failing = {"tr-1"}  # the target is still failing: rollback
    state = make_state(repo)
    update, _ = await run_verifier(state)
    merged = {**state, **update}
    assert merged["iteration"] == 1
    current = current_failures(merged)
    assert [r.scenario_id for r in current] == ["tr-1"]  # routing and the Diagnostician read this iteration only
    failures = diagnostician.select_failures(merged)
    assert [r.scenario_id for _, r in failures[0]] == ["tr-1"]
    assert after_verifier(merged) == "Diagnostician"


async def test_a_bad_commit_sha_is_refused_loudly(repo, fakes):
    fakes.failing = {"tr-1"}
    state = make_state(repo)
    state["patches"][0] = state["patches"][0].model_copy(update={"commit_sha": "--abort"})
    sink = InMemorySink()
    with use_sink(sink), pytest.raises(verifier.VerifierError):
        await verifier.run(state)
    assert sink.events[-1].type == "error"


# --- level choice --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("layer, level", [("prompt_policy", 1), ("test", 1), ("code", 2), ("tool", 2)])
async def test_level_depends_on_the_diagnosed_layer(repo, fakes, layer, level):
    update, sink = await run_verifier(make_state(repo, layer=layer))
    assert [c[0] for c in fakes.calls] == ([1] if level == 1 else [2, 2, 1])  # L2: before, after, L1 metrics rerun
    assert sorted(fakes.calls[-1][1]) == sorted(s.id for s in SCENARIOS)  # failing + train + held-out + acceptance
    assert sink.events[-1].payload["level"] == level
    assert update["patches"][-1].verdict == "kept"
    if level == 2:
        assert update["cost_usd"] == pytest.approx(0.15)  # Level 2 itself is free; the Level 1 metrics rerun is billed


async def test_disputed_scenarios_are_not_rerun(repo, fakes):
    await run_verifier(make_state(repo, disputed_scenario_ids=["tr-2"]))
    assert "tr-2" not in fakes.calls[0][1]


@pytest.mark.parametrize(
    "error",
    [
        runner.RunnerError(
            "The Level 2 check needs ANTHROPIC_APP_API_KEY to be set for the agent's app, and it is not."
        ),
        SandboxError("Could not reach the Docker daemon: DockerException"),
    ],
)
async def test_level2_unavailable_rolls_back_with_a_reason(repo, fakes, error):
    fakes.l2_error = error
    update, sink = await run_verifier(make_state(repo, layer="code"))
    assert update["patches"][-1].verdict == "rolled_back"
    assert update["iteration"] == 1
    message = sink.events[-1].message
    assert message.startswith("The fix did not help, so I rolled it back (round 1): it couldn't be checked: ")
    assert str(error).rstrip(".") in message
    assert sink.events[-1].payload["unverified"] is True
    assert commits(repo[0])[0].startswith("Revert")
    assert len(update["metrics"]) == 2


async def test_level1_failure_also_means_unverified_and_rolled_back(repo, fakes):
    fakes.l1_error = runner.RunnerError("None of the scenarios could be run because the agent's model did not answer.")
    update, _ = await run_verifier(make_state(repo))
    assert update["patches"][-1].verdict == "rolled_back"


# --- bookkeeping ---------------------------------------------------------------------------------------------


async def test_metrics_are_appended_exactly_once_per_iteration(repo, fakes):
    state = make_state(repo)
    update, sink = await run_verifier(state)
    assert [m.iteration for m in update["metrics"]] == [0, 1]
    assert sum(1 for e in sink.events if e.type == "metrics") == 1
    # feeding the result back (a resumed node) replaces the entry instead of duplicating it
    again, _ = await run_verifier(
        {**state, "metrics": update["metrics"], "patches": [state["patches"][0]], "iteration": 0}
    )
    assert [m.iteration for m in again["metrics"]] == [0, 1]


async def test_only_the_changed_keys_are_returned_and_state_is_not_mutated(repo, fakes):
    state = make_state(repo)
    patches_before = list(state["patches"])
    update, _ = await run_verifier(state)
    assert set(update) <= {
        "patches",
        "iteration",
        "metrics",
        "cost_usd",
        "head_commit",
        "results",
        "scenarios",
        "level2_baseline",
        "stop_reason",
        "status",
    }
    assert state["patches"] == patches_before
    assert state["patches"][0].verdict == "pending"
    assert state["iteration"] == 0


async def test_refuses_a_patch_that_was_already_judged(repo, fakes):
    state = make_state(repo)
    state["patches"][0] = state["patches"][0].model_copy(update={"verdict": "kept"})
    with use_sink(InMemorySink()), pytest.raises(verifier.VerifierError):
        await verifier.run(state)


# --- budget --------------------------------------------------------------------------------------------------


async def test_budget_already_spent_reverts_the_unverified_patch_and_stops_cleanly(repo, fakes, monkeypatch):
    monkeypatch.setenv("RUN_COST_CAP_USD", "0.05")
    update, sink = await run_verifier(make_state(repo))  # cost_usd 0.1 >= cap
    assert fakes.calls == []  # nothing was re-run
    assert update["patches"][-1].verdict == "rolled_back"
    assert update["stop_reason"] == "budget"
    assert update["status"] == "done"  # a baseline score exists
    assert commits(repo[0])[0].startswith("Revert")
    assert sink.events[-2].type == "finished"
    assert sink.events[-1].type == "progress"
    assert "cost limit" in sink.events[-1].message


async def test_budget_exceeded_during_the_rerun_is_a_clean_stop(repo, fakes, monkeypatch):
    async def exceeding(state, scenarios, *, iteration=None):
        raise runner.budget.BudgetExceeded(0.31, 0.3)

    monkeypatch.setenv("RUN_COST_CAP_USD", "0.3")
    monkeypatch.setattr(runner, "run_level1", exceeding)
    update, sink = await run_verifier(make_state(repo))
    assert update["patches"][-1].verdict == "rolled_back"
    assert update["cost_usd"] == pytest.approx(0.31)
    assert update["stop_reason"] == "budget"
    assert after_verifier({**make_state(repo), **update}) == "Done"
    assert_no_heldout_leak(sink)


# --- stop conditions (routing is reused, unchanged) ----------------------------------------------------------


async def test_iteration_cap_is_reported_with_a_partial_report(repo, fakes):
    fakes.failing = {"tr-1"}
    base = results_for({"tr-1"})
    state = make_state(
        repo,
        iteration=4,
        metrics=[
            IterationMetrics(
                iteration=i,
                recall=0.5,
                precision=0.5,
                f1=0.5,
                severity_accuracy=1.0,
                heldout_f1=0.5 + 0.1 * i,
                acceptance_pass_rate=1.0,
                cost_usd=0.1,
            )
            for i in range(5)
        ],
        results=base,
    )
    update, sink = await run_verifier(state)
    assert update["iteration"] == 5
    assert after_verifier({**state, **update}) == "Done"
    report = sink.events[-1]
    assert report.type == "progress"
    assert report.payload == {"stop_reason": "iteration_cap"}
    assert "round limit" in report.message
    assert "fix 1 rolled back" in report.message
    assert "round 5" in report.message


async def test_two_rounds_without_gain_stop_and_a_kept_patch_continues_otherwise(repo, fakes):
    fakes.failing = {"tr-1"}
    state = make_state(repo)
    first, sink = await run_verifier(state)
    assert after_verifier({**state, **first}) == "Diagnostician"  # one round without gain: keep going
    assert sink.events[-1].type == "finished"  # no stop report yet

    # second round, same flat score: routing now stops, and the Verifier says so
    root = repo[0]
    (root / "agent" / "policy.yaml").write_text("rules: [patched again]\n")
    git(root, "commit", "-q", "-am", "patch 2")
    patch2 = first["patches"][0].model_copy(
        update={"id": "patch-2", "verdict": "pending", "commit_sha": git(root, "rev-parse", "HEAD")}
    )
    second_state = {**state, **first, "patches": first["patches"] + [patch2]}
    second, sink2 = await run_verifier(second_state)
    assert after_verifier({**second_state, **second}) == "Done"
    assert sink2.events[-1].payload == {"stop_reason": "no_gain"}
    assert "two rounds in a row" in sink2.events[-1].message
    assert_no_heldout_leak(sink2)


async def test_kept_patch_routes_back_to_the_runner(repo, fakes):
    state = make_state(repo)
    update, _ = await run_verifier(state)
    assert after_verifier({**state, **update}) == "Runner"


# --- Level 2: same-level comparison --------------------------------------------------------------------------


def worktrees(root: Path) -> list[str]:
    return [line for line in git(root, "worktree", "list", "--porcelain").splitlines() if line.startswith("worktree ")]


async def test_level2_runs_the_app_as_it_was_before_the_patch_in_a_temporary_worktree(repo, fakes):
    update, _ = await run_verifier(make_state(repo, layer="code"))
    assert [c[0] for c in fakes.calls] == [2, 2, 1]
    baseline_repo, after_repo = fakes.repos
    assert after_repo == str(repo[0])
    assert baseline_repo != after_repo
    assert fakes.policies[:2] == ["rules: []\n", "rules: [patched]\n"]  # before = parent commit, after = the patch
    assert not Path(baseline_repo).exists()  # the temporary checkout is gone
    assert len(worktrees(repo[0])) == 1  # and git no longer lists it
    assert update["patches"][-1].verdict == "kept"


async def test_level2_regressions_are_judged_against_level2_not_the_level1_baseline(repo, fakes):
    # tr-2 failed already at Level 2 before the patch (but passed in the Runner's Level 1 results in state),
    # so it failing again after the patch is no regression.
    fakes.baseline_failing = {"tr-1", "tr-2"}
    fakes.failing = {"tr-2"}
    update, sink = await run_verifier(make_state(repo, layer="code"))
    assert update["patches"][-1].verdict == "kept"
    assert sink.events[-1].payload["checks"]["no_regressions"] is True
    # ...whereas at Level 1 the same outcome is a regression
    fakes.calls.clear()
    update, _ = await run_verifier(make_state(repo, layer="prompt_policy"))
    assert update["patches"][-1].verdict == "rolled_back"


async def test_level2_heldout_is_compared_with_the_level2_heldout_before(repo, fakes):
    # state's Level 1 baseline says held-out F1 is 1.0; the Level 2 app scored 0.0 before and 0.0 after.
    fakes.baseline_failing = {"tr-1", HELD_OUT[0]}
    fakes.failing = {HELD_OUT[0]}
    update, sink = await run_verifier(make_state(repo, layer="tool"))
    assert update["patches"][-1].verdict == "kept"
    assert sink.events[-1].payload["heldout_f1_before"] == 0.0
    assert sink.events[-1].payload["heldout_f1_after"] == 0.0


async def test_level2_heldout_drop_still_rolls_back(repo, fakes):
    fakes.failing = {HELD_OUT[0]}  # baseline_failing is {"tr-1"}: held-out 1.0 before
    update, sink = await run_verifier(make_state(repo, layer="code"))
    assert update["patches"][-1].verdict == "rolled_back"
    assert "hidden-check score dropped" in sink.events[-1].message


async def test_level2_baseline_is_cached_by_commit(repo, fakes):
    root, sha = repo
    first, _ = await run_verifier(make_state(repo, layer="code"))
    assert first["level2_baseline"]["commit"] == sha  # a kept patch: its own Level 2 results become the baseline
    assert len(fakes.calls) == 3

    # the next patch's parent is that kept commit, so the cached baseline is used: only the "after" run happens
    (root / "agent" / "policy.yaml").write_text("rules: [patched twice]\n")
    git(root, "commit", "-q", "-am", "patch 2")
    sha2 = git(root, "rev-parse", "HEAD")
    patch2 = first["patches"][0].model_copy(update={"id": "patch-2", "commit_sha": sha2, "verdict": "pending"})
    state2 = {**make_state((root, sha2), layer="code"), **first, "patches": [patch2], "iteration": 1}
    fakes.calls.clear()
    second, _ = await run_verifier(state2)
    assert [c[0] for c in fakes.calls] == [2, 1]
    assert second["patches"][-1].verdict == "kept"


async def test_a_stale_cache_is_ignored(repo, fakes):
    stale = {"commit": "0" * 40, "results": results_for(set())}
    await run_verifier(make_state(repo, layer="code", level2_baseline=stale))
    assert [c[0] for c in fakes.calls] == [2, 2, 1]


async def test_level2_rollback_caches_the_baseline_under_the_revert_commit(repo, fakes):
    fakes.failing = {"tr-1"}
    update, _ = await run_verifier(make_state(repo, layer="code"))
    assert update["patches"][-1].verdict == "rolled_back"
    assert update["level2_baseline"]["commit"] == update["head_commit"]
    assert {r.scenario_id for r in update["level2_baseline"]["results"]} == {s.id for s in SCENARIOS}


async def test_level2_baseline_failure_is_unverified_and_cleans_up(repo, fakes):
    fakes.l2_error = SandboxError("Building the app image failed: BuildError")
    update, sink = await run_verifier(make_state(repo, layer="code"))
    assert update["patches"][-1].verdict == "rolled_back"
    assert "it couldn't be checked: Building the app image failed" in sink.events[-1].message
    assert len(worktrees(repo[0])) == 1


# --- git identity for reverts --------------------------------------------------------------------------------


@pytest.fixture
def no_global_git_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = tmp_path / "empty-gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(var, raising=False)


async def test_revert_works_in_a_repo_with_no_git_identity(tmp_path, monkeypatch, no_global_git_config):
    info = build_repo(tmp_path / "bare", identity=False)
    root = info[0]
    assert subprocess.run(["git", "-C", str(root), "config", "user.name"], capture_output=True).returncode != 0
    Fakes(monkeypatch).failing = {"tr-1"}
    update, _ = await run_verifier(make_state(info))
    assert update["patches"][-1].verdict == "rolled_back"
    assert git(root, "log", "-1", "--format=%an <%ae>|%cn <%ce>") == (
        "Susanoo <susanoo@users.noreply.github.com>|Susanoo <susanoo@users.noreply.github.com>"
    )
    assert (
        subprocess.run(["git", "-C", str(root), "config", "--local", "user.name"], capture_output=True).returncode != 0
    )
    assert (
        subprocess.run(["git", "-C", str(root), "config", "--local", "user.email"], capture_output=True).returncode != 0
    )


async def test_a_configured_identity_is_left_alone(repo, fakes, no_global_git_config):
    fakes.failing = {"tr-1"}
    await run_verifier(make_state(repo))
    assert git(repo[0], "log", "-1", "--format=%an <%ae>") == "Test <test@example.com>"


# --- test-layer patches: scenario files ----------------------------------------------------------------------


async def test_kept_test_patch_reruns_and_returns_the_edited_train_scenarios(repo, fakes):
    info = commit_scenario_edit(repo)
    update, sink = await run_verifier(make_state(info, layer="test"))
    assert update["patches"][-1].verdict == "kept"
    rerun = {s.id: s for s in fakes.scenarios_seen[0]}
    assert rerun["tr-1"].title == "EDITED title"  # the edited card was what got re-run
    new = {s.id: s for s in update["scenarios"]}
    assert new["tr-1"].title == "EDITED title"
    assert [s.id for s in update["scenarios"]] == [s.id for s in SCENARIOS]  # same list, same order
    for untouched in ("tr-2", HELD_OUT[0], HELD_OUT[1], "acc-1"):
        assert new[untouched] == next(s for s in SCENARIOS if s.id == untouched)
    assert_no_heldout_leak(sink)  # and the junk held-out file in the repo was never read (it would have failed)


async def test_rolled_back_test_patch_returns_the_original_scenarios(repo, fakes):
    info = commit_scenario_edit(repo)
    fakes.failing = {"tr-1"}
    update, _ = await run_verifier(make_state(info, layer="test"))
    assert update["patches"][-1].verdict == "rolled_back"
    assert update["scenarios"] == SCENARIOS
    on_disk = Scenario.model_validate_json((repo[0] / "scenarios" / "train" / "tr-1.json").read_text())
    assert on_disk.title == SCENARIOS[0].title  # the revert restored the file


async def test_only_diagnosed_train_scenarios_are_reloaded(repo, fakes):
    root = repo[0]
    # Edit tr-2 (not diagnosed) and an acceptance file on disk: neither may leak into the returned scenarios.
    (root / "scenarios" / "train" / "tr-2.json").write_text(
        SCENARIOS[1].model_copy(update={"title": "SNEAK"}).model_dump_json()
    )
    (root / "scenarios" / "train" / "acc-1.json").write_text(
        SCENARIOS[4].model_copy(update={"title": "SNEAK"}).model_dump_json()
    )
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "sneaky edits")
    info = (root, git(root, "rev-parse", "HEAD"))
    state = make_state(info, layer="test")
    state["failure_ledger"][0] = state["failure_ledger"][0].model_copy(update={"scenario_ids": ["tr-1", "acc-1"]})
    update, _ = await run_verifier(state)
    titles = {s.id: s.title for s in update["scenarios"]}
    assert titles["tr-2"] == SCENARIOS[1].title
    assert titles["acc-1"] == SCENARIOS[4].title


async def test_an_unreadable_edited_scenario_cannot_be_verified(repo, fakes):
    (repo[0] / "scenarios" / "train" / "tr-1.json").write_text("{broken")
    git(repo[0], "commit", "-q", "-am", "break it")
    info = (repo[0], git(repo[0], "rev-parse", "HEAD"))
    update, sink = await run_verifier(make_state(info, layer="test"))
    assert update["patches"][-1].verdict == "rolled_back"
    assert "it couldn't be checked: the edited scenario tr-1 could not be read" in sink.events[-1].message
    assert fakes.calls == []


async def test_non_test_layers_never_return_scenarios(repo, fakes):
    update, _ = await run_verifier(make_state(repo, layer="prompt_policy"))
    assert "scenarios" not in update


# --- a patch the Fixer wrapper already refused ---------------------------------------------------------------


def refused_state(repo, **extra):
    state = make_state(repo, **extra)
    state["patches"][0] = state["patches"][0].model_copy(update={"verdict": "rolled_back", "lines_changed": 0})
    return state


async def test_an_already_refused_patch_only_gets_the_rounds_bookkeeping(repo, fakes):
    state = refused_state(repo, fix_rejection_reason="it tried to change files outside the agent's prompt and policy.")
    update, sink = await run_verifier(state)
    assert fakes.calls == []  # nothing was re-run
    assert commits(repo[0]) == ["patch", "base"]  # nothing was reverted
    assert set(update) == {"iteration", "metrics", "results"}  # head_commit and patches are left alone
    assert {r.iteration for r in update["results"]} == {1}
    assert update["iteration"] == 1
    assert [m.iteration for m in update["metrics"]] == [0, 1]
    old, new = update["metrics"]
    assert new.model_copy(update={"iteration": 0, "cost_usd": old.cost_usd}) == old  # a copy of the previous scores
    assert [(e.type) for e in sink.events] == ["started", "metrics", "finished"]
    assert sink.events[-1].message == (
        "The fix was refused before it could be checked (round 1): "
        "it tried to change files outside the agent's prompt and policy."
    )
    assert sink.events[-1].payload == {"verdict": "rolled_back", "patch_id": "patch-1", "refused": True}
    assert after_verifier({**state, **update}) == "Diagnostician"


async def test_an_already_refused_patch_has_a_default_reason(repo, fakes):
    _, sink = await run_verifier(refused_state(repo))
    assert sink.events[-1].message.startswith("The fix was refused before it could be checked (round 1): ")
    assert sink.events[-1].message.endswith(".")


async def test_an_already_refused_patch_still_reports_a_stop(repo, fakes):
    state = refused_state(repo, iteration=4, fix_rejection_reason="it was too large")
    update, sink = await run_verifier(state)
    assert update["iteration"] == 5
    assert after_verifier({**state, **update}) == "Done"
    assert sink.events[-1].type == "progress"
    assert sink.events[-1].payload == {"stop_reason": "iteration_cap"}
    assert "fix 1 rolled back" in sink.events[-1].message


@pytest.mark.parametrize("verdict", ["kept", "awaiting_approval"])
async def test_other_judged_verdicts_stay_a_loud_error(repo, fakes, verdict):
    state = make_state(repo)
    state["patches"][0] = state["patches"][0].model_copy(update={"verdict": verdict})
    with use_sink(InMemorySink()), pytest.raises(verifier.VerifierError):
        await verifier.run(state)


# --- review fixes: one headline curve, fail-closed ----------------------------------------------------------


async def test_P1_the_level1_before_comes_from_the_current_results_not_the_last_metrics_entry(repo, fakes):
    # metrics[1] is a stale/odd entry (held-out 0.0); the current Level 1 results say held-out F1 is 1.0.
    l1_before = results_for({"tr-1"}, iteration=1)
    odd = compute_metrics(results_for({"tr-1", HELD_OUT[0]}, 1), SCENARIOS, iteration=1, cost_usd=0.1)
    assert odd.heldout_f1 == 0.0
    state = make_state(
        repo,
        results=l1_before,
        iteration=1,
        metrics=[compute_metrics(l1_before, SCENARIOS, iteration=0, cost_usd=0.1), odd],
    )
    fakes.failing = {HELD_OUT[0]}  # the patch fixes tr-1 but breaks the hidden check: 1.0 -> 0.0
    update, sink = await run_verifier(state)
    assert update["patches"][-1].verdict == "rolled_back"
    finished = next(e for e in sink.events if e.type == "finished")
    assert "hidden-check score dropped from 1.00 to 0.00" in finished.message


async def test_level2_numbers_never_enter_the_metrics_curve(repo, fakes):
    fakes.baseline_failing = {"tr-1", HELD_OUT[0]}  # Level 2 held-out is 0.0 before ...
    fakes.failing = set()  # ... and 1.0 after (Level 2 and the Level 1 rerun alike)
    state = make_state(repo, layer="code")
    update, sink = await run_verifier(state)
    assert update["patches"][-1].verdict == "kept"
    assert sink.events[-1].payload["heldout_f1_before"] == 0.0  # Level 2 numbers live in the payload only
    assert [c[0] for c in fakes.calls] == [2, 2, 1]
    new = update["metrics"][-1]
    # the recorded entry is computed from the Level 1 rerun and costs include its model spend
    assert new == compute_metrics(update["results"], SCENARIOS, iteration=1, cost_usd=new.cost_usd)
    assert new.cost_usd == pytest.approx(0.15)
    assert {r.iteration for r in update["results"]} == {1}
    assert update["level2_baseline"]["commit"] == repo[1]  # the cache keeps the Level 2 results separately


async def test_a_kept_level2_patch_whose_metrics_rerun_fails_is_rolled_back(repo, fakes):
    fakes.l1_error = runner.RunnerError("None of the scenarios could be run because the agent's model did not answer.")
    update, sink = await run_verifier(make_state(repo, layer="code"))
    assert update["patches"][-1].verdict == "rolled_back"
    assert "couldn't be checked" in sink.events[-1].message
    assert commits(repo[0])[0].startswith("Revert")


async def test_level2_rollback_restamps_level1_results_and_records_no_level2_number(repo, fakes):
    fakes.failing = {"tr-1"}
    state = make_state(repo, layer="tool")
    update, _ = await run_verifier(state)
    assert update["patches"][-1].verdict == "rolled_back"
    assert update["metrics"][-1].heldout_f1 == state["metrics"][0].heldout_f1
    assert {r.iteration for r in update["results"]} == {1}
    assert fakes.calls[-1][0] == 2  # no Level 1 rerun when the patch is rolled back


async def test_a_scenario_that_passed_before_and_has_no_result_after_is_a_regression(repo, fakes, monkeypatch):
    async def drops_tr2(state, scenarios, *, iteration=None):
        rows = [r for r in results_for(set(), iteration) if r.scenario_id != "tr-2"]
        return rows, 0.0

    monkeypatch.setattr(runner, "run_level1", drops_tr2)
    update, sink = await run_verifier(make_state(repo))
    assert update["patches"][-1].verdict == "rolled_back"
    assert sink.events[-1].payload["regressed"] == ["tr-2"]


async def test_the_pre_patch_level2_run_uses_a_distinct_run_id_and_image_tag(repo, fakes, monkeypatch):
    seen: list[str] = []
    original = fakes.level2

    async def spy(state, scenarios, **kw):
        seen.append(state["run_id"])
        return await original(state, scenarios, **kw)

    monkeypatch.setattr(runner, "run_level2", spy)
    await run_verifier(make_state(repo, layer="code"))
    assert len(seen) == 2 and seen[0] != seen[1]
    assert seen[1] == "run-1"
    assert re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?", seen[0])
    long = verifier._baseline_run_id("a" * 40)
    assert long != "a" * 40 and len(long) <= 40 and re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?", long)


class Boom(Exception):
    pass


@pytest.mark.parametrize("where", ["level1", "level2", "metrics"])
async def test_an_unexpected_error_still_reverts_the_patch_and_scrubs_the_event(repo, fakes, monkeypatch, where):
    secret = "sk-ant-" + "api03-" + "B" * 24
    layer = "code" if where == "level2" else "prompt_policy"
    if where == "level1":
        fakes.l1_error = Boom(f"exploded with key {secret}")
    elif where == "level2":
        fakes.l2_error = Boom(f"exploded with key {secret}")
    else:

        def bad_metrics(*a, **k):
            raise Boom(f"exploded with key {secret}")

        monkeypatch.setattr(verifier, "compute_metrics", bad_metrics)
        monkeypatch.setattr(verifier, "_restored", lambda s, a, i, c: ([], fake_metrics(i, c)))
    update, sink = await run_verifier(make_state(repo, layer=layer))
    assert update["patches"][-1].verdict == "rolled_back"
    assert commits(repo[0])[0].startswith("Revert")  # the patch is not left applied
    assert update["head_commit"] == git(repo[0], "rev-parse", "HEAD")
    types = [e.type for e in sink.events]
    assert "error" in types
    assert "it couldn't be checked" in sink.events[-1].message
    assert secret not in json.dumps([(e.message, e.payload) for e in sink.events])


def fake_metrics(iteration: int, cost: float) -> IterationMetrics:
    return IterationMetrics(
        iteration=iteration,
        recall=1.0,
        precision=1.0,
        f1=1.0,
        severity_accuracy=1.0,
        heldout_f1=1.0,
        acceptance_pass_rate=1.0,
        cost_usd=cost,
    )


async def test_an_unexpected_error_is_reraised_only_if_the_revert_also_fails(repo, fakes):
    fakes.l1_error = Boom("exploded")
    state = make_state(repo)
    state["patches"][0] = state["patches"][0].model_copy(update={"commit_sha": "deadbeef0"})  # cannot be reverted
    sink = InMemorySink()
    with use_sink(sink), pytest.raises(verifier.VerifierError):
        await verifier.run(state)
    assert sink.events[-1].type == "error"
