"""GitHub tool code that ships inside generated repos. [PRD › Connectors and API budget › Connector contract]

Agent Smith copies each function's source into the generated app's `agent/tools.py`, which cannot import
`susanoo` and never sees a credential. So every function here is self-contained:

- standard library only, imported inside the body; no names from this module;
- the repo and token come from `GITHUB_REPO` / `GITHUB_TOKEN`, the env vars `GitHubConnector.env()` sets,
  read at call time inside the container;
- writes are dry-run unless `GITHUB_WRITES_ENABLED` is "true";
- every failure is answered as a plain-English result dict, never raised and never echoing the token.

Each returns `{"ok", "message", "data", "dry_run"}`, the dict form of `ToolResult`. Names and arguments match
`GitHubConnector.tools()`, which is where Agent Smith reads the schemas.
"""

from collections.abc import Callable


def list_pull_requests(state: str = "open", limit: int = 10) -> dict:
    """List pull requests in the connected repository."""
    import json
    import os
    import urllib.error
    import urllib.parse
    import urllib.request

    def done(ok: bool, message: str, data: object = None) -> dict:
        return {"ok": ok, "message": message, "data": data, "dry_run": False}

    if state not in ("open", "closed", "all"):
        return done(False, "state must be open, closed or all.")
    try:
        per_page = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        return done(False, "Those arguments don't fit this tool. Check its input schema.")
    try:
        repo = os.environ["GITHUB_REPO"]
        query = urllib.parse.urlencode({"state": state, "per_page": per_page})
        request = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/pulls?{query}",
            headers={
                "Authorization": "Bearer " + os.environ["GITHUB_TOKEN"],
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "Susanoo-agent",
            },
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            pulls = json.loads(response.read().decode("utf-8"))
    except KeyError:
        return done(False, "GitHub isn't connected to this agent, so this tool can't run.")
    except urllib.error.HTTPError as exc:
        reason = {401: "the token was rejected", 403: "the token isn't allowed to", 404: "it wasn't found"}
        return done(False, f"Couldn't list pull requests: {reason.get(exc.code, f'GitHub answered {exc.code}')}.")
    except (urllib.error.URLError, TimeoutError):
        return done(False, "We couldn't reach GitHub. Try again in a moment.")
    except ValueError:
        return done(False, "GitHub sent back something we couldn't read. Try again in a moment.")
    prs = [{"number": p["number"], "title": p["title"], "author": (p.get("user") or {}).get("login")} for p in pulls]
    return done(True, f"Found {len(prs)} pull requests in {repo}.", prs)


def get_pull_request(number: int) -> dict:
    """Read a pull request in the connected repository: title, description and unified diff."""
    import json
    import os
    import urllib.error
    import urllib.request

    def done(ok: bool, message: str, data: object = None) -> dict:
        return {"ok": ok, "message": message, "data": data, "dry_run": False}

    def fetch(url: str, accept: str) -> str:
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": "Bearer " + os.environ["GITHUB_TOKEN"],
                "Accept": accept,
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "Susanoo-agent",
            },
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.read().decode("utf-8", "replace")

    max_diff_chars = 200_000
    try:
        number = int(number)
    except (TypeError, ValueError):
        return done(False, "Those arguments don't fit this tool. Check its input schema.")
    try:
        url = f"https://api.github.com/repos/{os.environ['GITHUB_REPO']}/pulls/{number}"
        pr = json.loads(fetch(url, "application/vnd.github+json"))
        diff = fetch(url, "application/vnd.github.diff")
    except KeyError:
        return done(False, "GitHub isn't connected to this agent, so this tool can't run.")
    except urllib.error.HTTPError as exc:
        reason = {401: "the token was rejected", 403: "the token isn't allowed to", 404: "it wasn't found"}
        return done(
            False, f"Couldn't read pull request #{number}: {reason.get(exc.code, f'GitHub answered {exc.code}')}."
        )
    except (urllib.error.URLError, TimeoutError):
        return done(False, "We couldn't reach GitHub. Try again in a moment.")
    except ValueError:
        return done(False, "GitHub sent back something we couldn't read. Try again in a moment.")
    data = {
        "number": pr["number"],
        "title": pr["title"],
        "body": pr.get("body") or "",
        "head_sha": pr["head"]["sha"],
        "diff": diff[:max_diff_chars],
        "diff_truncated": len(diff) > max_diff_chars,
    }
    return done(True, f"Read pull request #{number}: {pr['title']}", data)


