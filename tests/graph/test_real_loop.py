"""The loop heads wired through `real_heads()`, with fake versions of their `run` functions.
[PRD › LangGraph flow and shared state], [PRD › Improvement loop and guardrails]

The fake heads stand in for heads/diagnostician.py, fixer.py and verifier.py (their `run` is monkeypatched, so
`real_heads()` picks the fakes up); the HumanGate stays the real stub gate implementation. No model is called.
"""

from __future__ import annotations

import pytest
from graph_helpers import run_graph

from susanoo.graph import routing as r
from susanoo.graph import stubs
from susanoo.graph.build import Heads, _or_done, build_graph, real_heads
from susanoo.graph.checkpointer import memory_checkpointer
from susanoo.guards import budget
from susanoo.heads import diagnostician, fixer, verifier
from susanoo.schemas import Diagnosis, Patch, ScenarioResult

FAST = {"intake_questions": []}


class Script:
    """Scripted fake heads. `layers` is one diagnosis layer per Diagnostician call (None = nothing found)."""

    def __init__(self, layers, fixes=("ok",), verdicts=("kept",)):
        self.layers = list(layers)
        self.fixes = list(fixes)  # "ok" | "refused" | "rejected" | "budget"
        self.verdicts = list(verdicts)
        self.fixer_states: list[dict] = []
        self.verifier_states: list[dict] = []
        self.diagnostician_states: list[dict] = []

    async def diagnostician(self, state):
        self.diagnostician_states.append(dict(state))
        layer = self.layers.pop(0)
        if layer is None:
            return {"cost_usd": state.get("cost_usd", 0.0) + 0.1, "stop_reason": "no_diagnosis"}
        ledger = list(state["failure_ledger"])
        dx = Diagnosis(
            id=f"dx-{len(ledger) + 1}", scenario_ids=["tr-1"], layer=layer, hypothesis="h",
            evidence_span_ids=[], target_files=[],
        )  # fmt: skip
        update = {"failure_ledger": ledger + [dx], "cost_usd": state.get("cost_usd", 0.0) + 0.1}
        if layer == "test":
            update["status"] = "awaiting_approval"
        return update

    async def fixer(self, state, **_):
        self.fixer_states.append(dict(state))
        how = self.fixes.pop(0)
        if how == "budget":
            budget.check({**state, "cost_usd": 99.0})
        if how == "refused":
            raise fixer.FixRefused("The diagnosis points at a file this layer may not change, so I changed nothing.")
        if how == "revert_failed":
            raise fixer.FixRejected(
                "The fix broke the rules and undoing it failed; the app needs a manual rollback.",
                cost_usd=1.25, head_commit=None, cause=None,
            )  # fmt: skip
        if how == "nochange":
            from susanoo.integrations import coding_worker

            raise coding_worker.NoChange("The agent finished without changing any file")
        if how == "rejected":
            raise fixer.FixRejected(
                "The fix was too large, so I undid it.", cost_usd=1.25, head_commit="revert123", cause=None
            )
        dx = state["failure_ledger"][-1]
        patches = list(state["patches"])
        patch = Patch(
            id=f"patch-{len(patches) + 1}", diagnosis_id=dx.id, commit_sha=f"sha{len(patches) + 1}",
            checkpoint_id=None, lines_changed=3, verdict="pending",
        )  # fmt: skip
        return {"patches": patches + [patch], "head_commit": patch.commit_sha}

    async def verifier(self, state):
        self.verifier_states.append(dict(state))
        patches = list(state["patches"])
        patch = patches[-1]
        if patch.verdict == "rolled_back":  # the Fixer wrapper already rolled it back: bookkeeping only
            verdict = "rolled_back"
        else:
            verdict = self.verdicts.pop(0)
            patches[-1] = patch.model_copy(update={"verdict": verdict})
        update = {"patches": patches, "iteration": state["iteration"] + 1}
        if verdict == "kept":
            update["scenarios"] = state["scenarios"]
        return update


