"""Level 2 sandbox: build and run the generated app in a locked-down container. [PRD › Testing environments]

[PRD › Deployment › Sandbox limits]: per-container CPU and memory caps, no host volume mounts, auto-stop after
an idle timeout. [PRD › Connectors and API budget › Credential handling]: connector credentials reach the app
only as container env vars, passed in the create call, never written to a file and never logged (keys only).

Generated code never runs on the host. `build_image` runs the Dockerfile inside the Docker daemon; nothing here
imports, execs or `docker exec`s the generated app.

The `docker` SDK is imported lazily; every function takes an injectable client (`DockerClient` protocol) so
unit tests need no daemon. The functions are blocking: call them from async code with `asyncio.to_thread`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

NETWORK_ENV = "SANDBOX_NETWORK"
DEFAULT_NETWORK = "susanoo_sandbox"  # dedicated; NOT the api/worker compose network
PREVIEW_DOMAIN_ENV = "PREVIEW_DOMAIN"
DEFAULT_PREVIEW_DOMAIN = "susanoo.jugaadchhabra.dev"

DEFAULT_CPU = 1.0
DEFAULT_MEM = "512m"
DEFAULT_IDLE_TIMEOUT_S = 900
DEFAULT_PORT = 8000
PIDS_LIMIT = 256
TMPFS = {"/tmp": "rw,noexec,nosuid,size=64m"}
STOP_GRACE_S = 5

# PROPOSED (pending Jugaad): label convention for Caddy `run-<id>` routing. Not yet in docs/integration-points.md.
# Containers join the dedicated SANDBOX_NETWORK (default `susanoo_sandbox`, created here if missing, not internal).
# Caddy must also join it to reach `susanoo-run-<id>:<port>`. Jugaad should restrict that network's egress/ICC.
LABEL_MANAGED = "susanoo.managed"  # "true" on every sandbox container; reap_idle only touches these
LABEL_RUN_ID = "susanoo.run_id"
LABEL_PREVIEW_HOST = "susanoo.preview_host"  # run-<run_id>.<PREVIEW_DOMAIN>
LABEL_PORT = "susanoo.port"  # the port inside the container that Caddy proxies to
LABEL_EXPIRES_AT = "susanoo.expires_at"  # ISO-8601 UTC; initial idle deadline

_RUN_ID = re.compile(r"^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$")  # [a-z0-9-]{1,40}, no leading/trailing hyphen
_MEM = re.compile(r"^[1-9][0-9]*[kmg]$")
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DOMAIN = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")
_TAG = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,100}(:[A-Za-z0-9_][A-Za-z0-9._-]{0,127})?$")


# Image labels are merged into the container's labels by Docker, so these keys could hijack Caddy routing or
# our reaper bookkeeping. An image declaring any of them is refused.
FORBIDDEN_LABEL_PREFIXES = ("susanoo.", "caddy")


class SandboxError(Exception):
    """The sandbox could not build or run the app; the message is plain English and holds no env values."""


class DockerClient(Protocol):
    """The slice of `docker.DockerClient` used here; tests pass a fake."""

    @property
    def images(self) -> Any: ...

    @property
    def containers(self) -> Any: ...

    @property
    def networks(self) -> Any: ...


@dataclass(frozen=True)
class AppHandle:
    run_id: str
    url: str  # public preview URL via Caddy
    container_id: str
    preview_host: str
    internal_url: str  # reachable on the SANDBOX_NETWORK (for Level 2 test calls from the worker)
    expires_at: datetime


def validate_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not _RUN_ID.match(run_id):
        raise ValueError("run_id must be 1-40 lowercase letters, digits or hyphens (not starting or ending with '-')")
    return run_id


def container_name(run_id: str) -> str:
    return f"susanoo-run-{validate_run_id(run_id)}"  # PROPOSED (pending Jugaad)


def preview_domain() -> str:
    domain = os.environ.get(PREVIEW_DOMAIN_ENV, "").strip().lower() or DEFAULT_PREVIEW_DOMAIN
    if not _DOMAIN.match(domain):
        raise ValueError(f"{PREVIEW_DOMAIN_ENV} is not a valid domain name")
    return domain


def _validate_env(env: Mapping[str, str]) -> dict[str, str]:
    clean: dict[str, str] = {}
    for key, value in env.items():
        if not isinstance(key, str) or not _ENV_KEY.match(key):
            raise ValueError("environment variable names must be letters, digits and underscores")
        if not isinstance(value, str) or "\x00" in value:
            raise ValueError(f"environment variable {key} must be a string without NUL bytes")
        clean[key] = value
    return clean


def container_config(
    run_id: str,
    image: str,
    *,
    env: Mapping[str, str],
    cpu: float = DEFAULT_CPU,
    mem: str = DEFAULT_MEM,
    idle_timeout_s: int = DEFAULT_IDLE_TIMEOUT_S,
    port: int = DEFAULT_PORT,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The full `containers.create` kwargs. Deliberately has no volumes, binds, mounts, ports or privileges."""
    name = container_name(run_id)
    if not _TAG.match(image):
        raise ValueError("image must be a plain image name or tag")
    if not (0 < cpu <= 4):
        raise ValueError("cpu must be above 0 and at most 4")
    if not _MEM.match(mem):
        raise ValueError("mem must look like 512m or 1g")
    if idle_timeout_s <= 0:
        raise ValueError("idle_timeout_s must be positive")
    if not (1 <= port <= 65535):
        raise ValueError("port must be between 1 and 65535")

    now = now or datetime.now(UTC)
    expires_at = (now + timedelta(seconds=idle_timeout_s)).isoformat()
    # PROPOSED (pending Jugaad): Caddy reads these labels to route run-<id>.<PREVIEW_DOMAIN> to this container.
    labels = {
        LABEL_MANAGED: "true",
        LABEL_RUN_ID: run_id,
        LABEL_PREVIEW_HOST: f"run-{run_id}.{preview_domain()}",
        LABEL_PORT: str(port),
        LABEL_EXPIRES_AT: expires_at,
    }
    return {
        "image": image,
        "name": name,
        "labels": labels,
        # Our PORT goes last so credentials/env can never repoint the app away from the labelled port.
        "environment": {**_validate_env(env), "PORT": str(port)},
        "network": os.environ.get(NETWORK_ENV) or DEFAULT_NETWORK,
        "mem_limit": mem,
        "memswap_limit": mem,  # no swap
        "nano_cpus": int(cpu * 1_000_000_000),
        "pids_limit": PIDS_LIMIT,
        "privileged": False,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "read_only": True,
        "tmpfs": dict(TMPFS),
        "restart_policy": {"Name": "no"},
        "detach": True,
    }


