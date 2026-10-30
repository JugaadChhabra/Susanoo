"""Least-privilege GitHub App client and deduplicated draft-PR publisher."""

import base64
import difflib
import os
import re
import time
from pathlib import PurePosixPath

import httpx
import jwt
from susanoo_contracts import PatchBundle

from .database import PlatformError


def repository(value):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value or "") or any(
        part in {".", ".."} for part in (value or "").split("/")
    ):
        raise PlatformError("INVALID_INPUT", "GitHub repositories must use owner/name.", 422)
    return value


class GitHubApp:
    def __init__(self, transport=None):
        self.client = httpx.Client(
            base_url="https://api.github.com",
            timeout=30,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "Susanoo",
            },
        )

    def request(self, method, path, token, **kwargs):
        response = self.client.request(method, path, headers={"Authorization": f"Bearer {token}"}, **kwargs)
        if not response.is_success:
            raise PlatformError(
                "PROVIDER_ERROR",
                f"GitHub returned status {response.status_code}; reconcile before retrying publication.",
                502,
            )
        return response.json() if response.content else None

    def installation_token(self, installation_id, repo, write=False):
        return self.installation_token_info(installation_id, repo, write)["token"]

    def installation_token_info(self, installation_id, repo, write=False):
        repo = repository(repo)
        app_id = os.environ.get("PLATFORM_GITHUB_APP_ID", "")
        private_key = os.environ.get("PLATFORM_GITHUB_APP_PRIVATE_KEY", "")
        if not app_id or not private_key:
            raise PlatformError("UNAVAILABLE", "GitHub App service credentials are not configured.", 503)
        app_token = jwt.encode(
            {"iat": int(time.time()) - 30, "exp": int(time.time()) + 540, "iss": app_id}, private_key, algorithm="RS256"
        )
        actual = self.request("GET", f"/repos/{repo}/installation", app_token)
        if actual["id"] != installation_id:
            raise PlatformError("FORBIDDEN", "The selected repository belongs to another installation.", 403)
        permissions = {"contents": "write" if write else "read"}
        if write:
            permissions["pull_requests"] = "write"
        issued = self.request(
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            app_token,
            json={"repositories": [repo.split("/")[1]], "permissions": permissions},
        )
        return issued

    def revoke_token(self, token):
        response = self.client.delete("/installation/token", headers={"Authorization": f"Bearer {token}"})
        if response.status_code not in {204, 401}:
            raise PlatformError("PROVIDER_ERROR", "Source credential revocation needs reconciliation.", 502)

    def draft_pr(self, repo, token, proposal, bundle: PatchBundle, allowed_paths):
        repo = repository(repo)
        marker = f"<!-- susanoo-publication:{proposal['publication_key']} -->"
        meta = self.request("GET", f"/repos/{repo}", token)
        default_branch = meta["default_branch"]
        branch = "susanoo/repair-" + str(proposal["id"])
        prs = self.request(
            "GET",
            f"/repos/{repo}/pulls",
            token,
            params={"state": "all", "head": repo.split("/")[0] + ":" + branch, "per_page": 100},
        )
        for pr in prs:
            if marker in (pr.get("body") or ""):
                return pr["html_url"]
        head = self.request("GET", f"/repos/{repo}/git/ref/heads/{default_branch}", token)
        if head["object"]["sha"] != proposal["base_commit"]:
            raise PlatformError(
                "STALE_BASE", "The repository base changed; regenerate and revalidate the proposal.", 409
            )
        patch_policy(bundle, allowed_paths)
        commit = self.request("GET", f"/repos/{repo}/git/commits/{proposal['base_commit']}", token)
        tree = []
        for file in bundle.files:
            path = file.path
            response = self.client.get(
                f"/repos/{repo}/contents/{path}",
                params={"ref": bundle.base_commit},
                headers={"Authorization": f"Bearer {token}"},
            )
            if response.status_code == 404:
                before = None
            elif response.is_success:
                data = response.json()
                if data.get("type") != "file":
                    raise PlatformError("INVALID_INPUT", "Patches may modify regular text files only.", 422)
                before = base64.b64decode(data["content"]).decode("utf-8")
            else:
                raise PlatformError("PROVIDER_ERROR", "The pinned file could not be read.", 502)
            if before != file.before:
                raise PlatformError("STALE_BASE", "Patch original bytes differ from the validated base.", 409)
            blob = (
                None
                if file.after is None
                else self.request(
                    "POST", f"/repos/{repo}/git/blobs", token, json={"content": file.after, "encoding": "utf-8"}
                )["sha"]
            )
            tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob})
        tree_sha = self.request(
            "POST", f"/repos/{repo}/git/trees", token, json={"base_tree": commit["tree"]["sha"], "tree": tree}
        )["sha"]
        new_commit = self.request(
            "POST",
            f"/repos/{repo}/git/commits",
            token,
            json={
                "message": f"fix(agent): proposed Susanoo repair {proposal['id']}",
                "tree": tree_sha,
                "parents": [proposal["base_commit"]],
            },
        )["sha"]
        existing = self.client.get(
            f"/repos/{repo}/git/ref/heads/{branch}", headers={"Authorization": f"Bearer {token}"}
        )
        if existing.status_code == 404:
            self.request(
                "POST", f"/repos/{repo}/git/refs", token, json={"ref": "refs/heads/" + branch, "sha": new_commit}
            )
        elif not existing.is_success:
            raise PlatformError("PROVIDER_ERROR", "Could not reconcile the proposal branch.", 502)
        else:
            existing_commit = self.request(
                "GET", f"/repos/{repo}/git/commits/{existing.json()['object']['sha']}", token
            )
            if (
                existing_commit["tree"]["sha"] != tree_sha
                or existing_commit["parents"][0]["sha"] != proposal["base_commit"]
            ):
                raise PlatformError("PUBLICATION_CONFLICT", "The publication branch contains different work.", 409)
        pr = self.request(
            "POST",
            f"/repos/{repo}/pulls",
            token,
            json={
                "title": f"Susanoo proposed {proposal['layer']} repair",
                "head": branch,
                "base": default_branch,
                "draft": True,
                "body": marker
                + "\nScoped proposed repair. Reproduction, valid controls and held-out validation are attached "
                "in the authorized Susanoo evidence view. No automatic merge or deployment.",
            },
        )
        return pr["html_url"]

    def close(self):
        self.client.close()


