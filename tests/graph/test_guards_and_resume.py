"""Review fixes: budget stop, bounded test-layer loop, strict approvals, validated resume, exactly-once pause events."""

from __future__ import annotations

import pytest
from graph_helpers import run_graph
from langgraph.types import Command

from susanoo.emit import InMemorySink, use_sink
from susanoo.graph import stubs
from susanoo.graph.build import Heads, build_graph
from susanoo.graph.checkpointer import memory_checkpointer
from susanoo.graph.config import RECURSION_LIMIT, run_config
from susanoo.graph.resume import resume_run, validate_resume
from susanoo.graph.routing import MAX_ITERATIONS
from susanoo.graph.state import initial_state
from susanoo.schemas import Diagnosis, IterationMetrics, ScenarioResult

# --- 1. budget stop ends the run at Done ------------------------------------------------------


async def test_cost_cap_before_any_score_ends_at_done_as_failed(monkeypatch):
    monkeypatch.setenv("RUN_COST_CAP_USD", "0.3")
    run = await run_graph()
    assert run.nodes[-2:] == ["Simulator", "Done"]  # Runner never ran
    assert run.final["status"] == "failed"
    assert run.final["stop_reason"] == "budget"
    errors = [e for e in run.events if e.type == "error"]
    assert [(e.head, e.message) for e in errors] == [("simulator", "Stopped: this run reached its $0.30 cost cap.")]
    assert run.events[-1].head == "system" and run.events[-1].payload["stop_reason"] == "budget"
    assert "cost limit" in run.events[-1].message


async def test_cost_cap_after_a_score_exists_ends_at_done_as_done(monkeypatch):
    monkeypatch.setenv("RUN_COST_CAP_USD", "0.4")
    run = await run_graph()
    assert run.nodes[-2:] == ["Diagnostician", "Done"] or run.nodes[-2:] == ["Fixer", "Done"]
    assert run.final["status"] == "done"  # we have a baseline score
    assert run.final["stop_reason"] == "budget"
    assert [e.type for e in run.events].count("error") == 1


async def test_budget_stop_skips_the_nodes_own_next_edge(monkeypatch):
    monkeypatch.setenv("RUN_COST_CAP_USD", "0.3")
    run = await run_graph()
    assert "Runner" not in run.nodes  # the Simulator -> Runner edge did not fire alongside Done


# --- 2. the test-layer / rejected-gate loop is bounded ----------------------------------------


def _loop_heads(f1_for):
    """A diagnostician that always blames a test, and a Runner that always fails a fresh scenario."""

    async def runner(state):
        i = state.get("iteration", 0)
        result = ScenarioResult(
            scenario_id=f"s{i}", iteration=i, findings=[], matched=0, missed=1, false_positives=0,
            severity_correct=0, passed=False, trace_id="t",
        )  # fmt: skip
        update = {"results": [result]}
        metrics = list(state.get("metrics", []))
        if not any(m.iteration == i for m in metrics):
            f1 = f1_for(i)
            metrics.append(
                IterationMetrics(
                    iteration=i, recall=f1, precision=f1, f1=f1, severity_accuracy=f1, heldout_f1=f1,
                    acceptance_pass_rate=0, cost_usd=0,
                )
            )  # fmt: skip
            update["metrics"] = metrics
        return update

    async def diagnostician(state):
        failing = [r.scenario_id for r in state["results"] if not r.passed]
        dx = Diagnosis(
            id=f"dx-{len(state['failure_ledger']) + 1}", scenario_ids=failing, layer="test", hypothesis="h",
            evidence_span_ids=[], target_files=[],
        )  # fmt: skip
        return {"failure_ledger": [*state["failure_ledger"], dx]}

    return Heads(runner=runner, diagnostician=diagnostician)


async def test_always_test_layer_and_always_reject_stops_for_no_gain_whatever_the_score():
    # rejected gates are rounds without a kept patch; a rising held-out F1 does not count as improvement
    run = await run_graph(heads=_loop_heads(lambda i: 0.1 * (i + 1)), answers={"gate": {"approved": False}})
    assert run.final["stop_reason"] == "no_gain"
    assert run.final["iteration"] == 2
    assert run.final["gate_rejected_rounds"] == [1, 2]
    assert run.nodes.count("HumanGate") == 2
    assert run.nodes[-2:] == ["Runner", "Done"]
    assert run.final["status"] == "done"


