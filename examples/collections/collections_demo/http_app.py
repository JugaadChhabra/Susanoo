"""The demo agent behind HTTP: sync JSON (I03), async submit/poll (I04), SSE and WebSocket (I05).

Uses the wrapper pattern: the runner passes the agent its own invocation context (with the scoped
tool-route token) in the request body, as a customer wrapper would for an agent with configurable
tool endpoints.
"""

from __future__ import annotations

import asyncio
import json
import uuid

from fastapi import FastAPI, WebSocket
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from susanoo_sdk import InvocationContext, Message

from .agent import CollectionsAgent, Mode


class Turn(BaseModel):
    session_id: str
    message: str
    context: dict


def create_app(mode: Mode = "correct") -> FastAPI:
    app = FastAPI(title="Synthetic collections agent")
    agent = CollectionsAgent(mode)
    tasks: dict[str, dict] = {}
    submissions: list[str] = []
    app.state.submissions = submissions

    async def answer(turn: Turn) -> str:
        context = InvocationContext.model_validate(turn.context)
        reply = await agent.respond(turn.session_id, Message(text=turn.message), context)
        return reply.text or ""

    @app.post("/chat")
    async def chat(turn: Turn):
        return {"reply": {"text": await answer(turn)}, "session": {"id": turn.session_id}}

    @app.post("/tasks", status_code=202)
    async def submit(turn: Turn):
        task_id = uuid.uuid4().hex
        submissions.append(task_id)
        tasks[task_id] = {"status": "running"}

        async def work():
            try:
                tasks[task_id] = {"status": "succeeded", "result": {"text": await answer(turn)}}
            except Exception as error:  # surfaced as a terminal failure state
                tasks[task_id] = {"status": "failed", "error": type(error).__name__}

        asyncio.get_running_loop().create_task(work())
        return {"task": {"id": task_id}}

    @app.get("/tasks/{task_id}")
    async def status(task_id: str):
        return {"id": task_id, **tasks.get(task_id, {"status": "unknown"})}

    def events(text: str):
        words = text.split(" ")
        for index, word in enumerate(words):
            chunk = word if index == len(words) - 1 else word + " "
            yield {"id": f"e{index}", "type": "delta", "text": chunk}
        yield {"id": "done", "type": "done"}

    @app.post("/stream")
    async def stream(turn: Turn):
        text = await answer(turn)

        async def body():
            for event in events(text):
                yield f"id: {event['id']}\ndata: {json.dumps(event)}\n\n"

        return StreamingResponse(body(), media_type="text/event-stream")

    @app.websocket("/ws")
    async def socket(ws: WebSocket):
        await ws.accept()
        turn = Turn.model_validate_json(await ws.receive_text())
        for event in events(await answer(turn)):
            await ws.send_text(json.dumps(event))
        await ws.close()

    return app


REQUEST = {"session_id": "{{session_id}}", "message": "{{message.text}}", "context": "{{context}}"}
