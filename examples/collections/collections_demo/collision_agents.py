"""Synthetic shared-ledger actors for collision runs [Spec §21; T35]. No model key needed.

Each factory returns an actor script that talks only to its ``ToolProxy``. ``correct`` modes use
version checks and retry with the same operation key; seeded defect modes do an unchecked
read-modify-write (``lost_update``), retry an unacknowledged write under a fresh key
(``duplicate_retry``) or overwrite ownership (``ownership_steal``). Defects are injected on purpose and
labelled as such; they are never reported as discovered real defects.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal

from susanoo_runner.collision import AckTimeout, RouterRejected
from susanoo_simulator import SimulatorError

PayMode = Literal["correct", "lost_update", "duplicate_retry"]
ClaimMode = Literal["correct", "ownership_steal"]
MAX_ATTEMPTS = 5

Script = Callable[[Any], Awaitable[None]]


async def _read(tools: Any, account_id: str) -> dict[str, Any] | None:
    result = await tools.call("read_account", {"account_id": account_id})
    return result.response.get("account") if result.status == "ok" else None


def payer(mode: PayMode, *, account_id: str, amount_minor: int, payment_ref: str) -> Script:
    """Pay ``amount_minor`` once against a shared account."""

    async def run(tools: Any) -> None:
        key = f"{tools.actor}:{payment_ref}:1"
        attempt = 0
        account = await _read(tools, account_id)
        while account is not None and attempt < MAX_ATTEMPTS:
            attempt += 1
            if mode == "lost_update":
                operation = "set_balance"
                request = {
                    "account_id": account_id,
                    "balance_minor": account["balance_minor"] - amount_minor,
                    "amount_minor": amount_minor,
                    "payment_ref": payment_ref,
                }
            else:
                operation = "post_payment"
                request = {
                    "account_id": account_id,
                    "amount_minor": amount_minor,
                    "expected_version": account["version"],
                    "payment_ref": payment_ref,
                }
            try:
                result = await tools.call(operation, request, key=key)
            except RouterRejected:
                return  # token revoked: stop without further writes
            except (AckTimeout, SimulatorError):
                if mode == "duplicate_retry":
                    # Defect: treats the missing ack as "not done", re-reads and writes under a new key.
                    key = f"{tools.actor}:{payment_ref}:{attempt + 1}"
                    account = await _read(tools, account_id)
                continue  # correct/lost_update: retry the very same request and key
            if result.status in {"ok", "replayed"}:
                return
            if result.code == "version_conflict":
                key = f"{tools.actor}:{payment_ref}:{attempt + 1}"
                account = await _read(tools, account_id)
                continue
            return  # business rejection (settled, insufficient balance, permission): stop

    return run


def claimer(mode: ClaimMode, *, account_id: str) -> Script:
    """Take ownership of a shared account."""

    async def run(tools: Any) -> None:
        account = await _read(tools, account_id)
        if account is None:
            return
        try:
            if mode == "ownership_steal":
                await tools.call("set_owner", {"account_id": account_id}, key=f"{tools.actor}:claim")
            else:
                await tools.call(
                    "claim_account",
                    {"account_id": account_id, "expected_version": account["version"]},
                    key=f"{tools.actor}:claim",
                )
        except (RouterRejected, AckTimeout, SimulatorError):
            return

    return run
