"""POST /runs/{id}/try: review a pasted diff, or a PR from the connected repo, with the run's own agent.
[PRD › Non-technical users] (#33), [PRD › Connectors and API budget › Credential handling] (#34)

The API forwards the diff to the run's public preview over HTTPS (`<preview_url>/review`, the generated app's own
review route in templates/app-template/backend/routes/review.py), so it never joins the sandbox network. The
generated app pays for the review from its own capped key, so this costs no run quota. A preview that is asleep,
slow or failing is a 503 with a plain-English message; the preview's own error text is never echoed (it could hold
anything), and the diff is never logged.

A PR (`owner/repo#123` or its github.com link) is fetched as a diff with the token of the run's GitHub connection,
read from Vault by `run_connection_env()` (#30, which also registers it for redaction), then takes the pasted-diff
path. Only the connection's own repository can be tried. The token only goes into the Authorization header to
api.github.com; GitHub's own error text is never echoed.

Muaaz owns agent.py; this is a separate platform router so his file stays untouched.
"""

import logging
import re
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ValidationError

from susanoo.connectors.runtime_env import ConnectionNotYours, run_connection_env
from susanoo.sandbox.docker import preview_domain
from susanoo.schemas import Finding
from susanoo_api.deps import CurrentUserDep
from susanoo_api.models import TryResult
from susanoo_api.routers.runs import RunRow, RunStoreDep, owned_run

router = APIRouter(prefix="/runs", tags=["try"])

logger = logging.getLogger("susanoo_api.try_it")

MAX_DIFF_CHARS = 20_000  # the agent page's own cap (apps/web/lib/agent.ts)
PREVIEW_TIMEOUT_S = 60.0

GITHUB_API = "https://api.github.com"
GITHUB_TIMEOUT_S = 15.0

# Tests swap these for an httpx.MockTransport; None means the real network.
_transport: httpx.AsyncBaseTransport | None = None
_github_transport: httpx.AsyncBaseTransport | None = None

# owner/repo#123 or https://github.com/owner/repo/pull/123 (the agent page's parsePrRef accepts the same two)
_PR_REF = re.compile(
    r"^(?:https://github\.com/)?([A-Za-z0-9-]{1,39})/([A-Za-z0-9._-]{1,100})(?:#|/pull/)([1-9][0-9]{0,9})/?$"
)

NOT_FINISHED = "Your agent isn't finished yet. You can try it here once the run is done."
NO_PREVIEW = (
    "Your agent has no live preview right now, so it can't review changes from this page. "
    "If the run only just finished, its preview may still be starting: try again in a minute."
)
ASLEEP = (
    "Your agent's preview isn't answering, so it couldn't review this change. "
    "It may be asleep: previews go to sleep a while after the run finishes."
)
TOO_SLOW = "Your agent took too long to answer (over a minute). Please try again with a smaller change."
FAILED = "Your agent couldn't review this change right now. Please try again in a minute."
NOT_A_PR = "Use a GitHub pull request link, or owner/repo#number."
CONNECT_GITHUB = "Connect GitHub to try a PR."
GITHUB_DOWN = "We couldn't reach GitHub to read that pull request. Please try again in a minute."


class TryIn(BaseModel):
    """What the agent page sends: `{"kind": "diff", "diff": ...}` or `{"kind": "pr", "pr": "owner/repo#123"}`."""

    kind: Literal["diff", "pr"]
    diff: str | None = None
    pr: str | None = None


async def _diff_from(run: RunRow, body: TryIn) -> str:
    if body.kind == "pr":
        diff = (await _pr_diff(run.id, body.pr or "")).strip()
        if not diff:
            raise HTTPException(422, "That pull request has no code changes to review.")
        if len(diff) > MAX_DIFF_CHARS:
            raise HTTPException(
                422,
                f"That pull request's changes are too long to try here (over {MAX_DIFF_CHARS:,} characters). "
                "Paste part of it as a diff instead.",
            )
        return diff
    diff = (body.diff or "").strip()
    if not diff:
        raise HTTPException(422, "Paste a code change (a git diff) first, then ask for a review.")
    if len(diff) > MAX_DIFF_CHARS:
        raise HTTPException(422, f"That change is too long. Keep it under {MAX_DIFF_CHARS:,} characters.")
    return diff


