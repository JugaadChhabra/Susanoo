"""GET /internal/preview-allowed: Caddy's on-demand TLS ask for run-<id> previews. [PRD › Deployment]

Caddy calls http://api:8000/internal/preview-allowed?domain=<host> before getting a certificate for a host.
200 only for run-<run_id>.<PREVIEW_DOMAIN> whose run exists and has a preview URL recorded; 404 for everything
else, so nobody can make Caddy request certificates for random names.

No user auth: this is for Caddy on the compose network only. Requests through the public api domain arrive via
Caddy's reverse_proxy, which always sets X-Forwarded-For, so any forwarded request is refused with 404.
"""

import logging
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


async def has_preview(run_id: str) -> bool:
    """True when the run exists and has a preview URL recorded. Tests replace this."""
    async with session() as conn:
        cur = await conn.execute(
            "select 1 from public.runs where id = %s and preview_url is not null",
            (run_id,),
        )
        return await cur.fetchone() is not None


def _run_id_for(host: str) -> str | None:
    """The canonical run id in run-<id>.<PREVIEW_DOMAIN>, or None for any other host."""
    match = _RUN_HOST.match(host.strip().lower())
    if not match or match.group(2) != preview_domain():
        return None
    try:
        run_id = str(UUID(match.group(1)))
    except ValueError:
        return None
    return run_id if run_id == match.group(1) else None  # only the hyphenated form the container is named with


@router.get("/preview-allowed")
async def preview_allowed(request: Request, domain: str = "") -> dict[str, bool]:
    if any(name in request.headers for name in _FORWARDED):
        raise HTTPException(404, "Not found.")
    run_id = _run_id_for(domain)
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
