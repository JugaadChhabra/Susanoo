"""Agent card: what the agent does, can do and will not do, in plain sentences. [PRD › Non-technical users]

`agent_card(plan, agent_prompt_text, policy_rules, connectors)` asks Haiku to describe the agent for someone
non-technical. The answer is validated (no prompts, YAML, code, file names or rule ids); if the model fails or
its answer is not plain enough, a deterministic card built from the plan, the rules and the connectors is returned
instead, so the page always has something truthful to show.

Budget: pass `state` (any mapping with `cost_usd`, normally the run's total so far) to have the per-run cap checked
before the call. A run that stopped on its cap still gets a card: the deterministic one. `on_cost` receives the
model call's USD cost so the caller can add it to the run's `cost_usd`.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import anthropic
from pydantic import BaseModel, ValidationError, field_validator

from susanoo.guards import budget
from susanoo.integrations import llm
from susanoo.schemas import Plan
from susanoo.translate import _common

log = logging.getLogger(__name__)

MAX_SENTENCE_CHARS = 240
MAX_ITEMS = 8
MAX_PROMPT_CHARS = 12_000
MAX_RULES_SHOWN = 6

# Things a plain-English card must never contain.
_NOT_PLAIN = re.compile(
    r"[`{}<>]"  # code, markup, JSON
    r"|\bya?ml\b|\bjson\b|\bsystem prompt\b|\bprompt\.md\b|\bpolicy file\b|\bpolicy\.\w+"
    r"|\b[\w-]+\.(?:py|md|ya?ml|json|txt|toml|ts|js)\b"  # file names
    r"|\w+_\w+"  # snake_case identifiers
    r"|\w\(\)|\w\(\.\.\.\)"  # calls such as Depends(...)
    r"|^\s*[-*#]\s|\n",  # bullets, headings or several lines
    re.I,
)


def _sentence_problem(text: str, rule_ids: Sequence[str] = (), max_chars: int = MAX_SENTENCE_CHARS) -> str | None:
    if not text.strip():
        return "empty"
    if len(text) > max_chars:
        return "too long"
    if _NOT_PLAIN.search(text):
        return "not plain English"
    for rule_id in rule_ids:
        if re.search(rf"(?<![a-z0-9-]){re.escape(rule_id)}(?![a-z0-9-])", text, re.I):
            return "shows a rule id"
    return None


def _end_sentence(text: str) -> str:
    text = " ".join(text.split())
    return text if text.endswith((".", "!", "?")) else text + "."


class AgentCard(BaseModel):
    """The plain-English summary of an agent. Every field is sentences, never code."""

    what_it_does: str
    can_do: list[str]
    wont_do: list[str]

    @field_validator("what_it_does", mode="before")
    @classmethod
    def _one_text(cls, v: Any) -> str:
        if not isinstance(v, str) or "\n" in v.strip():
            raise ValueError("what_it_does must be one short paragraph of plain text")
        text = _end_sentence(v)
        problem = _sentence_problem(text, max_chars=2 * MAX_SENTENCE_CHARS)
        if problem:
            raise ValueError(f"what_it_does is {problem}")
        return text

    @field_validator("can_do", "wont_do", mode="before")
    @classmethod
    def _sentences(cls, v: Any) -> list[str]:
        if not isinstance(v, list) or not all(isinstance(s, str) for s in v):
            raise ValueError("must be a list of sentences")
        if any("\n" in s.strip() for s in v):
            raise ValueError("each item must be a single sentence")
        out = [_end_sentence(s) for s in v if s.strip()]
        if not 1 <= len(out) <= MAX_ITEMS:
            raise ValueError(f"needs 1 to {MAX_ITEMS} sentences")
        for s in out:
            problem = _sentence_problem(s)
            if problem:
                raise ValueError(f"a sentence is {problem}")
        return out

    def all_text(self) -> list[str]:
        return [self.what_it_does, *self.can_do, *self.wont_do]


def check_no_rule_ids(card: AgentCard, rule_ids: Sequence[str]) -> None:
    """Raise ValueError when any sentence of `card` shows one of the policy's rule ids."""
    for text in card.all_text():
        if _sentence_problem(text, rule_ids) == "shows a rule id":
            raise ValueError("the card shows a rule id")


# --- connectors ----------------------------------------------------------------------------------------------


def _connector_info(connector: Any) -> tuple[str, list[tuple[str, bool]]]:
    """(name, [(plain description, is_write)]) from a `ConnectorSpec`, a mapping or a bare name (never scopes)."""
    if isinstance(connector, str):
        return connector.strip(), []
    get = connector.get if isinstance(connector, Mapping) else lambda k, d=None: getattr(connector, k, d)
    name = str(get("name", "") or get("id", "") or "").strip()
    actions = []
    for a in get("actions", None) or []:
        ag = a.get if isinstance(a, Mapping) else lambda k, d=None, a=a: getattr(a, k, d)
        desc = str(ag("description", "") or "").strip().rstrip(".")
        if desc:
            actions.append((desc, bool(ag("write", False))))
    return name, actions


