"""Connectors generated agents use to reach real systems. [PRD › Connectors and API budget]"""

from susanoo.connectors.base import (
    Connector,
    ConnectorGuide,
    ConnectorSpec,
    ConnectorTool,
    Credential,
    InvalidCredential,
    TestResult,
    ToolResult,
    install_log_redaction,
    known_secret_values,
    redact,
    redact_secret_values,
    redact_value,
)
from susanoo.connectors.anthropic import AnthropicConnector
from susanoo.connectors.github import GitHubConnector


def registry() -> dict[str, Connector]:
    """Every live connector, by id."""
    return {c.spec.id: c for c in (GitHubConnector(), AnthropicConnector())}


__all__ = [
    "Connector",
    "ConnectorGuide",
    "ConnectorSpec",
    "ConnectorTool",
    "Credential",
    "InvalidCredential",
    "TestResult",
    "ToolResult",
    "install_log_redaction",
    "known_secret_values",
    "redact",
    "redact_secret_values",
    "redact_value",
    "registry",
]
