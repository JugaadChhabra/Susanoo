"""LangGraph recipe (I14): a deterministic collections graph served as a ``SusanooAgent`` over JSONL [Spec §4, §19].

Tested with langgraph 1.2.14 (``tests/runtime/test_recipes.py``). No model key: nodes use fixed parsing
rules, so the run is reproducible. Swap the nodes for LLM-backed ones in a real agent; the wrapper stays:

- one LangGraph thread per Susanoo session (``InMemorySaver`` keyed by ``session_id``) for multi-turn memory;
- the approved ``ToolClient`` is passed through LangGraph's runtime ``context``, never through graph state
  or ``configurable``, so the route token is never checkpointed;
- tool events are reported from the client, not invented.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypedDict

from collections_demo.agent import ACCOUNT, AMOUNT, CONFIRM, DATE, HUMAN, REJECTION_HANDOFF, WAIVE, to_minor
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from susanoo_sdk import InvocationContext, Message, Reply, SusanooAgent, ToolClient, ToolTransportError


class Conversation(TypedDict, total=False):
    text: str
    account_id: str | None
    amount_minor: int | None
    due_date: str | None
    promise_id: str | None
    handed_off: bool
    reply: str


@dataclass
class Deps:
    tools: ToolClient
    attempt_id: str


def absorb(state: Conversation) -> Conversation:
    text, update = state["text"], {}
    if match := ACCOUNT.search(text):
        update["account_id"] = match.group(0)
    if match := AMOUNT.search(text):
        update["amount_minor"] = to_minor(match.group(1) or match.group(2))
    if match := DATE.search(text):
        update["due_date"] = match.group(1)
    return update


def route(state: Conversation) -> Literal["ask", "handoff", "already", "promise"]:
    text = state["text"]
    if not state.get("account_id"):
        return "ask"
    if HUMAN.search(text) or WAIVE.search(text):
        return "handoff"
    if state.get("promise_id") and CONFIRM.search(text):
        return "already"
    if state.get("amount_minor") is None or state.get("due_date") is None:
        return "ask"
    return "promise"


def ask(state: Conversation) -> Conversation:
    if not state.get("account_id"):
        return {"reply": "Could you share your account number? It starts with ACC-."}
    if state.get("amount_minor") is None:
        return {"reply": "How much can you pay?"}
    return {"reply": "On what date can you make the payment? Please use YYYY-MM-DD."}


def already(state: Conversation) -> Conversation:
    return {"reply": f"Your promise {state['promise_id']} is already recorded."}


async def handoff(state: Conversation, runtime: Runtime[Deps]) -> Conversation:
    if not state.get("handed_off"):
        reason = "dispute" if HUMAN.search(state["text"]) else "out_of_policy"
        result = await runtime.context.tools.call(
            "request_handoff", {"account_id": state["account_id"], "reason": reason}
        )
        if result.status not in {"ok", "replayed"}:
            return {"reply": "I couldn't reach a specialist. Please call back."}
    return {"handed_off": True, "reply": "I've passed this to a specialist who will call you."}


async def promise(state: Conversation, runtime: Runtime[Deps]) -> Conversation:
    tools = runtime.context.tools
    account = await tools.call("read_account", {"account_id": state["account_id"]})
    if account.status == "rejected":
        return {"reply": "I couldn't find that account. A specialist will help."}
    info = account.response["account"]
    request = {
        "account_id": state["account_id"],
        "amount": {"amount_minor": state["amount_minor"], "currency": info["balance"]["currency"]},
        "due_date": {"date": state["due_date"], "timezone": info["timezone"]},
    }
    key = f"{runtime.context.attempt_id}:{state['account_id']}:{state['amount_minor']}:{state['due_date']}"
    try:
        result = await tools.call("create_promise", request, key)
    except ToolTransportError:
        try:  # an ambiguous write is retried only with the same idempotency key
            result = await tools.call("create_promise", request, key)
        except ToolTransportError:
            return {"reply": "I couldn't confirm the promise because the system didn't respond."}
    if result.status in {"ok", "replayed"}:
        promise_id = result.response["promise"]["id"]
        return {"promise_id": promise_id, "reply": f"Your promise is recorded as {promise_id}."}
    if result.code in REJECTION_HANDOFF:
        return {"reply": "__handoff__"}
    return {"reply": "I couldn't record that promise. A specialist will follow up."}


def after_promise(state: Conversation) -> Literal["handoff", "__end__"]:
    return "handoff" if state.get("reply") == "__handoff__" else END


def build_graph():
    graph = StateGraph(Conversation, context_schema=Deps)
    graph.add_node("absorb", absorb)
    graph.add_node("ask", ask)
    graph.add_node("already", already)
    graph.add_node("handoff", handoff)
    graph.add_node("promise", promise)
    graph.add_edge(START, "absorb")
    graph.add_conditional_edges("absorb", route)
    graph.add_conditional_edges("promise", after_promise)
    for node in ("ask", "already", "handoff"):
        graph.add_edge(node, END)
    return graph.compile(checkpointer=InMemorySaver())


class LangGraphAgent(SusanooAgent):
    """Wraps any compiled LangGraph whose state has ``text`` in and ``reply`` out."""

    name = "langgraph-collections-recipe"
    version = "1.0"

    def __init__(self) -> None:
        self.graph = build_graph()

    async def respond(self, session_id: str, message: Message, context: InvocationContext) -> Reply:
        if context.tool_route is None:
            return Reply(text="I can't reach the account system right now.")
        tools = ToolClient(context.tool_route)
        try:
            state = await self.graph.ainvoke(
                {"text": message.text},
                config={"configurable": {"thread_id": session_id}},
                context=Deps(tools=tools, attempt_id=context.attempt_id),
            )
        finally:
            await tools.aclose()
        return Reply(text=state["reply"], tool_events=tools.events)