async def _pr_diff(run_id: str, ref: str) -> str:
    """The PR's unified diff, read with the run's GitHub connection. Every failure is a plain-English 422/503."""
    match = _PR_REF.match(ref.strip())
    if not match:
        raise HTTPException(422, NOT_A_PR)
    owner, name, number = match.groups()
    try:
        env = await run_connection_env(run_id)
    except ConnectionNotYours:
        logger.warning("Run %s: a picked connection isn't its owner's, so no PR was read", run_id)
        raise HTTPException(422, CONNECT_GITHUB) from None
    token, repo = env.get("GITHUB_TOKEN"), env.get("GITHUB_REPO")
    if not token or not repo:
        raise HTTPException(422, CONNECT_GITHUB)
    if f"{owner}/{name}".lower() != repo.lower():
        raise HTTPException(422, f"Your GitHub connection is for {repo}, so it can only try pull requests from there.")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.diff",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Susanoo",
    }
    try:
        async with httpx.AsyncClient(
            base_url=GITHUB_API, transport=_github_transport, timeout=GITHUB_TIMEOUT_S, headers=headers
        ) as gh:
            response = await gh.get(f"/repos/{repo}/pulls/{number}")
    except httpx.HTTPError as exc:  # its text could hold the request; only the type is logged
        logger.warning("Run %s: could not reach GitHub for a PR (%s)", run_id, type(exc).__name__)
        raise HTTPException(503, GITHUB_DOWN) from None
    if response.status_code == 404:
        raise HTTPException(422, f"We couldn't find pull request #{number} in {repo}.")
    if response.status_code in (401, 403):
        logger.warning("Run %s: GitHub answered %s reading a PR", run_id, response.status_code)
        raise HTTPException(
            422,
            f"GitHub didn't let your connection read pull request #{number} in {repo}. The token may have expired "
            "or lack Pull requests: Read-only. Replace it on the Connections page.",
        )
    if response.status_code != 200:
        logger.warning("Run %s: GitHub answered %s reading a PR", run_id, response.status_code)
        raise HTTPException(503, GITHUB_DOWN)
    return response.text


def _review_url(run: RunRow) -> str:
    """The preview's review route, built from the run id (never from a stored host, so it can't point elsewhere).

    The worker records preview_url once the app answers, always as https://run-<id>.<PREVIEW_DOMAIN>; a stored URL
    that says anything else is refused rather than called.
    """
    if not run.preview_url:
        raise HTTPException(503, NO_PREVIEW if run.status == "done" else NOT_FINISHED)
    try:
        expected = f"https://run-{run.id}.{preview_domain()}"
    except ValueError:
        logger.error("PREVIEW_DOMAIN is not a valid domain name; Try it can't reach any preview")
        raise HTTPException(503, NO_PREVIEW) from None
    if run.preview_url.rstrip("/").lower() != expected:
        logger.warning("Run %s has a preview URL that isn't its own preview address; not calling it", run.id)
        raise HTTPException(503, NO_PREVIEW)
    return f"{expected}/review"


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
        # The app's own errors are JSON; Caddy answers a stopped (asleep) preview's container with a bare 502.
        raise HTTPException(503, FAILED if _is_json(response) else ASLEEP)
    try:
        data = response.json()
        return [Finding.model_validate(item) for item in data["findings"]]
    except (ValueError, KeyError, TypeError, ValidationError) as exc:  # ValueError covers bad JSON
        logger.warning("Run %s: the preview's review wasn't readable (%s)", run_id, type(exc).__name__)
        raise HTTPException(503, FAILED) from None


def _is_json(response: httpx.Response) -> bool:
    return response.headers.get("content-type", "").startswith("application/json")


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
    url = _review_url(run)  # before GitHub is asked: a run with no live preview has nothing to review with
    diff = await _diff_from(run, body)
    findings = await _review(run.id, url, diff)
    return TryResult(summary=summarize(findings), findings=findings)
