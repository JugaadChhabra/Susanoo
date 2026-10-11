"""GitHub: a fine-grained token scoped to one repo. [PRD › Connectors and API budget › Demo connectors]

Reads pull requests and contents. Posting a review comment is optional and dry-run until the user switches
writes on. The token only ever goes into an Authorization header; it is never logged or returned.
"""

import logging
import re
from collections.abc import Mapping
from typing import Any

import httpx
from pydantic import SecretStr

from susanoo.connectors.base import (
    Connector,
    ConnectorAction,
    ConnectorField,
    ConnectorSpec,
    ConnectorTool,
    Credential,
    InvalidCredential,
    TestResult,
    ToolResult,
    dry_run_or,
)

logger = logging.getLogger("susanoo.connectors.github")

API = "https://api.github.com"
MAX_DIFF_CHARS = 200_000
MAX_FILE_CHARS = 100_000

_REPO = re.compile(r"^([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9._-]{1,100})$")
_REPO_URL = re.compile(r"^(?:https?://)?(?:www\.)?github\.com/", re.IGNORECASE)


def normalise_repo(value: str) -> str:
    """"acme/payments-api", "https://github.com/acme/payments-api(.git)" -> "acme/payments-api"."""
    repo = _REPO_URL.sub("", value.strip()).strip("/")
    repo = repo.removesuffix(".git")
    if not _REPO.match(repo) or repo.endswith("/.") or repo.endswith("/.."):
        raise InvalidCredential("Enter the repository as owner/name, for example acme/payments-api.")
    return repo


SPEC = ConnectorSpec(
    id="github",
    name="GitHub",
    summary="Lets your agent read pull requests and code in one repository.",
    fields=[
        ConnectorField(
            name="repo",
            label="Repository",
            help="The one repository your agent may read, as owner/name.",
            secret=False,
            placeholder="acme/payments-api",
        ),
        ConnectorField(
            name="token",
            label="Fine-grained personal access token",
            help="Starts with github_pat_. Stored encrypted and never shown again.",
            secret=True,
            placeholder="github_pat_…",
        ),
    ],
    scopes=[
        "Pull requests: Read-only",
        "Contents: Read-only",
        "Pull requests: Read and write (optional, for comments)",
    ],
    actions=[
        ConnectorAction(name="list_pull_requests", description="See the repository's pull requests", write=False),
        ConnectorAction(name="get_pull_request", description="Read a pull request and its changes", write=False),
        ConnectorAction(name="get_file", description="Read a file for context around a change", write=False),
        ConnectorAction(
            name="post_review_comment", description="Post the review as a comment on a pull request", write=True
        ),
    ],
)


