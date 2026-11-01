"""I08 CLI and OCI container agents over the JSONL protocol [Spec §4, §9, §19; T21].

Both adapters reuse ``susanoo_sdk.jsonl.JsonlAgentProcess``: frames, size bounds, deadlines, process
groups and the sanitized environment are the same as for a Python or Node agent.

``CliAgentAdapter`` runs an approved argument vector. It is never a shell string, never ``shell=True``
and never a command taken from a scenario: shells are refused as the executable.

``ContainerAgentAdapter`` runs an image pinned by digest through the docker CLI with no network by
default, a read-only root, every capability dropped, no-new-privileges, pid/memory/cpu limits, a
non-root user, a bounded tmpfs, no host mounts except one explicit read-only artifact directory and
environment only from an explicit allowlist (values travel in the CLI's environment, never argv).
The container is always removed at teardown, and crash evidence (exit code, redacted stderr tail)
is kept. A container is not a claim of perfect hostile-code isolation (Spec §9).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import uuid
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from susanoo_sdk import (
    AdapterError,
    AgentReply,
    CancelResult,
    CapabilityReport,
    InvocationContext,
    Message,
    SessionHandle,
)
from susanoo_sdk.jsonl import BASE_ENV_KEYS, FrameLimits, JsonlAgentProcess

REDACTED = "[REDACTED]"
SECRET_PATTERNS = (
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 [REDACTED]"),
    (
        re.compile(r"(?i)\b([A-Za-z0-9_]*(?:token|secret|password|passwd|api[_-]?key))\s*[=:]\s*[^\s,;\"']{4,}"),
        r"\1=[REDACTED]",
    ),
)
SHELLS = frozenset(
    {"sh", "bash", "zsh", "dash", "ksh", "mksh", "fish", "csh", "tcsh", "ash", "busybox", "env", "xargs"}
    | {"cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe"}
)
# The docker CLI needs to find its daemon; these configure the client, never the container.
DOCKER_CLIENT_ENV = ("HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY")
RESERVED_ENV = frozenset(BASE_ENV_KEYS) | frozenset(DOCKER_CLIENT_ENV) | {"PYTHONUNBUFFERED"}
ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
NAME_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
IMAGE_NAME = re.compile(rf"^(?:[a-z0-9.-]+(?::[0-9]{{1,5}})?/)?{NAME_COMPONENT}(?:/{NAME_COMPONENT})*$")
DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
NETWORK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}
DENIED_MOUNT_ROOTS = ("/proc", "/sys", "/dev", "/var/run", "/run", "/etc", "/private/var/run")


def redact(text: str, secrets: Sequence[str] = ()) -> str:
    """Mask known secret values and credential-shaped substrings in diagnostics."""
    for value in sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True):
        text = text.replace(value, REDACTED)
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def validate_image_ref(image: str) -> str:
    """Accept only content-addressed images: ``name@sha256:<64 hex>`` or a local ``sha256:<64 hex>`` image ID."""
    if DIGEST.fullmatch(image):
        return image
    name, at, digest = image.partition("@")
    if not at:
        raise ValueError(f"Image {image!r} is not pinned by digest; use name@sha256:<digest>")
    if not DIGEST.fullmatch(digest):
        raise ValueError("Image digest must be sha256:<64 lowercase hex>")
    if not IMAGE_NAME.fullmatch(name):
        raise ValueError(f"Image name {name!r} is invalid or carries a tag; pin by digest only")
    return image


class CrashEvidence(BaseModel):
    """What survives a crashed or misbehaving agent process. The stderr tail is already redacted."""

    model_config = ConfigDict(extra="forbid")

    adapter: Literal["cli", "container"]
    exit_code: int | None
    error_code: str | None = None
    stderr_tail: str = ""
    container_name: str | None = None
    note: str | None = None


class _Supervised:
    """Shared JSONL delegation with secret tracking, redacted crash evidence and error enrichment."""

    kind: Literal["cli", "container"]

    def __init__(self, process: JsonlAgentProcess, secrets: Sequence[str]):
        self._process = process
        self._secrets: set[str] = {s for s in secrets if s}
        self.evidence: CrashEvidence | None = None

    def _track(self, context: InvocationContext) -> InvocationContext:
        if context.tool_route is not None:
            self._secrets.add(context.tool_route.token.get_secret_value())
        return context

    def stderr_tail(self) -> str:
        return redact(bytes(self._process.stderr_tail).decode(errors="replace"), sorted(self._secrets))

    def _exit_code(self) -> int | None:
        proc = getattr(self._process, "_proc", None)
        return None if proc is None else proc.returncode

    def _record(self, error: AdapterError | None, note: str | None = None) -> None:
        self.evidence = CrashEvidence(
            adapter=self.kind,
            exit_code=self._exit_code(),
            error_code=error.code if error else None,
            stderr_tail=self.stderr_tail()[-4096:],
            container_name=getattr(self, "container_name", None),
            note=note,
        )

    async def _call(self, coro):
        try:
            return await coro
        except AdapterError as error:
            proc = getattr(self._process, "_proc", None)
            if proc is not None and proc.returncode is None and getattr(self._process, "_broken", None):
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(proc.wait(), 2)  # the bridge killed it; collect the exit code
            self._record(error)
            exit_code = self._exit_code()
            suffix = f" (exit code {exit_code})" if exit_code is not None else ""
            raise AdapterError(error.code, redact(str(error), sorted(self._secrets)) + suffix) from None

    async def preflight(self) -> CapabilityReport:
        report = await self._call(self._process.preflight())
        return report.model_copy(update={"adapter": self.kind, "notes": [*report.notes, *self._notes()]})

    def _notes(self) -> list[str]:
        return []

    async def start_session(self, context: InvocationContext) -> SessionHandle:
        return await self._call(self._process.start_session(self._track(self._route(context))))

    async def invoke(self, session: SessionHandle, message: Message, context: InvocationContext) -> AgentReply:
        return await self._call(self._process.invoke(session, message, self._track(self._route(context))))

    async def cancel(self, invocation_id: str) -> CancelResult:
        return await self._call(self._process.cancel(invocation_id))

    async def close_session(self, session: SessionHandle) -> None:
        await self._call(self._process.close_session(session))

    def _route(self, context: InvocationContext) -> InvocationContext:
        return context

    async def aclose(self) -> None:
        await self._process.aclose()
        if self.evidence is None:
            exit_code = self._exit_code()
            if exit_code not in (None, 0):
                self._record(None, "process ended with a non-zero exit code")


class CliProfile(BaseModel):
    """Locally approved command. ``argv[0]`` is resolved to an absolute executable at construction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    argv: list[str] = Field(min_length=1, max_length=64)
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict, description="Explicit values; nothing inherited")
    limits: FrameLimits = Field(default_factory=FrameLimits)

    @field_validator("argv", mode="before")
    @classmethod
    def vector_only(cls, value: object) -> object:
        if isinstance(value, str):
            raise ValueError("argv must be an argument vector, never a shell string")
        return value

    @model_validator(mode="after")
    def approved(self) -> CliProfile:
        if not all(isinstance(part, str) and part and "\x00" not in part for part in self.argv):
            raise ValueError("argv parts must be non-empty strings")
        program = PurePosixPath(self.argv[0].replace("\\", "/")).name.lower()
        if program in SHELLS:
            raise ValueError(f"{program} is a shell or command launcher; give the agent's own executable")
        for name in self.env:
            if not ENV_NAME.fullmatch(name):
                raise ValueError(f"Invalid environment variable name {name!r}")
        return self


