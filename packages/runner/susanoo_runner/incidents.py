"""Failure-to-test: incidents -> stable signatures -> draft scenarios [Spec §18 › Passive production evidence; T34].

Candidate incidents come from imported or passive evidence (``TraceRecord``). They are grouped by a
stable signature that ignores volatile values (ids, amounts, dates, emails), each group keeps a
representative, and a group can be turned into a **draft** scenario plus a synthetic fixture. Drafts:

- have ``status="draft"``, ``requires_approval=True`` and ``auto_replay=False`` as schema literals;
- replace every production identifier with a synthetic alias (the real values are kept only as
  digests, so the mapping cannot leak them);
- carry no checks: requirements and checks are authored and approved by a person, never inferred;
- become an approved ``LocalCase`` only through ``approve_draft`` with an approver and checks.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from susanoo_contracts import content_hash

from .imports import TraceRecord

FAILURE_STATUSES = {"error", "failed", "failure", "rejected", "timeout", "escalated", "complaint"}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Incident(_Model):
    incident_id: str
    record_id: str
    conversation_id: str
    occurred_at: AwareDatetime | None = None
    kind: Literal["tool_error", "reported_outcome", "agent_error"]
    tool_name: str | None = None
    code: str | None = None
    message: str = ""
    user_turns: list[str] = Field(default_factory=list, description="redacted user messages, in order")


class IncidentGroup(_Model):
    signature: str
    signature_basis: dict[str, str]
    count: int
    first_seen: AwareDatetime | None
    last_seen: AwareDatetime | None
    representative: Incident
    member_ids: list[str]


class DraftScenario(_Model):
    status: Literal["draft"] = "draft"
    requires_approval: Literal[True] = True
    auto_replay: Literal[False] = False
    approved_requirement: Literal[False] = False
    draft_id: str
    source_signature: str
    source_count: int
    title: str
    rationale: str
    case: dict[str, Any] = Field(description="LocalCase-shaped skeleton; checks intentionally empty")
    fixture: dict[str, Any] = Field(description="synthetic fixture recipe with aliases only")
    alias_digests: dict[str, str] = Field(description="synthetic alias -> sha256 of the production value")
    created_at: AwareDatetime


class DraftNotApproved(PermissionError):
    pass


# --- signatures ---------------------------------------------------------------------------------

_VOLATILE = [
    (re.compile(r"\[[A-Z_]+\]"), "<redacted>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ][0-9:.]+Z?)?\b"), "<date>"),
    (re.compile(r"\b[A-Z]{2,6}-\d{2,}\b"), "<id>"),
    (re.compile(r"\b[0-9a-f]{16,}\b", re.I), "<hex>"),
    (re.compile(r"\d+(?:[.,]\d+)*"), "<n>"),
    (re.compile(r"\s+"), " "),
]


def normalize_message(text: str) -> str:
    text = text.strip()
    for pattern, replacement in _VOLATILE:
        text = pattern.sub(replacement, text)
    return text.lower()[:300]


def signature(incident: Incident) -> tuple[str, dict[str, str]]:
    basis = {
        "kind": incident.kind,
        "tool": incident.tool_name or "",
        "code": normalize_message(incident.code or ""),
        "message": normalize_message(incident.message),
    }
    return content_hash(basis), basis


def incidents_from_records(records: list[TraceRecord]) -> list[Incident]:
    """Tool errors, agent errors and failing reported outcomes become candidate incidents."""
    out = []
    for record in records:
        users = [t.text for t in record.turns if t.role == "user" and t.text]
        for turn in record.turns:
            if turn.status != "error" and not turn.error:
                continue
            out.append(
                Incident(
                    incident_id=content_hash({"r": record.record_id, "t": turn.index}),
                    record_id=record.record_id,
                    conversation_id=record.conversation_id,
                    occurred_at=turn.timestamp,
                    kind="tool_error" if turn.role == "tool" else "agent_error",
                    tool_name=turn.tool_name,
                    code=turn.error,
                    message=turn.error or "",
                    user_turns=users,
                )
            )
        for index, outcome in enumerate(record.outcomes):
            if outcome.status.lower() not in FAILURE_STATUSES:
                continue
            out.append(
                Incident(
                    incident_id=content_hash({"r": record.record_id, "o": index}),
                    record_id=record.record_id,
                    conversation_id=record.conversation_id,
                    occurred_at=outcome.timestamp,
                    kind="reported_outcome",
                    code=outcome.status,
                    message=f"{outcome.kind} {outcome.status}",
                    user_turns=users,
                )
            )
    return out


def group_incidents(incidents: list[Incident]) -> list[IncidentGroup]:
    """Largest groups first. The representative is the earliest incident with the most user context."""
    groups: dict[str, tuple[dict[str, str], list[Incident]]] = {}
    for incident in incidents:
        key, basis = signature(incident)
        groups.setdefault(key, (basis, []))[1].append(incident)
    out = []
    for key, (basis, members) in groups.items():
        times = [m.occurred_at for m in members if m.occurred_at]
        representative = sorted(
            members, key=lambda m: (-len(m.user_turns), m.occurred_at or datetime.max.replace(tzinfo=UTC))
        )[0]
        out.append(
            IncidentGroup(
                signature=key,
                signature_basis=basis,
                count=len(members),
                first_seen=min(times) if times else None,
                last_seen=max(times) if times else None,
                representative=representative,
                member_ids=[m.incident_id for m in members],
            )
        )
    return sorted(out, key=lambda g: (-g.count, g.signature))


# --- synthetic aliases --------------------------------------------------------------------------

_IDENTIFIERS = [
    ("account", re.compile(r"\b[A-Z]{2,6}-\d{2,}\b")),
    ("uuid", re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("number", re.compile(r"\b\d{6,}\b")),
]


class AliasMap:
    """Production value -> synthetic alias, stable within one draft. Only digests of real values are kept."""

    def __init__(self) -> None:
        self._aliases: dict[str, str] = {}
        self.digests: dict[str, str] = {}
        self._counts: dict[str, int] = {}

    def alias(self, kind: str, value: str) -> str:
        if value not in self._aliases:
            self._counts[kind] = self._counts.get(kind, 0) + 1
            n = self._counts[kind]
            synthetic = {
                "account": f"SYN-{1000 + n}",
                "uuid": f"00000000-0000-4000-8000-{n:012d}",
                "email": f"person{n}@example.invalid",
                "number": f"9{n:05d}",
            }[kind]
            self._aliases[value] = synthetic
            self.digests[synthetic] = content_hash({"kind": kind, "value": value})
        return self._aliases[value]

    def rewrite(self, text: str) -> str:
        for kind, pattern in _IDENTIFIERS:
            text = pattern.sub(lambda m, kind=kind: self.alias(kind, m.group(0)), text)
        return text

    @property
    def synthetic_values(self) -> list[str]:
        return list(self.digests)


def propose_draft(group: IncidentGroup, *, domain: str, fixture_adapter: str = "susanoo_simulator") -> DraftScenario:
    """Draft scenario + synthetic fixture from a group's representative. Never approved, never replayed."""
    rep = group.representative
    aliases = AliasMap()
    script = [{"message": aliases.rewrite(text)} for text in rep.user_turns] or [
        {"message": "[author the opening customer message]"}
    ]
    entity_ids = [alias for alias in aliases.synthetic_values if alias.startswith("SYN-")]
    fixture_aliases = {f"fixture.entity_{i + 1}": alias for i, alias in enumerate(entity_ids)}
    what = rep.tool_name or rep.kind.replace("_", " ")
    title = f"Draft from {group.count} production incident(s): {what} {normalize_message(rep.code or rep.message)}"
    draft_id = content_hash({"signature": group.signature, "script": script})[:16]
    case = {
        "id": f"DRAFT-{draft_id[:8]}",
        "title": title[:200],
        "requirement_ids": [],
        "required": False,
        "visibility": "public",
        "modality": "text",
        "script": script,
        "observations": [],
        "checks": [],
        "injected_faults": [],
    }
    fixture = {
        "adapter": fixture_adapter,
        "synthetic": True,
        "recipe": {
            "domain": domain,
            "seed": {"domain": domain, "entities": {}, "aliases": fixture_aliases},
            "faults": {"name": "none", "rules": []},
        },
        "note": "Entities must be authored synthetically; nothing was copied from production.",
    }
    return DraftScenario(
        draft_id=draft_id,
        source_signature=group.signature,
        source_count=group.count,
        title=title[:200],
        rationale=(
            f"{group.count} incident(s) share signature {group.signature[:12]} ({group.signature_basis['kind']}, "
            f"tool={group.signature_basis['tool'] or '-'}). Representative conversation is shown with synthetic "
            "aliases. A reviewer must write requirements and checks before approval."
        ),
        case=case,
        fixture=fixture,
        alias_digests=aliases.digests,
        created_at=datetime.now(UTC),
    )


