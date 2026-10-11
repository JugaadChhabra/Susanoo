"""API app shell: CORS, one error shape, /health, router mounting."""

import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.responses import Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from susanoo.connectors import install_log_redaction
from susanoo_api.routers import mount_routers

logger = logging.getLogger("susanoo_api")

LOCAL_ORIGIN = "http://localhost:3000"

# Tests swap this for an httpx.MockTransport; None means the real network.
_transport: httpx.AsyncBaseTransport | None = None


def error_response(status: int, code: str, message: str) -> JSONResponse:
    """The one error shape every API error uses: {"error": {"code", "message"}}."""
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


def _allowed_origins() -> list[str]:
    domain = os.environ.get("APP_DOMAIN", "").strip().rstrip("/")
    if not domain:
        return [LOCAL_ORIGIN]
    return [domain if "://" in domain else f"https://{domain}"]


async def _check_supabase() -> dict[str, str]:
    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    if not url:
        return {"status": "unconfigured", "message": "SUPABASE_URL is not set."}
    key = os.environ.get("SUPABASE_ANON_KEY", "")
    try:
        async with httpx.AsyncClient(transport=_transport, timeout=5) as client:
            response = await client.get(f"{url}/auth/v1/health", headers={"apikey": key})
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return {
            "status": "unreachable",
            "message": f"Supabase answered with status {exc.response.status_code}.",
        }
    except httpx.HTTPError as exc:
        logger.warning("Supabase health check failed: %s", type(exc).__name__)
        return {"status": "unreachable", "message": "Could not reach Supabase."}
    return {"status": "ok", "message": "Supabase is reachable."}


def create_app() -> FastAPI:
    install_log_redaction()  # tokens never reach a log line, whoever logs them [PRD › Credential handling]
    app = FastAPI(title="Susanoo API")

    # Added before CORS so it sits inside it: 500 responses still get CORS headers.
    @app.middleware("http")
    async def catch_unhandled(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        try:
            return await call_next(request)
        except Exception:
            logger.exception("Unhandled error on %s %s", request.method, request.url.path)
            return error_response(500, "internal_error", "Something went wrong on our side.")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_allowed_origins(),
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 401: "unauthorized", 403: "forbidden", 429: "rate_limited"}.get(
            exc.status_code, "http_error"
        )
        return error_response(exc.status_code, code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(part) for part in first.get("loc", ()))
        message = f"{where}: {first.get('msg', 'Invalid request.')}" if where else "Invalid request."
        return error_response(422, "validation_error", message)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        supabase = await _check_supabase()
        return {"status": "ok" if supabase["status"] == "ok" else "degraded", "supabase": supabase}

    mount_routers(app)
    return app


app = create_app()
