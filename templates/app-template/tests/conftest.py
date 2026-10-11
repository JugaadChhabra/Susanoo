"""Shared fakes. No test here ever calls the real Anthropic API."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any


class FakeMessages:
    def __init__(self, reply: str | Exception) -> None:
        self.reply = reply
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self.reply, Exception):
            raise self.reply
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.reply)])


class FakeClient:
    def __init__(self, reply: str | Exception | list[dict[str, Any]]) -> None:
        if isinstance(reply, list):
            reply = json.dumps({"findings": reply})
        self.messages = FakeMessages(reply)


SQL_DIFF = """diff --git a/app/db/queries.py b/app/db/queries.py
--- a/app/db/queries.py
+++ b/app/db/queries.py
@@ -4,3 +4,9 @@
 def get_order(order_id: int):
     conn = get_connection()
     return conn.execute("SELECT * FROM orders WHERE id = %s", (order_id,)).fetchone()
+
+
+def search_orders(customer_name: str):
+    conn = get_connection()
+    query = f"SELECT * FROM orders WHERE customer = '{customer_name}'"
+    return conn.execute(query).fetchall()
"""

SQL_FINDING = {
    "file": "app/db/queries.py",
    "line": 11,
    "rule_id": "no-raw-sql",
    "severity": "block",
    "message": "The query is built with an f-string. Pass the name as a query parameter.",
}


def text_response(text: str) -> Any:
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn")


def tool_response(*calls: tuple[str, str, Any]) -> Any:
    blocks = [SimpleNamespace(type="tool_use", id=i, name=n, input=a) for i, n, a in calls]
    return SimpleNamespace(content=blocks, stop_reason="tool_use")


class ScriptedMessages:
    """Replays a fixed list of responses; repeats the last one if the script runs out."""

    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]


class ScriptedClient:
    def __init__(self, responses: list[Any]) -> None:
        self.messages = ScriptedMessages(responses)