async def test_kept_patch_every_round_ends_at_the_iteration_cap():
    from susanoo.schemas import Patch

    heads = _loop_heads(lambda i: 0.1 * (i + 1))

    async def diagnostician(state):
        failing = [r.scenario_id for r in state["results"] if not r.passed]
        dx = Diagnosis(
            id=f"dx-{len(state['failure_ledger']) + 1}", scenario_ids=failing, layer="code", hypothesis="h",
            evidence_span_ids=[], target_files=[],
        )  # fmt: skip
        return {"failure_ledger": [*state["failure_ledger"], dx]}

    async def fixer(state):
        dx = state["failure_ledger"][-1]
        n = len(state["patches"]) + 1
        patch = Patch(
            id=f"p{n}", diagnosis_id=dx.id, commit_sha=f"c{n}", checkpoint_id=None, lines_changed=1, verdict="pending"
        )
        return {"patches": [*state["patches"], patch]}

    async def verifier(state):
        patches = list(state["patches"])
        patches[-1] = patches[-1].model_copy(update={"verdict": "kept"})
        return {"patches": patches, "iteration": state["iteration"] + 1}

    run = await run_graph(heads=Heads(runner=heads.runner, diagnostician=diagnostician, fixer=fixer, verifier=verifier))
    assert run.final["stop_reason"] == "iteration_cap"
    assert run.final["iteration"] == MAX_ITERATIONS
    assert run.nodes.count("Verifier") == MAX_ITERATIONS
    assert run.nodes[-2:] == ["Verifier", "Done"]


async def test_flat_score_while_rejecting_stops_for_no_gain():
    run = await run_graph(heads=_loop_heads(lambda i: 0.5), answers={"gate": {"approved": False}})
    assert run.final["stop_reason"] == "no_gain"
    assert run.final["iteration"] == 2
    assert run.nodes[-2:] == ["Runner", "Done"]


def test_recursion_limit_covers_the_worst_case_run():
    assert RECURSION_LIMIT == 8 + 4 * MAX_ITERATIONS + 1 + 11
    assert run_config("r1") == {"configurable": {"thread_id": "r1"}, "recursion_limit": RECURSION_LIMIT}


# --- 3. malformed approvals never approve -----------------------------------------------------


@pytest.mark.parametrize(
    "reply", [True, "yes", 1, {"approved": "yes"}, {"approved": 1}, {"approved": False}, {"ok": True}]
)
async def test_gate_approves_only_on_an_explicit_true(reply):
    run = await run_graph(answers={"gate": reply})
    assert run.final["gate_decision"] == "rejected"
    assert run.final["disputed_scenario_ids"] == ["tr-1"]
    assert len(run.final["patches"]) == 1  # no test change was made


async def test_gate_approves_on_explicit_true():
    run = await run_graph(answers={"gate": {"approved": True}})
    assert run.final["gate_decision"] == "approved"


@pytest.mark.parametrize("reply", [{"foo": 1}, {"approved": ["acc-1"]}, {"approved_ids": "acc-1"}, "yes", True])
async def test_scenario_approval_without_approved_ids_raises_instead_of_approving_all(reply):
    graph = build_graph(checkpointer=memory_checkpointer())
    config = run_config("r")
    with use_sink(InMemorySink()):
        await graph.ainvoke({**initial_state("r", "s"), "intake_questions": []}, config)
        with pytest.raises(ValueError):
            await graph.ainvoke(Command(resume=reply), config)
    assert "plan" not in (await graph.aget_state(config)).values


async def test_scenario_approval_extended_shape_applies_rejections_and_edits():
    reply = {
        "approved_ids": ["acc-1", "acc-2"],
        "rejected_ids": ["acc-2"],
        "edits": {"acc-1": {"title": "A developer logs a customer's phone number."}},
    }
    run = await run_graph({"intake_questions": []}, answers={"scenarios": reply})
    acceptance = [s for s in run.final["scenarios"] if s.split == "acceptance"]
    assert [(s.id, s.title, s.approved_by_user) for s in acceptance] == [
        ("acc-1", "A developer logs a customer's phone number.", True)
    ]
    finished = next(e for e in run.events if e.message.endswith("locked as the acceptance set."))
    assert finished.payload == {"approved": ["acc-1"], "rejected": ["acc-2"], "edited": ["acc-1"]}


async def test_an_empty_approved_ids_list_approves_nothing():
    run = await run_graph({"intake_questions": []}, answers={"scenarios": {"approved_ids": []}})
    assert [s for s in run.final["scenarios"] if s.split == "acceptance"] == []


# --- 4. validated resume, exactly-once pause events -------------------------------------------

BAD_FOR_QUESTIONS = [
    {},
    None,
    [],
    "yes",
    {"foo": 1},
    {"answers": "x"},
    {"approved_ids": ["acc-1"]},
    {"answers": {"nope": "x"}},
]


