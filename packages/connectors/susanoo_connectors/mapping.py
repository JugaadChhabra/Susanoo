"""Typed request/response mapping and local secret references [Spec §4 I03, §10, §29].

No expression evaluation: a template value is either a literal or *exactly* one known placeholder
(``{{message.text}}``, ``{{session_id}}``, ``{{attempt_id}}``, ``{{turn}}``, ``{{namespace}}``,
``{{context}}``). Placeholders are never interpolated into larger strings. Responses are read with
the evaluator's closed path grammar. Secrets are names resolved from the local environment at call
time and never appear in logs, reports or exceptions.
"""

from __future__ import annotations

import base64
import os
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from susanoo_evaluator.dsl import PATH_PATTERN
from susanoo_evaluator.engine import _MISSING, get_path
from susanoo_sdk import AdapterError, InvocationContext, Message

PLACEHOLDERS = {"message.text", "session_id", "attempt_id", "turn", "namespace", "context"}
SECRET_REF = r"^[A-Z][A-Z0-9_]{0,127}$"


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AuthRef(Model):
    kind: Literal["none", "bearer", "header", "basic"] = "none"
    secret_ref: str | None = Field(default=None, pattern=SECRET_REF)
    username_ref: str | None = Field(default=None, pattern=SECRET_REF)
    header: str = "Authorization"

    def headers(self) -> dict[str, str]:
        if self.kind == "none":
            return {}
        secret = _secret(self.secret_ref)
        if self.kind == "bearer":
            return {self.header: f"Bearer {secret}"}
        if self.kind == "header":
            return {self.header: secret}
        user = _secret(self.username_ref)
        return {"Authorization": "Basic " + base64.b64encode(f"{user}:{secret}".encode()).decode()}


def _secret(name: str | None) -> str:
    if not name or name not in os.environ:
        raise AdapterError("missing_secret", f"Local secret {name or '(unnamed)'} is not configured")
    return os.environ[name]


class ResponseMapping(Model):
    text_path: str | None = Field(default=None, pattern=PATH_PATTERN)
    structured_path: str | None = Field(default=None, pattern=PATH_PATTERN)
    session_path: str | None = Field(default=None, pattern=PATH_PATTERN)


def validate_template(template: Any) -> None:
    if isinstance(template, dict):
        for value in template.values():
            validate_template(value)
    elif isinstance(template, list):
        for value in template:
            validate_template(value)
    elif isinstance(template, str) and "{{" in template:
        name = template.removeprefix("{{").removesuffix("}}")
        if not (template.startswith("{{") and template.endswith("}}")) or name not in PLACEHOLDERS:
            raise ValueError(f"Template value {template!r} is not exactly one known placeholder")


def render(template: Any, message: Message, context: InvocationContext, session_id: str) -> Any:
    if isinstance(template, dict):
        return {key: render(value, message, context, session_id) for key, value in template.items()}
    if isinstance(template, list):
        return [render(value, message, context, session_id) for value in template]
    if isinstance(template, str) and template.startswith("{{") and template.endswith("}}"):
        name = template[2:-2]
        if name == "message.text":
            return message.text
        if name == "session_id":
            return session_id
        if name == "attempt_id":
            return context.attempt_id
        if name == "turn":
            return context.turn
        if name == "namespace":
            return context.namespace
        if name == "context":
            # Wrapper pattern: the agent's own context, including its scoped tool-route token.
            data = context.model_dump(mode="json")
            if context.tool_route is not None:
                data["tool_route"]["token"] = context.tool_route.token.get_secret_value()
            return data
    return template


def extract(body: Any, path: str | None) -> Any:
    if path is None:
        return None
    value = get_path(body, path)
    if value is _MISSING:
        raise AdapterError("response_mapping", f"Response has no value at {path}")
    return value
