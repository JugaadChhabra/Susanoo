"""Approved local source acquisition, builds, and repair validation for platform jobs."""

from __future__ import annotations

import asyncio
import hashlib
import http
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from contextlib import suppress
from functools import partial
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from susanoo_connectors.source import (
    ArchiveLimits,
    BuildRecipe,
    GitSource,
    Snapshot,
    SourceError,
    acquire_git,
    import_archive,
    import_directory,
    tree_digest,
)
from susanoo_contracts import (
    BuildResult,
    PatchBundle,
    RepairIn,
    RepairResult,
    SourceBuildReport,
    SourceSpec,
    content_hash,
)

from .journal import JournalError


class _LocalApproval(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_kind: Literal["git", "directory", "archive"] = "git"
    source_url: str | None = None
    local_path: str | None = None
    snapshot_digest: str | None = None
    recipe: dict
    allowed_hosts: list[str] = Field(default_factory=lambda: ["github.com"])
    allow_local_source: bool = False
    use_source_broker: bool = False
    secrets: dict[str, str] = Field(default_factory=dict)


class _JobInputs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job_kind: str


class _ApprovedSourceInputs(_JobInputs):
    source_kind: Literal["git", "directory", "archive"] = "git"
    source_url: str | None = Field(default=None, min_length=1, max_length=500)
    local_path: str | None = Field(default=None, min_length=1, max_length=4096)
    snapshot_digest: str | None = None
    recipe: dict
    allowed_hosts: list[str] = Field(default_factory=lambda: ["github.com"])
    allow_local_source: bool = False
    use_source_broker: bool = False
    secrets: dict[str, str] = Field(default_factory=dict)


class _SourceInputs(_ApprovedSourceInputs):
    job_kind: str


class _AgentRepairInputs(_ApprovedSourceInputs):
    job_kind: str
    repair_kind: str
    agent_argv: list[str] = Field(min_length=1, max_length=64)
    agent_env: dict[str, str] = Field(default_factory=dict)
    reproduction_case_id: str
    control_case_ids: list[str] = Field(min_length=1, max_length=20)
    heldout_case_ids: list[str] = Field(min_length=1, max_length=50)
    unit_test_command: dict
    approved_unit_test_digest: str
    failing_check_ids: list[str] = Field(min_length=1, max_length=50)
    rationale: str = Field(min_length=1, max_length=4000)


class _SimulatorRepairInputs(_ApprovedSourceInputs):
    job_kind: str
    repair_kind: str
    entity_kinds: list[str] = Field(min_length=1, max_length=30)
    proposal: dict
    reduction: dict
    violation_category: str = Field(min_length=1, max_length=100)
    controls: list[dict] = Field(min_length=1, max_length=50)
    heldout: list[dict] = Field(min_length=1, max_length=50)


def _source(claim, inputs):
    version = next((v for v in claim.execution_manifest.versions if v.kind == "source"), None)
    if version is None or not version.approved:
        raise JournalError("source_approval_required", "A pinned, approved source version is required")
    spec = SourceSpec.model_validate(version.data)
    try:
        source_inputs = {
            key: inputs[key]
            for key in (
                "source_kind",
                "source_url",
                "local_path",
                "snapshot_digest",
                "recipe",
                "allowed_hosts",
                "allow_local_source",
                "use_source_broker",
                "secrets",
            )
            if key in inputs
        }
        local = _LocalApproval.model_validate(source_inputs)
        recipe = BuildRecipe.model_validate(local.recipe)
    except ValidationError as exc:
        raise JournalError("source_approval_invalid", "Locally approved source inputs are incomplete") from exc
    if recipe.digest != spec.recipe_hash:
        raise JournalError("recipe_digest_mismatch", "Build recipe differs from the approved source version")
    if local.source_kind != spec.source_kind:
        raise JournalError("source_kind_mismatch", "Local source kind differs from its approved version")
    if spec.source_kind == "git":
        if local.local_path is not None or local.snapshot_digest is not None or local.source_url is None:
            raise JournalError("source_approval_invalid", "Git approval needs only its pinned remote URL")
        source = GitSource(
            url=local.source_url,
            commit=spec.commit,
            allowed_hosts=local.allowed_hosts,
            allow_local=local.allow_local_source,
        )
    else:
        if local.source_url is not None or local.local_path is None or local.snapshot_digest != spec.snapshot_digest:
            raise JournalError(
                "source_approval_invalid", "Local source needs its exact locally approved snapshot path and digest"
            )
        path = Path(local.local_path)
        if path.is_symlink():
            raise JournalError("source_path_invalid", "A source path cannot be a symbolic link")
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise JournalError("source_path_invalid", "Locally approved source path is unavailable") from exc
        if spec.source_kind == "directory" and not resolved.is_dir():
            raise JournalError("source_path_invalid", "Directory source path must name a local directory")
        if spec.source_kind == "archive" and not resolved.is_file():
            raise JournalError("source_path_invalid", "Archive source path must name a local file")
        source = resolved
    return spec, source, recipe, local


def _job_inputs(kind, inputs):
    if not isinstance(inputs, dict):
        raise JournalError("source_inputs_invalid", "Locally approved job inputs must be an object")
    if inputs.get("job_kind") != kind:
        raise JournalError("source_inputs_invalid", "Locally approved inputs do not match the claimed job kind")
    try:
        if kind in {"source_import", "build"}:
            return _SourceInputs.model_validate(inputs)
        if kind == "repair_retest" and inputs.get("repair_kind") == "agent":
            return _AgentRepairInputs.model_validate(inputs)
        if kind == "repair_retest" and inputs.get("repair_kind") == "simulator":
            return _SimulatorRepairInputs.model_validate(inputs)
    except ValidationError as exc:
        raise JournalError("source_inputs_invalid", "Locally approved producer inputs are invalid") from exc
    raise JournalError("repair_kind_invalid", "repair_kind must be agent or simulator")


def _acquire(
    source,
    destination: Path,
    cancelled: threading.Event | None = None,
    *,
    source_kind="git",
    expected_digest=None,
    max_source_bytes=500 * 1024 * 1024,
    auth_header=None,
):
    if source_kind == "git":
        snapshot = acquire_git(
            source, destination, cancelled=cancelled, max_total_bytes=max_source_bytes, auth_header=auth_header
        )
    elif source_kind == "directory":
        if cancelled and cancelled.is_set():
            raise SourceError("cancelled", "Source acquisition was cancelled")
        tree_digest(source, max_total_bytes=max_source_bytes)
        snapshot = import_directory(source, destination, max_total_bytes=max_source_bytes)
    elif source_kind == "archive":
        if cancelled and cancelled.is_set():
            raise SourceError("cancelled", "Source acquisition was cancelled")
        snapshot = import_archive(
            source,
            destination,
            ArchiveLimits(max_files=10000, max_file_bytes=25 * 1024 * 1024, max_total_bytes=max_source_bytes),
        )
    else:
        raise SourceError("source_kind_unsupported", f"Unsupported source kind {source_kind!r}")
    if source_kind == "git" and snapshot.commit != source.commit:
        raise SourceError("commit_mismatch", "Snapshot did not preserve the pinned commit")
    digest, files = tree_digest(
        Path(snapshot.path), max_files=10000, max_file_bytes=25 * 1024 * 1024, max_total_bytes=max_source_bytes
    )
    if expected_digest and digest != expected_digest:
        raise SourceError("snapshot_digest_mismatch", "Local source bytes differ from the approved snapshot digest")
    snapshot = snapshot.model_copy(update={"digest": digest, "files": files})
    return snapshot


def lockfile_digest(root):
    names = {
        "uv.lock",
        "poetry.lock",
        "Pipfile.lock",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "Cargo.lock",
        "Gemfile.lock",
        "go.sum",
    }
    files = {}
    root = Path(root)
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if ".git" in relative.parts or path.name not in names:
            continue
        if path.is_symlink() or not path.is_file():
            raise SourceError("unsafe_lockfile", "Dependency lockfiles must be regular snapshot files")
        files[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return content_hash(files)


def remove_owned_tree(root):
    root = Path(root).resolve()
    def readonly(function, name, error):
        target = Path(name)
        if not isinstance(error, PermissionError) or target.is_symlink() or not target.resolve().is_relative_to(root):
            raise error
        target.chmod(stat.S_IRUSR | stat.S_IWUSR | (stat.S_IXUSR if target.is_dir() else 0))
        function(name)
    shutil.rmtree(root, onexc=readonly)


def _bounded_build(
    snapshot: Snapshot,
    recipe: BuildRecipe,
    approved: str,
    secrets: dict,
    byte_limit: int,
    cancelled: threading.Event | None = None,
):
    """Run approved argv steps with capped logs and process-tree teardown on timeout."""
    if approved != recipe.digest:
        raise SourceError("recipe_not_approved", "Recipe digest is not locally approved")
    if os.name != "posix":
        from susanoo_connectors.source import BuildResult as ConnectorBuildResult

        return ConnectorBuildResult(
            status="unsupported_runtime",
            recipe_digest=recipe.digest,
            snapshot_digest=snapshot.digest,
            log_tail="Bounded process-tree build isolation is unavailable on this runner OS",
        )
    if (
        not recipe.steps
        or any(not argv or not all(isinstance(part, str) and part for part in argv) for argv in recipe.steps)
        or byte_limit < 1
    ):
        raise SourceError("invalid_recipe", "Build steps and output limit must be non-empty and bounded")
    if recipe.runtime == "container":
        from susanoo_connectors.source import BuildResult as ConnectorBuildResult

        return ConnectorBuildResult(
            status="unsupported_runtime",
            recipe_digest=recipe.digest,
            snapshot_digest=snapshot.digest,
            log_tail="Container builds need a rootless, network-disabled local builder",
        )
    if not hasattr(os, "killpg"):
        from susanoo_connectors.source import BuildResult as ConnectorBuildResult

        return ConnectorBuildResult(
            status="unsupported_runtime",
            recipe_digest=recipe.digest,
            snapshot_digest=snapshot.digest,
            log_tail="Process-tree teardown is unavailable on this runner",
        )
    missing = [
        name
        for name, value in recipe.env.items()
        if value.startswith("secret:") and value.removeprefix("secret:") not in secrets
    ]
    if missing:
        from susanoo_connectors.source import BuildResult as ConnectorBuildResult

        return ConnectorBuildResult(
            status="missing_secret",
            recipe_digest=recipe.digest,
            snapshot_digest=snapshot.digest,
            log_tail=f"missing secret(s): {sorted(missing)}",
        )
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"}
    env.update(
        {
            key: secrets[value.removeprefix("secret:")] if value.startswith("secret:") else value
            for key, value in recipe.env.items()
        }
    )
    logs = bytearray()
    logs_lock = threading.Lock()
    overflow = threading.Event()
    process_id = [None]

    def drain(stream):
        while chunk := stream.read(4096):
            with logs_lock:
                room = max(0, byte_limit - len(logs))
                if room:
                    logs.extend(chunk[:room])
                if len(chunk) > room:
                    overflow.set()
            if overflow.is_set() and process_id[0] is not None:
                _kill_group(process_id[0])

    def tail():
        with logs_lock:
            value = bytes(logs).decode("utf-8", "replace")
        for secret in secrets.values():
            if secret:
                value = value.replace(secret, "[redacted]")
        encoded = value.encode("utf-8", "replace")[-min(byte_limit, 2000) :]
        while True:
            try:
                return encoded.decode("utf-8")
            except UnicodeDecodeError as exc:
                encoded = encoded[exc.start + 1 :]

    with tempfile.TemporaryDirectory(prefix="susanoo-platform-build-") as temporary:
        tree = Path(temporary) / "src"
        source_tree = Path(snapshot.path)
        links = [path.relative_to(source_tree).as_posix() for path in source_tree.rglob("*") if path.is_symlink()]
        if links:
            from susanoo_connectors.source import BuildResult as ConnectorBuildResult

            return ConnectorBuildResult(
                status="failed",
                recipe_digest=recipe.digest,
                snapshot_digest=snapshot.digest,
                log_tail=f"Source snapshot contains symlink(s): {', '.join(links[:5])}",
            )
        shutil.copytree(source_tree, tree, symlinks=True, ignore=shutil.ignore_patterns(".git"))
        for index, argv in enumerate(recipe.steps):
            if cancelled and cancelled.is_set():
                raise InterruptedError("Build dispatch cancelled")
            try:
                process = subprocess.Popen(
                    argv,
                    cwd=tree,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    preexec_fn=_build_limits(recipe),
                )
            except (FileNotFoundError, subprocess.SubprocessError, OSError) as exc:
                from susanoo_connectors.source import BuildResult as ConnectorBuildResult

                return ConnectorBuildResult(
                    status="unsupported_runtime",
                    recipe_digest=recipe.digest,
                    snapshot_digest=snapshot.digest,
                    log_tail=f"step {index}: could not start approved process ({type(exc).__name__})",
                )
            reader = threading.Thread(target=drain, args=(process.stdout,), daemon=True)
            process_id[0] = process.pid
            reader.start()
            deadline = time.monotonic() + min(recipe.timeout_seconds, 3600)
            reason = None
            while process.poll() is None:
                if cancelled and cancelled.is_set():
                    reason = "cancelled"
                    _kill_group(process.pid)
                    break
                if overflow.is_set():
                    _kill_group(process.pid)
                    break
                if time.monotonic() >= deadline:
                    reason = "timed_out"
                    _kill_group(process.pid)
                    break
                time.sleep(0.025)
            code = process.returncode
            _kill_group(process.pid)
            process.wait()
            reader.join(timeout=2)
            if reason == "cancelled" or (cancelled and cancelled.is_set()):
                raise InterruptedError("Build dispatch cancelled")
            if reason == "timed_out":
                from susanoo_connectors.source import BuildResult as ConnectorBuildResult

                return ConnectorBuildResult(
                    status="timed_out", recipe_digest=recipe.digest, snapshot_digest=snapshot.digest, log_tail=tail()
                )
            if overflow.is_set():
                from susanoo_connectors.source import BuildResult as ConnectorBuildResult

                return ConnectorBuildResult(
                    status="failed",
                    recipe_digest=recipe.digest,
                    snapshot_digest=snapshot.digest,
                    log_tail=_bounded_log(tail() + "\n[output limit exceeded; build terminated]", byte_limit),
                )
            # A successful command that leaves descendants behind is not allowed to outlive its build step.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if code:
                from susanoo_connectors.source import BuildResult as ConnectorBuildResult

                is_install = any(x in " ".join(argv) for x in ("install", "sync", "ci"))
                status = "dependency_install_failed" if is_install else "failed"
                return ConnectorBuildResult(
                    status=status,
                    recipe_digest=recipe.digest,
                    snapshot_digest=snapshot.digest,
                    log_tail=tail(),
                    exit_code=code,
                )
        if cancelled and cancelled.is_set():
            raise InterruptedError("Build dispatch cancelled")
    from susanoo_connectors.source import BuildResult as ConnectorBuildResult

    return ConnectorBuildResult(
        status="succeeded", recipe_digest=recipe.digest, snapshot_digest=snapshot.digest, log_tail=tail(), exit_code=0
    )


def _build_limits(recipe):
    def apply():
        import resource

        cpu_seconds = min(recipe.cpu_seconds, 600)
        memory_bytes = min(recipe.memory_bytes, 2 * 1024**3)
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        if hasattr(resource, "RLIMIT_CORE"):
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    return apply


def _kill_group(process_id):
    try:
        os.killpg(process_id, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _bounded_log(value, limit):
    encoded = value.encode("utf-8", "replace")[-min(limit, 2000) :]
    while True:
        try:
            return encoded.decode("utf-8")
        except UnicodeDecodeError as exc:
            encoded = encoded[exc.start + 1 :]


async def _thread_with_cancellation(function, *args, cancelled: threading.Event):
    task = asyncio.create_task(asyncio.to_thread(function, *args, cancelled))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancelled.set()
        with suppress(BaseException):
            await asyncio.shield(task)
        raise


async def _dispatch_local(journal, operation, call):
    attempt = str(journal.claim.attempt_id)
    step = journal.prepare_step(attempt, f"source-{uuid4().hex}", "agent_invoke", content_hash(operation))
    scope = f"{attempt}:agent"
    if not journal.reserve(scope, "agent", step.operation_id):
        journal.abandon_step(attempt, step.step_id, "budget_exceeded")
        raise JournalError("budget_exceeded", "Source-job agent allowance exhausted")
    journal.mark_dispatching(attempt, step.step_id, scope=scope)
    running = asyncio.create_task(call())
    try:
        while not running.done():
            if journal.cancel.requested or journal.lost_authority:
                running.cancel()
                with suppress(BaseException):
                    await running
                raise JournalError("cancelled", "Source execution stopped after cancellation or authority loss")
            try:
                await asyncio.wait_for(asyncio.shield(running), 0.05)
            except TimeoutError:
                pass
        result = await running
        journal.record_response(attempt, step.step_id, dispatch_state="acknowledged", side_effect_state="none_expected")
        return result
    except BaseException:
        if not running.done():
            running.cancel()
            with suppress(BaseException):
                await running
        journal.record_response(
            attempt,
            step.step_id,
            dispatch_state="uncertain",
            side_effect_state="requires_observation",
            error="source_job_incomplete",
        )
        raise


async def produce(kind, claim, inputs, *, journal, profile, agent_factory=None, environment=None):
    """Return `(report tuples, verdict, typed job summary)` for the platform dispatcher."""
    if kind not in {"source_import", "build", "repair_retest"}:
        raise JournalError("unsupported_source_job", f"Unsupported source job {kind}")
    approved_inputs = _job_inputs(kind, inputs)
    payload = approved_inputs.model_dump(mode="json")
    if kind == "repair_retest":
        if approved_inputs.use_source_broker:
            spec, source, _, approval = _source(claim, payload)
            mirror = Path(str(journal.path) + ".source")
            if mirror.exists():
                raise JournalError("snapshot_exists", "A previous source mirror requires reconciliation")
            mirror.mkdir(mode=0o700)
            try:
                snapshot = await _approved_acquisition(
                    claim, journal, profile, spec, source, approval, mirror / "checkout", threading.Event()
                )
                if lockfile_digest(snapshot.path) != spec.lockfile_hash:
                    raise JournalError("lockfile_mismatch", "Private source dependency locks differ from the pin")
                local = {
                    **payload,
                    "source_url": Path(snapshot.path).as_uri(),
                    "allow_local_source": True,
                    "use_source_broker": False,
                }
                return await _repair(
                    claim, local, journal=journal, profile=profile, agent_factory=agent_factory, environment=environment
                )
            finally:
                remove_owned_tree(mirror)
        return await _repair(
            claim, payload, journal=journal, profile=profile, agent_factory=agent_factory, environment=environment
        )
    spec, source, recipe, approval = _source(claim, payload)
    if spec.recipe_hash != recipe.digest:
        raise JournalError("recipe_digest_mismatch", "Recipe pin changed after approval")
    root = Path(str(journal.path) + ".source")
    if root.exists():
        raise JournalError("snapshot_exists", "A source job snapshot already exists; refusing to reuse it")
    root.mkdir(mode=0o700, parents=True)
    cancelled = threading.Event()
    try:
        snapshot = await _approved_acquisition(
            claim, journal, profile, spec, source, approval, root / "checkout", cancelled
        )
        if lockfile_digest(snapshot.path) != spec.lockfile_hash:
            raise JournalError("lockfile_mismatch", "Snapshot dependency lockfiles differ from the pinned source")
        if kind == "source_import":
            result_status, log_tail, exit_code = "succeeded", "", None
        else:

            async def build():
                return await _thread_with_cancellation(
                    _bounded_build,
                    snapshot,
                    recipe,
                    recipe.digest,
                    approval.secrets,
                    min(profile.max_output_bytes, claim.execution_manifest.limits.max_output_bytes),
                    cancelled=cancelled,
                )

            result = await _dispatch_local(
                journal,
                {"operation": "approved_build", "recipe_digest": recipe.digest},
                build,
            )
            log_tail, exit_code = result.log_tail, result.exit_code
            result_status = (
                "succeeded"
                if result.status == "succeeded"
                else ("failed" if result.status in {"failed", "dependency_install_failed"} else "unavailable")
            )
        report = SourceBuildReport(
            source_kind=spec.source_kind,
            status=result_status,
            source_commit=snapshot.commit,
            snapshot_digest=snapshot.digest,
            recipe_digest=recipe.digest,
            files=snapshot.files,
            runtime=recipe.runtime,
            log_tail=log_tail,
            exit_code=exit_code,
        )
        summary = BuildResult(
            job_kind=kind,
            status=result_status,
            source_commit=snapshot.commit,
            snapshot_digest=snapshot.digest,
        )
        return [("source_build", report)], "UNKNOWN", summary
    except SourceError as exc:
        raise JournalError(exc.code, str(exc)) from exc
    finally:
        if root.exists():
            remove_owned_tree(root)


async def _approved_acquisition(claim, journal, profile, spec, source, approval, destination, cancelled):
    from .platform_source_auth import source_auth

    async def acquire():
        async with source_auth(claim, journal, source, enabled=approval.use_source_broker) as header:
            acquisition = partial(
                _acquire,
                source,
                destination,
                source_kind=spec.source_kind,
                expected_digest=spec.snapshot_digest,
                max_source_bytes=min(profile.max_storage_bytes, claim.execution_manifest.limits.max_storage_bytes),
                auth_header=header,
            )
            return await _thread_with_cancellation(acquisition, cancelled=cancelled)

    return await _dispatch_local(
        journal,
        {
            "operation": "source_acquire",
            "source_kind": spec.source_kind,
            "commit": spec.commit,
            "snapshot_digest": spec.snapshot_digest,
        },
        acquire,
    )


class _GuardedToolProxy:
    """Loopback-only relay so JSONL agents reach the simulator through parent-authorized HTTP."""

    def __init__(self, environment, byte_limit):
        self.environment = environment
        self.byte_limit = byte_limit
        self.allowed: dict[str, set[str]] = {}
        self.server = None

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0, limit=16384)
        host, port = self.server.sockets[0].getsockname()[:2]
        return f"http://{host}:{port}"

    def allow(self, namespace, operations):
        self.allowed[namespace] = set(operations)

    async def close(self):
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    async def _reply(self, writer, status, body=b"{}", content_type="application/json"):
        reason = http.HTTPStatus(status).phrase
        head = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {content_type}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n"
        ).encode("ascii")
        writer.write(head + body)
        await writer.drain()

    async def _handle(self, reader, writer):
        try:
            request_line = await asyncio.wait_for(reader.readline(), 2)
            if len(request_line) > 8192:
                await self._reply(writer, 431)
                return
            method, target, protocol = request_line.decode("ascii").strip().split(" ")
            if protocol not in {"HTTP/1.0", "HTTP/1.1"} or method != "POST" or "?" in target:
                await self._reply(writer, 405)
                return
            headers = {}
            total = 0
            while True:
                line = await asyncio.wait_for(reader.readline(), 2)
                total += len(line)
                if total > 16384 or not line:
                    await self._reply(writer, 431)
                    return
                if line in {b"\r\n", b"\n"}:
                    break
                name, separator, value = line.decode("latin-1").partition(":")
                key = name.strip().lower()
                if not separator or key in headers:
                    await self._reply(writer, 400)
                    return
                headers[key] = value.strip()
            if "transfer-encoding" in headers:
                await self._reply(writer, 400)
                return
            try:
                size = int(headers.get("content-length", "0"))
            except ValueError:
                await self._reply(writer, 400)
                return
            if size < 0 or size > self.byte_limit:
                await self._reply(writer, 413)
                return
            match = re.fullmatch(r"/fixtures/([^/]+)/operations/([A-Za-z][A-Za-z0-9_-]{0,63})", target)
            if not match:
                await self._reply(writer, 404)
                return
            namespace, operation = match.groups()
            if operation not in self.allowed.get(namespace, set()):
                await self._reply(writer, 403)
                return
            body = await asyncio.wait_for(reader.readexactly(size), 2) if size else b"{}"
            forwarded = {
                key: headers[key]
                for key in ("authorization", "content-type", "x-susanoo-operation-id")
                if key in headers
            }
            response = await self.environment._client.post(target, content=body, headers=forwarded)
            await self._reply(
                writer,
                response.status_code,
                response.content,
                response.headers.get("content-type", "application/json"),
            )
        except (TimeoutError, asyncio.IncompleteReadError, UnicodeError, ValueError):
            with suppress(Exception):
                await self._reply(writer, 400)
        except Exception:
            with suppress(Exception):
                await self._reply(writer, 502)
        finally:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()


class _GuardedSimulatorFixtures:
    """Fixture adapter whose administrative calls use the parent's guarded HTTP transport."""

    def __init__(self, environment, proxy, base_url, admin_key):
        from susanoo_simulator.adapter import SimulatorFixtureAdapter

        self._adapter = SimulatorFixtureAdapter(
            base_url,
            admin_key,
            transport=environment._client._transport,
        )
        self.proxy = proxy

    async def prepare(self, *args, **kwargs):
        return await self._adapter.prepare(*args, **kwargs)

    def tool_route(self, fixture):
        route = self._adapter.tool_route(fixture)
        self.proxy.allow(route.namespace, route.operations)
        return route.model_copy(update={"base_url": self.proxy.base_url})

    async def observe(self, *args, **kwargs):
        return await self._adapter.observe(*args, **kwargs)

    async def reconcile(self, *args, **kwargs):
        return await self._adapter.reconcile(*args, **kwargs)

    async def cleanup(self, *args, **kwargs):
        return await self._adapter.cleanup(*args, **kwargs)

    async def aclose(self):
        await self._adapter.aclose()


class _RepairHarness:
    def __init__(self, cases, *, parent_journal, environment, profile, limits, agent_argv, agent_env, byte_limit):
        from .platform_jobs import GuardedAgent
        from .journal import Journal
        from .executor import AttemptExecutor
        from susanoo_sdk import FrameLimits, JsonlAgentProcess

        self.cases = cases
        self.parent_journal = parent_journal
        self.environment = environment
        self.profile = profile
        self.limits = profile.narrow(limits)
        self.agent_argv = agent_argv
        self.agent_env = agent_env
        self.byte_limit = byte_limit
        self.proxy = _GuardedToolProxy(environment, byte_limit)
        self.agents = []
        self.run_id = uuid4().hex
        self._temporary = tempfile.TemporaryDirectory(
            prefix=parent_journal.path.name + ".harness-", dir=parent_journal.path.parent
        )
        self.child_journal = Journal(Path(self._temporary.name) / "attempts.sqlite")
        self.fixtures = _GuardedSimulatorFixtures(
            environment,
            self.proxy,
            str(environment._client.base_url).rstrip("/"),
            environment._admin_key,
        )
        self.guard_agent = GuardedAgent
        self.frame_limits = FrameLimits(
            max_frame_bytes=min(1_048_576, max(1024, byte_limit)),
            max_total_bytes=max(1024, min(64 * 1_048_576, byte_limit)),
            request_deadline_seconds=max(1.0, self.limits.invocation_timeout_seconds),
            max_stderr_bytes=min(65_536, byte_limit),
        )

        def agent_factory():
            adapter = JsonlAgentProcess(
                self.agent_argv,
                env=self.agent_env,
                cwd=str(self._worktree),
                limits=self.frame_limits,
                name="approved-worktree-agent",
            )
            self.agents.append(adapter)
            return self.guard_agent(adapter, self.parent_journal)

        self.agent_factory = agent_factory
        self.executor = AttemptExecutor(
            self.child_journal,
            run_id=self.run_id,
            limits=self.limits,
            agent_factory=self.agent_factory,
            fixtures=self.fixtures,
            budget_scope=self.run_id,
        )
        self._worktree = None

    async def start(self):
        self.proxy.base_url = await self.proxy.start()

    async def run(self, tree, case_ids):
        self._worktree = tree
        results = {}
        for case_id in case_ids:
            attempt = await self.executor.run(self.cases[case_id])
            reliable = (
                attempt.execution_state == "completed"
                and attempt.cleanup_state == "succeeded"
                and attempt.reconciliation_state in {"not_needed", "resolved"}
            )
            results[case_id] = attempt.business_verdict if reliable else "UNKNOWN"
            agent = self.agents[-1] if self.agents else None
            if agent is not None:
                await agent.aclose()
        return results

    async def reproduce(self, tree):
        result = await self.run(tree, [self.reproduction_case_id])
        return result[self.reproduction_case_id]

    async def controls(self, tree):
        return await self.run(tree, self.control_case_ids)

    async def evaluate_heldout(self, tree):
        verdicts = await self.run(tree, self.heldout_case_ids)
        return {
            "split_digest": self.split_digest,
            "passed": sum(value == "PASS" for value in verdicts.values()),
            "failed": sum(value == "FAIL" for value in verdicts.values()),
            "unknown": sum(value == "UNKNOWN" for value in verdicts.values()),
        }

    async def evaluate(self, tree):
        return await self.evaluate_heldout(tree)

    async def close(self):
        for agent in self.agents:
            with suppress(Exception):
                await agent.aclose()
        await self.fixtures.aclose()
        await self.proxy.close()
        self.child_journal.close()
        self._temporary.cleanup()


def _repair_context(claim, journal, profile, inputs):
    from susanoo_contracts import content_hash as hash_content

    repair = claim.execution_manifest.repair
    if repair is None:
        raise JournalError("repair_assignment_missing", "Repair retest needs its exact pinned repair assignment")
    source_version = next((value for value in claim.execution_manifest.versions if value.kind == "source"), None)
    if source_version is None or not source_version.approved:
        raise JournalError("source_approval_required", "Repair retest needs an approved pinned source version")
    source_spec = SourceSpec.model_validate(source_version.data)
    if (
        repair.base_commit != source_spec.commit
        or claim.execution_manifest.finding_id != repair.finding_id
        or repair.layer not in {"agent", "simulator"}
    ):
        raise JournalError("repair_assignment_mismatch", "Repair assignment differs from the pinned finding or source")
    if not hasattr(journal, "client") or not hasattr(journal, "control"):
        raise JournalError(
            "repair_artifact_unavailable", "A live platform journal is required to fetch the assigned patch"
        )
    if not journal.lost_authority:
        journal.control()
    url = f"{journal.endpoint}/artifacts/{repair.patch_artifact_id}/download"
    try:
        response = journal.client.http.post(url, json={"lease_token": claim.lease_token})
        response.raise_for_status()
    except Exception as exc:
        raise JournalError(
            "repair_artifact_unavailable", "The assigned patch artifact could not be downloaded"
        ) from exc
    byte_limit = min(profile.max_output_bytes, claim.execution_manifest.limits.max_output_bytes)
    if len(response.content) > byte_limit:
        raise JournalError("output_limit", "Assigned patch artifact exceeds the local repair input limit")
    try:
        bundle = PatchBundle.model_validate_json(response.content)
    except ValidationError as exc:
        raise JournalError(
            "repair_patch_invalid", "Assigned patch artifact is not a valid canonical patch bundle"
        ) from exc
    if bundle.base_commit != repair.base_commit:
        raise JournalError("repair_assignment_mismatch", "Patch bundle base differs from the assigned repair base")
    diff = bundle.validation_diff()
    expected_digest = content_hash({"diff": diff, "layer": repair.layer})
    if not repair.validation_patch_digest or repair.validation_patch_digest != expected_digest:
        raise JournalError("repair_patch_digest_mismatch", "Patch bytes differ from the approved validation digest")
    request = RepairIn(
        finding_id=repair.finding_id,
        source_version_id=source_version.id,
        patch_artifact_id=repair.patch_artifact_id,
        layer=repair.layer,
    )
    proposal_hash = hash_content(
        {
            "request": request.model_dump(mode="json"),
            "patch_digest": hashlib.sha256(response.content).hexdigest(),
        }
    )
    if proposal_hash != repair.proposal_hash:
        raise JournalError("repair_proposal_mismatch", "Patch artifact and repair metadata do not match the proposal")
    return repair, source_spec, diff, bundle


def _repair_cases(claim, profile, inputs):
    from susanoo_contracts import CheckSpec, CaseSpec, FixtureSpec, SuiteSpec
    from susanoo_sdk import ObservationQuery
    from .suite import LocalCase, Limits

    versions = claim.execution_manifest.versions
    by_id = {str(value.id): value for value in versions}
    requested = {inputs.reproduction_case_id, *inputs.control_case_ids, *inputs.heldout_case_ids}
    if len(requested) != 1 + len(inputs.control_case_ids) + len(inputs.heldout_case_ids):
        raise JournalError("repair_suite_invalid", "Reproduction, controls and held-out case IDs must be disjoint")
    suites = [
        value
        for value in versions
        if value.kind == "suite"
        and value.approved
        and requested <= {str(case_id) for case_id in SuiteSpec.model_validate(value.data).case_version_ids}
    ]
    if len(suites) != 1:
        raise JournalError("repair_suite_invalid", "All repair cases must belong to one approved pinned suite")
    suite = SuiteSpec.model_validate(suites[0].data)
    limits = Limits(**{name: getattr(suite.limits, name) for name in Limits.model_fields})
    cases = {}
    for identifier in requested:
        version = by_id.get(identifier)
        if version is None or version.kind != "case" or not version.approved:
            raise JournalError("repair_suite_invalid", "A selected repair case is not pinned and approved")
        case = CaseSpec.model_validate(version.data)
        should_be_hidden = identifier in inputs.heldout_case_ids
        if (case.visibility == "hidden") != should_be_hidden or version.hidden != should_be_hidden:
            raise JournalError("repair_suite_invalid", "Held-out cases must be hidden and public cases must be visible")
        if case.modality != "text":
            raise JournalError("repair_suite_invalid", "This repair harness only supports approved text cases")
        fixture_version = by_id.get(str(case.fixture_version_id))
        if fixture_version is None or fixture_version.kind != "fixture" or not fixture_version.approved:
            raise JournalError("repair_suite_invalid", "A selected repair fixture is missing or unapproved")
        fixture = FixtureSpec.model_validate(fixture_version.data)
        if fixture.adapter != "susanoo_simulator" or not fixture.synthetic:
            raise JournalError("repair_suite_invalid", "Repair cases require synthetic local simulator fixtures")
        checks, queries = [], {}
        requirement_ids = []
        for check_id in case.check_version_ids:
            check_version = by_id.get(str(check_id))
            if check_version is None or check_version.kind != "check" or not check_version.approved:
                raise JournalError("repair_suite_invalid", "A selected repair check is missing or unapproved")
            spec = CheckSpec.model_validate(check_version.data)
            if spec.operator == "semantic":
                raise JournalError("repair_suite_invalid", "The local repair harness requires deterministic checks")
            entity = spec.entity or getattr(profile, "observation_kinds", {}).get(spec.observation)
            if not entity:
                raise JournalError("repair_suite_invalid", f"Observation kind for {spec.observation} is not approved")
            checks.append(spec.evaluator_definition(check_version.id))
            requirement_ids.append(str(spec.requirement_version_id))
            queries[spec.observation] = ObservationQuery(
                name=spec.observation,
                kind=entity,
                window_seconds=spec.window_seconds,
                baseline=spec.operator in {"transition", "unchanged", "forbidden_change"},
            )
        local_case = LocalCase(
            id=identifier,
            title=version.title,
            requirement_ids=sorted(set(requirement_ids)),
            required=case.required,
            visibility=case.visibility,
            modality="text",
            script=[step.model_dump(mode="json") for step in case.script],
            fixture=fixture,
            observations=list(queries.values()),
            checks=checks,
        )
        if profile.admit(local_case):
            raise JournalError("profile_refused", f"Repair case {identifier} exceeds local profile authority")
        cases[identifier] = local_case
    return cases, limits


async def _repair(claim, inputs, *, journal, profile, agent_factory, environment):
    """Validate the assigned patch against a local simulator using immutable pinned cases."""
    from .agent_repair import (
        HeldoutExposureLedger,
        PatchProposal,
        RepairPolicy,
        UnitTestCommand,
        validate_repair,
    )
    from .repair import RepairProposal as SimulatorProposal
    from .repair import validate_simulator_repair

    approved = _job_inputs("repair_retest", inputs)
    repair, source_spec, diff, bundle = _repair_context(claim, journal, profile, approved)
    _, source, _, _ = _source(claim, approved.model_dump(mode="json"))
    if environment is None:
        raise JournalError(
            "repair_environment_unavailable", "A guarded local simulator is required for repair validation"
        )
    if approved.repair_kind != repair.layer:
        raise JournalError(
            "repair_assignment_mismatch", "Locally approved repair layer differs from the assigned repair"
        )
    try:
        if repair.layer == "agent":
            case_map, suite_limits = _repair_cases(claim, profile, approved)
            command = UnitTestCommand.model_validate(approved.unit_test_command)
            if approved.approved_unit_test_digest != command.digest:
                raise JournalError("command_not_approved", "Unit-test command differs from local approval")
            proposal = PatchProposal(
                finding_id=str(repair.finding_id),
                layer="agent",
                origin="manual_upload",
                diff=diff,
                rationale=approved.rationale,
            )
            claim_limits = claim.execution_manifest.limits
            output_limit = min(profile.max_output_bytes, claim_limits.max_output_bytes)
            harness = _RepairHarness(
                case_map,
                parent_journal=journal,
                environment=environment,
                profile=profile,
                limits=suite_limits,
                agent_argv=approved.agent_argv,
                agent_env=approved.agent_env,
                byte_limit=output_limit,
            )
            harness.reproduction_case_id = approved.reproduction_case_id
            harness.control_case_ids = approved.control_case_ids
            harness.heldout_case_ids = approved.heldout_case_ids
            harness.split_digest = content_hash(
                {
                    "heldout": [
                        (
                            case_id,
                            next(
                                value.content_hash
                                for value in claim.execution_manifest.versions
                                if str(value.id) == case_id
                            ),
                        )
                        for case_id in approved.heldout_case_ids
                    ]
                }
            )
            heldout = harness
            work_root = Path(str(journal.path) + ".repair-work")
            if work_root.exists():
                raise JournalError("snapshot_exists", "A previous repair worktree requires operator reconciliation")
            work_root.mkdir(mode=0o700)
            try:
                await harness.start()
                ledger = HeldoutExposureLedger(path=Path(str(journal.path) + ".heldout.json"))
                simulator_pin = _simulator_pin(claim)
                validation = await validate_repair(
                    source,
                    proposal,
                    harness=harness,
                    heldout=heldout,
                    ledger=ledger,
                    unit_tests=command,
                    unit_tests_approved_digest=approved.approved_unit_test_digest,
                    simulator_digest=simulator_pin,
                    failing_check_ids=approved.failing_check_ids,
                    policy=RepairPolicy(),
                    parent=work_root,
                    cancellation=lambda: journal.cancel.requested or journal.lost_authority,
                )
            finally:
                await harness.close()
                remove_owned_tree(work_root)
            checks = {check.name: check.passed for check in validation.checks}
            summary = RepairResult(
                job_kind="repair_retest",
                source_commit=validation.base_commit,
                patch_artifact_id=repair.patch_artifact_id,
                base_commit=repair.base_commit,
                validation_passed=validation.outcome == "accepted",
                heldout_passed=checks.get("heldout_matches", False),
                controls_passed=checks.get("valid_controls_preserved", False),
                criteria_unchanged=checks.get("criteria_not_weakened", False),
            )
            verdict = (
                "PASS"
                if validation.outcome == "accepted"
                else "FAIL"
                if validation.outcome == "rejected"
                else "UNKNOWN"
            )
            return [("agent_repair", validation)], verdict, summary

        if repair.layer == "simulator":
            if not environment or not hasattr(environment, "_client"):
                raise JournalError("repair_environment_unavailable", "A guarded simulator transport is required")
            from .fidelity import ActionPlan
            from .reduce import ReductionResult
            from .platform_jobs import _contract

            contract = _contract(claim, approved.model_dump(mode="json"))
            proposal = SimulatorProposal.model_validate(approved.proposal)
            reduction = ReductionResult.model_validate(approved.reduction)
            validation = await validate_simulator_repair(
                contract,
                proposal,
                reduction,
                lambda result: any(
                    candidate.category == approved.violation_category for candidate in result.candidates
                ),
                controls=[ActionPlan.model_validate(value) for value in approved.controls],
                heldout=[ActionPlan.model_validate(value) for value in approved.heldout],
                sim_env=environment,
                ref_env=environment,
            )
            checks = {check.name: check.passed for check in validation.checks}
            summary = RepairResult(
                job_kind="repair_retest",
                source_commit=source_spec.commit,
                patch_artifact_id=repair.patch_artifact_id,
                base_commit=repair.base_commit,
                validation_passed=validation.outcome == "accepted",
                heldout_passed=checks.get("heldout_matches", False),
                controls_passed=checks.get("valid_controls_preserved", False),
                criteria_unchanged=checks.get("criteria_unchanged", False),
            )
            verdict = (
                "PASS"
                if validation.outcome == "accepted"
                else "FAIL"
                if validation.outcome == "rejected"
                else "UNKNOWN"
            )
            return [("simulator_repair", validation)], verdict, summary
    except (ValidationError, ValueError, SourceError) as exc:
        raise JournalError("repair_inputs_invalid", str(exc)) from exc
    raise JournalError("repair_kind_invalid", "repair_kind must be agent or simulator")


def _simulator_pin(claim):
    values = claim.execution_manifest.versions
    environment = next((value for value in values if value.kind == "environment"), None)
    if environment is None:
        raise JournalError("repair_environment_unavailable", "Repair requires an immutable simulator environment")
    fixture = next((value for value in values if value.id == environment.data.fixture_version_id), None)
    if fixture is None:
        raise JournalError("repair_environment_unavailable", "Pinned simulator fixture is missing")
    return content_hash(
        {
            "environment": environment.model_dump(mode="json"),
            "fixture": fixture.model_dump(mode="json"),
        }
    )
