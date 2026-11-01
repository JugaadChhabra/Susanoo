"""Susanoo agent transports. Capabilities are declared per connection, never inferred from a protocol label.

Connectors with optional dependencies are imported from their modules: ``susanoo_connectors.mcp`` (mcp SDK),
``susanoo_connectors.grpc_connector`` (protobuf, grpcio for the wire), ``susanoo_connectors.media`` (voice).
"""

from .a2a import A2AAgentAdapter, A2AConnection
from .browser import BrowserAgentAdapter, BrowserConnection, PlaywrightDriver
from .container import CliAgentAdapter, CliProfile, ContainerAgentAdapter, ContainerProfile, CrashEvidence
from .graphql import GraphqlAgentAdapter, GraphqlConnection
from .http import AsyncHttpAgentAdapter, AsyncHttpConnection, HttpAgentAdapter, HttpConnection
from .mapping import AuthRef, ResponseMapping
from .streaming import SseAgentAdapter, StreamConnection, WebSocketAgentAdapter
from .webhook import (
    MemoryRelayQueue,
    MemoryTaskRegistry,
    WebhookAgentAdapter,
    WebhookConnection,
    WebhookRejected,
    handle_callback,
)

__all__ = [
    "A2AAgentAdapter",
    "A2AConnection",
    "AsyncHttpAgentAdapter",
    "AsyncHttpConnection",
    "AuthRef",
    "BrowserAgentAdapter",
    "BrowserConnection",
    "CliAgentAdapter",
    "CliProfile",
    "ContainerAgentAdapter",
    "ContainerProfile",
    "CrashEvidence",
    "GraphqlAgentAdapter",
    "GraphqlConnection",
    "HttpAgentAdapter",
    "HttpConnection",
    "MemoryRelayQueue",
    "MemoryTaskRegistry",
    "PlaywrightDriver",
    "ResponseMapping",
    "SseAgentAdapter",
    "StreamConnection",
    "WebSocketAgentAdapter",
    "WebhookAgentAdapter",
    "WebhookConnection",
    "WebhookRejected",
    "handle_callback",
]