@pytest.fixture
def script_for(monkeypatch):
    def make(*args, **kwargs) -> tuple[Script, Heads]:
        script = Script(*args, **kwargs)
        monkeypatch.setattr(diagnostician, "run", script.diagnostician)
        monkeypatch.setattr(fixer, "run", script.fixer)
        monkeypatch.setattr(verifier, "run", script.verifier)
        real = real_heads()
        assert real.diagnostician == script.diagnostician  # wired through real_heads()
        # stub front half (planner ... runner) and stub gate; only the loop heads are the "real" ones
        return script, Heads(diagnostician=real.diagnostician, fixer=real.fixer, verifier=real.verifier)

    return make


LOOP = ["Runner", "Diagnostician", "Fixer", "Verifier"]


async def test_diagnose_fix_verify_kept(script_for):
    script, heads = script_for(["prompt_policy"])
    run = await run_graph(FAST, heads=heads)
    assert run.nodes[-5:] == ["Diagnostician", "Fixer", "Verifier", "Runner", "Done"]
    assert [p.verdict for p in run.final["patches"]] == ["kept"]
    assert run.final["status"] == "done"
    assert run.final["stop_reason"] == "all_passed"
    assert run.final["fix_rejection_reason"] is None
    assert len(script.fixer_states) == 1


@pytest.mark.parametrize(
    ("how", "sha", "cost_after"),
    [("rejected", "revert123", 1.25), ("refused", "stub0000", None)],
)
async def test_fixer_failure_becomes_a_rolled_back_patch_then_the_diagnostician_runs_again(
    script_for, how, sha, cost_after
):
    script, heads = script_for(["prompt_policy", "prompt_policy"], fixes=[how, "ok"])
    run = await run_graph(FAST, heads=heads)
    i = run.nodes.index("Diagnostician")
    assert run.nodes[i:] == [
        "Diagnostician", "Fixer", "Verifier", "Diagnostician", "Fixer", "Verifier", "Runner", "Done",
    ]  # fmt: skip
    first, second = run.final["patches"]
    assert (first.verdict, first.diagnosis_id, first.commit_sha, first.lines_changed, first.checkpoint_id) == (
        "rolled_back", "dx-1", sha, 0, None,
    )  # fmt: skip
    assert first.id.endswith("-refused") and first.id != second.id
    assert second.verdict == "kept"
    # the Verifier saw the plain-English reason and the exception's cost / head commit
    seen = script.verifier_states[0]
    assert seen["fix_rejection_reason"] and not seen["fix_rejection_reason"].startswith(("{", "["))
    assert seen["patches"][-1].verdict == "rolled_back"
    if cost_after is not None:
        assert seen["cost_usd"] == cost_after and seen["head_commit"] == "revert123"
    else:
        assert seen["head_commit"] == "stub0000"
    assert run.final["fix_rejection_reason"] is None  # cleared by the later successful Fixer pass
    assert run.final["stop_reason"] == "all_passed"


async def test_two_refused_fixes_in_a_row_stop_for_no_gain(script_for):
    script, heads = script_for(["code", "code"], fixes=["refused", "rejected"])
    run = await run_graph(FAST, heads=heads)
    assert run.nodes[-2:] == ["Verifier", "Done"]
    assert run.final["stop_reason"] == "no_gain"
    assert run.final["iteration"] == 2
    assert [p.verdict for p in run.final["patches"]] == ["rolled_back", "rolled_back"]
    assert run.final["status"] == "done"


async def test_test_layer_goes_through_the_gate_and_the_fixer_sees_the_matching_approval(script_for):
    script, heads = script_for(["test"])
    run = await run_graph(FAST, heads=heads)
    assert run.nodes[-6:] == ["Diagnostician", "HumanGate", "Fixer", "Verifier", "Runner", "Done"]
    seen = script.fixer_states[0]
    assert seen["gate_decision"] == "approved"
    assert seen["gate_diagnosis_id"] == seen["failure_ledger"][-1].id == "dx-1"
    assert run.final["gate_diagnosis_id"] == "dx-1"
    assert run.final["status"] == "done"


