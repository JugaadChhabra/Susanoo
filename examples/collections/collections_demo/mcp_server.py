"""Fake MCP provider for contract tests (I09) [Spec §4 I09, §19 MCP]. Not a live vendor server.

Tools: ``lookup_account`` (read-only, typed schema), ``chat`` (wraps the collections agent so an
explicit agent driver can evaluate it), ``close_account`` (mutating, never allowlisted in tests),
``fail`` (returns an MCP tool error), ``ask_model`` / ``ask_user`` (request sampling / elicitation
from the client, which Susanoo denies unless configured). Run over stdio or Streamable HTTP::

    python -m collections_demo.mcp_server stdio
    python -m collections_demo.mcp_server http --port 8765
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp_types import SamplingMessage, TextContent
from pydantic import BaseModel, Field


class AccountView(BaseModel):
    account_id: str
    status: str
    overdue_minor: int


class ChatReply(BaseModel):
    text: str
    session_id: str


class Confirmation(BaseModel):
    confirm: bool = Field(description="Confirm the action")


ACCOUNTS = {"ACC-1001": AccountView(account_id="ACC-1001", status="overdue", overdue_minor=450000)}


def build_server(mode: str = "correct") -> MCPServer:
    server = MCPServer("collections-fake-mcp", version="1.0")
    calls: list[str] = []
    server.calls = calls  # type: ignore[attr-defined]
    agent = None

    @server.tool()
    def lookup_account(account_id: str) -> AccountView:
        """Read one synthetic account."""
        calls.append("lookup_account")
        if account_id not in ACCOUNTS:
            raise ValueError("unknown account")
        return ACCOUNTS[account_id]

    @server.tool()
    def close_account(account_id: str) -> str:
        """Mutating tool that must never run unless explicitly allowlisted."""
        calls.append("close_account")
        return f"closed {account_id}"

    @server.tool()
    def fail() -> str:
        """Always fails."""
        raise ValueError("synthetic failure")

    @server.tool()
    async def chat(session_id: str, message: str, context: dict[str, Any]) -> ChatReply:
        """The collections agent as an MCP tool (needs an explicit agent driver to be evaluated)."""
        nonlocal agent
        from susanoo_sdk import InvocationContext, Message

        from .agent import CollectionsAgent

        agent = agent or CollectionsAgent(mode)  # type: ignore[arg-type]
        reply = await agent.respond(session_id, Message(text=message), InvocationContext.model_validate(context))
        return ChatReply(text=reply.text or "", session_id=session_id)

    @server.tool()
    async def ask_model(question: str, ctx: Context) -> str:
        """Asks the client to sample a model."""
        result = await ctx.session.create_message(
            messages=[SamplingMessage(role="user", content=TextContent(type="text", text=question))], max_tokens=16
        )
        return result.content.text if isinstance(result.content, TextContent) else "?"

    @server.tool()
    async def ask_user(ctx: Context) -> str:
        """Asks the client to elicit user input."""
        result = await ctx.elicit("Confirm?", Confirmation)
        return result.action

    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("transport", choices=["stdio", "http"])
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--mode", default="correct")
    args = parser.parse_args(argv)
    server = build_server(args.mode)
    if args.transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main(sys.argv[1:])
