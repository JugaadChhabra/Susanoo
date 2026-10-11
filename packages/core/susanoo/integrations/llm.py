"""Thin Anthropic client used by every head.

Heads never call the SDK directly (CLAUDE.md §4). They call `call(head, messages)`,
which picks the model from HEAD_MODELS, retries transient API errors with
exponential backoff, and returns the text plus token counts and cost.

The budget guard is the head's job: call `guards.budget.check(state)` before
`call(...)`, then add `result.cost_usd` to `state["cost_usd"]` in the head's return.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from dataclasses import dataclass
from typing import Any, Protocol

import anthropic

OPUS = "claude-opus-5-5"
SONNET = "claude-sonnet-5-5"
HAIKU = "claude-haiku-5-5"

# [PRD › The eight heads of Orochi]. Runner and Verifier are code, so they have no entry.
HEAD_MODELS: dict[str, str] = {
    "planner": OPUS,
    "diagnostician": OPUS,
    "builder": SONNET,
    "agent_smith": SONNET,
    "fixer": SONNET,
    "simulator": HAIKU,
    "translate": HAIKU,
}

# USD per 1M tokens. Update here only; cost_usd is computed from this table.
# opus/sonnet: published Anthropic rates (2026-09). haiku-5-5: no published rate found,
# so this carries the claude-haiku-4-5 rate as a PLACEHOLDER; confirm before reading cost reports.
PRICING_USD_PER_MTOK: dict[str, dict[str, float]] = {
    OPUS: {"input": 4.00, "output": 20.00},
    SONNET: {"input": 2.00, "output": 10.00},
    HAIKU: {"input": 1.00, "output": 5.00},
}

DEFAULT_MAX_TOKENS = 16000

RETRY_MAX_ATTEMPTS = 5
RETRY_BASE_DELAY_S = 1.0
RETRY_MAX_DELAY_S = 30.0
RETRY_JITTER_FRACTION = 0.1
# 408 timeout, 409 conflict, 529 overloaded; 429 and >=500 are caught by class below.
_TRANSIENT_STATUS = frozenset({408, 409, 529})

_JSON_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


class LLMError(Exception):
    """The model call failed in a way retrying will not fix."""


class LLMRefusal(LLMError):
    """The model declined the request (stop_reason == "refusal")."""


class LLMClient(Protocol):
    """The slice of `anthropic.AsyncAnthropic` that `call` uses; tests pass a fake."""

    @property
    def messages(self) -> Any: ...


@dataclass(frozen=True)
class LLMResult:
    text: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    model: str
    stop_reason: str | None = None

    def json(self) -> Any:
        """Parse `text` as JSON, tolerating a surrounding ```json fence.

        Raises ValueError (json.JSONDecodeError) if the model did not return JSON.
        """
        body = self.text.strip()
        fenced = _JSON_FENCE.match(body)
        return json.loads(fenced.group(1) if fenced else body)


_default_client: anthropic.AsyncAnthropic | None = None


def _get_default_client() -> anthropic.AsyncAnthropic:
    # max_retries=0: retries live in `call` so they are counted once, not SDK x ours.
    global _default_client
    if _default_client is None:
        _default_client = anthropic.AsyncAnthropic(max_retries=0)
    return _default_client


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    price = PRICING_USD_PER_MTOK[model]
    return (input_tokens * price["input"] + output_tokens * price["output"]) / 1_000_000


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, (anthropic.RateLimitError, anthropic.InternalServerError)):
        return True
    if isinstance(exc, anthropic.APIConnectionError):  # includes APITimeoutError
        return True
    return isinstance(exc, anthropic.APIStatusError) and exc.status_code in _TRANSIENT_STATUS


def _backoff_delay(attempt: int) -> float:
    delay = min(RETRY_BASE_DELAY_S * 2**attempt, RETRY_MAX_DELAY_S)
    return delay + random.uniform(0, delay * RETRY_JITTER_FRACTION)


async def _create_with_retry(client: LLMClient, request: dict[str, Any]) -> Any:
    for attempt in range(RETRY_MAX_ATTEMPTS):
        try:
            return await client.messages.create(**request)
        except Exception as exc:
            last_attempt = attempt == RETRY_MAX_ATTEMPTS - 1
            if not _is_transient(exc) or last_attempt:
                raise
            await asyncio.sleep(_backoff_delay(attempt))
    raise AssertionError("unreachable")  # loop always returns or raises


async def call(
    head: str,
    messages: list[dict[str, Any]],
    *,
    system: str | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    client: LLMClient | None = None,
    model: str | None = None,
    **kw: Any,
) -> LLMResult:
    """Run one model call for `head` and return its text, token counts and cost.

    `kw` is forwarded to `messages.create` (e.g. `output_config={"effort": "low"}`).
    Do not pass temperature/top_p/top_k or `thinking: disabled`: the Opus 5.5 and
    Sonnet 5.5 models reject them with a 400.

    `model` overrides HEAD_MODELS for calls made on behalf of the generated agent (the Runner's
    Level 1 runs use AgentBundle.model); it must have a row in PRICING_USD_PER_MTOK.
    """
    if model is None:
        try:
            model = HEAD_MODELS[head]
        except KeyError:
            raise ValueError(f"unknown head {head!r}; expected one of {sorted(HEAD_MODELS)}") from None
    elif model not in PRICING_USD_PER_MTOK:
        raise ValueError(f"no price for model {model!r}; add it to PRICING_USD_PER_MTOK")

    request: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "messages": messages, **kw}
    if system is not None:
        request["system"] = system

    response = await _create_with_retry(client or _get_default_client(), request)

    if response.stop_reason == "refusal":
        raise LLMRefusal(f"{head} request was declined by the model")

    text = "".join(block.text for block in response.content if block.type == "text")
    input_tokens = response.usage.input_tokens
    output_tokens = response.usage.output_tokens
    return LLMResult(
        text=text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd(model, input_tokens, output_tokens),
        model=model,
        stop_reason=response.stop_reason,
    )
