"""Helpers shared by the translate modules. [PRD › Non-technical users]

Every translate call is Haiku (`llm.call("translate", ...)`), goes through the budget guard first, and treats
anything a user or a diff wrote as untrusted data inside tagged blocks.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from susanoo.guards import budget
from susanoo.heads import simulator
from susanoo.integrations import llm, tracing
from susanoo.schemas import Rule

HEAD = "translate"
PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
MAX_TOKENS = 2000

# Our own wrapper tags (simulator.neutralise_tags covers the Simulator's); user text must not close or open one.
_OWN_TAGS = re.compile(
    r"<(/?\s*(?:plan|rules|connectors|agent_instructions|code_change|agent_findings|user_feedback)\b)", re.I
)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)

RuleLike = Rule | Mapping[str, Any]


def load_prompt(name: str) -> str:
    """The prompt file `prompts/<name>.md` without its leading HTML comment."""
    return _HTML_COMMENT.sub("", (PROMPTS / f"{name}.md").read_text(encoding="utf-8"), count=1).strip()


def neutralise(text: str) -> str:
    """Defang every wrapper tag inside untrusted text."""
    return _OWN_TAGS.sub(r"&lt;\1", simulator.neutralise_tags(text))


def block(label: str, value: Any) -> str:
    """`<label>…</label>` holding untrusted data (JSON for non-strings)."""
    body = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return f"<{label}>{neutralise(body)}</{label}>"


def normalise_rules(policy_rules: Sequence[RuleLike] | Mapping[str, Any]) -> list[Rule]:
    """Rules as `Rule`s. Accepts `Plan.rules`, the parsed `agent/policy.yaml` mapping (`{"rules": [...]}`) or its list.

    The generated repo's policy.yaml is the source of truth for rule ids; `Plan.rules` is the fallback.
    """
    items: Sequence[RuleLike]
    if isinstance(policy_rules, Mapping):
        items = policy_rules.get("rules") or []
    else:
        items = policy_rules
    return [r if isinstance(r, Rule) else Rule.model_validate(r) for r in items]


async def ask(
    system: str,
    message: str,
    *,
    step: str,
    state: Mapping[str, Any] | None,
    on_cost: Callable[[float], None] | None,
) -> llm.LLMResult:
    """One Haiku call. `state["cost_usd"]` (if given) is checked against the per-run cap first.

    Raises `budget.BudgetExceeded` when the run has already spent its cap. `on_cost` receives the call's USD cost
    right after the call (before the caller parses the answer), so a bad answer still reports what it cost.
    """
    budget.check(state if state is not None else {})
    with tracing.span(HEAD, step=step):
        result = await llm.call(
            HEAD, messages=[{"role": "user", "content": message}], system=system, max_tokens=MAX_TOKENS
        )
    if on_cost is not None:
        on_cost(result.cost_usd)
    return result