class CliAgentAdapter(_Supervised):
    """``AgentAdapter`` over an approved executable speaking JSONL (I08 CLI)."""

    kind = "cli"

    def __init__(self, profile: CliProfile, *, name: str = "cli"):
        executable = shutil.which(profile.argv[0]) if not os.path.isabs(profile.argv[0]) else profile.argv[0]
        if not executable or not os.access(executable, os.X_OK):
            raise ValueError(f"Executable {profile.argv[0]!r} not found or not executable")
        self.profile = profile
        argv = [executable, *profile.argv[1:]]
        super().__init__(
            JsonlAgentProcess(argv, env=dict(profile.env), cwd=profile.cwd, limits=profile.limits, name=name),
            list(profile.env.values()),
        )


class ContainerProfile(BaseModel):
    """Locally approved container runtime profile. Remote jobs cannot widen it (Spec §10)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    image: str
    command: list[str] = Field(default_factory=list, max_length=64, description="Arguments after the image")
    network: str = Field(default="none", description="'none', or an explicitly approved user network")
    user: str = Field(default="65532:65532", pattern=r"^[0-9]{1,10}(?::[0-9]{1,10})?$")
    memory_mb: int = Field(default=512, ge=16, le=65536)
    cpus: float = Field(default=1.0, gt=0, le=64)
    pids_limit: int = Field(default=128, ge=8, le=4096)
    tmpfs_mb: int = Field(default=16, ge=1, le=1024)
    artifact_dir: str | None = Field(default=None, description="Mounted read-only at /artifact")
    env_allowlist: list[str] = Field(default_factory=list, max_length=64)
    workdir: str | None = Field(default=None, pattern=r"^/[A-Za-z0-9._/-]*$")
    tool_route_host: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9.-]+$",
        description="Rewrite a loopback tool-route host (e.g. host.docker.internal) for a bridged container",
    )
    health_timeout_seconds: float = Field(default=60, gt=0, le=600)
    docker_bin: str = "docker"
    limits: FrameLimits = Field(default_factory=FrameLimits)

    @field_validator("image")
    @classmethod
    def pinned(cls, value: str) -> str:
        return validate_image_ref(value)

    @field_validator("network")
    @classmethod
    def network_policy(cls, value: str) -> str:
        if value in {"host"} or value.startswith("container:") or not NETWORK.fullmatch(value):
            raise ValueError("Network must be 'none' or a named network; host and container: modes are refused")
        return value

    @field_validator("user")
    @classmethod
    def non_root(cls, value: str) -> str:
        if value.split(":")[0] == "0" or value.split(":")[-1] == "0":
            raise ValueError("Containers run as a non-root user and group")
        return value

    @field_validator("env_allowlist")
    @classmethod
    def env_names(cls, value: list[str]) -> list[str]:
        for name in value:
            if not ENV_NAME.fullmatch(name) or name in RESERVED_ENV or name.startswith("DOCKER_"):
                raise ValueError(f"Environment name {name!r} is invalid or reserved for the docker client")
        return value

    @field_validator("artifact_dir")
    @classmethod
    def artifact_mount(cls, value: str | None) -> str | None:
        if value is None:
            return None
        path = Path(value)
        if not path.is_absolute():
            raise ValueError("artifact_dir must be absolute")
        resolved = path.resolve()
        if not resolved.is_dir():
            raise ValueError("artifact_dir must be an existing directory")
        text = resolved.as_posix()
        if (
            text == "/"
            or resolved == Path.home().resolve()
            or any(text == root or text.startswith(root + "/") for root in DENIED_MOUNT_ROOTS)
        ):
            raise ValueError("artifact_dir may not be the root, the home directory or a system path")
        if "," in text or "=" in text:
            raise ValueError("artifact_dir may not contain ',' or '='")
        return text


def docker_run_argv(profile: ContainerProfile, container_name: str) -> list[str]:
    """The exact ``docker run`` argument vector. Env values are never in it: ``-e NAME`` reads the CLI env."""
    argv = [
        profile.docker_bin,
        "run",
        "--rm",
        "-i",
        "--name",
        container_name,
        "--pull",
        "never",
        "--network",
        profile.network,
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        str(profile.pids_limit),
        "--memory",
        f"{profile.memory_mb}m",
        "--memory-swap",
        f"{profile.memory_mb}m",
        "--cpus",
        f"{profile.cpus:g}",
        "--user",
        profile.user,
        "--tmpfs",
        f"/tmp:rw,noexec,nosuid,nodev,size={profile.tmpfs_mb}m",
        "--log-driver",
        "none",
        "--label",
        "susanoo.adapter=container",
    ]
    if profile.artifact_dir:
        argv += ["--mount", f"type=bind,source={profile.artifact_dir},target=/artifact,readonly"]
    for name in profile.env_allowlist:
        argv += ["--env", name]
    if profile.workdir:
        argv += ["--workdir", profile.workdir]
    return [*argv, profile.image, *profile.command]


class ContainerAgentAdapter(_Supervised):
    """``AgentAdapter`` over a digest-pinned OCI image speaking JSONL on stdin/stdout (I08 container)."""

    kind = "container"

    def __init__(self, profile: ContainerProfile, *, env: dict[str, str] | None = None, name: str = "container"):
        env = dict(env or {})
        unknown = sorted(set(env) - set(profile.env_allowlist))
        if unknown:
            raise ValueError(f"Environment {unknown} is not in the approved allowlist")
        missing = sorted(set(profile.env_allowlist) - set(env))
        if missing:
            raise ValueError(f"Approved environment {missing} has no value; host values are never inherited")
        self.profile = profile
        self.container_name = f"susanoo-agent-{uuid.uuid4().hex[:16]}"
        client_env = {key: os.environ[key] for key in DOCKER_CLIENT_ENV if key in os.environ}
        self.argv = docker_run_argv(profile, self.container_name)
        super().__init__(
            JsonlAgentProcess(self.argv, env={**client_env, **env}, limits=profile.limits, name=name),
            list(env.values()),
        )
        self._healthy = False
        self.removed: bool | None = None

    def _notes(self) -> list[str]:
        return [
            f"image {self.profile.image}",
            f"network {self.profile.network}",
            "read-only root, all capabilities dropped, no-new-privileges, non-root user",
            "container isolation is not a claim of hostile-code isolation",
        ]

    async def _docker(self, *args: str, timeout: float = 30) -> tuple[int, str]:
        client_env = {key: os.environ[key] for key in (*BASE_ENV_KEYS, *DOCKER_CLIENT_ENV) if key in os.environ}
        proc = await asyncio.create_subprocess_exec(
            self.profile.docker_bin,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=client_env,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            return -1, "timed out"
        return proc.returncode or 0, out.decode(errors="replace")[-2000:]

    async def health_check(self) -> CapabilityReport:
        """Image present locally (never pulled implicitly), then a protocol preflight within the health deadline."""
        if shutil.which(self.profile.docker_bin) is None:
            raise AdapterError("runtime_unavailable", "The docker CLI is not installed")
        code, output = await self._docker("image", "inspect", "--format", "{{.Id}}", self.profile.image)
        if code != 0:
            self._record(AdapterError("image_missing", output), "image not present locally")
            raise AdapterError("image_missing", f"Image {self.profile.image} is not available locally")
        try:
            report = await asyncio.wait_for(super().preflight(), self.profile.health_timeout_seconds)
        except TimeoutError:
            self._record(AdapterError("health_check_failed", "timeout"))
            raise AdapterError("health_check_failed", "Container did not answer preflight in time") from None
        except AdapterError as error:
            raise AdapterError("health_check_failed", str(error)) from None
        self._healthy = True
        return report.model_copy(update={"checks": {**report.checks, "image_present": True, "preflight": True}})

    async def preflight(self) -> CapabilityReport:
        return await self.health_check()

    async def start_session(self, context: InvocationContext) -> SessionHandle:
        if not self._healthy:
            await self.health_check()
        return await super().start_session(context)

    def _route(self, context: InvocationContext) -> InvocationContext:
        route = context.tool_route
        host = self.profile.tool_route_host
        if route is None or host is None:
            return context
        parts = urlsplit(route.base_url)
        if (parts.hostname or "") not in LOOPBACK:
            return context
        netloc = host if parts.port is None else f"{host}:{parts.port}"
        rewritten = route.model_copy(update={"base_url": urlunsplit(parts._replace(netloc=netloc))})
        return context.model_copy(update={"tool_route": rewritten})

    async def aclose(self) -> None:
        """Stop the CLI process group, then always remove the container by name."""
        try:
            await super().aclose()
        finally:
            await self._remove()

    async def _remove(self, attempts: int = 50) -> None:
        # ``--rm`` may already be removing it, in which case ``rm -f`` returns early: wait until it is gone.
        for _ in range(attempts):
            await self._docker("rm", "-f", self.container_name, timeout=30)
            code, out = await self._docker("ps", "-aq", "--filter", f"name=^/{self.container_name}$", timeout=30)
            if code == 0 and not out.strip():
                self.removed = True
                return
            await asyncio.sleep(0.2)
        self.removed = False
        if self.evidence is None:
            self._record(None, "container could not be confirmed removed")
