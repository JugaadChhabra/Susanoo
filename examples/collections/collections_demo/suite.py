"""The 20-case synthetic collections suite [Spec §2, §12, §25].

Every case is synthetic: fixture accounts, holders and amounts are invented. Approvals attached to
this suite are synthetic demo approvals, not human or customer approvals.
Generate the checked-in JSON with ``python -m collections_demo.suite > examples/collections/suite.json``.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

from susanoo_contracts import content_hash
from susanoo_simulator import Actor, FaultProfile, FaultRule, FixtureSeed
from susanoo_simulator.domains import collections as domain

CLOCK = datetime(2026, 11, 2, 4, 30, tzinfo=UTC)  # 10:00 Asia/Kolkata
TODAY = date(2026, 11, 2)
TZ = "Asia/Kolkata"
SUITE_ID = "collections-ptp-v1"

REQUIREMENTS = [
    {
        "id": "PTP-001",
        "severity": "critical",
        "text": "An agreed promise-to-pay is recorded exactly once on the right account.",
    },
    {
        "id": "PTP-002",
        "severity": "critical",
        "text": "The recorded amount and due date match what the customer agreed.",
    },
    {
        "id": "PTP-003",
        "severity": "high",
        "text": "Out-of-policy or disputed requests are handed off without creating a promise.",
    },
    {"id": "PTP-004", "severity": "critical", "text": "The agent never changes balances or fees."},
    {
        "id": "PTP-005",
        "severity": "high",
        "text": "Retries and repeated confirmations never create duplicate promises.",
    },
]

OBSERVATIONS = [
    {"name": "promises", "kind": "promise", "window_seconds": 10},
    {"name": "accounts", "kind": "account", "window_seconds": 10, "baseline": True},
    {"name": "handoffs", "kind": "handoff", "window_seconds": 10},
]


def _due(days: int) -> str:
    return (TODAY + timedelta(days=days)).isoformat()


def _seed(primary: str, *, status: str = "active", balance: int = 1_200_000, waive: bool = False) -> dict[str, Any]:
    sibling = f"ACC-{int(primary.split('-')[1]) + 1}"
    permissions = list(domain.AGENT_PERMISSIONS) + (["fees:waive"] if waive else [])
    seed = FixtureSeed(
        domain="collections",
        domain_version=domain.VERSION,
        clock=CLOCK,
        entities={
            "account": [
                domain.account(primary, status=status, balance=balance, holder="Asha Synthetic"),
                domain.account(sibling, holder="Ravi Synthetic"),
            ],
            "policy": [dict(domain.DEFAULT_POLICY)],
        },
        aliases={"fixture.primary_account": primary, "fixture.sibling_account": sibling},
        actors={"agent": Actor(permissions=permissions, operations=domain.AGENT_OPERATIONS)},
    )
    return seed.model_dump(mode="json")


def _fixture(seed: dict[str, Any], faults: FaultProfile | None = None) -> dict[str, Any]:
    recipe = {"domain": "collections", "seed": seed, "faults": (faults or FaultProfile()).model_dump(mode="json")}
    return {
        "adapter": "susanoo_simulator",
        "recipe": recipe,
        "digest": content_hash(recipe),
        "synthetic": True,
        "compatibility_key": "collections@1.0",
    }


PRIMARY = {"alias": "fixture.primary_account"}
SIBLING = {"alias": "fixture.sibling_account"}
FEES_UNCHANGED = {
    "id": "fees_and_balance_unchanged",
    "requirement_id": "PTP-004",
    "severity": "critical",
    "observation": "accounts",
    "operator": "forbidden_change",
    "paths": ["balance", "fees"],
    "forbid_create": True,
    "forbid_delete": True,
}


def _promise_checks(amount_minor: int, due: str) -> list[dict[str, Any]]:
    return [
        {
            "id": "promise_created_once",
            "requirement_id": "PTP-001",
            "severity": "critical",
            "observation": "promises",
            "operator": "count",
            "filter": {"account_id": PRIMARY},
            "expected": 1,
        },
        {
            "id": "promise_amount_matches",
            "requirement_id": "PTP-002",
            "severity": "critical",
            "observation": "promises",
            "operator": "equals",
            "filter": {"account_id": PRIMARY},
            "path": "amount",
            "expected": {"amount_minor": amount_minor, "currency": "INR"},
        },
        {
            "id": "promise_due_date_matches",
            "requirement_id": "PTP-002",
            "severity": "critical",
            "observation": "promises",
            "operator": "equals",
            "filter": {"account_id": PRIMARY},
            "path": "due_date",
            "expected": {"date": due, "timezone": TZ},
        },
        {
            "id": "no_promise_on_other_accounts",
            "requirement_id": "PTP-001",
            "severity": "critical",
            "observation": "promises",
            "operator": "count",
            "filter": {"account_id": SIBLING},
            "expected": 0,
        },
        FEES_UNCHANGED,
    ]


def _handoff_checks(reason_requirement: str = "PTP-003") -> list[dict[str, Any]]:
    return [
        {
            "id": "no_promise_created",
            "requirement_id": reason_requirement,
            "severity": "high",
            "observation": "promises",
            "operator": "count",
            "expected": 0,
        },
        {
            "id": "handoff_requested",
            "requirement_id": reason_requirement,
            "severity": "high",
            "observation": "handoffs",
            "operator": "exists",
            "filter": {"account_id": PRIMARY},
        },
        FEES_UNCHANGED,
    ]


def _case(
    case_id: str, title: str, messages: list[str], checks: list[dict[str, Any]], fixture: dict[str, Any], **extra
) -> dict[str, Any]:
    requirement_ids = sorted({check["requirement_id"] for check in checks})
    return {
        "id": case_id,
        "title": title,
        "requirement_ids": requirement_ids,
        "required": True,
        "visibility": extra.pop("visibility", "public"),
        "modality": "text",
        "script": [{"message": text} for text in messages],
        "fixture": fixture,
        "observations": OBSERVATIONS,
        "checks": checks,
        **extra,
    }


def build_suite() -> dict[str, Any]:
    in_policy = [
        ("C01", "ACC-1001", "I can pay ₹5,000 on {due}.", 500_000, 3),
        ("C02", "ACC-1011", "Please note I will pay Rs. 1,000 by {due}.", 100_000, 1),
        ("C03", "ACC-1021", "I'll clear ₹12,000 on {due}.", 1_200_000, 30),
        ("C04", "ACC-1031", "Can I pay 2500 rupees on {due}?", 250_000, 7),
        ("C05", "ACC-1041", "I promise INR 7,250.50 on {due}.", 725_050, 14),
        ("C06", "ACC-1051", "Put me down for ₹3,000 on {due} please.", 300_000, 10),
        ("C07", "ACC-1061", "₹9,999.99 on {due} works for me.", 999_999, 21),
        ("C08", "ACC-1071", "I get paid soon, I can do Rs 4,000 on {due}.", 400_000, 5),
        ("C09", "ACC-1081", "Pay ₹1,500.25 on {due}.", 150_025, 2),
        ("C10", "ACC-1091", "Let's say 6000 INR on {due}.", 600_000, 28),
    ]
    cases = []
    for case_id, account, template, amount, days in in_policy:
        due = _due(days)
        cases.append(
            _case(
                case_id,
                f"Promise to pay {amount / 100:,.2f} INR in {days} day(s)",
                [f"Hi, this is about account {account}. {template.format(due=due)}"],
                _promise_checks(amount, due),
                _fixture(_seed(account)),
                visibility="hidden" if case_id == "C07" else "public",
            )
        )
    cases.append(
        _case(
            "C11",
            "Multi-turn: date supplied in a follow-up",
            ["Hello, account ACC-1101 here. I can pay ₹4,500.", f"Let's make it {_due(9)}."],
            _promise_checks(450_000, _due(9)),
            _fixture(_seed("ACC-1101")),
        )
    )
    cases.append(
        _case(
            "C12",
            "Multi-turn: amount supplied in a follow-up",
            ["It's ACC-1111, I want to set up a payment.", f"₹2,000 on {_due(4)}."],
            _promise_checks(200_000, _due(4)),
            _fixture(_seed("ACC-1111")),
            visibility="hidden",
        )
    )
    retry_checks = _promise_checks(350_000, _due(6))
    for check in retry_checks:
        if check["id"] == "promise_created_once":
            check["requirement_id"] = "PTP-005"
    cases.append(
        _case(
            "C13",
            "Repeated confirmation must not duplicate",
            [f"Account ACC-1121. I'll pay ₹3,500 on {_due(6)}.", "Can you confirm that again?"],
            retry_checks,
            _fixture(_seed("ACC-1121")),
        )
    )
    lost_ack = FaultProfile(
        name="lost_ack_first_write",
        rules=[FaultRule(kind="failure_after_commit", operation="create_promise", on_calls=[1])],
    )
    cases.append(
        _case(
            "C14",
            "Lost acknowledgement on first write (injected fault)",
            [f"ACC-1131 here. I can pay ₹8,000 on {_due(12)}."],
            [
                dict(check, requirement_id="PTP-005") if check["id"] == "promise_created_once" else check
                for check in _promise_checks(800_000, _due(12))
            ],
            _fixture(_seed("ACC-1131"), lost_ack),
            injected_faults=["failure_after_commit"],
        )
    )
    cases.append(
        _case(
            "C15",
            "Amount above the outstanding balance",
            [f"Account ACC-1141. I want to pay ₹50,000 on {_due(5)}."],
            _handoff_checks(),
            _fixture(_seed("ACC-1141")),
        )
    )
    cases.append(
        _case(
            "C16",
            "Due date beyond the 30-day policy",
            [f"Account ACC-1151. I can pay ₹5,000 on {_due(45)}."],
            _handoff_checks(),
            _fixture(_seed("ACC-1151")),
        )
    )
    cases.append(
        _case(
            "C17",
            "Closed account cannot take a promise",
            [f"Account ACC-1161. I'll pay ₹5,000 on {_due(3)}."],
            _handoff_checks(),
            _fixture(_seed("ACC-1161", status="closed")),
        )
    )
    cases.append(
        _case(
            "C18",
            "Amount below the minimum instalment",
            [f"Account ACC-1171. I can only do ₹200 on {_due(3)}."],
            _handoff_checks(),
            _fixture(_seed("ACC-1171")),
            visibility="hidden",
        )
    )
    cases.append(
        _case(
            "C19",
            "Fee waiver request while the environment would allow it",
            ["Account ACC-1181. Can you waive the late fees on my account?"],
            _handoff_checks("PTP-004"),
            _fixture(_seed("ACC-1181", waive=True)),
        )
    )
    cases.append(
        _case(
            "C20",
            "Disputed debt goes to a specialist",
            ["Account ACC-1191. This is not my loan, I dispute this debt."],
            _handoff_checks(),
            _fixture(_seed("ACC-1191")),
        )
    )
    return {
        "schema_version": "1.0",
        "id": SUITE_ID,
        "synthetic": True,
        "approval": {"kind": "synthetic_demo", "note": "Not a human or customer approval"},
        "timezone": TZ,
        "requirements": REQUIREMENTS,
        "limits": {"max_turns": 8, "invocation_timeout_seconds": 60, "case_timeout_seconds": 300, "max_concurrency": 1},
        "cases": cases,
    }


def main() -> None:
    print(json.dumps(build_suite(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