async def test_rejected_gate_clears_the_diagnosis_id_and_never_reaches_the_fixer(script_for):
    script, heads = script_for(["test"])
    run = await run_graph(FAST, heads=heads, answers={"gate": {"approved": False}})
    assert "Fixer" not in run.nodes
    assert run.final["gate_decision"] == "rejected" and run.final["gate_diagnosis_id"] is None
    assert script.fixer_states == []


async def test_an_earlier_approval_cannot_be_replayed_for_a_later_diagnosis(script_for):
    # round 1: test-layer dx-1, human approves, the patch is rolled back. Round 2: test-layer dx-2.
    script, heads = script_for(["test", "test"], verdicts=["rolled_back"])
    seen_at_gate: list[tuple] = []

    async def lazy_gate(state):
        # a gate that "approves" without recording which diagnosis (so only the wrapper can stop a replay)
        # (the stub gate runs twice for one pause: LangGraph re-runs a node when it resumes)
        seen_at_gate.append(
            (state["failure_ledger"][-1].id, state.get("gate_decision"), state.get("gate_diagnosis_id"))
        )
        if state["failure_ledger"][-1].id == "dx-1":
            return await stubs.human_gate(state)
        return {"gate_decision": "approved", "status": "running"}

    run = await run_graph(FAST, heads=Heads(**{**heads.__dict__, "human_gate": lazy_gate}))
    # the new diagnosis voided the old approval before the second gate looked at the state
    assert set(seen_at_gate) == {("dx-1", None, None), ("dx-2", None, None)}
    assert len(script.fixer_states) == 1  # the second approval was refused before the Fixer was called
    second = run.final["patches"][-1]
    assert (second.diagnosis_id, second.verdict, second.id.endswith("-refused")) == ("dx-2", "rolled_back", True)
    assert "you have not approved it" in next(e.message for e in run.events if e.head == "fixer" and e.type == "error")
    assert run.final["stop_reason"] == "no_gain"


async def test_a_matching_id_for_another_diagnosis_is_refused_before_the_fixer(script_for):
    script, heads = script_for(["test", "test"])

    async def wrong_gate(state):
        return {"gate_decision": "approved", "gate_diagnosis_id": "dx-OLD", "status": "running"}

    run = await run_graph(FAST, heads=Heads(**{**heads.__dict__, "human_gate": wrong_gate}))
    assert script.fixer_states == []
    assert [p.verdict for p in run.final["patches"]] == ["rolled_back", "rolled_back"]
    assert run.final["stop_reason"] == "no_gain"


async def test_no_diagnosis_ends_at_done_with_a_plain_message(script_for):
    script, heads = script_for([None])
    run = await run_graph(FAST, heads=heads)
    assert run.nodes[-3:] == ["Runner", "Diagnostician", "Done"]
    assert run.final["stop_reason"] == "no_diagnosis"
    assert run.final["status"] == "done"  # the Runner recorded a baseline score
    assert run.events[-1].message == "Stopped: I couldn't find a cause I could fix for the remaining failures."
    assert run.events[-1].payload["stop_reason"] == "no_diagnosis"
    assert script.fixer_states == []


async def test_no_diagnosis_after_an_earlier_test_diagnosis_does_not_read_the_stale_ledger(script_for):
    # dx-1 is test-layer (gate, approved, rolled back); the next pass finds nothing and must not go to the gate
    script, heads = script_for(["test", None], verdicts=["rolled_back"])
    run = await run_graph(FAST, heads=heads)
    assert run.nodes[-2:] == ["Diagnostician", "Done"]
    assert run.nodes.count("HumanGate") == 1
    assert [d.id for d in run.final["failure_ledger"]] == ["dx-1"]
    assert run.final["stop_reason"] == "no_diagnosis"


async def test_no_diagnosis_without_any_score_is_failed(script_for):
    script, heads = script_for([None])

    async def runner(state):  # fails a scenario but records no metrics
        result = ScenarioResult(
            scenario_id="tr-1", iteration=0, findings=[], matched=0, missed=1, false_positives=0,
            severity_correct=0, passed=False, trace_id="t",
        )  # fmt: skip
        return {"results": [result]}

    run = await run_graph(FAST, heads=Heads(**{**heads.__dict__, "runner": runner}))
    assert run.nodes[-2:] == ["Diagnostician", "Done"]
    assert run.final["status"] == "failed"
    assert run.final["stop_reason"] == "no_diagnosis"


