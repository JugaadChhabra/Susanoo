"""Thumbs down + "it should have..." becomes a new Scenario. [PRD › Non-technical users]

The user never edits the agent. Their feedback becomes a test (a Scenario with an answer key) that joins the
normal loop as a `train` scenario; the Diagnostician, Fixer and Verifier then fix the right layer and prove it.
This module only returns the Scenario (or a plain-English rejection); it never touches the agent.

Haiku proposes the answer key from the user's words. Nothing it says is trusted: the key is validated with the
Simulator's checks (kind agrees with the key, no repeated issues inside the +-3 match window, every expected line is
an ADDED line of the user's diff, rule ids exist in the policy, plain-English title) and with extra checks for
feedback (an empty key needs a real false alarm to correct; a key the agent already satisfies is not a correction).

The scenario is `split="train"`, `approved_by_user=False`: it came from feedback, not from the intake approval
that makes the locked acceptance set. The caller writes it (and its world sidecar, see
`simulator.world_from_diff`) into the generated repo's `scenarios/train/`.

Budget: pass `state` (a mapping with `cost_usd`, the run's total so far) to have the per-run cap checked before
the call; a spent budget returns a rejection rather than raising. `on_cost` receives
the model call's USD cost so the caller can add it to the run's `cost_usd`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal

import anthropic
from pydantic import BaseModel

from susanoo.guards import budget
from susanoo.heads import simulator
from susanoo.integrations import llm
from susanoo.schemas import Finding, Scenario
from susanoo.scoring import score_scenario
from susanoo.translate import _common

log = logging.getLogger(__name__)

MAX_DIFF_CHARS = 12_000
MAX_EXPECTATION_CHARS = 1_000
MAX_EXPECTED_ISSUES = 5
MAX_QUESTION_CHARS = 200

RejectionCode = Literal[
    "thumbs_up",
    "no_expectation",
    "too_long",
    "bad_diff",
    "budget",
    "model_error",
    "unclear",
    "invalid",
    "unknown_rule",
    "nothing_to_correct",
    "already_correct",
]


class FeedbackRejected(BaseModel):
    """The feedback could not become a test. `reason` is plain English, safe to show the user as is."""

    code: RejectionCode
    reason: str


def _no(code: RejectionCode, reason: str) -> FeedbackRejected:
    return FeedbackRejected(code=code, reason=reason)


# What to tell the user for each way the model's key can fail validation (keys of simulator.DROP_LABELS).
_DROP_REASONS: dict[str, tuple[RejectionCode, str]] = {
    "rules": (
        "unknown_rule",
        "That is about something none of this agent's rules cover. Add it as a rule first, then give feedback again.",
    ),
    "lines": (
        "unclear",
        "We could not tell exactly which lines you mean. Say which part of the change it should have looked at.",
    ),
    "dupes": ("invalid", "That would list the same problem twice. Describe it once, and say which part it is."),
}
_GENERIC_INVALID = (
    "invalid",
    "We could not turn that into a clear test. Say what the agent should have flagged, or should have left alone.",
)


def _numbered(diff: str) -> str:
    """The diff as the reviewer sees it: `<new line number> + code` for added lines, a blank marker for context."""
    shown = simulator.added_lines(diff, include_context=True)
    added = simulator.added_lines(diff)
    parts: list[str] = []
    for path, lines in shown.items():
        parts.append(f"=== FILE: {path} ===")
        parts.extend(f"{n:>5} {'+' if n in added.get(path, {}) else ' '} {text}" for n, text in sorted(lines.items()))
    return "\n".join(parts)


def _question(raw: Any, rule_ids: Sequence[str]) -> str | None:
    """The model's follow-up question when it is short, one line and plain; else None."""
    if not isinstance(raw, str):
        return None
    q = " ".join(raw.split())
    if not q or len(q) > MAX_QUESTION_CHARS or any(c in q for c in "`{}<>") or _mentions_rule_id(q, rule_ids):
        return None
    return q


def _mentions_rule_id(text: str, rule_ids: Sequence[str]) -> bool:
    low = text.lower()
    return any(rule_id.lower() in low for rule_id in rule_ids)


