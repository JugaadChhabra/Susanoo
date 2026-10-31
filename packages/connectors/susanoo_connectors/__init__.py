"""Susanoo agent transports. Capabilities are declared per connection, never inferred from a protocol label."""

from .http import AsyncHttpAgentAdapter, AsyncHttpConnection, HttpAgentAdapter, HttpConnection
from .mapping import AuthRef, ResponseMapping
from .streaming import SseAgentAdapter, StreamConnection, WebSocketAgentAdapter

__all__ = [
    "AsyncHttpAgentAdapter",
    "AsyncHttpConnection",
    "AuthRef",
    "HttpAgentAdapter",
    "HttpConnection",
    "ResponseMapping",
    "SseAgentAdapter",
    "StreamConnection",
    "WebSocketAgentAdapter",
]