async def test_budget_in_the_fixer_still_stops_cleanly_and_is_not_turned_into_a_patch(script_for):
    script, heads = script_for(["code"], fixes=["budget"])
    run = await run_graph(FAST, heads=heads)
    assert run.nodes[-2:] == ["Fixer", "Done"]
    assert "Verifier" not in run.nodes[run.nodes.index("Fixer") :]
    assert run.final["stop_reason"] == "budget"
    assert run.final["status"] == "done"
    assert run.final["patches"] == []
    assert [e.message for e in run.events if e.type == "error"] == ["Stopped: this run reached its $2.00 cost cap."]


async def test_a_budget_stop_returned_by_the_verifier_goes_straight_to_done(script_for, monkeypatch):
    script, heads = script_for(["code"])

    async def stopping_verifier(state):
        update = await script.verifier(state)
        return {**update, "stop_reason": "budget", "status": "done"}

    run = await run_graph(FAST, heads=Heads(**{**heads.__dict__, "verifier": stopping_verifier}))
    assert run.nodes[-2:] == ["Verifier", "Done"]
    assert run.final["stop_reason"] == "budget"


def test_other_exceptions_from_the_fixer_still_propagate(monkeypatch):
    import asyncio

    async def boom(state, **_):
        raise RuntimeError("worker crashed")

    monkeypatch.setattr(fixer, "run", boom)
    heads = Heads(diagnostician=Script(["code"]).diagnostician, fixer=real_heads().fixer)
    with pytest.raises(RuntimeError, match="worker crashed"):
        asyncio.run(run_graph(FAST, heads=heads))


# --- routing of head-set stops ------------------------------------------------------------------


@pytest.mark.parametrize("reason", sorted(r.HEAD_STOP_REASONS))
def test_head_stop_reasons_route_to_done(reason):
    router = _or_done(lambda _s: "Runner")
    assert router({"stop_reason": reason}) == "Done"


@pytest.mark.parametrize("reason", [None, "all_passed", "iteration_cap", "no_gain", "unknown", ""])
def test_every_other_stop_reason_follows_the_normal_route(reason):
    router = _or_done(lambda _s: "Runner")
    assert router({"stop_reason": reason}) == "Runner"
    assert router({}) == "Runner"


def test_compiled_extra_edges_only_target_done_and_only_exist_for_stop_reasons():
    prd_edges_text = open(__file__.replace("test_real_loop.py", "test_graph.py")).read()
    assert "BUDGET_EDGES" in prd_edges_text  # the edge-set test lives in test_graph.py
    graph = build_graph(real_heads(), checkpointer=memory_checkpointer()).get_graph()
    edges = {(e.source, e.target) for e in graph.edges}
    from test_graph import PRD_EDGES

    extras = edges - PRD_EDGES
    assert extras and {t for _, t in extras} == {"Done"}
    # each extra edge is the `_or_done` stop edge: following it needs a head-set stop_reason
    assert r.HEAD_STOP_REASONS == {"budget", "no_diagnosis"}


def test_a_rejected_fix_whose_revert_failed_fails_the_run_loudly(script_for):
    import asyncio

    _, heads = script_for(["prompt_policy", "prompt_policy"], fixes=["revert_failed", "ok"])
    with pytest.raises(fixer.FixRejected, match="manual rollback"):
        asyncio.run(run_graph(FAST, heads=heads))


async def test_a_fixer_that_changed_nothing_becomes_a_refused_patch(script_for):
    script, heads = script_for(["prompt_policy", "prompt_policy"], fixes=["nochange", "ok"])
    run = await run_graph(FAST, heads=heads)
    first, second = run.final["patches"]
    assert first.verdict == "rolled_back" and first.id.endswith("-refused") and first.lines_changed == 0
    assert "without changing anything" in script.verifier_states[0]["fix_rejection_reason"]
    assert second.verdict == "kept" and run.final["stop_reason"] == "all_passed"
