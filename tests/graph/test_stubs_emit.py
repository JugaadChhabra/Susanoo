"""Stub heads follow the head interface; emit sinks; checkpointer setup. [PRD › Contracts (locked) › Head interface]"""

from __future__ import annotations

import os

import pytest

from susanoo import emit as emit_module
from susanoo.emit import InMemorySink, emit, use_sink
from susanoo.graph import stubs
from susanoo.graph.checkpointer import postgres_checkpointer
from susanoo.guards import budget

MODEL_HEADS = ["planner", "builder", "agent_smith", "simulator", "diagnostician"]


async def test_in_memory_sink_numbers_events_per_run():
    sink = InMemorySink()
    with use_sink(sink):
        a = await emit({"run_id": "a"}, "planner", "started", "one")
        b = await emit({"run_id": "a"}, "planner", "finished", "two", {"k": 1})
        c = await emit({"run_id": "b"}, "system", "progress", "other run")
    assert (a.seq, b.seq, c.seq) == (1, 2, 1)
    assert b.payload == {"k": 1} and b.at.endswith("+00:00")
    assert sink.events == [a, b, c]


async def test_in_memory_sink_validates_against_the_contract():
    with use_sink(InMemorySink()), pytest.raises(ValueError):
        await emit({"run_id": "a"}, "not-a-head", "started", "x")


async def test_default_sink_is_the_database_writer(monkeypatch):
    calls = []

    async def fake_emit_event(*args):
        calls.append(args)
        return "event"

    monkeypatch.setattr("susanoo.db.emit_event", fake_emit_event)
    assert await emit({"run_id": "r"}, "planner", "started", "hi", {"a": 1}, "http://trace") == "event"
    assert calls == [("r", "planner", "started", "hi", {"a": 1}, "http://trace")]
    assert emit_module._sink.get() is emit_module._db_sink


@pytest.mark.parametrize("name", MODEL_HEADS)
async def test_model_stubs_check_the_budget(name, monkeypatch):
    monkeypatch.setenv("RUN_COST_CAP_USD", "1")
    state = {"run_id": "r", "cost_usd": 1.0, "failure_ledger": [], "results": [], "patches": []}
    with use_sink(InMemorySink()), pytest.raises(budget.BudgetExceeded):
        await getattr(stubs, name)(state)


async def test_stub_returns_only_changed_keys_and_emits_started_then_finished():
    sink = InMemorySink()
    state = {"run_id": "r", "cost_usd": 0.0, "intake_answers": {}}
    with use_sink(sink):
        update = await stubs.planner(state)
    assert set(update) == {"plan", "cost_usd"}
    assert [e.type for e in sink.events] == ["started", "finished"]
    assert state == {"run_id": "r", "cost_usd": 0.0, "intake_answers": {}}  # input not mutated


async def test_missing_database_url_raises_a_clear_error(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="DATABASE_URL is not set"):
        async with postgres_checkpointer():
            pass


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")
async def test_postgres_checkpointer_runs_the_graph_end_to_end():
    """Creates the checkpoint tables in TEST_DATABASE_URL (never DATABASE_URL) and runs the stub graph."""
    import uuid

    from langgraph.types import Command

    from susanoo.graph.build import build_graph

    thread = f"test-{uuid.uuid4()}"
    config = {"configurable": {"thread_id": thread}}
    with use_sink(InMemorySink()):
        async with postgres_checkpointer(os.environ["TEST_DATABASE_URL"]) as saver:
            graph = build_graph(checkpointer=saver)
            out = await graph.ainvoke({"run_id": thread, "spec": "s", "intake_questions": []}, config)
            assert out["__interrupt__"][0].value["kind"] == "scenarios"
        async with postgres_checkpointer(os.environ["TEST_DATABASE_URL"], run_setup=False) as saver:  # new connection
            graph = build_graph(checkpointer=saver)
            out = await graph.ainvoke(Command(resume={"approved_ids": ["acc-1", "acc-2"]}), config)
            assert out["__interrupt__"][0].value["kind"] == "gate"