def _connectors_for_prompt(connectors: Sequence[Any]) -> list[dict[str, Any]]:
    out = []
    for c in connectors:
        name, actions = _connector_info(c)
        if name:
            out.append(
                {
                    "name": name,
                    "can": [d for d, w in actions if not w],
                    "changes_need_switching_on": [d for d, w in actions if w],
                }
            )
    return out


# --- deterministic fallback ----------------------------------------------------------------------------------


def _humanise(rule_id: str) -> str:
    return rule_id.replace("-", " ").replace("_", " ").strip()


def _plain_or(text: str, fallback: str, rule_ids: Sequence[str]) -> str:
    cleaned = _end_sentence(text)
    return fallback if _sentence_problem(cleaned, rule_ids) else cleaned


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:]


def fallback_card(
    plan: Plan, policy_rules: Sequence[_common.RuleLike] | Mapping[str, Any], connectors: Sequence[Any] = ()
) -> AgentCard:
    """A card built only from the plan, the rules and the connectors. No model."""
    rules = _common.normalise_rules(policy_rules)
    rule_ids = [r.id for r in rules]

    what = _plain_or(plan.summary, "", rule_ids)
    if not what or len(what) > 2 * MAX_SENTENCE_CHARS:
        what = _plain_or(
            f"{plan.product_name} is an agent that checks work against the rules you set.",
            "This agent checks work against the rules you set.",
            rule_ids,
        )

    can: list[str] = []
    for rule in rules[:MAX_RULES_SHOWN]:
        sentence = f'Flags work that breaks your rule: "{rule.text.strip().rstrip(".")}".'
        if _sentence_problem(sentence, rule_ids):
            sentence = f'Flags work that breaks your "{_humanise(rule.id)}" rule.'
        can.append(sentence)
    if len(rules) > MAX_RULES_SHOWN:
        can.append(f"It also checks {len(rules) - MAX_RULES_SHOWN} more of your rules.")

    wont = ["It does not judge anything outside the rules you set."]
    for connector in connectors:
        name, actions = _connector_info(connector)
        for desc, write in actions:
            if write:
                where = name or "a connected service"
                wont.append(f"It will not {_lower_first(desc)} in {where} until you switch that on.")
            else:
                can.append(f"It can {_lower_first(desc)}" + (f" through {name}." if name else "."))
    closing = "It does not change itself: it only improves after a test shows the fix works."

    can = [s for s in can if not _sentence_problem(s, rule_ids)][:MAX_ITEMS]
    middle = [s for s in wont[1:] if not _sentence_problem(s, rule_ids)]
    wont = [wont[0], *middle[: MAX_ITEMS - 2], closing]
    return AgentCard(what_it_does=what, can_do=can or ["It checks work against the rules you set."], wont_do=wont)


# --- the model call --------------------------------------------------------------------------------------------


async def agent_card(
    plan: Plan,
    agent_prompt_text: str,
    policy_rules: Sequence[_common.RuleLike] | Mapping[str, Any],
    connectors: Sequence[Any] = (),
    *,
    state: Mapping[str, Any] | None = None,
    on_cost: Callable[[float], None] | None = None,
) -> AgentCard:
    """Describe the agent in plain sentences (Haiku), or fall back to a deterministic card.

    `agent_prompt_text` is the agent's own instruction text (agent/prompt.md); it is shown to the model as
    untrusted data and never copied into the card. `policy_rules` is `Plan.rules`, or the parsed
    `agent/policy.yaml`. `connectors` are `ConnectorSpec`s (or mappings / names) of the tools the agent has.
    The fallback is used when the budget is spent, the model call fails, or its answer is not valid plain English.
    """
    rules = _common.normalise_rules(policy_rules)
    rule_ids = [r.id for r in rules]
    message = "\n".join(
        [
            _common.block("plan", {"product_name": plan.product_name, "summary": plan.summary}),
            _common.block("rules", [{"text": r.text, "severity": r.default_severity} for r in rules]),
            _common.block("connectors", _connectors_for_prompt(connectors)),
            _common.block("agent_instructions", agent_prompt_text[:MAX_PROMPT_CHARS]),
        ]
    )
    try:
        result = await _common.ask(
            _common.load_prompt("translate_agent_card"), message, step="agent_card", state=state, on_cost=on_cost
        )
    except budget.BudgetExceeded:
        log.info("agent card: run is over its budget, using the deterministic card")
        return fallback_card(plan, rules, connectors)
    except (llm.LLMError, anthropic.APIError) as e:
        log.warning("agent card: model call failed (%s), using the deterministic card", e)
        return fallback_card(plan, rules, connectors)

    try:
        card = AgentCard.model_validate(result.json())
        check_no_rule_ids(card, rule_ids)
    except (ValueError, ValidationError, TypeError) as e:  # JSONDecodeError is a ValueError
        log.warning("agent card: model answer rejected (%s), using the deterministic card", e)
        return fallback_card(plan, rules, connectors)
    return card


__all__ = ["AgentCard", "agent_card", "check_no_rule_ids", "fallback_card"]
