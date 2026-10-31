"""Small issue-tracker domain: shows the engine is not hardcoded to collections [Spec §14]."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..engine import Domain, OpContext, Operation, _iso

VERSION = "1.0"
TRANSITIONS = {
    ("open", "in_progress"),
    ("in_progress", "resolved"),
    ("resolved", "closed"),
    ("resolved", "open"),
    ("in_progress", "open"),
}


class _Req(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IssueRef(_Req):
    issue_id: str = Field(min_length=1, max_length=64)


class CreateIssue(_Req):
    title: str = Field(min_length=1, max_length=200)
    priority: Literal["low", "medium", "high", "urgent"] = "medium"


class AssignIssue(IssueRef):
    assignee: str = Field(min_length=1, max_length=100)


class TransitionIssue(IssueRef):
    status: Literal["open", "in_progress", "resolved", "closed"]


def _issue(ctx: OpContext, issue_id: str) -> dict[str, Any]:
    issue = ctx.get("issue", issue_id)
    if issue is None:
        ctx.reject("not_found", f"Issue {issue_id} does not exist")
    return issue


def read_issue(ctx: OpContext, request: dict[str, Any]) -> dict[str, Any]:
    return {"issue": _issue(ctx, request["issue_id"])}


def create_issue(ctx: OpContext, request: dict[str, Any]) -> dict[str, Any]:
    issue = ctx.create(
        "issue",
        {
            "title": request["title"],
            "priority": request["priority"],
            "status": "open",
            "assignee": None,
            "reporter": ctx.actor,
            "created_at": _iso(ctx.now),
        },
    )
    return {"issue": issue}


def assign_issue(ctx: OpContext, request: dict[str, Any]) -> dict[str, Any]:
    issue = _issue(ctx, request["issue_id"])
    if issue["status"] == "closed":
        ctx.reject("issue_closed", "Closed issues cannot be reassigned")
    return {"issue": ctx.update("issue", issue["id"], {"assignee": request["assignee"]})}


def transition_issue(ctx: OpContext, request: dict[str, Any]) -> dict[str, Any]:
    issue = _issue(ctx, request["issue_id"])
    if (issue["status"], request["status"]) not in TRANSITIONS:
        ctx.reject("invalid_transition", f"Cannot move {issue['status']} -> {request['status']}")
    if request["status"] == "in_progress" and not issue["assignee"]:
        ctx.reject("unassigned", "Assign the issue before starting work")
    return {"issue": ctx.update("issue", issue["id"], {"status": request["status"]})}


DOMAIN = Domain(
    name="issue_tracker",
    version=VERSION,
    entity_kinds=("issue",),
    id_prefixes={"issue": "ISS"},
    operations={
        "read_issue": Operation("read_issue", "issue:read", False, read_issue, IssueRef),
        "create_issue": Operation("create_issue", "issue:create", True, create_issue, CreateIssue),
        "assign_issue": Operation("assign_issue", "issue:assign", True, assign_issue, AssignIssue),
        "transition_issue": Operation("transition_issue", "issue:transition", True, transition_issue, TransitionIssue),
    },
)
