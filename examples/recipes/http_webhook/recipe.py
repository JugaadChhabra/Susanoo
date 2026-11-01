"""HTTP recipe (I14) for hosted agents and n8n-style webhooks, via the generic I03 ``HttpAgentAdapter`` [Spec §4, §19].

Two typed connections, no custom code paths:

- ``n8n_chat_connection``: an n8n Chat Trigger / webhook workflow (``chatInput`` + ``sessionId`` in,
  ``output`` out). The workflow uses its own backend configuration, so point that at a Susanoo
  simulator namespace or test tenant; the runner still grades from the observer, never the reply text.
- ``wrapper_connection``: a customer wrapper that accepts the invocation context (with the scoped tool
  route) in the body, as ``collections_demo.http_app`` does. This is what runs end to end in tests.

Credentials are secret *references* (environment variable names), never literal header values.
Tested against local fake servers only (``tests/runtime/test_recipes.py``); no live n8n instance.
"""

from __future__ import annotations

from susanoo_connectors import AuthRef, HttpAgentAdapter, HttpConnection, ResponseMapping


def n8n_chat_connection(url: str, *, secret_ref: str | None = None, header: str = "Authorization") -> HttpConnection:
    """n8n header auth: ``header`` other than Authorization sends the raw secret; Authorization sends a bearer."""
    if secret_ref is None:
        auth = AuthRef()
    elif header.lower() == "authorization":
        auth = AuthRef(kind="bearer", secret_ref=secret_ref)
    else:
        auth = AuthRef(kind="header", secret_ref=secret_ref, header=header)
    return HttpConnection(
        url=url,
        request={"chatInput": "{{message.text}}", "sessionId": "{{session_id}}", "action": "sendMessage"},
        response=ResponseMapping(text_path="output"),
        auth=auth,
    )


def wrapper_connection(url: str, *, text_path: str = "reply.text") -> HttpConnection:
    return HttpConnection(
        url=url,
        request={"session_id": "{{session_id}}", "message": "{{message.text}}", "context": "{{context}}"},
        response=ResponseMapping(text_path=text_path),
    )


def adapter(connection: HttpConnection) -> HttpAgentAdapter:
    return HttpAgentAdapter(connection)
