"""FastAPI app for the code review product. Layer: code."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse

from backend.routes.review import ReviewStore, router as review_router

WEB_INDEX = Path(__file__).resolve().parent.parent / "web" / "index.html"

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))


def create_app(client: Any | None = None) -> FastAPI:
    """Build the app. Pass a client (a fake in tests), else one is built from ANTHROPIC_APP_API_KEY on first use."""
    app = FastAPI(title="Code review", docs_url=None, redoc_url=None)
    app.state.client = client
    app.state.store = ReviewStore()

    @app.exception_handler(RequestValidationError)
    async def plain_validation_error(_: Request, __: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"detail": "That request was not understood. Send JSON like {\"diff\": \"...\"}."},
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(WEB_INDEX, media_type="text/html")

    app.include_router(review_router)
    return app


app = create_app()
