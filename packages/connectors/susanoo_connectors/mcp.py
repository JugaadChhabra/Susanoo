"""MCP tool-server connector (I09) on the ``mcp`` Python SDK 2.3 [Spec §4 I09, §19 MCP; T21, T23].

Supported protocol revisions are pinned: ``2025-11-25`` (initialize handshake) and ``2026-07-28``
(``server/discover``). The negotiated revision must equal the configured one or preflight fails.
Transports are stdio (an argument vector, never a shell string; the child gets the SDK's safe
default environment plus named values) and Streamable HTTP.

Only allowlisted tools are callable. Their schemas come from the server's listing, may be pinned by
digest after review, and every argument object is validated against the input schema before it is
sent (structured output against the output schema when declared). Sampling and elicitation are
not advertised and so are refused unless the connection enables them *and* a handler is supplied.

A tool server is not an agent. :class:`McpToolClient` tests tool behavior directly;
:class:`McpAgentAdapter` refuses to exist without an explicit agent driver.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal, Protocol

import jsonschema
from mcp import Client, StdioServerParameters
from mcp.client.session import ElicitationFnT, SamplingFnT
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from pydantic import Field, model_validator
from susanoo_contracts import Capabilities
from susanoo_sdk import (
    AdapterError,
    AgentReply,
    CancelResult,
    CapabilityReport,
    InvocationContext,
    Message,
    Reply,
    SessionHandle,
    ToolEvent,
    Unavailable,
)
from susanoo_sdk.agent import to_agent_reply

from .mapping import SECRET_REF, AuthRef, Model, ResponseMapping, extract, render, validate_template

SUPPORTED_REVISIONS = ("2025-11-25", "2026-07-28")
Revision = Literal["2025-11-25", "2026-07-28"]
_MODE = {"2025-11-25": "legacy", "2026-07-28": "auto"}
_TOOL_NAME = r"^[A-Za-z0-9_.\-]{1,128}$"


class McpConnection(Model):
    kind: Literal["mcp"] = "mcp"
    protocol_revision: Revision
    transport: Literal["stdio", "streamable_http"]
    command: list[str] | None = Field(default=None, min_length=1, description="stdio argument vector")
    env: dict[str, str] = Field(default_factory=dict, description="Non-secret child environment values")
    env_secret_refs: dict[str, str] = Field(default_factory=dict, description="Child variable -> local secret ref")
    cwd: str | None = None
    url: str | None = Field(default=None, pattern=r"^https?://")
    auth: AuthRef = Field(default_factory=AuthRef)
    allowed_tools: list[str] = Field(min_length=1)
    tool_schema_digests: dict[str, str] = Field(default_factory=dict)
    allow_sampling: bool = False
    allow_elicitation: bool = False
    timeout_seconds: float = Field(default=60, gt=0, le=300)
    max_result_bytes: int = Field(default=262_144, ge=1024)

    @model_validator(mode="after")
    def typed(self) -> McpConnection:
        if self.transport == "stdio" and (not self.command or self.url):
            raise ValueError("stdio needs command (an argument vector) and no url")
        if self.transport == "streamable_http" and (not self.url or self.command):
            raise ValueError("streamable_http needs url and no command")
        if not all(re.fullmatch(_TOOL_NAME, name) for name in self.allowed_tools):
            raise ValueError("Allowlisted tool names must match " + _TOOL_NAME)
        if set(self.tool_schema_digests) - set(self.allowed_tools):
            raise ValueError("Schema digests may only pin allowlisted tools")
        if not all(re.fullmatch(SECRET_REF, ref) for ref in self.env_secret_refs.values()):
            raise ValueError("env_secret_refs values must be secret reference names")
        return self


def schema_digest(name: str, input_schema: dict[str, Any], output_schema: dict[str, Any] | None) -> str:
    canonical = json.dumps(
        {"name": name, "input": input_schema, "output": output_schema}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class ReviewedTool(Model):
    name: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    digest: str


class McpToolResult(Model):
    """What a tool call returned. ``is_error`` is a tool-level error, a legitimate observation."""

    tool: str
    is_error: bool
    text: str | None = None
    structured: dict[str, Any] | None = None


def _validator(schema: dict[str, Any], what: str) -> jsonschema.protocols.Validator:
    cls = jsonschema.validators.validator_for(schema, default=jsonschema.Draft202012Validator)
    try:
        cls.check_schema(schema)
    except jsonschema.SchemaError:
        raise AdapterError("preflight_failed", f"{what} schema is not a valid JSON Schema") from None
    return cls(schema)


class _Worker:
    """Owns the SDK client in one task: anyio scopes must enter and exit in the same task, while the
    runner calls adapters from ``wait_for`` child tasks. Requests are serialized through a queue."""

    def __init__(self, open_client: Callable[[], Client]):
        self._open = open_client
        self._queue: asyncio.Queue[tuple[Callable[[Client], Awaitable[Any]], asyncio.Future[Any]] | None] = (
            asyncio.Queue()
        )
        self._task: asyncio.Task[None] | None = None
        self.client: Client | None = None

    async def start(self) -> Client:
        ready: asyncio.Future[Client] = asyncio.get_running_loop().create_future()
        self._task = asyncio.create_task(self._run(ready))
        return await ready

    async def _run(self, ready: asyncio.Future[Client]) -> None:
        current: asyncio.Future[Any] | None = None
        try:
            async with self._open() as client:
                self.client = client
                ready.set_result(client)
                while (item := await self._queue.get()) is not None:
                    call, current = item
                    try:
                        result = await call(client)
                    except Exception as error:
                        if not current.done():
                            current.set_exception(error)
                    else:
                        if not current.done():
                            current.set_result(result)
        except BaseException as error:  # connection failures arrive as exception groups from anyio
            if not ready.done():
                ready.set_exception(error)
        finally:
            self.client = None
            closed = AdapterError("mcp_closed", "MCP connection closed; the request outcome is unknown")
            if current is not None and not current.done():
                current.set_exception(closed)
            while not self._queue.empty():
                item = self._queue.get_nowait()
                if item is not None and not item[1].done():
                    item[1].set_exception(closed)

    async def call(self, fn: Callable[[Client], Awaitable[Any]]) -> Any:
        if self._task is None or self._task.done():
            raise AdapterError("mcp_closed", "MCP connection is not open")
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        await self._queue.put((fn, future))
        await asyncio.wait([future, self._task], return_when=asyncio.FIRST_COMPLETED)
        if not future.done():
            raise AdapterError("mcp_closed", "MCP connection closed; the request outcome is unknown")
        return future.result()

    async def close(self) -> None:
        if self._task is not None and not self._task.done():
            await self._queue.put(None)
            try:
                await asyncio.wait_for(self._task, 10)
            except Exception:
                self._task.cancel()


class McpToolClient:
    """Tool-testing client: preflight, then call allowlisted tools with validated arguments."""

    def __init__(
        self,
        connection: McpConnection,
        *,
        http_transport: Any = None,
        secret_env: Mapping[str, str] | None = None,
        sampling_handler: SamplingFnT | None = None,
        elicitation_handler: ElicitationFnT | None = None,
    ):
        if sampling_handler is not None and not connection.allow_sampling:
            raise ValueError("A sampling handler needs allow_sampling=true on the connection")
        if elicitation_handler is not None and not connection.allow_elicitation:
            raise ValueError("An elicitation handler needs allow_elicitation=true on the connection")
        if connection.allow_sampling and sampling_handler is None:
            raise ValueError("allow_sampling needs an explicit sampling handler")
        if connection.allow_elicitation and elicitation_handler is None:
            raise ValueError("allow_elicitation needs an explicit elicitation handler")
        self.connection = connection
        self._http_transport = http_transport
        self._secret_env = secret_env
        self._http_client: Any = None
        self._sampling, self._elicitation = sampling_handler, elicitation_handler
        self._worker: _Worker | None = None
        self.tools: dict[str, ReviewedTool] = {}
        self.server_tools: list[str] = []
        self.negotiated_revision: str | None = None
        self.events: list[ToolEvent] = []

    def _client(self) -> Client:
        c = self.connection
        if c.transport == "stdio":
            assert c.command is not None
            env = dict(c.env)
            for child, ref in c.env_secret_refs.items():
                source = self._secret_env if self._secret_env is not None else os.environ
                if ref not in source:
                    raise AdapterError("missing_secret", f"Local secret {ref} is not configured")
                env[child] = source[ref]
            server: Any = StdioServerParameters(command=c.command[0], args=c.command[1:], env=env, cwd=c.cwd)
        else:
            import httpx2  # the MCP SDK's own HTTP client; headers carry the resolved auth reference

            assert c.url is not None
            self._http_client = httpx2.AsyncClient(
                headers=c.auth.headers(), timeout=c.timeout_seconds, transport=self._http_transport,
                follow_redirects=False, trust_env=False,
            )
            server = streamable_http_client(c.url, http_client=self._http_client)
        return Client(
            server,
            mode=_MODE[c.protocol_revision],
            read_timeout_seconds=c.timeout_seconds,
            sampling_callback=self._sampling,
            elicitation_callback=self._elicitation,
            cache=None,
        )

    async def connect(self) -> None:
        if self._worker is not None:
            return
        worker = _Worker(self._client)
        try:
            client = await asyncio.wait_for(worker.start(), self.connection.timeout_seconds)
        except AdapterError:
            raise
        except BaseException as error:
            await worker.close()
            raise AdapterError("preflight_failed", f"MCP connection failed: {type(error).__name__}") from None
        self._worker = worker
        self.negotiated_revision = client.protocol_version
        try:
            if client.protocol_version != self.connection.protocol_revision:
                raise AdapterError(
                    "protocol_version_mismatch",
                    f"Server negotiated MCP {client.protocol_version}; pinned {self.connection.protocol_revision}",
                )
            await self._review_tools()
        except BaseException:
            await self.aclose()
            raise

    async def _review_tools(self) -> None:
        async def list_all(client: Client) -> list[Any]:
            tools, cursor = [], None
            for _ in range(100):
                page = await client.list_tools(cursor=cursor)
                tools.extend(page.tools)
                cursor = page.next_cursor
                if cursor is None:
                    return tools
            raise AdapterError("preflight_failed", "tools/list did not finish within 100 pages")

        listed = await self._call(list_all)
        self.server_tools = [tool.name for tool in listed]
        by_name = {tool.name: tool for tool in listed}
        missing = [name for name in self.connection.allowed_tools if name not in by_name]
        if missing:
            raise AdapterError("preflight_failed", f"Allowlisted tools are not offered: {missing}")
        for name in self.connection.allowed_tools:
            tool = by_name[name]
            digest = schema_digest(name, tool.input_schema, tool.output_schema)
            pinned = self.connection.tool_schema_digests.get(name)
            if pinned is not None and pinned != digest:
                raise AdapterError("tool_schema_changed", f"Tool {name} schema differs from the reviewed digest")
            _validator(tool.input_schema, f"{name} input")
            if tool.output_schema is not None:
                _validator(tool.output_schema, f"{name} output")
            self.tools[name] = ReviewedTool(
                name=name, input_schema=tool.input_schema, output_schema=tool.output_schema, digest=digest
            )

    async def _call(self, fn: Callable[[Client], Awaitable[Any]]) -> Any:
        assert self._worker is not None
        try:
            return await self._worker.call(fn)
        except AdapterError:
            raise
        except MCPError as error:
            raise AdapterError("mcp_error", f"MCP error {error.code}: {error.error.message[:300]}") from None
        except TimeoutError:
            raise AdapterError("mcp_timeout", "MCP request timed out") from None

    async def preflight(self) -> CapabilityReport:
        await self.connect()
        return CapabilityReport(
            adapter="mcp_tools",
            adapter_version=self.connection.protocol_revision,
            capabilities=Capabilities(invoke=False, tool_events=True, modalities=["tool"]),
            checks={
                "protocol_revision": self.negotiated_revision == self.connection.protocol_revision,
                "sampling_enabled": self.connection.allow_sampling,
                "elicitation_enabled": self.connection.allow_elicitation,
            },
            notes=[
                "tool server only: tests tool behavior; evaluating an agent needs an explicit agent driver",
                f"reviewed tools {sorted(self.tools)}",
            ],
        )

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> McpToolResult:
        await self.connect()
        tool = self.tools.get(name)
        if tool is None:
            raise AdapterError("tool_not_allowlisted", f"Tool {name!r} is not allowlisted; nothing was sent")
        errors = sorted(_validator(tool.input_schema, name).iter_errors(arguments), key=lambda e: list(e.path))
        if errors:
            raise AdapterError("tool_arguments_invalid", f"{name} arguments violate its schema: {errors[0].message}")
        try:
            result = await self._call(lambda client: client.call_tool(name, arguments))
        except AdapterError as error:
            self.events.append(ToolEvent(operation=name, status="transport_error", detail={"code": error.code}))
            raise
        size = len(result.model_dump_json())
        if size > self.connection.max_result_bytes:
            raise AdapterError("output_limit", f"{name} result exceeded the configured size")
        texts = [block.text for block in result.content if getattr(block, "type", None) == "text"]
        structured = result.structured_content
        if not result.is_error and tool.output_schema is not None:
            if structured is None:
                raise AdapterError("tool_output_invalid", f"{name} declared an output schema but sent none")
            problem = next(iter(_validator(tool.output_schema, name).iter_errors(structured)), None)
            if problem is not None:
                raise AdapterError("tool_output_invalid", f"{name} output violates its schema: {problem.message}")
        self.events.append(ToolEvent(operation=name, status="error" if result.is_error else "ok"))
        return McpToolResult(
            tool=name, is_error=bool(result.is_error), text="".join(texts) if texts else None, structured=structured
        )

    async def aclose(self) -> None:
        if self._worker is not None:
            await self._worker.close()
            self._worker = None
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def __aenter__(self) -> McpToolClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()


class McpAgentToolDriver(Model):
    """Declares that one allowlisted tool *is* the agent: one call per turn through a closed template."""

    kind: Literal["agent_tool"] = "agent_tool"
    tool: str = Field(pattern=_TOOL_NAME)
    arguments: dict[str, Any]
    response: ResponseMapping

    @model_validator(mode="after")
    def typed(self) -> McpAgentToolDriver:
        validate_template(self.arguments)
        return self


class McpAgentDriver(Protocol):
    """A local agent loop that uses the MCP tools (for example, a customer's model-driven agent)."""

    async def respond(
        self, tools: McpToolClient, session_id: str, message: Message, context: InvocationContext
    ) -> Reply: ...


class McpAgentAdapter:
    """AgentAdapter over MCP. Only constructible with an explicit agent driver [T23]."""

    def __init__(
        self,
        connection: McpConnection,
        driver: McpAgentToolDriver | McpAgentDriver | None = None,
        *,
        tools: McpToolClient | None = None,
    ):
        if driver is None:
            raise AdapterError(
                "not_an_agent",
                "An MCP tool server is not an agent; provide an agent driver to evaluate one",
            )
        if isinstance(driver, McpAgentToolDriver) and driver.tool not in connection.allowed_tools:
            raise ValueError(f"Agent tool {driver.tool} must be allowlisted")
        self.connection = connection
        self.driver = driver
        self.tools = tools or McpToolClient(connection)

    async def preflight(self) -> CapabilityReport:
        await self.tools.connect()
        return CapabilityReport(
            adapter="mcp_agent",
            adapter_version=self.connection.protocol_revision,
            capabilities=Capabilities(invoke=True, sessions=True, multi_turn=True, tool_events=True),
            checks={"protocol_revision": True, "agent_driver": True},
            notes=[f"agent driver {type(self.driver).__name__}; tools {sorted(self.tools.tools)}"],
        )

    async def start_session(self, context: InvocationContext) -> SessionHandle:
        await self.tools.connect()
        return SessionHandle(session_id=f"s-{uuid.uuid4().hex[:16]}", attempt_id=context.attempt_id)

    async def invoke(self, session: SessionHandle, message: Message, context: InvocationContext) -> AgentReply:
        invocation_id = f"inv-{uuid.uuid4().hex[:12]}"
        if not isinstance(self.driver, McpAgentToolDriver):
            start = len(self.tools.events)
            reply = await self.driver.respond(self.tools, session.session_id, message, context)
            if reply.tool_events is None:
                reply = reply.model_copy(update={"tool_events": self.tools.events[start:]})
            return to_agent_reply(reply, invocation_id, session.session_id)
        d = self.driver
        try:
            result = await self.tools.call_tool(d.tool, render(d.arguments, message, context, session.session_id))
        except AdapterError as error:
            if error.code in {"mcp_timeout", "mcp_closed"}:
                raise AdapterError("invocation_uncertain", "Agent tool call did not complete; never resent") from None
            raise
        if result.is_error:
            raise AdapterError("agent_tool_error", f"Agent tool {d.tool} returned an error")
        body = result.structured or {}
        text = extract(body, d.response.text_path)
        structured = extract(body, d.response.structured_path)
        if text is not None and not isinstance(text, str):
            raise AdapterError("response_mapping", "Mapped text is not a string")
        if structured is not None and not isinstance(structured, dict):
            raise AdapterError("response_mapping", "Mapped structured value is not an object")
        return AgentReply(
            invocation_id=invocation_id,
            session_id=session.session_id,
            text=text,
            structured=structured,
            usage=Unavailable(reason="MCP does not report usage"),
            tool_events=Unavailable(reason="the agent tool does not report its inner tool events"),
        )

    async def cancel(self, invocation_id: str) -> CancelResult:
        return CancelResult(invocation_id=invocation_id, acknowledged=False, note="MCP tool calls are not cancelled")

    async def close_session(self, session: SessionHandle) -> None:
        return None

    async def aclose(self) -> None:
        await self.tools.aclose()
