"""Fake A2A 0.3.0 provider for contract tests (I10) [Spec §19 A2A]. Not a live vendor agent.

Serves an Agent Card at ``/.well-known/agent-card.json`` and a JSON-RPC endpoint at ``/a2a`` with
``message/send``, ``message/stream`` (only when streaming is advertised), ``tasks/get`` and
``tasks/cancel``. Each message runs the collections agent; ``contextId`` is the agent session. The
agent's invocation context arrives in ``message.metadata.susanoo_context`` (wrapper pattern).
Behaviour switches let tests exercise version mismatch, slow tasks and failures.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from susanoo_sdk import InvocationContext, Message

from .agent import CollectionsAgent, Mode


def create_app(
    mode: Mode = "correct",
    *,
    protocol_version: str = "0.3.0",
    streaming: bool = True,
    working_polls: int = 0,
    fail_with: str | None = None,
    rpc_url: str | None = None,
    rpc_path: str = "/a2a",
) -> FastAPI:
    app = FastAPI(title="Fake A2A collections agent")
    agent = CollectionsAgent(mode)
    tasks: dict[str, dict[str, Any]] = {}
    pending: dict[str, int] = {}
    app.state.methods = []
    app.state.sends = 0

    @app.get("/.well-known/agent-card.json")
    async def card(request: Request):
        base = str(request.base_url).rstrip("/")
        return {
            "protocolVersion": protocol_version,
            "name": "Collections agent",
            "description": "Synthetic collections agent",
            "url": rpc_url or f"{base}{rpc_path}",
            "version": "1.0.0",
            "preferredTransport": "JSONRPC",
            "capabilities": {"streaming": streaming, "pushNotifications": False},
            "defaultInputModes": ["text/plain"],
            "defaultOutputModes": ["text/plain"],
            "skills": [{"id": "collections", "name": "Collections", "description": "Promises and handoffs"}],
        }

    async def run_turn(message: dict[str, Any]) -> dict[str, Any]:
        app.state.sends += 1
        context_id = message.get("contextId") or uuid.uuid4().hex
        task_id = uuid.uuid4().hex
        text = "".join(p.get("text", "") for p in message.get("parts", []) if p.get("kind") == "text")
        ctx = InvocationContext.model_validate((message.get("metadata") or {})["susanoo_context"])
        reply = await agent.respond(context_id, Message(text=text), ctx)
        if fail_with:
            state, artifacts = fail_with, []
        else:
            state = "working" if working_polls else "completed"
            artifacts = [{"artifactId": "reply", "parts": [{"kind": "text", "text": reply.text or ""}]}]
        task = {
            "kind": "task",
            "id": task_id,
            "contextId": context_id,
            "status": {"state": state},
            "artifacts": artifacts if state == "completed" else [],
        }
        tasks[task_id] = {**task, "final_artifacts": artifacts}
        pending[task_id] = working_polls
        return task

    def ok(request_id: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def err(request_id: Any, code: int, message: str) -> JSONResponse:
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

    def public(task: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in task.items() if k != "final_artifacts"}

    @app.post("/a2a")
    async def rpc(request: Request):
        body = await request.json()
        method, request_id, params = body.get("method"), body.get("id"), body.get("params") or {}
        app.state.methods.append(method)
        if method == "message/send":
            return ok(request_id, await run_turn(params["message"]))
        if method == "message/stream":
            if not streaming:
                return err(request_id, -32004, "streaming not supported")
            task = await run_turn(params["message"])
            stored = tasks[task["id"]]

            async def events():
                submitted = {**task, "status": {"state": "submitted"}, "artifacts": []}
                yield f"data: {json.dumps(ok(request_id, submitted))}\n\n"
                for part in stored["final_artifacts"][0]["parts"] if stored["final_artifacts"] else []:
                    for i, word in enumerate(part["text"].split(" ")):
                        chunk = {
                            "kind": "artifact-update",
                            "taskId": task["id"],
                            "contextId": task["contextId"],
                            "append": i > 0,
                            "artifact": {"artifactId": "reply", "parts": [{"kind": "text", "text": word + " "}]},
                        }
                        yield f"data: {json.dumps(ok(request_id, chunk))}\n\n"
                final = {
                    "kind": "status-update",
                    "taskId": task["id"],
                    "contextId": task["contextId"],
                    "status": {"state": "completed" if not fail_with else fail_with},
                    "final": True,
                }
                yield f"data: {json.dumps(ok(request_id, final))}\n\n"

            return StreamingResponse(events(), media_type="text/event-stream")
        if method == "tasks/get":
            task = tasks.get(params.get("id"))
            if task is None:
                return err(request_id, -32001, "task not found")
            if pending.get(task["id"], 0) > 0:
                pending[task["id"]] -= 1
                if pending[task["id"]] == 0 and task["status"]["state"] == "working":
                    task["status"] = {"state": "completed"}
                    task["artifacts"] = task["final_artifacts"]
            return ok(request_id, public(task))
        if method == "tasks/cancel":
            task = tasks.get(params.get("id"))
            if task is None:
                return err(request_id, -32001, "task not found")
            if task["status"]["state"] in {"completed", "failed", "canceled", "rejected"}:
                return err(request_id, -32002, "task not cancelable")
            task["status"] = {"state": "canceled"}
            return ok(request_id, public(task))
        return err(request_id, -32601, "method not found")

    @app.post("/slow-a2a")
    async def slow(request: Request):
        await asyncio.sleep(2)
        return {}

    return app
