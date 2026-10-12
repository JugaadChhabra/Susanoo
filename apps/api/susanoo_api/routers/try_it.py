"""POST /runs/{id}/try: review a pasted diff with the run's own agent. [PRD › Non-technical users] (#33)

The API forwards the diff to the run's public preview over HTTPS (`<preview_url>/review`, the generated app's own
review route in templates/app-template/backend/routes/review.py), so it never joins the sandbox network. The
generated app pays for the review from its own capped key, so this costs no run quota. A preview that is asleep,
slow or failing is a 503 with a plain-English message; the preview's own error text is never echoed (it could hold
anything), and the diff is never logged.

Muaaz owns agent.py; this is a separate platform router so his file stays untouched.
"""

import logging

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ValidationError

from susanoo.schemas import Finding
from susanoo_api.deps import CurrentUserDep
from susanoo_api.models import TryResult
from susanoo_api.routers.runs import RunRow, RunStoreDep, owned_run

router = APIRouter(prefix="/runs", tags=["try"])

logger = logging.getLogger("susanoo_api.try_it")

MAX_DIFF_CHARS = 20_000  # the agent page's own cap (apps/web/lib/agent.ts)
PREVIEW_TIMEOUT_S = 60.0

# Tests swap this for an httpx.MockTransport; None means the real network.
_transport: httpx.AsyncBaseTransport | None = None

NOT_FINISHED = "Your agent isn't finished yet. You can try it here once the run is done."
NO_PREVIEW = "Your agent has no live preview right now, so it can't review changes from this page."
ASLEEP = (
    "Your agent's preview isn't answering, so it couldn't review this change. "
    "It may be asleep: previews go to sleep a while after the run finishes."
)
TOO_SLOW = "Your agent took too long to answer (over a minute). Please try again with a smaller change."
FAILED = "Your agent couldn't review this change right now. Please try again in a minute."


class TryIn(BaseModel):
    """What the agent page sends: `{"kind": "diff", "diff": ...}` (or a PR, which isn't supported yet)."""

    kind: str
    diff: str | None = None
    pr: str | None = None


def _diff_from(body: TryIn) -> str:
    if body.kind != "diff":
        raise HTTPException(422, "Trying a pull request isn't switched on yet. Paste the change as a diff instead.")
    diff = (body.diff or "").strip()
    if not diff:
        raise HTTPException(422, "Paste a code change (a git diff) first, then ask for a review.")
    if len(diff) > MAX_DIFF_CHARS:
        raise HTTPException(422, f"That change is too long. Keep it under {MAX_DIFF_CHARS:,} characters.")
    return diff


def _review_url(run: RunRow) -> str:
    """The preview's review route. Only an https preview URL is ever called (the worker only records those)."""
    if not run.preview_url:
        raise HTTPException(503, NO_PREVIEW if run.status == "done" else NOT_FINISHED)
    url = httpx.URL(run.preview_url)
    if url.scheme != "https" or not url.host:
        logger.warning("Run %s has a preview URL that isn't https; not calling it", run.id)
        raise HTTPException(503, NO_PREVIEW)
    return str(url.copy_with(path="/review", query=None, fragment=None))


async def _review(run_id: str, url: str, diff: str) -> list[Finding]:
    try:
        async with httpx.AsyncClient(transport=_transport, timeout=PREVIEW_TIMEOUT_S) as client:
            response = await client.post(url, json={"diff": diff})
    except httpx.TimeoutException:
        logger.warning("Run %s: the preview timed out reviewing a diff", run_id)
        raise HTTPException(503, TOO_SLOW) from None
    except httpx.HTTPError as exc:
        logger.warning("Run %s: could not reach the preview (%s)", run_id, type(exc).__name__)
        raise HTTPException(503, ASLEEP) from None
    if response.status_code != 200:
        logger.warning("Run %s: the preview answered %s to a review", run_id, response.status_code)
        raise HTTPException(503, FAILED)
    try:
        data = response.json()
        return [Finding.model_validate(item) for item in data["findings"]]
    except (ValueError, KeyError, TypeError, ValidationError) as exc:  # ValueError covers bad JSON
        logger.warning("Run %s: the preview's review wasn't readable (%s)", run_id, type(exc).__name__)
        raise HTTPException(503, FAILED) from None


def summarize(findings: list[Finding]) -> str:
    """One plain-English line above the findings."""
    total = len(findings)
    if total == 0:
        return "No problems found in this change."
    found = f"Found {total} possible {'problem' if total == 1 else 'problems'}"
    blocking = sum(1 for f in findings if f.severity == "block")
    if blocking == 0:
        return f"{found}."
    if blocking == total:
        return f"{found}, and {'it' if total == 1 else 'all of them'} should block the change."
    return f"{found}, and {blocking} of them should block the change."


@router.post("/{run_id}/try", response_model=TryResult)
async def try_agent(run_id: str, body: TryIn, user: CurrentUserDep, store: RunStoreDep) -> TryResult:
    run = await owned_run(store, user.id, run_id)
    diff = _diff_from(body)
    findings = await _review(run.id, _review_url(run), diff)
    return TryResult(summary=summarize(findings), findings=findings)