def approve_draft(
    draft: DraftScenario,
    *,
    approver: str,
    requirement_ids: list[str],
    checks: list[dict],
    observations: list[dict],
    fixture: dict[str, Any] | None = None,
) -> Any:
    """Turn a reviewed draft into a ``LocalCase``.

    Requires an approver, requirements, human-authored checks and observations, and an authored synthetic
    fixture (with digest and compatibility key). Fixture aliases may only use the draft's synthetic values.
    """
    from .suite import LocalCase

    if not approver.strip():
        raise DraftNotApproved("An approver is required")
    if not checks or not requirement_ids or not observations:
        raise DraftNotApproved("Approval needs reviewer-authored requirements, observations and checks")
    authored = fixture or {k: v for k, v in draft.fixture.items() if k != "note"}
    if not authored.get("digest") or not authored.get("compatibility_key"):
        raise DraftNotApproved("Author the synthetic fixture (digest and compatibility key) before approval")
    if authored.get("synthetic") is not True:
        raise DraftNotApproved("Fixtures proposed from incidents must stay synthetic")
    return LocalCase.model_validate(
        {
            **draft.case,
            "id": draft.case["id"].replace("DRAFT-", "INC-"),
            "requirement_ids": requirement_ids,
            "required": True,
            "fixture": authored,
            "observations": observations,
            "checks": checks,
        }
    )
