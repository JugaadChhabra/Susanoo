"""The code reviewer: diff in, list[Finding] out. Layer: code (prompt and policy live in agent/)."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from agent.tools import (
    MAX_TOOL_ROUNDS,
    TOOL_ERROR_PREFIX,
    Rule,
    all_tool_schemas,
    annotate_diff,
    call_tool,
    load_rules,
    lookup_rule,
    parse_diff,
)

logger = logging.getLogger("app.reviewer")

AGENT_DIR = Path(__file__).resolve().parent.parent / "agent"
DEFAULT_MODEL = "claude-sonnet-5-5"
MAX_OUTPUT_TOKENS = 4096


class Finding(BaseModel):
    """Mirrors susanoo.schemas.core.Finding field for field (this repo is standalone)."""

    file: str
    line: int
    rule_id: str  # always a policy rule id
    severity: str
    message: str


class ReviewerNotConfigured(Exception):
    """No Anthropic key was provided for the app."""


class ReviewFailed(Exception):
    """The model call failed or its answer could not be read. The message is plain English."""


def build_client() -> Any:
    """Build the real Anthropic client from ANTHROPIC_APP_API_KEY (never ANTHROPIC_API_KEY)."""
    key = os.environ.get("ANTHROPIC_APP_API_KEY", "").strip()
    if not key:
        raise ReviewerNotConfigured(
            "The reviewer is not switched on yet because no Anthropic key has been set for this app. "
            "Set ANTHROPIC_APP_API_KEY and try again."
        )
    import anthropic

    return anthropic.AsyncAnthropic(api_key=key, timeout=90.0, max_retries=2)


def _extract_json(text: str) -> Any:
    text = text.strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(text[start : end + 1])


class Reviewer:
    def __init__(
        self,
        client: Any,
        model: str | None = None,
        agent_dir: Path = AGENT_DIR,
    ) -> None:
        self.client = client
        self.model = model or os.environ.get("APP_MODEL") or DEFAULT_MODEL
        self.agent_dir = agent_dir
        self.rules: list[Rule] = load_rules(agent_dir / "policy.yaml")
        self.last_dropped = 0

    def system_prompt(self) -> str:
        prompt = (self.agent_dir / "prompt.md").read_text(encoding="utf-8")
        rules = "\n".join(f"- `{r.id}` (default_severity: {r.default_severity}): {r.text}" for r in self.rules)
        return f"{prompt.rstrip()}\n\n## Rules in force\n\nThese are the only rule ids you may use.\n\n{rules}\n"

    def user_message(self, diff: str) -> str:
        files = parse_diff(diff)
        body = annotate_diff(files) if files else diff
        body = body.replace("</diff>", "<\\/diff>")  # the diff cannot close its own data block
        return f"Review this change.\n\n<diff>\n{body}\n</diff>"

    async def _create(self, system: str, messages: list[dict[str, Any]]) -> Any:
        try:
            return await self.client.messages.create(
                model=self.model,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=system,
                messages=list(messages),
                tools=all_tool_schemas(),
            )
        except Exception as exc:  # anthropic.APIError and network failures; never leak details or keys
            logger.error("model call failed: %s", type(exc).__name__)
            raise ReviewFailed(
                "The review could not be completed because the AI service did not answer. Please try again in a moment."
            ) from exc

    async def review(self, diff: str) -> list[Finding]:
        system = self.system_prompt()
        messages: list[dict[str, Any]] = [{"role": "user", "content": self.user_message(diff)}]
        response = await self._create(system, messages)

        for _ in range(MAX_TOOL_ROUNDS):
            tool_calls = [b for b in response.content if getattr(b, "type", "") == "tool_use"]
            if not tool_calls:
                break
            results = []
            for call in tool_calls:
                output = call_tool(call.name, call.input, diff=diff, policy=self.rules)
                block: dict[str, Any] = {"type": "tool_result", "tool_use_id": call.id, "content": output}
                if output.startswith(TOOL_ERROR_PREFIX):
                    block["is_error"] = True
                results.append(block)
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": results})
            response = await self._create(system, messages)

        if any(getattr(b, "type", "") == "tool_use" for b in response.content):
            logger.error("reviewer still asking for tools after %d rounds", MAX_TOOL_ROUNDS)
            raise ReviewFailed(
                "The review took too many steps to finish, so no findings are shown. Please try again."
            )

        text = "".join(getattr(b, "text", "") for b in response.content if getattr(b, "type", "") == "text")
        return self.parse_findings(text)

    def parse_findings(self, text: str) -> list[Finding]:
        try:
            payload = _extract_json(text)
            raw = payload["findings"] if isinstance(payload, dict) else payload
            if not isinstance(raw, list):
                raise TypeError("findings is not a list")
        except (ValueError, KeyError, TypeError) as exc:
            logger.error("unreadable reviewer output (%d characters)", len(text))
            raise ReviewFailed(
                "The review came back in a form we could not read, so no findings are shown. Please try again."
            ) from exc

        findings: list[Finding] = []
        dropped_unknown_rule = dropped_malformed = 0
        for item in raw:
            try:
                finding = Finding.model_validate(item)
            except ValidationError:
                dropped_malformed += 1
                continue
            rule = lookup_rule(finding.rule_id, self.rules)
            if rule is None:
                dropped_unknown_rule += 1
                continue
            if finding.severity not in ("block", "warn", "nit"):
                dropped_malformed += 1
                continue
            findings.append(finding)

        self.last_dropped = dropped_unknown_rule + dropped_malformed
        if self.last_dropped:
            logger.warning(
                "dropped %d finding(s): %d with a rule id not in policy.yaml, %d malformed",
                self.last_dropped,
                dropped_unknown_rule,
                dropped_malformed,
            )
        return findings
