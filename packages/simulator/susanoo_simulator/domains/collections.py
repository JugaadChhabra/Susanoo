"""Synthetic collections sandbox [Spec §2, §14]: accounts, promises-to-pay, handoffs, permissions.

This is a synthetic reference implementation, not a replica certified against any vendor. Policy
lives in a ``policy`` entity so a simulator and a reference can differ in an inspectable way.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from ..engine import Domain, OpContext, Operation, _iso

VERSION = "1.0"
DEFAULT_POLICY = {"id": "policy", "max_promise_days": 30, "single_open_promise": False}


class _Req(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MoneyIn(_Req):
    amount_minor: StrictInt
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class DateIn(_Req):
    date: date
    timezone: str


class AccountRef(_Req):
    account_id: str = Field(min_length=1, max_length=64)


class CreatePromise(AccountRef):
    amount: MoneyIn
    due_date: DateIn
    note: str | None = Field(default=None, max_length=500)


class RequestHandoff(AccountRef):
    reason: Literal["out_of_policy", "customer_request", "dispute", "hardship", "other"]
    note: str | None = Field(default=None, max_length=500)


class AdjustBalance(AccountRef):
    balance: MoneyIn


def _policy(ctx: OpContext) -> dict[str, Any]:
    return ctx.get("policy", "policy") or DEFAULT_POLICY


def _account(ctx: OpContext, account_id: str) -> dict[str, Any]:
    account = ctx.get("account", account_id)
    if account is None:
        ctx.reject("not_found", f"Account {account_id} does not exist")
    return account


def _public(account: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "holder_name", "balance", "fees", "minimum_installment", "status", "timezone")
    return {key: account[key] for key in keys if key in account}


def read_account(ctx: OpContext, request: dict[str, Any]) -> dict[str, Any]:
    return {"account": _public(_account(ctx, request["account_id"]))}


def list_promises(ctx: OpContext, request: dict[str, Any]) -> dict[str, Any]:
    _account(ctx, request["account_id"])
    rows = ctx.list("promise", lambda row: row["account_id"] == request["account_id"])
    return {"promises": sorted(rows, key=lambda row: row["id"])}


def create_promise(ctx: OpContext, request: dict[str, Any]) -> dict[str, Any]:
    account = _account(ctx, request["account_id"])
    policy = _policy(ctx)
    amount, due = request["amount"], request["due_date"]
    if account["status"] not in {"active", "promise_to_pay"}:
        ctx.reject("account_not_eligible", f"Account status {account['status']} does not accept promises")
    if amount["currency"] != account["balance"]["currency"]:
        ctx.reject("currency_mismatch", "Promise currency differs from the account balance")
    minimum, balance = account["minimum_installment"]["amount_minor"], account["balance"]["amount_minor"]
    if not minimum <= amount["amount_minor"] <= balance:
        ctx.reject("amount_out_of_policy", f"Amount must be between {minimum} and {balance} minor units")
    if due["timezone"] != account["timezone"]:
        ctx.reject("timezone_mismatch", "Due date must use the account timezone")
    today = ctx.now.astimezone(ZoneInfo(account["timezone"])).date()
    days = (date.fromisoformat(due["date"]) - today).days
    if not 1 <= days <= policy["max_promise_days"]:
        ctx.reject("date_out_of_policy", f"Due date must be 1 to {policy['max_promise_days']} days ahead")
    if policy.get("single_open_promise"):
        open_rows = ctx.list("promise", lambda row: row["account_id"] == account["id"] and row["status"] == "open")
        if open_rows:
            ctx.reject("open_promise_exists", f"Account already has open promise {open_rows[0]['id']}")
    promise = ctx.create(
        "promise",
        {
            "account_id": account["id"],
            "amount": amount,
            "due_date": due,
            "status": "open",
            "created_by": ctx.actor,
            "created_at": _iso(ctx.now),
            "note": request.get("note"),
        },
    )
    ctx.update("account", account["id"], {"status": "promise_to_pay"})
    return {"promise": promise}


def request_handoff(ctx: OpContext, request: dict[str, Any]) -> dict[str, Any]:
    account = _account(ctx, request["account_id"])
    handoff = ctx.create(
        "handoff",
        {
            "account_id": account["id"],
            "reason": request["reason"],
            "note": request.get("note"),
            "created_by": ctx.actor,
            "created_at": _iso(ctx.now),
        },
    )
    return {"handoff": handoff}


def adjust_balance(ctx: OpContext, request: dict[str, Any]) -> dict[str, Any]:
    account = _account(ctx, request["account_id"])
    return {"account": _public(ctx.update("account", account["id"], {"balance": request["balance"]}))}


def waive_fees(ctx: OpContext, request: dict[str, Any]) -> dict[str, Any]:
    account = _account(ctx, request["account_id"])
    zero = {"amount_minor": 0, "currency": account["fees"]["currency"]}
    return {"account": _public(ctx.update("account", account["id"], {"fees": zero}))}


DOMAIN = Domain(
    name="collections",
    version=VERSION,
    entity_kinds=("account", "promise", "handoff", "policy"),
    id_prefixes={"promise": "PTP", "handoff": "HND", "account": "ACC"},
    operations={
        "read_account": Operation("read_account", "account:read", False, read_account, AccountRef),
        "list_promises": Operation("list_promises", "promise:read", False, list_promises, AccountRef),
        "create_promise": Operation("create_promise", "promise:create", True, create_promise, CreatePromise),
        "request_handoff": Operation("request_handoff", "handoff:create", True, request_handoff, RequestHandoff),
        "adjust_balance": Operation("adjust_balance", "balance:adjust", True, adjust_balance, AdjustBalance),
        "waive_fees": Operation("waive_fees", "fees:waive", True, waive_fees, AccountRef),
    },
)

AGENT_PERMISSIONS = ["account:read", "promise:read", "promise:create", "handoff:create"]
AGENT_OPERATIONS = list(DOMAIN.operations)


def account(
    account_id: str,
    *,
    balance: int = 1_200_000,
    minimum: int = 100_000,
    fees: int = 25_000,
    status: str = "active",
    holder: str = "Synthetic Holder",
    timezone: str = "Asia/Kolkata",
    currency: str = "INR",
) -> dict[str, Any]:
    """A synthetic account record. Amounts are integer minor units (paise for INR)."""
    return {
        "id": account_id,
        "holder_name": holder,
        "balance": {"amount_minor": balance, "currency": currency},
        "fees": {"amount_minor": fees, "currency": currency},
        "minimum_installment": {"amount_minor": minimum, "currency": currency},
        "status": status,
        "timezone": timezone,
    }


def utc_iso(moment: datetime) -> str:
    return _iso(moment)
