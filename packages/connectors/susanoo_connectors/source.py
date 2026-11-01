"""Source acquisition, snapshots, runtime suggestions and approved builds (I01/I02) [Spec §9].

Git runs from an argument vector with a sanitized environment and config: hooks, automatic
submodules, external filters, fsmonitor and prompts are disabled, and only allowlisted schemes and
hosts are accepted. The checkout is verified against the pinned commit. Archives reject traversal,
absolute paths, links, oversize members and decompression bombs. A local directory is copied into an
isolated snapshot; the original is never modified. Runtime detection only *suggests*; a build needs
an approved recipe and runs in a resource-limited process with no control-plane credentials.
"""

from __future__ import annotations

import hashlib
import os
import re
try:
    import resource
except ImportError:
    resource = None
import shutil
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field
from susanoo_contracts import content_hash

COMMIT = re.compile(r"^[a-f0-9]{40}$")
GIT_CONFIG = [
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "submodule.recurse=false",
    "-c",
    "protocol.ext.allow=never",
    "-c",
    "credential.helper=",
    "-c",
    "advice.detachedHead=false",
]


class SourceError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GitSource(Model):
    url: str = Field(min_length=1, max_length=500)
    commit: str = Field(pattern=COMMIT.pattern)
    allowed_hosts: list[str] = Field(default_factory=lambda: ["github.com"])
    allow_local: bool = False


class ArchiveLimits(Model):
    max_files: int = Field(default=5000, ge=1)
    max_file_bytes: int = Field(default=25 * 1024 * 1024, ge=1)
    max_total_bytes: int = Field(default=200 * 1024 * 1024, ge=1)
    max_ratio: float = Field(default=100.0, gt=1)


class Snapshot(Model):
    kind: Literal["git", "archive", "directory"]
    origin: str
    commit: str | None = None
    digest: str
    files: int
    path: str


class RuntimeSuggestion(Model):
    kind: Literal["python", "node", "container", "susanoo_config"]
    evidence: str
    status: Literal["suggestion"] = "suggestion"


class BuildRecipe(Model):
    runtime: Literal["python", "node", "container"]
    steps: list[list[str]] = Field(min_length=1, max_length=20)
    timeout_seconds: int = Field(default=600, ge=1, le=3600)
    memory_bytes: int = Field(default=2 * 1024**3, ge=64 * 1024**2)
    cpu_seconds: int = Field(default=600, ge=1)
    env: dict[str, str] = Field(default_factory=dict)

    @property
    def digest(self) -> str:
        return content_hash(self.model_dump(mode="json"))


class BuildResult(Model):
    status: Literal[
        "succeeded", "dependency_install_failed", "missing_secret", "unsupported_runtime", "timed_out", "failed"
    ]
    recipe_digest: str
    snapshot_digest: str
    log_tail: str
    exit_code: int | None = None


def validate_git_url(source: GitSource) -> None:
    url = source.url
    if url.startswith("-") or "::" in url.split("/")[0]:
        raise SourceError("unsafe_url", "Option-like or transport-helper URLs are refused")
    parts = urlsplit(url)
    if parts.scheme == "file":
        if not source.allow_local:
            raise SourceError("unsafe_url", "Local file remotes need explicit local mode")
        return
    if parts.scheme not in {"https", "ssh"}:
        raise SourceError("unsafe_url", f"Scheme {parts.scheme or '(none)'} is not allowed")
    if parts.password or (parts.username and parts.scheme == "https"):
        raise SourceError("credentials_in_url", "Credentials never go in URLs; use a local credential helper")
    if parts.hostname not in source.allowed_hosts:
        raise SourceError("host_not_allowed", f"Host {parts.hostname} is not on the allowlist")


def _git_env(home: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_LFS_SKIP_SMUDGE": "1",
        "GIT_ALLOW_PROTOCOL": "https:ssh:file",
        "LANG": "C",
    }