@dataclass
class _Tracked:
    handle: AppHandle
    idle_timeout_s: int
    last_activity: datetime


class Sandbox:
    """Builds images and runs/reaps sandbox containers through an injectable Docker client."""

    def __init__(self, client: DockerClient) -> None:
        self.client = client
        self._apps: dict[str, _Tracked] = {}
        self._networks_ready: set[str] = set()

    def build_image(self, repo_path: str | Path, tag: str) -> str:
        """Build the generated repo's Dockerfile inside the Docker daemon. Returns the tag."""
        if not _TAG.match(tag):
            raise ValueError("tag must be a plain image name or tag")
        path = Path(repo_path)
        if not (path / "Dockerfile").is_file():
            raise SandboxError("The generated app has no Dockerfile, so it cannot be built.")
        try:
            self.client.images.build(path=str(path), tag=tag, rm=True, forcerm=True)
        except Exception as exc:
            raise SandboxError(f"Building the app image failed: {type(exc).__name__}") from exc
        try:
            self._check_image(tag)
        except SandboxError:
            self._remove_image(tag)  # never leave a refused image around
            raise
        return tag

    def start_app(
        self,
        run_id: str,
        image: str,
        *,
        env: Mapping[str, str],
        cpu: float = DEFAULT_CPU,
        mem: str = DEFAULT_MEM,
        idle_timeout_s: int = DEFAULT_IDLE_TIMEOUT_S,
        port: int = DEFAULT_PORT,
        now: datetime | None = None,
    ) -> AppHandle:
        """Create and start the app container. `env` is the already-built connector env (never fetched here)."""
        now = now or datetime.now(UTC)
        config = container_config(
            run_id, image, env=env, cpu=cpu, mem=mem, idle_timeout_s=idle_timeout_s, port=port, now=now
        )
        name = config["name"]
        self._check_image(image)  # also covers images we did not build
        self._ensure_network(config["network"])
        self._remove_named(name)  # a stale container from an earlier attempt of this run
        log.info("Starting sandbox %s (env keys: %s)", name, sorted(config["environment"]))
        container = None
        try:
            container = self.client.containers.create(**config)  # env goes in here and nowhere else
            self._check_no_mounts(container)  # last line of defence: no volume of any kind may be attached
            container.start()
        except SandboxError:
            if container is not None:
                self._force_remove(container)
            raise
        except Exception as exc:
            if container is not None:
                self._force_remove(container)
            raise SandboxError(f"Starting the app container failed: {type(exc).__name__}") from exc

        host = config["labels"][LABEL_PREVIEW_HOST]
        handle = AppHandle(
            run_id=run_id,
            url=f"https://{host}",
            container_id=container.id,
            preview_host=host,
            internal_url=f"http://{name}:{port}",
            expires_at=now + timedelta(seconds=idle_timeout_s),
        )
        self._apps[run_id] = _Tracked(handle, idle_timeout_s, now)
        return handle

    def touch(self, run_id: str, now: datetime | None = None) -> None:
        """Record activity on a run's app so its idle clock restarts."""
        tracked = self._apps.get(run_id)
        if tracked is not None:
            tracked.last_activity = now or datetime.now(UTC)

    def stop_app(self, handle: AppHandle) -> None:
        self._apps.pop(handle.run_id, None)
        try:
            container = self.client.containers.get(handle.container_id)
        except Exception as exc:
            if type(exc).__name__ == "NotFound":
                return  # already gone
            raise SandboxError(f"Could not find the app container: {type(exc).__name__}") from exc
        self._force_remove(container)

    def reap_idle(self, now: datetime | None = None) -> list[str]:
        """Stop every sandbox container idle past its timeout. Returns the run ids reaped."""
        now = now or datetime.now(UTC)
        reaped: list[str] = []
        for container in self.client.containers.list(all=True, filters={"label": f"{LABEL_MANAGED}=true"}):
            labels = container.labels or {}
            run_id = labels.get(LABEL_RUN_ID, "")
            tracked = self._apps.get(run_id)
            if tracked is not None:
                deadline = tracked.last_activity + timedelta(seconds=tracked.idle_timeout_s)
            else:  # started by an earlier process: only the label deadline is known
                try:
                    deadline = datetime.fromisoformat(labels[LABEL_EXPIRES_AT])
                except (KeyError, ValueError):
                    continue
                if deadline.tzinfo is None:
                    deadline = deadline.replace(tzinfo=UTC)
            if now >= deadline:
                self._force_remove(container)
                self._apps.pop(run_id, None)
                reaped.append(run_id)
        return reaped

    # -- helpers

    def _check_image(self, tag: str) -> None:
        """Refuse images that declare volumes (anonymous host volumes bypass read_only) or hijack-able labels."""
        try:
            config = self.client.images.get(tag).attrs.get("Config") or {}
        except Exception as exc:
            raise SandboxError(f"Could not inspect the app image: {type(exc).__name__}") from exc
        if config.get("Volumes"):
            raise SandboxError(
                "The app image declares a data volume (VOLUME in its Dockerfile). Sandbox apps may not use "
                "volumes, so the image was refused."
            )
        for key in config.get("Labels") or {}:
            if str(key).lower().startswith(FORBIDDEN_LABEL_PREFIXES):
                raise SandboxError(
                    "The app image sets a routing label that only the sandbox may set, so the image was refused."
                )

    def _check_no_mounts(self, container: Any) -> None:
        try:
            container.reload()
        except Exception as exc:
            raise SandboxError(f"Could not inspect the app container: {type(exc).__name__}") from exc
        if (getattr(container, "attrs", None) or {}).get("Mounts"):
            raise SandboxError("The app container would have a volume mounted, so it was not started.")

    def _remove_image(self, tag: str) -> None:
        try:
            self.client.images.remove(tag, force=True)
        except Exception as exc:
            log.warning("Could not remove refused image %s (%s)", tag, type(exc).__name__)

    def _ensure_network(self, name: str) -> None:
        if name in self._networks_ready:
            return
        try:
            try:
                self.client.networks.get(name)
            except Exception as exc:
                if type(exc).__name__ != "NotFound":
                    raise
                try:
                    self.client.networks.create(name, driver="bridge", internal=False, labels={LABEL_MANAGED: "true"})
                except Exception:
                    self.client.networks.get(name)  # lost a creation race: fine if it exists now
        except Exception as exc:
            raise SandboxError(f"Could not set up the sandbox network: {type(exc).__name__}") from exc
        self._networks_ready.add(name)

    def _remove_named(self, name: str) -> None:
        try:
            self._force_remove(self.client.containers.get(name))
        except Exception as exc:
            if type(exc).__name__ != "NotFound":
                raise SandboxError(f"Could not check for an old app container: {type(exc).__name__}") from exc

    def _force_remove(self, container: Any) -> None:
        try:
            container.stop(timeout=STOP_GRACE_S)
        except Exception as exc:  # already stopped is fine; remove below is the real guarantee
            log.debug("stop before remove raised %s", type(exc).__name__)
        try:
            container.remove(v=True, force=True)  # v=True: drop any anonymous volume too
        except Exception as exc:
            if type(exc).__name__ != "NotFound":
                raise SandboxError(f"Could not remove the app container: {type(exc).__name__}") from exc


