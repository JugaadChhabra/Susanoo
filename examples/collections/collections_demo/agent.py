"""Deterministic synthetic collections agent with switchable faulty behaviours. No model key needed.

The agent parses synthetic customer messages with fixed rules, calls the collections tools through
its approved route and replies in plain English. Faulty modes reproduce the failures Susanoo must
catch from independent evidence, not from what the agent says.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Literal

from susanoo_sdk import InvocationContext, Message, Reply, SusanooAgent, ToolClient, ToolTransportError

Mode = Literal[
    "correct",
    "false_completion",
    "wrong_amount",
    "wrong_account",
    "duplicate",
    "forbidden_change",
    "handoff",
    "timeout_after_write",
    "lost_ack",
]
MODES: tuple[Mode, ...] = Mode.__args__  # type: ignore[attr-defined]

ACCOUNT = re.compile(r"\bACC-\d{3,6}\b")
AMOUNT = re.compile(r"(?:₹|rs\.?|inr)\s*([\d,]+(?:\.\d{1,2})?)|([\d,]+(?:\.\d{1,2})?)\s*(?:rupees|inr)", re.I)
DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
WAIVE = re.compile(r"\bwaive|\bremove (?:the )?(?:late )?fees?\b", re.I)
HUMAN = re.compile(r"\bdispute\b|\bnot my (?:loan|account)\b|\bspeak to (?:a )?(?:person|human|agent)\b", re.I)
CONFIRM = re.compile(r"\bconfirm\b", re.I)
REJECTION_HANDOFF = {"amount_out_of_policy", "date_out_of_policy", "account_not_eligible", "currency_mismatch"}


def to_minor(text: str) -> int | None:
    try:
        value = Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None
    return int(value * 100) if value == value.quantize(Decimal("0.01")) else None


@dataclass
class Conversation:
    account_id: str | None = None
    amount_minor: int | None = None
    due_date: str | None = None
    timezone: str | None = None
    promise_id: str | None = None
    handed_off: bool = False
    notes: list[str] = field(default_factory=list)


class CollectionsAgent(SusanooAgent):
    name = "collections-demo"
    version = "1.0"

    def __init__(self, mode: Mode = "correct", *, hang_seconds: float = 3600):
        if mode not in MODES:
            raise ValueError(f"Unknown mode {mode}")
        self.mode, self.hang_seconds = mode, hang_seconds
        self.sessions: dict[str, Conversation] = {}

    async def start_session(self, context: InvocationContext) -> str:
        session_id = await super().start_session(context)
        self.sessions[session_id] = Conversation()
        return session_id

    async def close_session(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)

    async def respond(self, session_id: str, message: Message, context: InvocationContext) -> Reply:
        convo = self.sessions.setdefault(session_id, Conversation())
        self._absorb(convo, message.text)
        if context.tool_route is None:
            return Reply(text="I can't reach the account system right now.")
        tools = ToolClient(context.tool_route)
        try:
            text = await self._act(convo, message.text, tools, context)
        finally:
            await tools.aclose()
        return Reply(text=text, tool_events=tools.events)

    def _absorb(self, convo: Conversation, text: str) -> None:
        if match := ACCOUNT.search(text):
            convo.account_id = match.group(0)
        if match := AMOUNT.search(text):
            convo.amount_minor = to_minor(match.group(1) or match.group(2))
        if match := DATE.search(text):
            convo.due_date = match.group(1)

    async def _act(self, convo: Conversation, text: str, tools: ToolClient, ctx: InvocationContext) -> str:
        if convo.account_id is None:
            return "Could you share your account number? It starts with ACC-."
        if self.mode == "handoff":
            return await self._handoff(convo, tools, "customer_request", "I've asked a specialist to call you back.")
        if HUMAN.search(text):
            return await self._handoff(convo, tools, "dispute", "I've passed this to a specialist who will call you.")
        if WAIVE.search(text):
            if self.mode == "forbidden_change":
                await tools.call("waive_fees", {"account_id": convo.account_id})
                return "I've waived your late fees."
            if self.mode == "false_completion":
                return "Done, your fees are waived."
            return await self._handoff(
                convo, tools, "out_of_policy", "I can't waive fees myself, so I've asked a specialist to review it."
            )
        if convo.promise_id and CONFIRM.search(text) and self.mode not in {"duplicate", "lost_ack"}:
            return f"Your promise {convo.promise_id} is already recorded."
        if convo.amount_minor is None:
            return "How much can you pay?"
        if convo.due_date is None:
            return "On what date can you make the payment? Please use YYYY-MM-DD."
        if self.mode == "false_completion":
            rupees = Decimal(convo.amount_minor) / 100
            return f"Done! I've recorded your promise to pay ₹{rupees:,.2f} on {convo.due_date}."
        return await self._promise(convo, tools, ctx)

    async def _promise(self, convo: Conversation, tools: ToolClient, ctx: InvocationContext) -> str:
        account = await tools.call("read_account", {"account_id": convo.account_id})
        if account.status == "rejected":
            return await self._handoff(
                convo, tools, "other", "I couldn't find that account, so a specialist will help."
            )
        info = account.response["account"]
        account_id, amount = convo.account_id, convo.amount_minor
        if self.mode == "wrong_account":
            prefix, digits = account_id.rsplit("-", 1)
            account_id = f"{prefix}-{int(digits) + 1:0{len(digits)}d}"
        if self.mode == "wrong_amount":
            amount = amount // 10
        request = {
            "account_id": account_id,
            "amount": {"amount_minor": amount, "currency": info["balance"]["currency"]},
            "due_date": {"date": convo.due_date, "timezone": info["timezone"]},
        }
        safe_key = f"{ctx.attempt_id}:{account_id}:{amount}:{convo.due_date}"
        key = None if self.mode in {"duplicate", "lost_ack"} else safe_key
        try:
            result = await tools.call("create_promise", request, key)
        except ToolTransportError:
            # A lost acknowledgement may still have written. Retry only with the same idempotency key.
            try:
                result = await tools.call("create_promise", request, key)
            except ToolTransportError:
                return "I couldn't confirm the promise because the system didn't respond. Please call back."
        if self.mode == "duplicate":
            await tools.call("create_promise", request, None)
        if result.status in {"ok", "replayed"}:
            convo.promise_id = result.response["promise"]["id"]
            if self.mode == "forbidden_change":
                await tools.call("waive_fees", {"account_id": account_id})
            if self.mode == "timeout_after_write":
                await asyncio.sleep(self.hang_seconds)
            rupees = Decimal(convo.amount_minor) / 100
            return (
                f"Thank you. Your promise to pay ₹{rupees:,.2f} on {convo.due_date} is recorded as {convo.promise_id}."
            )
        if result.code in REJECTION_HANDOFF:
            return await self._handoff(
                convo,
                tools,
                "out_of_policy",
                "That falls outside what I can agree, so I've asked a specialist to call.",
            )
        return "I couldn't record that promise. A specialist will follow up."

    async def _handoff(self, convo: Conversation, tools: ToolClient, reason: str, text: str) -> str:
        if not convo.handed_off:
            result = await tools.call("request_handoff", {"account_id": convo.account_id, "reason": reason})
            convo.handed_off = result.status in {"ok", "replayed"}
        return text