def _git(args: list[str], cwd: Path, env: dict[str, str], timeout: int = 300) -> str:
    result = subprocess.run(
        ["git", *GIT_CONFIG, *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False
    )
    if result.returncode != 0:
        raise SourceError("git_failed", f"git {args[0]} failed: {result.stderr.strip()[-300:]}")
    return result.stdout


def tree_digest(root: Path) -> tuple[str, int]:
    """Digest over (path, executable bit, content hash) for every regular file, excluding .git."""
    entries = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0] == ".git":
            continue
        if path.is_symlink():
            entries.append([relative.as_posix(), "link", os.readlink(path)])
        elif path.is_file():
            executable = bool(path.stat().st_mode & stat.S_IXUSR)
            entries.append([relative.as_posix(), executable, hashlib.sha256(path.read_bytes()).hexdigest()])
    return content_hash(entries), len(entries)


def acquire_git(source: GitSource, destination: Path) -> Snapshot:
    validate_git_url(source)
    destination = Path(destination)
    if destination.exists() and any(destination.iterdir()):
        raise SourceError("destination_not_empty", "Snapshot destination must be empty")
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="susanoo-git-home-") as home:
        env = _git_env(Path(home))
        _git(["init", "--quiet"], destination, env)
        # Disable every external filter/diff/merge driver before any file is checked out.
        (destination / ".git" / "info").mkdir(parents=True, exist_ok=True)
        (destination / ".git" / "info" / "attributes").write_text("* -filter -diff -merge\n")
        _git(["remote", "add", "origin", "--", source.url], destination, env)
        try:
            _git(
                ["fetch", "--quiet", "--depth", "1", "--no-recurse-submodules", "origin", source.commit],
                destination,
                env,
            )
        except SourceError:
            _git(["fetch", "--quiet", "--no-recurse-submodules", "origin"], destination, env)
        _git(["checkout", "--quiet", "--detach", source.commit], destination, env)
        head = _git(["rev-parse", "HEAD"], destination, env).strip()
    if head != source.commit:
        raise SourceError("commit_mismatch", "Checked-out commit differs from the pinned commit")
    digest, files = tree_digest(destination)
    return Snapshot(kind="git", origin=source.url, commit=head, digest=digest, files=files, path=str(destination))


def import_archive(archive: Path, destination: Path, limits: ArchiveLimits | None = None) -> Snapshot:
    limits = limits or ArchiveLimits()
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        if len(members) > limits.max_files:
            raise SourceError("too_many_files", f"Archive has more than {limits.max_files} entries")
        total = 0
        for member in members:
            name = PurePosixPath(member.filename)
            if (
                name.is_absolute()
                or ".." in name.parts
                or member.filename.startswith(("/", "\\"))
                or ":" in name.parts[0]
            ):
                raise SourceError("path_traversal", f"Unsafe archive path {member.filename}")
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise SourceError("link_refused", f"Archive link {member.filename} refused")
            if member.file_size > limits.max_file_bytes:
                raise SourceError("file_too_large", f"{member.filename} exceeds the per-file limit")
            if member.compress_size and member.file_size / member.compress_size > limits.max_ratio:
                raise SourceError("decompression_bomb", f"{member.filename} expands suspiciously")
            total += member.file_size
            if total > limits.max_total_bytes:
                raise SourceError("archive_too_large", "Archive exceeds the expanded size limit")
        for member in members:
            target = (destination / member.filename).resolve()
            if not target.is_relative_to(destination):
                raise SourceError("path_traversal", f"Unsafe archive path {member.filename}")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            with bundle.open(member) as source_file, open(target, "wb") as out:
                while chunk := source_file.read(65536):
                    written += len(chunk)
                    if written > limits.max_file_bytes:  # never trust the declared size alone
                        raise SourceError("decompression_bomb", f"{member.filename} expanded past its limit")
                    out.write(chunk)
    digest, files = tree_digest(destination)
    return Snapshot(kind="archive", origin=str(archive), digest=digest, files=files, path=str(destination))


def import_directory(source: Path, destination: Path) -> Snapshot:
    source, destination = Path(source).resolve(), Path(destination)

    def ignore(directory: str, names: list[str]) -> list[str]:
        skipped = [n for n in names if n == ".git"]
        for name in names:
            path = Path(directory) / name
            if path.is_symlink() and not path.resolve().is_relative_to(source):
                skipped.append(name)  # links escaping the project are not copied
        return skipped

    shutil.copytree(source, destination, symlinks=True, ignore=ignore)
    digest, files = tree_digest(destination)
    return Snapshot(kind="directory", origin=str(source), digest=digest, files=files, path=str(destination))