@pytest.mark.parametrize("bad", BAD_FOR_QUESTIONS)
async def test_resume_run_rejects_bad_values_before_touching_the_graph(bad):
    saver = memory_checkpointer()
    graph = build_graph(checkpointer=saver)
    sink = InMemorySink()
    with use_sink(sink):
        await graph.ainvoke(initial_state("r", "s"), run_config("r"))
        before = len(sink.events)
        with pytest.raises(ValueError):
            await resume_run(graph, "r", bad)
    assert len(sink.events) == before  # nothing was emitted by the failed resume
    assert (await graph.aget_state(run_config("r"))).next == ("UserQuestions",)


async def test_question_needed_is_emitted_once_across_bad_and_good_resumes():
    graph = build_graph(checkpointer=memory_checkpointer())
    sink = InMemorySink()
    config = run_config("r")
    with use_sink(sink):
        await graph.ainvoke(initial_state("r", "s"), config)
        with pytest.raises(ValueError):
            await resume_run(graph, "r", {})
        with pytest.raises(ValueError):
            await resume_run(graph, "r", None)
        # a raw resume that bypasses the validator makes LangGraph pause again; it must not re-announce.
        # (Command(resume=None) crashes inside LangGraph itself, which is why resume_run exists.)
        await graph.ainvoke(Command(resume={}), config)
        assert (await graph.aget_state(config)).next == ("UserQuestions",)
        out = await resume_run(graph, "r", {"answers": {"q-strictness": "block"}})
    assert out["__interrupt__"][0].value["kind"] == "scenarios"
    assert [e.type for e in sink.events].count("question_needed") == 1
    assert sink.events[3].type == "question_needed" and sink.events[4].type == "finished"
    assert [e.seq for e in sink.events] == list(range(1, len(sink.events) + 1))


async def test_every_pause_is_announced_once_even_after_raw_bad_resumes():
    graph = build_graph(checkpointer=memory_checkpointer())
    sink = InMemorySink()
    config = run_config("r")
    answers = [
        {"answers": {}},
        {"approved_ids": ["acc-1", "acc-2"]},
        {"approved": True},
    ]
    with use_sink(sink):
        out = await graph.ainvoke(initial_state("r", "s"), config)
        for answer in answers:
            await graph.ainvoke(Command(resume={}), config)  # ignored, pauses again
            out = await resume_run(graph, "r", answer)
    assert "__interrupt__" not in out
    types = [(e.head, e.type) for e in sink.events]
    assert types.count(("planner", "question_needed")) == 1
    assert types.count(("planner", "approval_needed")) == 1
    assert types.count(("fixer", "approval_needed")) == 1


async def test_resume_run_without_a_pending_pause_raises():
    graph = build_graph(checkpointer=memory_checkpointer())
    with use_sink(InMemorySink()):
        with pytest.raises(ValueError, match="not waiting"):
            await resume_run(graph, "never-started", {"answers": {}})
        await graph.ainvoke({**initial_state("done", "s"), "intake_questions": []}, run_config("done"))
        await resume_run(graph, "done", {"approved_ids": ["acc-1", "acc-2"]})
        out = await resume_run(graph, "done", {"approved": True})
        assert out["status"] == "done"
        with pytest.raises(ValueError, match="not waiting"):
            await resume_run(graph, "done", {"approved": True})


@pytest.mark.parametrize(
    ("kind", "good"),
    [
        ("questions", {"answers": {"q": "a"}}),
        ("scenarios", {"approved_ids": []}),
        ("scenarios", {"approved_ids": ["a"], "rejected_ids": ["b"], "edits": {"a": {"title": "t"}}}),
        ("gate", {"approved": True}),
        ("gate", {"approved": False}),
    ],
)
def test_validate_resume_accepts_good_shapes(kind, good):
    validate_resume(kind, good)


@pytest.mark.parametrize(
    ("kind", "bad"),
    [
        ("gate", {"approved": "yes"}),
        ("gate", {"approved": 1}),
        ("gate", True),
        ("scenarios", {"approved_ids": ["a"], "edits": {"a": "title"}}),
        ("scenarios", {"approved_ids": [1]}),
        ("questions", {"answers": []}),
        ("mystery", {"x": 1}),
    ],
)
def test_validate_resume_rejects_bad_shapes(kind, bad):
    with pytest.raises(ValueError):
        validate_resume(kind, bad)


def test_stub_module_has_no_private_pause_left():
    assert not hasattr(stubs, "_pause")