def patch_policy(bundle, allowed_paths):
    if not allowed_paths:
        raise PlatformError("FORBIDDEN", "Source policy does not approve any patch paths.", 403)
    changed = 0
    seen = set()
    for file in bundle.files:
        path = PurePosixPath(file.path)
        if (
            not re.fullmatch(r"[A-Za-z0-9_. /-]+", file.path)
            or str(path) != file.path
            or path.is_absolute()
            or ".." in path.parts
            or file.path in seen
        ):
            raise PlatformError("FORBIDDEN", "Patch path is unsafe or duplicated.", 403)
        seen.add(file.path)
        if any(part.lower() in {".git", "heldout", ".env", "secrets"} for part in path.parts):
            raise PlatformError("FORBIDDEN", "Patch touches a protected path.", 403)
        if not any(
            file.path == prefix or (prefix.endswith("/") and file.path.startswith(prefix)) for prefix in allowed_paths
        ):
            raise PlatformError("FORBIDDEN", "Patch exceeds the approved source paths.", 403)
        diff = list(difflib.ndiff((file.before or "").splitlines(), (file.after or "").splitlines()))
        changed += sum(line.startswith(("+ ", "- ")) for line in diff)
    if not 0 < changed <= 60:
        raise PlatformError("FORBIDDEN", "Patch must change between 1 and 60 lines.", 403)
