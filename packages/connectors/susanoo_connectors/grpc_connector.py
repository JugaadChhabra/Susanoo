"""gRPC agent adapter against reviewed protobuf descriptors (I10) [Spec §4 I10, §19 gRPC; T21-T23].

The connection carries a serialized ``FileDescriptorSet`` pinned by SHA-256 digest after review, and
an explicit table of mapped unary methods with pinned request/response types and a reviewed
``mutating`` flag. Nothing comes from server reflection, and only mapped methods can be called, so
a descriptor that also declares a destructive method does not make it reachable. Requests are
rendered with the closed placeholders of :mod:`.mapping` and parsed into dynamic messages
(unknown fields rejected); responses are decoded into dicts read with the closed path grammar.

Status mapping: request-side codes (``INVALID_ARGUMENT``, ``PERMISSION_DENIED``...) are rejections.
``UNAVAILABLE``/``DEADLINE_EXCEEDED`` and other post-send failures on a mutating method are
*uncertain* and never retried; read-only methods may retry ``UNAVAILABLE``/``DEADLINE_EXCEEDED``
within a small budget. Streaming methods are an unsupported feature and fail preflight. An optional
version method makes a server API version mismatch an explicit preflight failure.

The wire transport is a :class:`GrpcTransport` protocol. :class:`GrpcioTransport` imports
``grpcio`` lazily and fails clearly when it is not installed.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from google.protobuf import descriptor_pb2, descriptor_pool, json_format, message_factory
from google.protobuf.message import DecodeError
from pydantic import Field, model_validator
from susanoo_contracts import Capabilities
from susanoo_evaluator.dsl import PATH_PATTERN
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

_NAME = r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$"
REJECTED = {
    "INVALID_ARGUMENT",
    "NOT_FOUND",
    "ALREADY_EXISTS",
    "PERMISSION_DENIED",
    "UNAUTHENTICATED",
    "FAILED_PRECONDITION",
    "OUT_OF_RANGE",
    "UNIMPLEMENTED",
}
READ_RETRYABLE = {"UNAVAILABLE", "DEADLINE_EXCEEDED"}
StatusClass = Literal["rejected", "uncertain", "retryable", "failed"]


def classify_status(code: str, *, mutating: bool) -> StatusClass:
    """How a non-OK status is treated. Mutating methods never get ``retryable``."""
    if code in REJECTED:
        return "rejected"
    if mutating:
        return "uncertain"
    return "retryable" if code in READ_RETRYABLE else "failed"


def descriptor_digest(descriptor_set: bytes) -> str:
    return hashlib.sha256(descriptor_set).hexdigest()


class GrpcStatusError(Exception):
    """A non-OK gRPC status from the transport. ``code`` is the canonical name, e.g. ``UNAVAILABLE``."""

    def __init__(self, code: str, details: str = ""):
        super().__init__(f"{code}: {details}")
        self.code = code
        self.details = details


class GrpcTransport(Protocol):
    async def unary_unary(
        self, path: str, request: bytes, *, timeout: float, metadata: list[tuple[str, str]]
    ) -> bytes: ...

    async def aclose(self) -> None: ...


class GrpcioTransport:
    """``grpc.aio`` channel with identity (bytes) serializers. ``grpcio`` is imported on first use."""

    def __init__(
        self,
        target: str,
        *,
        tls: bool = True,
        max_message_bytes: int = 4_194_304,
        authority: str | None = None,
    ):
        self.target, self.tls, self.max_message_bytes = target, tls, max_message_bytes
        self.authority = authority
        self._channel: Any = None

    def _open(self) -> Any:
        try:
            import grpc
            from grpc import aio
        except ImportError:
            raise AdapterError(
                "dependency_missing", "gRPC connections need the grpcio package on the runner (grpcio>=1.66)"
            ) from None
        if self._channel is None:
            options = [
                ("grpc.max_send_message_length", self.max_message_bytes),
                ("grpc.max_receive_message_length", self.max_message_bytes),
                ("grpc.enable_http_proxy", 0),
            ]
            if self.authority:
                options.append(("grpc.default_authority", self.authority))
            if self.tls:
                if self.authority:
                    options.append(("grpc.ssl_target_name_override", self.authority))
                self._channel = aio.secure_channel(self.target, grpc.ssl_channel_credentials(), options=options)
            else:
                self._channel = aio.insecure_channel(self.target, options=options)
        return self._channel

    async def unary_unary(self, path: str, request: bytes, *, timeout: float, metadata: list[tuple[str, str]]) -> bytes:
        channel = self._open()
        from grpc import aio

        try:
            return await channel.unary_unary(path)(request, timeout=timeout, metadata=metadata)
        except aio.AioRpcError as error:
            raise GrpcStatusError(error.code().name, error.details() or "") from None

    async def aclose(self) -> None:
        if self._channel is not None:
            await self._channel.close()
            self._channel = None


class GrpcMethod(Model):
    """One reviewed unary method. Types are pinned so a changed descriptor cannot silently retarget it."""

    service: str = Field(pattern=_NAME)
    method: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    request_type: str = Field(pattern=_NAME)
    response_type: str = Field(pattern=_NAME)
    mutating: bool
    deadline_seconds: float = Field(default=30, gt=0, le=300)
    request: dict[str, Any] = Field(default_factory=dict)
    response: ResponseMapping = Field(default_factory=ResponseMapping)

    @model_validator(mode="after")
    def typed(self) -> GrpcMethod:
        validate_template(self.request)
        return self

    @property
    def path(self) -> str:
        return f"/{self.service}/{self.method}"


class GrpcVersionCheck(Model):
    method: str = Field(description="Key of a non-mutating mapped method")
    version_path: str = Field(pattern=PATH_PATTERN)
    expected: str = Field(min_length=1, max_length=100)


class GrpcConnection(Model):
    kind: Literal["grpc"] = "grpc"
    target: str = Field(pattern=r"^[A-Za-z0-9.\-\[\]:]+:\d{1,5}$")
    tls: bool = True
    descriptor_set_b64: str = Field(min_length=4, max_length=2_000_000)
    descriptor_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    methods: dict[str, GrpcMethod] = Field(min_length=1)
    invoke_method: str
    version_check: GrpcVersionCheck | None = None
    auth: AuthRef = Field(default_factory=AuthRef)
    read_retries: int = Field(default=1, ge=0, le=3)
    max_message_bytes: int = Field(default=1_048_576, ge=1024, le=4_194_304)

    @model_validator(mode="after")
    def typed(self) -> GrpcConnection:
        if self.invoke_method not in self.methods:
            raise ValueError("invoke_method must name a mapped method")
        if self.version_check is not None:
            check = self.methods.get(self.version_check.method)
            if check is None or check.mutating:
                raise ValueError("version_check must use a mapped, non-mutating method")
        return self

    def descriptor_set(self) -> bytes:
        return base64.b64decode(self.descriptor_set_b64, validate=True)


class _Resolved:
    def __init__(self, connection: GrpcConnection):
        raw = connection.descriptor_set()
        if descriptor_digest(raw) != connection.descriptor_digest:
            raise AdapterError("descriptor_digest_mismatch", "Descriptor set differs from the reviewed digest")
        files = descriptor_pb2.FileDescriptorSet()
        try:
            files.ParseFromString(raw)
        except DecodeError:
            raise AdapterError("preflight_failed", "Descriptor set is not a FileDescriptorSet") from None
        pool = descriptor_pool.DescriptorPool()
        try:
            for file in files.file:
                pool.Add(file)
        except (TypeError, ValueError) as error:
            raise AdapterError("preflight_failed", f"Descriptor set does not load: {error}") from None
        self.classes: dict[str, tuple[type, type]] = {}
        for key, mapped in connection.methods.items():
            try:
                service = pool.FindServiceByName(mapped.service)
            except KeyError:
                raise AdapterError("preflight_failed", f"Service {mapped.service} is not in the descriptor") from None
            method = service.methods_by_name.get(mapped.method)
            if method is None:
                raise AdapterError("preflight_failed", f"Method {mapped.path} is not in the descriptor")
            if method.client_streaming or method.server_streaming:
                raise AdapterError("unsupported_feature", f"{mapped.path} is a streaming method; only unary is mapped")
            if (method.input_type.full_name, method.output_type.full_name) != (
                mapped.request_type,
                mapped.response_type,
            ):
                raise AdapterError("preflight_failed", f"{mapped.path} types differ from the reviewed mapping")
            self.classes[key] = (
                message_factory.GetMessageClass(method.input_type),
                message_factory.GetMessageClass(method.output_type),
            )


class GrpcClient:
    """Calls mapped methods only, with typed messages, deadlines and status classification."""

    def __init__(self, connection: GrpcConnection, *, transport: GrpcTransport | None = None):
        self.connection = connection
        self.transport = transport or GrpcioTransport(
            connection.target, tls=connection.tls, max_message_bytes=connection.max_message_bytes
        )
        self._resolved: _Resolved | None = None
        self.attempts: list[str] = []

    def resolved(self) -> _Resolved:
        if self._resolved is None:
            self._resolved = _Resolved(self.connection)
        return self._resolved

    def _metadata(self) -> list[tuple[str, str]]:
        return [(name.lower(), value) for name, value in self.connection.auth.headers().items()]

    async def call(self, key: str, request: dict[str, Any], *, deadline: datetime | None = None) -> dict[str, Any]:
        mapped = self.connection.methods.get(key)
        if mapped is None:
            raise AdapterError("method_not_mapped", f"{key!r} is not a reviewed method; nothing was sent")
        request_cls, response_cls = self.resolved().classes[key]
        try:
            message = json_format.ParseDict(request, request_cls(), ignore_unknown_fields=False)
        except json_format.ParseError as error:
            raise AdapterError("request_invalid", f"{mapped.path} request does not match its type: {error}") from None
        payload = message.SerializeToString()
        if len(payload) > self.connection.max_message_bytes:
            raise AdapterError("request_limit", f"{mapped.path} request exceeds the configured size; not sent")
        tries = 1 if mapped.mutating else 1 + self.connection.read_retries
        for attempt in range(tries):
            timeout = mapped.deadline_seconds
            if deadline is not None:
                timeout = min(timeout, (deadline - datetime.now(UTC)).total_seconds())
            if timeout <= 0:
                raise AdapterError("deadline_exceeded", f"No time left to call {mapped.path}; not sent")
            self.attempts.append(key)
            try:
                raw = await self.transport.unary_unary(mapped.path, payload, timeout=timeout, metadata=self._metadata())
            except GrpcStatusError as error:
                kind = classify_status(error.code, mutating=mapped.mutating)
                if kind == "uncertain":
                    raise AdapterError(
                        "invocation_uncertain", f"{mapped.path} ended {error.code} after send; never retried"
                    ) from None
                if kind == "retryable" and attempt + 1 < tries:
                    continue
                raise AdapterError(f"grpc_{error.code.lower()}", f"{mapped.path} returned {error.code}") from None
            if len(raw) > self.connection.max_message_bytes:
                raise AdapterError("output_limit", f"{mapped.path} response exceeded the configured size")
            try:
                reply = response_cls.FromString(raw)
            except DecodeError:
                raise AdapterError("malformed_response", f"{mapped.path} response does not decode") from None
            return json_format.MessageToDict(
                reply, preserving_proto_field_name=True, always_print_fields_with_no_presence=True
            )
        raise AssertionError("unreachable")  # pragma: no cover

    async def aclose(self) -> None:
        await self.transport.aclose()


class GrpcAgentAdapter:
    def __init__(self, connection: GrpcConnection, *, transport: GrpcTransport | None = None):
        self.connection = connection
        self.client = GrpcClient(connection, transport=transport)

    async def preflight(self) -> CapabilityReport:
        self.client.resolved()
        check = self.connection.version_check
        version_ok: bool | None = None
        if check is not None:
            body = await self.client.call(check.method, self.connection.methods[check.method].request)
            actual = extract(body, check.version_path)
            if actual != check.expected:
                raise AdapterError(
                    "protocol_version_mismatch", f"Server API version {actual!r}; connection expects {check.expected}"
                )
            version_ok = True
        invoke = self.connection.methods[self.connection.invoke_method]
        return CapabilityReport(
            adapter="grpc",
            adapter_version="1.0",
            capabilities=Capabilities(invoke=True, sessions=True, multi_turn=True),
            checks={"descriptor_digest": True, "unary_only": True, **({"api_version": True} if version_ok else {})},
            notes=[f"invoke {invoke.path} (mutating={invoke.mutating}); descriptors pinned, no reflection"],
        )

    async def start_session(self, context: InvocationContext) -> SessionHandle:
        self.client.resolved()
        return SessionHandle(session_id=f"s-{uuid.uuid4().hex[:16]}", attempt_id=context.attempt_id)

    async def invoke(self, session: SessionHandle, message: Message, context: InvocationContext) -> AgentReply:
        mapped = self.connection.methods[self.connection.invoke_method]
        invocation_id = f"inv-{uuid.uuid4().hex[:12]}"
        request = render(mapped.request, message, context, session.session_id)
        body = await self.client.call(self.connection.invoke_method, request, deadline=context.deadline)
        text = extract(body, mapped.response.text_path)
        structured = extract(body, mapped.response.structured_path)
        if text is not None and not isinstance(text, str):
            raise AdapterError("response_mapping", "Mapped text is not a string")
        if structured is not None and not isinstance(structured, dict):
            raise AdapterError("response_mapping", "Mapped structured value is not an object")
        session_id = session.session_id
        if mapped.response.session_path:
            session_id = str(extract(body, mapped.response.session_path))
        return AgentReply(
            invocation_id=invocation_id,
            session_id=session_id,
            text=text,
            structured=structured,
            usage=Unavailable(reason="gRPC connection does not report usage"),
            tool_events=Unavailable(reason="gRPC connection does not report tool events"),
        )

    async def cancel(self, invocation_id: str) -> CancelResult:
        return CancelResult(
            invocation_id=invocation_id, acknowledged=False, note="client-side cancel only; not proof work stopped"
        )

    async def close_session(self, session: SessionHandle) -> None:
        return None

    async def aclose(self) -> None:
        await self.client.aclose()
