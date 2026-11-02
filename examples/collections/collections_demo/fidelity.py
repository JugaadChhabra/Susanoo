"""Synthetic simulator-vs-reference demonstration [Spec §2, §14-15].

Both sides are local synthetic implementations of the collections domain. The *reference* enforces
one open promise per account and denies fee waivers to agents. The *simulator* under test has two
seeded defects: it allows a second open promise (duplicate write) and lets agents waive fees
(permission bypass, an injected and labelled fault). Nothing here is certified against a vendor.
"""

from __future__ import annotations

from datetime import UTC, datetime

from susanoo_evaluator.normalize import Normalizer
from susanoo_runner.fidelity import ActionPlan, EnvironmentRef, FidelityContract, PlannedAction, Simplification
from susanoo_runner.repair import RepairProposal
from susanoo_simulator import Actor, FaultProfile, FaultRule, FixtureSeed
from susanoo_simulator.domains import collections as domain

CLOCK = datetime(2026, 11, 2, 4, 30, tzinfo=UTC)
AMOUNT = {"amount_minor": 500_000, "currency": "INR"}
DUE = {"date": "2026-11-09", "timezone": "Asia/Kolkata"}
REVIEWER = "synthetic-demo-reviewer"

REFERENCE = EnvironmentRef(
    label="reference",
    domain_version=domain.VERSION,
    policy={"max_promise_days": 30, "single_open_promise": True},
)
SIMULATOR = EnvironmentRef(
    label="simulator",
    domain_version=domain.VERSION,
    policy={"max_promise_days": 30, "single_open_promise": False},
    faults=FaultProfile(
        name="seeded-permission-defect", rules=[FaultRule(kind="permission_bypass", operation="waive_fees")]
    ),
)
REPAIRED = SIMULATOR.model_copy(
    update={"policy": {"max_promise_days": 30, "single_open_promise": True}, "faults": FaultProfile(name="repaired")}
)


def fixture() -> FixtureSeed:
    return FixtureSeed(
        domain="collections",
        domain_version=domain.VERSION,
        clock=CLOCK,
        entities={
            "account": [
                domain.account("ACC-2001", holder="Meera Synthetic"),
                domain.account("ACC-2002", holder="Kiran Synthetic"),
                domain.account("ACC-2003", holder="Dev Synthetic", status="disputed"),
            ]
        },
        aliases={"fixture.primary_account": "ACC-2001"},
        actors={"agent": Actor(permissions=domain.AGENT_PERMISSIONS, operations=domain.AGENT_OPERATIONS)},
    )


def contract() -> FidelityContract:
    return FidelityContract(
        id="collections-fidelity-v1",
        simulator=SIMULATOR,
        reference=REFERENCE,
        fixture=fixture(),
        operations=domain.AGENT_OPERATIONS,
        entity_kinds=["account", "promise", "handoff"],
        normalizers=[
            Normalizer(kind="generated_id", path=path, reviewed_by=REVIEWER, rationale="IDs are generated per side")
            for path in ("create_promise.promise.id", "request_handoff.handoff.id", "promise.id", "handoff.id")
        ]
        + [
            Normalizer(
                kind="timestamp_tolerance",
                path=path,
                tolerance_seconds=2,
                reviewed_by=REVIEWER,
                rationale="Write timestamps may differ by scheduling jitter",
            )
            for path in ("promise.created_at", "handoff.created_at")
        ],
        intentional_simplifications=[
            Simplification(path="handoff.note", rationale="The simulator does not model free-text handoff notes")
        ],
        reference_limitations=["The reference is a local synthetic implementation, not a vendor sandbox"],
    )


def _promise(step: str, account: str = "ACC-2001", key: str | None = None, **overrides) -> PlannedAction:
    request = {"account_id": account, "amount": AMOUNT, "due_date": DUE, **overrides}
    return PlannedAction(step=step, operation="create_promise", request=request, idempotency_key=key)


SESSION = ActionPlan(
    id="agent-session-replay",
    actions=[
        PlannedAction(step="s1", operation="read_account", request={"account_id": "ACC-2001"}),
        PlannedAction(step="s2", operation="list_promises", request={"account_id": "ACC-2001"}),
        _promise("s3", key="session-key-1"),
        PlannedAction(step="s4", operation="read_account", request={"account_id": "ACC-2001"}),
        _promise("s5", amount={"amount_minor": 300_000, "currency": "INR"}),
        PlannedAction(step="s6", operation="waive_fees", request={"account_id": "ACC-2001"}),
        PlannedAction(
            step="s7", operation="request_handoff", request={"account_id": "ACC-2001", "reason": "customer_request"}
        ),
        PlannedAction(step="s8", operation="list_promises", request={"account_id": "ACC-2001"}),
    ],
)

CONTROLS = [
    ActionPlan(
        id="control-single-promise",
        actions=[
            PlannedAction(step="r", operation="read_account", request={"account_id": "ACC-2001"}),
            _promise("p", key="control-1"),
            PlannedAction(step="l", operation="list_promises", request={"account_id": "ACC-2001"}),
        ],
    ),
    ActionPlan(
        id="control-out-of-policy",
        actions=[_promise("p", due_date={"date": "2027-03-01", "timezone": "Asia/Kolkata"})],
    ),
    ActionPlan(
        id="control-handoff",
        actions=[
            PlannedAction(
                step="h", operation="request_handoff", request={"account_id": "ACC-2003", "reason": "dispute"}
            )
        ],
    ),
]

HELDOUT = [
    ActionPlan(id="heldout-second-account", actions=[_promise("p", account="ACC-2002", key="heldout-1")]),
    ActionPlan(
        id="heldout-retry-with-key",
        actions=[_promise("a", key="heldout-2"), _promise("b", key="heldout-2")],
    ),
    ActionPlan(
        id="heldout-waiver-attempt",
        actions=[PlannedAction(step="w", operation="waive_fees", request={"account_id": "ACC-2002"})],
    ),
]


def duplicate_predicate(result) -> bool:
    return any(c.category == "duplicate_write" for c in result.candidates)


def access_predicate(result) -> bool:
    return any(c.category == "access_control" for c in result.candidates)


def repair_proposal() -> RepairProposal:
    return RepairProposal(
        before=SIMULATOR,
        after=REPAIRED,
        rationale="Enforce one open promise per account and remove the permission bypass on waive_fees",
    )
