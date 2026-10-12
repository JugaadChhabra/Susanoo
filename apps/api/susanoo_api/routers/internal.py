"""GET /internal/preview-allowed: Caddy's on-demand TLS ask for run-<id> previews. [PRD › Deployment]

Caddy calls http://api:8000/internal/preview-allowed?domain=<host> before getting a certificate for a host.
200 for run-<run_id>.<PREVIEW_DOMAIN> whose run exists and has a preview URL recorded, and for API_DOMAIN itself
(Caddy prefers the preview wildcard's on-demand policy for any name it covers, so without this the api site gets
no certificate); 404 for everything else, so nobody can make Caddy request certificates for random names.

No user auth: this is for Caddy on the compose network only. Requests through the public api domain arrive via
Caddy's reverse_proxy, which always sets X-Forwarded-For and keeps Host as the api domain, so a forwarded
request or one addressed to API_DOMAIN is refused with 404.
"""

import asyncio
import logging
import os
import re
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from susanoo.db import session
from susanoo.sandbox.docker import preview_domain

router = APIRouter(prefix="/internal", tags=["internal"], include_in_schema=False)

logger = logging.getLogger("susanoo_api.internal")

_NOT_ALLOWED = "No preview here."
_FORWARDED = ("x-forwarded-for", "x-forwarded-host", "forwarded")
_RUN_HOST = re.compile(r"^run-([0-9a-f-]{36})\.(.+)$")
# Any TLS handshake for a new well-formed name triggers an ask; this caps the database connections they open.
_DB_CHECKS = asyncio.Semaphore(4)


async def has_preview(run_id: str) -> bool:
    """True when the run exists and has a preview URL recorded. Tests replace this."""
    async with _DB_CHECKS, session() as conn:
        cur = await conn.execute(
            "select 1 from public.runs where id = %s and preview_url is not null",
            (run_id,),
        )
        return await cur.fetchone() is not None


def _public(request: Request) -> bool:
    """True when the request came through Caddy's public api site rather than straight over the compose network."""
    if any(name in request.headers for name in _FORWARDED):
        return True
    api_domain = os.environ.get("API_DOMAIN", "").strip().lower()
    host = request.headers.get("host", "").lower().rsplit(":", 1)[0]
    return bool(api_domain) and host == api_domain


def _run_id_for(host: str, domain: str) -> str | None:
    """The canonical run id in run-<id>.<domain>, or None for any other host."""
    match = _RUN_HOST.match(host.strip().lower())
    if not match or match.group(2) != domain:
        return None
    try:
        run_id = str(UUID(match.group(1)))
    except ValueError:
        return None
    return run_id if run_id == match.group(1) else None  # only the hyphenated form the container is named with


@router.get("/preview-allowed")
async def preview_allowed(request: Request, domain: str = "") -> dict[str, bool]:
    if _public(request):
        raise HTTPException(404, "Not found.")
    try:
        ours = preview_domain()
    except ValueError:
        logger.error("PREVIEW_DOMAIN is not a valid domain name; refusing every preview certificate")
        raise HTTPException(503, "Previews are not set up correctly on the server.") from None
    api_domain = os.environ.get("API_DOMAIN", "").strip().lower()
    if api_domain and domain.strip().lower() == api_domain:
        return {"allowed": True}
    run_id = _run_id_for(domain, ours)
    if run_id is None:
        raise HTTPException(404, _NOT_ALLOWED)
    try:
        allowed = await has_preview(run_id)
    except Exception:
        logger.exception("Could not check the preview for run %s", run_id)
        raise HTTPException(503, "Could not check this preview right now.") from None
    if not allowed:
        raise HTTPException(404, _NOT_ALLOWED)
    return {"allowed": True}