def detect_runtime(root: Path) -> list[RuntimeSuggestion]:
    root = Path(root)
    found = []
    for name, kind in (
        ("susanoo.yaml", "susanoo_config"),
        ("pyproject.toml", "python"),
        ("requirements.txt", "python"),
        ("package.json", "node"),
        ("Dockerfile", "container"),
    ):
        if (root / name).is_file():
            found.append(RuntimeSuggestion(kind=kind, evidence=name))
    return found


def _limits(recipe: BuildRecipe):
    def apply() -> None:
        resource.setrlimit(resource.RLIMIT_CPU, (recipe.cpu_seconds, recipe.cpu_seconds))
        try:
            resource.setrlimit(resource.RLIMIT_AS, (recipe.memory_bytes, recipe.memory_bytes))
        except (ValueError, OSError):
            pass  # macOS does not enforce RLIMIT_AS; containers enforce memory in deployment
        os.setsid()

    return apply


def run_build(
    snapshot: Snapshot, recipe: BuildRecipe, *, approved_digest: str, secrets: dict[str, str] | None = None
) -> BuildResult:
    """Run an owner-approved recipe in a copy of the snapshot. Never a shell; never control-plane secrets."""
    if approved_digest != recipe.digest:
        raise SourceError("recipe_not_approved", "The recipe differs from the approved digest")
    if resource is None:
        return BuildResult(
            status="unsupported_runtime", recipe_digest=recipe.digest, snapshot_digest=snapshot.digest,
            log_tail="Resource-bounded source builds require a POSIX runner or an approved container host",
        )
    if recipe.runtime == "container":
        return BuildResult(
            status="unsupported_runtime",
            recipe_digest=recipe.digest,
            snapshot_digest=snapshot.digest,
            log_tail="container builds run on the container host, not in-process",
        )
    missing = [
        name
        for name, value in recipe.env.items()
        if value.startswith("secret:") and value.removeprefix("secret:") not in (secrets or {})
    ]
    if missing:
        return BuildResult(
            status="missing_secret",
            recipe_digest=recipe.digest,
            snapshot_digest=snapshot.digest,
            log_tail=f"missing secret(s): {sorted(missing)}",
        )
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "HOME": snapshot.path}
    for name, value in recipe.env.items():
        env[name] = (secrets or {})[value.removeprefix("secret:")] if value.startswith("secret:") else value
    log = []
    with tempfile.TemporaryDirectory(prefix="susanoo-build-") as work:
        workdir = Path(work) / "src"
        shutil.copytree(snapshot.path, workdir, symlinks=True, ignore=shutil.ignore_patterns(".git"))
        for index, step in enumerate(recipe.steps):
            try:
                result = subprocess.run(
                    step,
                    cwd=workdir,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=recipe.timeout_seconds,
                    preexec_fn=_limits(recipe),
                    check=False,
                )
            except subprocess.TimeoutExpired:
                return BuildResult(
                    status="timed_out",
                    recipe_digest=recipe.digest,
                    snapshot_digest=snapshot.digest,
                    log_tail=_redact("\n".join(log), secrets)[-2000:],
                )
            except FileNotFoundError:
                return BuildResult(
                    status="unsupported_runtime",
                    recipe_digest=recipe.digest,
                    snapshot_digest=snapshot.digest,
                    log_tail=f"step {index}: {step[0]} not found",
                )
            log.append(result.stdout[-2000:] + result.stderr[-2000:])
            if result.returncode != 0:
                install = any(word in " ".join(step) for word in ("install", "sync", "ci"))
                return BuildResult(
                    status="dependency_install_failed" if install else "failed",
                    recipe_digest=recipe.digest,
                    snapshot_digest=snapshot.digest,
                    log_tail=_redact("\n".join(log), secrets)[-2000:],
                    exit_code=result.returncode,
                )
    return BuildResult(
        status="succeeded",
        recipe_digest=recipe.digest,
        snapshot_digest=snapshot.digest,
        log_tail=_redact("\n".join(log), secrets)[-2000:],
        exit_code=0,
    )


def _redact(text: str, secrets: dict[str, str] | None) -> str:
    for value in (secrets or {}).values():
        if value:
            text = text.replace(value, "[redacted]")
    return text
