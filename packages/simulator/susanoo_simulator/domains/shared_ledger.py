"""Shared-ledger collision domain [Spec §21 › Shared-state collisions; T35].

A synthetic account ledger meant for an *intentionally shared* fixture. It offers versioned
compare-and-set operations (``post_payment``, ``claim_account``, ``release_account``) and, as a seeded
defect surface, unchecked absolute writes (``set_balance``, ``set_owner``) that let a client do a
read-modify-write without a version check. Each operation is atomic inside the simulator; the
lost update only appears when two actors interleave reads and writes across calls. Not registered in
``DOMAINS`` so normal suites never see it; collision runs pass ``DOMAIN`` to ``Simulator`` directly.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from ..engine import Actor, Domain, FixtureSeed, OpContext, Operation, _iso

VERSION = "1.0"
# Allowed account status moves; a settled account never reopens.
STATUS_TRANSITIONS = frozenset({("open", "open"), ("open", "settled"), ("settled", "settled")})


class _Req(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AccountRef(_Req):
    account_id: str = Field(min_length=1, max_length=64)


class PostPayment(AccountRef):
    amount_minor: StrictInt = Field(gt=0)
    expected_version: StrictInt = Field(ge=1)
    payment_ref: str = Field(min_length=1, max_length=64)


class SetBalance(AccountRef):
    balance_minor: StrictInt
    amount_minor: StrictInt = Field(gt=0)
    payment_ref: str = Field(min_length=1, max_length=64)


class Claim(AccountRef):
    expected_version: StrictInt = Field(ge=1)


class SetOwner(AccountRef):
    owner: str | None = Field(default=None, max_length=100)


def _account(ctx: OpContext, account_id: str) -> dict[str, Any]:
    account = ctx.get("account", account_id)
    if account is None:
        ctx.reject("not_found", f"Account {account_id} does not exist")
    return account


def _versioned(ctx: OpContext, request: dict[str, Any]) -> dict[str, Any]:
    account = _account(ctx, request["account_id"])
    if account["version"] != request["expected_version"]:
        ctx.reject("version_conflict", f"Account is at version {account['version']}, not {request['expected_version']}")
    return account


def _payment(ctx: OpContext, account_id: str, amount: int, ref: str) -> dict[str, Any]:
    return ctx.create(
        "payment",
        {"account_id": account_id, "amount_minor": amount, "payment_ref": ref, "actor": ctx.actor, "at": _iso(ctx.now)},
    )


def _status(balance: int) -> str:
    return "settled" if balance == 0 else "open"


def read_account(ctx: OpContext, request: dict[str, Any]) -> dict[str, Any]:
    return {"account": _account(ctx, request["account_id"])}


def post_payment(ctx: OpContext, request: dict[str, Any]) -> dict[str, Any]:
    account = _versioned(ctx, request)
    if account["status"] == "settled":
        ctx.reject("account_settled", "Settled accounts take no further payments")
    if request["amount_minor"] > account["balance_minor"]:
        ctx.reject("insufficient_balance", "Payment exceeds the outstanding balance")
    balance = account["balance_minor"] - request["amount_minor"]
    payment = _payment(ctx, account["id"], request["amount_minor"], request["payment_ref"])
    updated = ctx.update(
        "account",
        account["id"],
        {"balance_minor": balance, "status": _status(balance), "version": account["version"] + 1},
    )
    return {"payment": payment, "account": updated}


def set_balance(ctx: OpContext, request: dict[str, Any]) -> dict[str, Any]:
    """Seeded defect surface: absolute write computed by the client, no version check."""
    account = _account(ctx, request["account_id"])
    balance = request["balance_minor"]
    payment = _payment(ctx, account["id"], request["amount_minor"], request["payment_ref"])
    updated = ctx.update(
        "account",
        account["id"],
        {"balance_minor": balance, "status": _status(balance), "version": account["version"] + 1},
    )
    return {"payment": payment, "account": updated}


def claim_account(ctx: OpContext, request: dict[str, Any]) -> dict[str, Any]:
    account = _versioned(ctx, request)
    if account["owner"] is not None:
        ctx.reject("already_owned", f"Account is owned by {account['owner']}")
    return {"account": ctx.update("account", account["id"], {"owner": ctx.actor, "version": account["version"] + 1})}


def release_account(ctx: OpContext, request: dict[str, Any]) -> dict[str, Any]:
    account = _versioned(ctx, request)
    if account["owner"] != ctx.actor:
        ctx.reject("not_owner", "Only the owner can release the account")
    return {"account": ctx.update("account", account["id"], {"owner": None, "version": account["version"] + 1})}


def set_owner(ctx: OpContext, request: dict[str, Any]) -> dict[str, Any]:
    """Seeded defect surface: overwrites ownership without checking the current owner or version."""
    account = _account(ctx, request["account_id"])
    owner = ctx.actor if request.get("owner") is None else request["owner"]
    return {"account": ctx.update("account", account["id"], {"owner": owner, "version": account["version"] + 1})}


DOMAIN = Domain(
    name="shared_ledger",
    version=VERSION,
    entity_kinds=("account", "payment"),
    id_prefixes={"payment": "PAY", "account": "ACC"},
    operations={
        "read_account": Operation("read_account", "account:read", False, read_account, AccountRef),
        "post_payment": Operation("post_payment", "payment:post", True, post_payment, PostPayment),
        "set_balance": Operation("set_balance", "payment:post", True, set_balance, SetBalance),
        "claim_account": Operation("claim_account", "account:claim", True, claim_account, Claim),
        "release_account": Operation("release_account", "account:claim", True, release_account, Claim),
        "set_owner": Operation("set_owner", "account:claim", True, set_owner, SetOwner),
    },
)

ACTOR_PERMISSIONS = ["account:read", "payment:post", "account:claim"]
ACTOR_OPERATIONS = list(DOMAIN.operations)


def account(account_id: str, *, balance: int = 100_000, currency: str = "INR") -> dict[str, Any]:
    """A synthetic ledger account. Balances are integer minor units."""
    return {
        "id": account_id,
        "balance_minor": balance,
        "currency": currency,
        "status": _status(balance),
        "owner": None,
        "version": 1,
    }


def seed(clock: datetime, actors: list[str], accounts: list[dict[str, Any]] | None = None) -> FixtureSeed:
    return FixtureSeed(
        domain=DOMAIN.name,
        domain_version=VERSION,
        clock=clock,
        entities={"account": accounts or [account("ACC-1")]},
        aliases={"fixture.shared_account": (accounts or [account("ACC-1")])[0]["id"]},
        actors={name: Actor(permissions=ACTOR_PERMISSIONS, operations=ACTOR_OPERATIONS) for name in actors},
    )