# --------------------------------------------------------------------------- module-level API (default client)

_default: Sandbox | None = None


def _default_sandbox() -> Sandbox:
    global _default
    if _default is None:
        try:
            import docker
        except ImportError:
            raise SandboxError(
                "The 'docker' Python package is not installed, so Level 2 runs are unavailable."
            ) from None
        try:
            _default = Sandbox(docker.from_env())
        except Exception as exc:
            raise SandboxError(f"Could not reach the Docker daemon: {type(exc).__name__}") from exc
    return _default


def build_image(repo_path: str | Path, tag: str) -> str:
    return _default_sandbox().build_image(repo_path, tag)


def start_app(
    run_id: str,
    image: str,
    *,
    env: Mapping[str, str],
    cpu: float = DEFAULT_CPU,
    mem: str = DEFAULT_MEM,
    idle_timeout_s: int = DEFAULT_IDLE_TIMEOUT_S,
    port: int = DEFAULT_PORT,
) -> AppHandle:
    return _default_sandbox().start_app(
        run_id, image, env=env, cpu=cpu, mem=mem, idle_timeout_s=idle_timeout_s, port=port
    )


def stop_app(handle: AppHandle) -> None:
    _default_sandbox().stop_app(handle)


def reap_idle(now: datetime | None = None) -> list[str]:
    return _default_sandbox().reap_idle(now)


async def reap_forever(interval_s: float = 60.0, *, sandbox: Sandbox | None = None) -> None:
    """Run `reap_idle` every `interval_s` seconds until cancelled. The worker starts this as a background task."""
    while True:
        try:
            reaped = await asyncio.to_thread((sandbox or _default_sandbox()).reap_idle)
            if reaped:
                log.info("Reaped idle sandbox apps: %s", reaped)
        except Exception as exc:  # never let one failed tick end idle auto-stop
            log.error("Idle reaping failed: %s", type(exc).__name__)
        await asyncio.sleep(interval_s)
