"""GraphQL agent adapter over HTTP (I03) [Spec §4 I03, §10, §19 HTTP/GraphQL].

The query document is a fixed, reviewed string; only ``variables`` are templated, with the same
closed whole-value placeholders as :mod:`.mapping`. Nothing is interpolated into the document and
no expression is evaluated. Response paths read the ``data`` object. A non-empty ``errors`` array
is classified (request invalid, auth, execution) and is never a passing reply; partial data with
errors is rejected unless the connection explicitly accepts it. Redirects are blocked by default,
request and response bodies are bounded, and a mutation whose response is lost is uncertain and
never resent.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Literal

import httpx
from pydantic import Field, model_validator
from susanoo_contracts import Capabilities
from susanoo_sdk import (
    AdapterError,
    AgentReply,
    CancelResult,
    CapabilityReport,
    InvocationContext,
    Message,
    SessionHandle,
    Unavailable,
)

from .mapping import AuthRef, Model, ResponseMapping, extract, render, validate_template

_OPERATION = re.compile(r"\b(query|mutation|subscription)\b|^\s*\{", re.M)
_COMMENT = re.compile(r"#[^\n]*")
REQUEST_INVALID = {"GRAPHQL_PARSE_FAILED", "GRAPHQL_VALIDATION_FAILED", "BAD_USER_INPUT", "BAD_REQUEST"}
AUTH = {"UNAUTHENTICATED", "FORBIDDEN"}


def operation_kind(document: str) -> Literal["query", "mutation", "subscription"]:
    """Kind of the first operation in a document; anonymous ``{ ... }`` is a query."""
    match = _OPERATION.search(_COMMENT.sub("", document))
    if match is None:
        raise ValueError("GraphQL document declares no operation")
    return match.group(1) or "query"  # type: ignore[return-value]


class GraphqlConnection(Model):
    kind: Literal["graphql"] = "graphql"
    url: str = Field(pattern=r"^https?://")
    query: str = Field(min_length=1, max_length=32_768)
    operation_name: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    variables: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    auth: AuthRef = Field(default_factory=AuthRef)
    response: ResponseMapping
    session_mode: Literal["client", "server", "none"] = "client"
    accept_partial_data: bool = False
    follow_redirects: bool = False
    timeout_seconds: float = Field(default=60, gt=0, le=300)
    max_request_bytes: int = Field(default=65_536, ge=256, le=1_048_576)
    max_response_bytes: int = Field(default=262_144, ge=1024)
    capabilities: Capabilities = Field(
        default_factory=lambda: Capabilities(invoke=True, sessions=True, multi_turn=True)
    )

    @model_validator(mode="after")
    def typed(self) -> GraphqlConnection:
        validate_template(self.variables)
        if "{{" in self.query:
            raise ValueError("The GraphQL document is fixed; only variables take placeholders")
        if operation_kind(self.query) == "subscription":
            raise ValueError("Subscriptions are not supported over synchronous HTTP")
        if any(name.lower() in {"authorization", "cookie"} for name in self.headers):
            raise ValueError("Credentials go through auth secret references, never literal headers")
        if self.session_mode == "server" and not self.response.session_path:
            raise ValueError("Server-issued sessions need response.session_path")
        return self

    @property
    def mutating(self) -> bool:
        return operation_kind(self.query) == "mutation"


def classify_errors(errors: list[Any]) -> str:
    """Adapter error code for a GraphQL ``errors`` array: the most specific class present."""
    codes = {
        str(error.get("extensions", {}).get("code", "")).upper()
        for error in errors
        if isinstance(error, dict) and isinstance(error.get("extensions", {}), dict)
    }
    if codes & AUTH:
        return "graphql_auth"
    if codes & REQUEST_INVALID:
        return "graphql_request_invalid"
    return "graphql_execution_error"


class GraphqlAgentAdapter:
    def __init__(self, connection: GraphqlConnection, *, transport: httpx.AsyncBaseTransport | None = None):
        self.connection = connection
        self._client = httpx.AsyncClient(
            timeout=connection.timeout_seconds,
            transport=transport,
            follow_redirects=connection.follow_redirects,
            max_redirects=3,
        )

    async def preflight(self) -> CapabilityReport:
        c = self.connection
        return CapabilityReport(
            adapter="graphql",
            adapter_version="1.0",
            capabilities=c.capabilities,
            checks={"mutation": c.mutating, "redirects_blocked": not c.follow_redirects},
            notes=["capabilities are declared by the connection, not inferred"],
        )

    async def start_session(self, context: InvocationContext) -> SessionHandle:
        return SessionHandle(session_id=f"s-{uuid.uuid4().hex[:16]}", attempt_id=context.attempt_id)

    def build_request(self, message: Message, context: InvocationContext, session_id: str) -> bytes:
        c = self.connection
        body: dict[str, Any] = {"query": c.query, "variables": render(c.variables, message, context, session_id)}
        if c.operation_name:
            body["operationName"] = c.operation_name
        encoded = json.dumps(body, separators=(",", ":")).encode()
        if len(encoded) > c.max_request_bytes:
            raise AdapterError("request_limit", "GraphQL request exceeds the configured size; not sent")
        return encoded

    async def invoke(self, session: SessionHandle, message: Message, context: InvocationContext) -> AgentReply:
        c = self.connection
        invocation_id = f"inv-{uuid.uuid4().hex[:12]}"
        content = self.build_request(message, context, session.session_id)
        headers = {
            **c.headers,
            **c.auth.headers(),
            "Content-Type": "application/json",
            "Accept": "application/graphql-response+json, application/json",
            "Idempotency-Key": invocation_id,
        }
        try:
            async with self._client.stream("POST", c.url, content=content, headers=headers) as response:
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > c.max_response_bytes:
                        raise AdapterError("output_limit", "GraphQL response exceeded the configured size")
        except httpx.TooManyRedirects:
            raise AdapterError("redirect_blocked", "Too many redirects") from None
        except httpx.TimeoutException:
            code = "invocation_uncertain" if c.mutating else "invocation_timeout"
            raise AdapterError(code, "No response before the timeout; never resent") from None
        except httpx.TransportError as error:
            code = "invocation_uncertain" if c.mutating else "transport_error"
            raise AdapterError(code, f"Transport failed: {type(error).__name__}") from None
        if 300 <= response.status_code < 400:
            raise AdapterError("redirect_blocked", "Endpoint redirected; redirects are blocked by default")
        if c.follow_redirects and response.url.host != httpx.URL(c.url).host:
            raise AdapterError("redirect_blocked", "Redirect left the configured host")
        try:
            body = json.loads(bytes(raw))
        except ValueError:
            if response.status_code >= 400:
                raise AdapterError(
                    f"http_{response.status_code}", "GraphQL endpoint returned an error status"
                ) from None
            raise AdapterError("malformed_response", "GraphQL response is not JSON") from None
        if not isinstance(body, dict):
            raise AdapterError("malformed_response", "GraphQL response is not an object")
        errors = body.get("errors")
        data = body.get("data")
        if errors is not None and (not isinstance(errors, list) or not errors):
            raise AdapterError("malformed_response", "GraphQL errors must be a non-empty list")
        if errors:
            partial = isinstance(data, dict)
            if not (partial and c.accept_partial_data):
                code = classify_errors(errors)
                if partial:
                    code = "graphql_partial"
                raise AdapterError(code, f"GraphQL returned {len(errors)} error(s)")
        if response.status_code >= 400:
            raise AdapterError(f"http_{response.status_code}", "GraphQL endpoint returned an error status")
        if not isinstance(data, dict):
            raise AdapterError("malformed_response", "GraphQL response has no data object")
        session_id = session.session_id
        if c.session_mode == "server":
            session_id = str(extract(data, c.response.session_path))
        text = extract(data, c.response.text_path)
        structured = extract(data, c.response.structured_path)
        if text is not None and not isinstance(text, str):
            raise AdapterError("response_mapping", "Mapped text is not a string")
        if structured is not None and not isinstance(structured, dict):
            raise AdapterError("response_mapping", "Mapped structured value is not an object")
        return AgentReply(
            invocation_id=invocation_id,
            session_id=session_id,
            text=text,
            structured=structured,
            usage=Unavailable(reason="GraphQL connection does not report usage"),
            tool_events=Unavailable(reason="GraphQL connection does not report tool events"),
        )

    async def cancel(self, invocation_id: str) -> CancelResult:
        return CancelResult(invocation_id=invocation_id, acknowledged=False, note="GraphQL over HTTP has no cancel")

    async def close_session(self, session: SessionHandle) -> None:
        return None

    async def aclose(self) -> None:
        await self._client.aclose()