def get_file(path: str, ref: str | None = None) -> dict:
    """Read one file from the connected repository, optionally at a commit or branch."""
    import os
    import urllib.error
    import urllib.parse
    import urllib.request

    def done(ok: bool, message: str, data: object = None) -> dict:
        return {"ok": ok, "message": message, "data": data, "dry_run": False}

    max_file_chars = 100_000
    segments = [s for s in str(path).split("/") if s]
    if not segments or any(s in (".", "..") for s in segments):
        return done(False, "Give a file path inside the repository, like app/main.py.")
    try:
        # Every character outside a plain path is escaped, so ?, # and %2e can't leave this file's URL.
        url = f"https://api.github.com/repos/{os.environ['GITHUB_REPO']}/contents/" + "/".join(
            urllib.parse.quote(s, safe="") for s in segments
        )
        if ref:
            url += "?" + urllib.parse.urlencode({"ref": str(ref)})
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": "Bearer " + os.environ["GITHUB_TOKEN"],
                "Accept": "application/vnd.github.raw+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "Susanoo-agent",
            },
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            text = response.read().decode("utf-8", "replace")
    except KeyError:
        return done(False, "GitHub isn't connected to this agent, so this tool can't run.")
    except urllib.error.HTTPError as exc:
        reason = {401: "the token was rejected", 403: "the token isn't allowed to", 404: "it wasn't found"}
        return done(False, f"Couldn't read {path}: {reason.get(exc.code, f'GitHub answered {exc.code}')}.")
    except (urllib.error.URLError, TimeoutError):
        return done(False, "We couldn't reach GitHub. Try again in a moment.")
    return done(True, f"Read {path}.", text[:max_file_chars])


def post_review_comment(number: int, body: str) -> dict:
    """Post a review comment on a pull request. Dry-run unless GITHUB_WRITES_ENABLED is "true"."""
    import json
    import os
    import urllib.error
    import urllib.request

    def done(ok: bool, message: str, dry_run: bool = False) -> dict:
        return {"ok": ok, "message": message, "data": None, "dry_run": dry_run}

    try:
        number = int(number)
    except (TypeError, ValueError):
        return done(False, "Those arguments don't fit this tool. Check its input schema.")
    if os.environ.get("GITHUB_WRITES_ENABLED", "").strip().lower() != "true":
        return done(True, f"Dry run: would have posted a review comment on pull request #{number}.", dry_run=True)
    try:
        request = urllib.request.Request(
            f"https://api.github.com/repos/{os.environ['GITHUB_REPO']}/pulls/{number}/reviews",
            data=json.dumps({"body": str(body), "event": "COMMENT"}).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": "Bearer " + os.environ["GITHUB_TOKEN"],
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "Susanoo-agent",
            },
        )
        with urllib.request.urlopen(request, timeout=15):
            pass
    except KeyError:
        return done(False, "GitHub isn't connected to this agent, so this tool can't run.")
    except urllib.error.HTTPError as exc:
        reason = {401: "the token was rejected", 403: "the token isn't allowed to", 404: "it wasn't found"}
        return done(
            False, f"Couldn't comment on pull request #{number}: {reason.get(exc.code, f'GitHub answered {exc.code}')}."
        )
    except (urllib.error.URLError, TimeoutError):
        return done(False, "We couldn't reach GitHub. Try again in a moment.")
    return done(True, f"Posted a review comment on pull request #{number}.")


TOOLS: list[Callable[..., dict]] = [list_pull_requests, get_pull_request, get_file, post_review_comment]