class GitHubConnector(Connector):
    spec = SPEC

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport  # tests pass an httpx.MockTransport; None means the real network

    def validate(self, values: Mapping[str, SecretStr]) -> tuple[SecretStr, dict[str, str]]:
        token = values.get("token", SecretStr("")).get_secret_value().strip()
        repo = normalise_repo(values.get("repo", SecretStr("")).get_secret_value())
        if not token:
            raise InvalidCredential("Paste your GitHub token.")
        if re.match(r"^gh[pousr]_", token):
            raise InvalidCredential(
                "This looks like a classic token, which can see all your repositories. "
                "Please make a fine-grained token for just this one repository."
            )
        if not token.startswith("github_pat_") or any(c.isspace() for c in token):
            raise InvalidCredential(
                "That doesn't look like a fine-grained GitHub token. It should start with github_pat_."
            )
        return SecretStr(token), {"repo": repo}

    def _client(self, credential: Credential) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=API,
            transport=self._transport,
            timeout=15,
            headers={
                "Authorization": f"Bearer {credential.secret.get_secret_value()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "Susanoo",
            },
        )

    async def test_connection(self, credential: Credential) -> TestResult:
        repo = credential.settings["repo"]
        try:
            async with self._client(credential) as gh:
                found = await gh.get(f"/repos/{repo}")
                if found.status_code == 401:
                    return TestResult(
                        ok=False, message="GitHub didn't accept this token. It may be mistyped, expired or revoked."
                    )
                if found.status_code in (403, 404):
                    return TestResult(
                        ok=False,
                        message=f"This token can't see {repo}. Check the name, and that you picked this repository "
                        "under Repository access when you made the token.",
                    )
                found.raise_for_status()
                pulls = await gh.get(f"/repos/{repo}/pulls", params={"per_page": 1})
                if pulls.status_code in (403, 404):
                    return TestResult(
                        ok=False,
                        message=f"Connected to {repo}, but the token can't read pull requests. "
                        "Set Pull requests to Read-only on the token.",
                    )
                pulls.raise_for_status()
                contents = await gh.get(f"/repos/{repo}/contents/")
                if contents.status_code == 403:
                    return TestResult(
                        ok=False,
                        message=f"Connected to {repo}, but the token can't read its code. "
                        "Set Contents to Read-only on the token.",
                    )
                if contents.status_code != 404:  # 404 here means an empty repository, which is fine
                    contents.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.warning("GitHub answered %s while testing a connection", exc.response.status_code)
            return TestResult(ok=False, message="GitHub had a problem checking this token. Please try again.")
        except httpx.HTTPError as exc:
            logger.warning("Could not reach GitHub while testing a connection: %s", type(exc).__name__)
            return TestResult(ok=False, message="We couldn't reach GitHub. Please try again in a minute.")
        return TestResult(ok=True, message=f"Connected to {repo}, read access to pull requests and code confirmed.")

    def tools(self, credential: Credential) -> list[ConnectorTool]:
        repo = credential.settings["repo"]

        async def list_pull_requests(state: str = "open", limit: int = 10) -> ToolResult:
            if state not in ("open", "closed", "all"):
                return ToolResult(ok=False, message="state must be open, closed or all.")
            async with self._client(credential) as gh:
                r = await gh.get(f"/repos/{repo}/pulls", params={"state": state, "per_page": max(1, min(limit, 50))})
                if not r.is_success:
                    return _failed("list pull requests", r)
                prs = [
                    {"number": p["number"], "title": p["title"], "author": (p.get("user") or {}).get("login")}
                    for p in r.json()
                ]
            return ToolResult(ok=True, message=f"Found {len(prs)} pull requests in {repo}.", data=prs)

        async def get_pull_request(number: int) -> ToolResult:
            async with self._client(credential) as gh:
                meta = await gh.get(f"/repos/{repo}/pulls/{int(number)}")
                if not meta.is_success:
                    return _failed(f"read pull request #{number}", meta)
                diff = await gh.get(
                    f"/repos/{repo}/pulls/{int(number)}", headers={"Accept": "application/vnd.github.diff"}
                )
                if not diff.is_success:
                    return _failed(f"read the changes in pull request #{number}", diff)
            pr = meta.json()
            data = {
                "number": pr["number"],
                "title": pr["title"],
                "body": pr.get("body") or "",
                "head_sha": pr["head"]["sha"],
                "diff": diff.text[:MAX_DIFF_CHARS],
                "diff_truncated": len(diff.text) > MAX_DIFF_CHARS,
            }
            return ToolResult(ok=True, message=f"Read pull request #{number}: {pr['title']}", data=data)

        async def get_file(path: str, ref: str | None = None) -> ToolResult:
            if ".." in path.split("/"):
                return ToolResult(ok=False, message="The file path can't contain '..'.")
            params = {"ref": ref} if ref else None
            async with self._client(credential) as gh:
                r = await gh.get(
                    f"/repos/{repo}/contents/{path.lstrip('/')}",
                    params=params,
                    headers={"Accept": "application/vnd.github.raw+json"},
                )
                if not r.is_success:
                    return _failed(f"read {path}", r)
            return ToolResult(ok=True, message=f"Read {path}.", data=r.text[:MAX_FILE_CHARS])

        async def post_review_comment(number: int, body: str) -> ToolResult:
            async def post() -> ToolResult:
                async with self._client(credential) as gh:
                    r = await gh.post(
                        f"/repos/{repo}/pulls/{int(number)}/reviews", json={"body": body, "event": "COMMENT"}
                    )
                    if not r.is_success:
                        return _failed(f"comment on pull request #{number}", r)
                return ToolResult(ok=True, message=f"Posted a review comment on pull request #{number}.")

            return await dry_run_or(credential, f"posted a review comment on pull request #{number}", post)

        return [
            ConnectorTool(
                name="list_pull_requests",
                description=f"List pull requests in {repo}.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "state": {"type": "string", "enum": ["open", "closed", "all"]},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                    },
                },
                write=False,
                run=list_pull_requests,
            ),
            ConnectorTool(
                name="get_pull_request",
                description=f"Read a pull request in {repo}: title, description and unified diff.",
                input_schema={
                    "type": "object",
                    "properties": {"number": {"type": "integer"}},
                    "required": ["number"],
                },
                write=False,
                run=get_pull_request,
            ),
            ConnectorTool(
                name="get_file",
                description=f"Read one file from {repo}, optionally at a commit or branch.",
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "ref": {"type": "string"}},
                    "required": ["path"],
                },
                write=False,
                run=get_file,
            ),
            ConnectorTool(
                name="post_review_comment",
                description=f"Post a review comment on a pull request in {repo}. Dry-run unless writes are on.",
                input_schema={
                    "type": "object",
                    "properties": {"number": {"type": "integer"}, "body": {"type": "string"}},
                    "required": ["number", "body"],
                },
                write=True,
                run=post_review_comment,
            ),
        ]

    def env(self, credential: Credential) -> dict[str, str]:
        return {
            "GITHUB_TOKEN": credential.secret.get_secret_value(),
            "GITHUB_REPO": credential.settings["repo"],
            "GITHUB_WRITES_ENABLED": "true" if credential.writes_enabled else "false",
        }


def _failed(action: str, response: httpx.Response) -> ToolResult:
    logger.warning("GitHub answered %s when trying to %s", response.status_code, action)
    reason = {401: "the token was rejected", 403: "the token isn't allowed to", 404: "it wasn't found"}.get(
        response.status_code, f"GitHub answered {response.status_code}"
    )
    return ToolResult(ok=False, message=f"Couldn't {action}: {reason}.", data=_safe_json(response))


def _safe_json(response: httpx.Response) -> Any:
    try:
        body = response.json()
    except ValueError:
        return None
    return {"message": body.get("message")} if isinstance(body, dict) else None
