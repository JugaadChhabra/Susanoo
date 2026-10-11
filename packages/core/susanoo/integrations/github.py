"""GitHub for the Builder: create the product repo in the org and push the first commit.
[PRD › Tech stack] (GitHub), [PRD › Repository structure › Generated product repo].

Environment:
  GITHUB_ORG          the org that owns generated repos (named in PROMPTBOOK_MUAAZ.md 2.4)
  GITHUB_ORG_TOKEN    token with repo-create scope in that org; `GITHUB_TOKEN` is accepted as a fallback.
                      The PRD only says "GitHub token with repo-create scope stored as a secret", so the
                      name is our choice. Note `GITHUB_TOKEN` inside GitHub Actions is a different token.

The token is a credential [CLAUDE.md §5]. It is never logged, never put in an error message, never written
to a file and never written into `.git/config`. For `git push` it travels only in the child process's
environment (GIT_CONFIG_* variables), not in argv, so it is not in `ps` output either.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

GITHUB_API = "https://api.github.com"
ORG_ENV = "GITHUB_ORG"
TOKEN_ENVS = ("GITHUB_ORG_TOKEN", "GITHUB_TOKEN")
API_VERSION = "2022-11-28"
_MAX_SLUG = 40


class GitHubError(Exception):
    """A GitHub call failed. The message is plain English and never contains the token."""


class RepoAlreadyExists(GitHubError):
    """The org already has a repo with that name."""


@dataclass(frozen=True)
class RepoInfo:
    full_name: str  # "org/name"
    clone_url: str  # https://github.com/org/name.git (no credentials in it)
    html_url: str


def github_config_from_env() -> tuple[str, str] | None:
    """(org, token) from the environment, or None when either is missing (local-only run)."""
    org = os.environ.get(ORG_ENV, "").strip()
    token = next((v.strip() for name in TOKEN_ENVS if (v := os.environ.get(name, "")).strip()), "")
    return (org, token) if org and token else None


def slugify(text: str) -> str:
    """Lowercase letters, digits and single hyphens only; never empty."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:_MAX_SLUG].strip("-")
    return slug or "app"


def repo_name(product_name: str, run_id: str) -> str:
    """Slugified product name plus a short run-id suffix, e.g. "python-code-review-3fa9c1"."""
    suffix = re.sub(r"[^a-z0-9]", "", run_id.lower())[:6] or "run"
    return f"{slugify(product_name)}-{suffix}"


class GitHubClient:
    """Thin async client over the GitHub REST API. Inject `http` (httpx.MockTransport) in tests."""

    def __init__(self, token: str, *, base_url: str = GITHUB_API, http: httpx.AsyncClient | None = None) -> None:
        if not token:
            raise ValueError("A GitHub token is required")
        self._token = token
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(base_url=base_url, timeout=30.0)

    def __repr__(self) -> str:  # never show the token
        return "GitHubClient(token=***)"

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def create_repo(self, org: str, name: str, *, private: bool = True, description: str = "") -> RepoInfo:
        try:
            resp = await self._http.post(
                f"/orgs/{org}/repos",
                json={"name": name, "private": private, "description": description[:350], "auto_init": False},
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": API_VERSION,
                },
            )
        except httpx.HTTPError as exc:
            raise GitHubError(f"Could not reach GitHub to create the repo ({type(exc).__name__}).") from None
        if resp.status_code == 201:
            data = resp.json()
            return RepoInfo(full_name=data["full_name"], clone_url=data["clone_url"], html_url=data["html_url"])
        raise _error_for(resp, org, name)

    async def create_and_push(self, org: str, name: str, repo_path: str | Path, *, description: str = "") -> RepoInfo:
        info = await self.create_repo(org, name, description=description)
        await push_repo(repo_path, info.clone_url, self._token)
        return info


def _error_for(resp: httpx.Response, org: str, name: str) -> GitHubError:
    status = resp.status_code
    try:
        detail = str(resp.json().get("message", ""))
    except ValueError:
        detail = ""
    if status == 422 and ("already exists" in detail.lower() or "name already" in resp.text.lower()):
        return RepoAlreadyExists(
            f"A repo named {org}/{name} already exists. Pick another product name or delete the old repo."
        )
    if status == 401:
        return GitHubError("GitHub rejected the token (401). Check that the token is valid and has not expired.")
    if status == 403:
        return GitHubError(
            "GitHub refused to create the repo (403). The token needs permission to create repos in the org."
        )
    if status == 404:
        return GitHubError(f"GitHub could not find the organisation {org!r}, or the token cannot see it (404).")
    if status == 422:
        return GitHubError(f"GitHub did not accept the repo name {name!r} (422): {detail or 'validation failed'}.")
    return GitHubError(f"GitHub returned an unexpected error ({status}) while creating the repo.")


async def push_repo(repo_path: str | Path, remote_url: str, token: str, *, branch: str = "main") -> None:
    """Push HEAD of the local repo to `remote_url` as `branch`, authenticating with `token`.

    The token goes only into the git child process's environment as an HTTP header. Nothing is written to
    `.git/config` (the stored `origin` URL has no credentials), and git's own errors are scrubbed before they
    are raised.
    """
    repo = Path(repo_path)
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
    }
    secrets = (token, basic)
    # `origin` is stored with the clean URL so later steps can find the remote without any credential.
    await _git(repo, ["remote", "remove", "origin"], env, secrets, check=False)
    await _git(repo, ["remote", "add", "origin", remote_url], env, secrets)
    await _git(repo, ["push", "origin", f"HEAD:refs/heads/{branch}"], env, secrets)


async def _git(
    repo: Path, args: list[str], env: dict[str, str], secrets: tuple[str, ...], *, check: bool = True
) -> None:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=repo, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, err = await proc.communicate()
    if proc.returncode != 0 and check:
        text = err.decode(errors="replace").strip()
        for secret in secrets:
            text = text.replace(secret, "***")
        raise GitHubError(f"git {args[0]} failed while publishing the repo: {text}")
