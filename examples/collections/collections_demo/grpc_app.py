"""Fake gRPC provider for contract tests (I10) [Spec §19 gRPC]. In-memory channel, not a live server.

Builds a reviewed ``FileDescriptorSet`` for ``susanoo.demo.v1.AgentService`` (unary ``Chat``,
``GetVersion`` and a destructive ``PurgeAccounts`` that is never mapped, plus a server-streaming
``StreamChat``) and serves the collections agent through a :class:`FakeGrpcTransport` that speaks
raw protobuf bytes exactly like a ``grpc.aio`` channel with identity serializers.
"""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable

from google.protobuf import descriptor_pb2, descriptor_pool, json_format, message_factory, struct_pb2
from susanoo_connectors.grpc_connector import GrpcStatusError, descriptor_digest
from susanoo_sdk import InvocationContext, Message

from .agent import CollectionsAgent, Mode

PACKAGE = "susanoo.demo.v1"
SERVICE = f"{PACKAGE}.AgentService"
Handler = Callable[[bytes], Awaitable[bytes]]


def _field(name: str, number: int, kind: int, type_name: str | None = None) -> descriptor_pb2.FieldDescriptorProto:
    f = descriptor_pb2.FieldDescriptorProto(
        name=name, number=number, type=kind, label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    )
    if type_name:
        f.type_name = type_name
    return f


def descriptor_set() -> bytes:
    string, message, boolean = (
        descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
        descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE,
        descriptor_pb2.FieldDescriptorProto.TYPE_BOOL,
    )
    struct_file = descriptor_pb2.FileDescriptorProto()
    struct_pb2.DESCRIPTOR.CopyToProto(struct_file)
    demo = descriptor_pb2.FileDescriptorProto(
        name="susanoo/demo/v1/agent.proto", package=PACKAGE, syntax="proto3", dependency=[struct_file.name]
    )
    demo.message_type.add(name="Turn").field.extend(
        [
            _field("session_id", 1, string),
            _field("message", 2, string),
            _field("context", 3, message, ".google.protobuf.Struct"),
        ]
    )
    demo.message_type.add(name="TurnReply").field.extend([_field("text", 1, string), _field("session_id", 2, string)])
    demo.message_type.add(name="VersionRequest")
    demo.message_type.add(name="VersionReply").field.extend([_field("api_version", 1, string)])
    demo.message_type.add(name="PurgeRequest").field.extend([_field("confirm", 1, boolean)])
    demo.message_type.add(name="PurgeReply").field.extend([_field("purged", 1, boolean)])
    service = demo.service.add(name="AgentService")
    service.method.add(name="Chat", input_type=f".{PACKAGE}.Turn", output_type=f".{PACKAGE}.TurnReply")
    service.method.add(
        name="GetVersion", input_type=f".{PACKAGE}.VersionRequest", output_type=f".{PACKAGE}.VersionReply"
    )
    service.method.add(
        name="PurgeAccounts", input_type=f".{PACKAGE}.PurgeRequest", output_type=f".{PACKAGE}.PurgeReply"
    )
    service.method.add(
        name="StreamChat", input_type=f".{PACKAGE}.Turn", output_type=f".{PACKAGE}.TurnReply", server_streaming=True
    )
    return descriptor_pb2.FileDescriptorSet(file=[struct_file, demo]).SerializeToString()


DESCRIPTOR_SET = descriptor_set()
DESCRIPTOR_B64 = base64.b64encode(DESCRIPTOR_SET).decode()
DESCRIPTOR_DIGEST = descriptor_digest(DESCRIPTOR_SET)


def _classes() -> dict[str, type]:
    files = descriptor_pb2.FileDescriptorSet.FromString(DESCRIPTOR_SET)
    pool = descriptor_pool.DescriptorPool()
    for file in files.file:
        pool.Add(file)
    names = ["Turn", "TurnReply", "VersionRequest", "VersionReply", "PurgeRequest", "PurgeReply"]
    return {n: message_factory.GetMessageClass(pool.FindMessageTypeByName(f"{PACKAGE}.{n}")) for n in names}


class FakeGrpcTransport:
    """In-memory channel: path -> handler on raw bytes. Records every path it was asked to call."""

    def __init__(self, handlers: dict[str, Handler]):
        self.handlers = handlers
        self.calls: list[str] = []
        self.metadata: list[list[tuple[str, str]]] = []

    async def unary_unary(self, path: str, request: bytes, *, timeout: float, metadata: list[tuple[str, str]]) -> bytes:
        self.calls.append(path)
        self.metadata.append(metadata)
        handler = self.handlers.get(path)
        if handler is None:
            raise GrpcStatusError("UNIMPLEMENTED", path)
        return await handler(request)

    async def aclose(self) -> None:
        return None


def collections_handlers(mode: Mode = "correct", *, api_version: str = "v1") -> dict[str, Handler]:
    classes = _classes()
    agent = CollectionsAgent(mode)
    purged: list[bool] = []

    async def chat(raw: bytes) -> bytes:
        turn = classes["Turn"].FromString(raw)
        context = InvocationContext.model_validate(json_format.MessageToDict(turn.context))
        reply = await agent.respond(turn.session_id, Message(text=turn.message), context)
        return classes["TurnReply"](text=reply.text or "", session_id=turn.session_id).SerializeToString()

    async def version(raw: bytes) -> bytes:
        return classes["VersionReply"](api_version=api_version).SerializeToString()

    async def purge(raw: bytes) -> bytes:
        purged.append(True)
        return classes["PurgeReply"](purged=True).SerializeToString()

    return {
        f"/{SERVICE}/Chat": chat,
        f"/{SERVICE}/GetVersion": version,
        f"/{SERVICE}/PurgeAccounts": purge,
    }