async def feedback_to_scenario(
    diff: str,
    findings: Sequence[Finding],
    thumbs_down: bool,
    expectation: str,
    policy_rules: Sequence[_common.RuleLike] | Mapping[str, Any],
    *,
    scenario_id: str,
    state: Mapping[str, Any] | None = None,
    on_cost: Callable[[float], None] | None = None,
) -> Scenario | FeedbackRejected:
    """Turn one thumbs-down and "it should have..." into a `train` Scenario, or say plainly why not.

    `diff` is the code change the agent reviewed (unified diff, as in `Scenario.diff`); `findings` what the agent
    reported for it; `policy_rules` the rules in force (`Plan.rules` or the parsed `agent/policy.yaml`; the
    policy's rule ids are the only ones the key may use); `scenario_id` a fresh id (lowercase letters, digits and
    hyphens, e.g. "fb-<run>-01") that the caller guarantees is unused.
    """
    if not thumbs_down:
        return _no("thumbs_up", "A thumbs up needs no new test. Use the thumbs down when the agent got it wrong.")
    expectation = expectation.strip()
    if not expectation:
        return _no("no_expectation", 'Please finish the sentence "It should have..." so we know what to test.')
    if len(expectation) > MAX_EXPECTATION_CHARS:
        return _no("too_long", "That is too long to turn into one test. Please keep it to a few sentences.")
    if len(diff) > MAX_DIFF_CHARS:
        return _no("bad_diff", "That code change is too big to turn into a test. Try feedback on a smaller change.")
    try:
        numbered = _numbered(diff)
        has_added = any(simulator.added_lines(diff).values())
    except simulator.CardDraftError:
        return _no("bad_diff", "We could not read that code change, so we cannot turn it into a test.")
    if not has_added:
        return _no("bad_diff", "That code change has no added lines, so there is nothing to test.")

    rules = _common.normalise_rules(policy_rules)
    rule_ids = {r.id for r in rules}
    message = "\n".join(
        [
            _common.block("rules", [r.model_dump() for r in rules]),
            _common.block("code_change", numbered),
            _common.block("agent_findings", [f.model_dump() for f in findings]),
            _common.block("user_feedback", expectation),
        ]
    )
    try:
        result = await _common.ask(
            _common.load_prompt("translate_feedback"), message, step="feedback", state=state, on_cost=on_cost
        )
    except budget.BudgetExceeded:
        return _no("budget", "This run has used up its budget, so we cannot write a new test from it right now.")
    except (llm.LLMError, anthropic.APIError) as e:
        log.warning("feedback: model call failed: %s", e)
        return _no("model_error", "We could not reach the assistant that writes tests. Please try again in a moment.")

    try:
        data = result.json()
    except ValueError:
        return _no(*_GENERIC_INVALID)
    if not isinstance(data, dict):
        return _no(*_GENERIC_INVALID)
    if data.get("decision") == "need_more_info":
        question = _question(data.get("question"), sorted(rule_ids))
        return _no(
            "unclear",
            question or "We need a bit more detail to write a test: which part of the change should it have flagged?",
        )
    if data.get("decision") != "scenario":
        return _no(*_GENERIC_INVALID)

    expected = data.get("expected") or []
    if not isinstance(expected, list) or len(expected) > MAX_EXPECTED_ISSUES:
        return _no(*_GENERIC_INVALID)
    raw = {
        "id": scenario_id,
        "kind": data.get("kind"),
        "title": data.get("title"),
        "diff": diff,  # always the user's own change, never the model's copy of it
        "expected": expected,
        "must_not_flag": data.get("must_not_flag") or [],
    }
    try:
        scenario, _ = simulator.validate_scenario(raw, rule_ids, set(), world_required=False)
    except simulator.ScenarioDrop as e:
        log.info("feedback: model answer dropped (%s): %s", e.category, e.detail)
        return _no(*_DROP_REASONS.get(e.category, _GENERIC_INVALID))

    return _check_against_findings(scenario, findings, rule_ids)


def _check_against_findings(
    scenario: Scenario, findings: Sequence[Finding], rule_ids: set[str]
) -> Scenario | FeedbackRejected:
    key = scenario.answer_key
    if not key.expected and not key.must_not_flag:
        if not any(f.severity in ("block", "warn") for f in findings):
            return _no(
                "nothing_to_correct",
                "The agent did not flag anything in this change, so there is nothing to correct. "
                "If it should have flagged something, tell us what.",
            )
    scored = score_scenario(scenario, list(findings), policy_rule_ids=rule_ids, iteration=0)
    if scored.passed:
        return _no(
            "already_correct",
            "The agent's review already matches what you describe. If something still looks wrong, "
            "tell us exactly what you would change.",
        )
    return scenario
