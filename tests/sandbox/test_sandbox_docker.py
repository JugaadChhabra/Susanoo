"""Level 2 container sandbox, against a fake Docker client (no daemon needed). [PRD › Deployment › Sandbox limits]"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from susanoo.sandbox import docker as sb

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
SECRET = "ghp" + "_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


class NotFound(Exception):
    pass


class FakeContainer:
    def __init__(self, cid, kwargs, registry):
        self.id = cid
        self.kwargs = kwargs
        self.labels = kwargs["labels"]
        self.registry = registry
        self.started = self.stopped = self.removed = False
        self.fail_start = False
        self.attrs: dict = {"Mounts": []}
        self.remove_kwargs: dict = {}

    def reload(self):
        pass

    def start(self):
        if self.fail_start:
            raise RuntimeError("boom " + str(self.kwargs["environment"]))
        self.started = True

    def stop(self, timeout=10):
        self.stopped = True

    def remove(self, v=False, force=False):
        self.remove_kwargs = {"v": v, "force": force}
        self.removed = True
        self.registry.pop(self.kwargs["name"], None)


class FakeContainers:
    def __init__(self):
        self.by_name: dict[str, FakeContainer] = {}
        self.create_calls: list[dict] = []
        self.fail_start = False
        self.mounts: list = []
        self.n = 0

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        self.n += 1
        c = FakeContainer(f"cid{self.n}", kwargs, self.by_name)
        c.fail_start = self.fail_start
        c.attrs = {"Mounts": list(self.mounts)}
        self.by_name[kwargs["name"]] = c
        return c

    def get(self, ident):
        for c in self.by_name.values():
            if ident in (c.id, c.kwargs["name"]):
                return c
        raise NotFound(ident)

    def list(self, all=False, filters=None):
        key, _, val = filters["label"].partition("=")
        return [c for c in self.by_name.values() if c.labels.get(key) == val]


class FakeNetworks:
    def __init__(self):
        self.existing: set[str] = set()
        self.created: list[dict] = []

    def get(self, name):
        if name not in self.existing:
            raise NotFound(name)

    def create(self, name, **kwargs):
        self.created.append({"name": name, **kwargs})
        self.existing.add(name)


class FakeImages:
    def __init__(self):
        self.builds: list[dict] = []
        self.error: Exception | None = None
        self.config: dict = {}
        self.removed: list[str] = []

    def get(self, tag):
        return SimpleNamespace(attrs={"Config": self.config})

    def remove(self, tag, force=False):
        self.removed.append(tag)

    def build(self, **kwargs):
        if self.error:
            raise self.error
        self.builds.append(kwargs)
        return SimpleNamespace(id="img"), []


@pytest.fixture
def client():
    return SimpleNamespace(containers=FakeContainers(), images=FakeImages(), networks=FakeNetworks())


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv(sb.NETWORK_ENV, raising=False)
    monkeypatch.delenv(sb.PREVIEW_DOMAIN_ENV, raising=False)
    NotFound.__name__ = "NotFound"


def start(client, run_id="abc-123", env=None, **kw):
    return sb.Sandbox(client).start_app(run_id, "gen:abc", env=env or {"GITHUB_TOKEN": SECRET}, now=NOW, **kw)


# ----------------------------------------------------------------------------------- container config


def test_config_has_no_mounts_and_full_lockdown(client):
    start(client)
    cfg = client.containers.create_calls[0]
    for forbidden in ("volumes", "binds", "mounts", "ports", "devices", "volumes_from", "pid_mode", "ipc_mode"):
        assert forbidden not in cfg
    assert cfg["privileged"] is False
    assert cfg["cap_drop"] == ["ALL"]
    assert "cap_add" not in cfg
    assert cfg["security_opt"] == ["no-new-privileges:true"]
    assert cfg["read_only"] is True
    assert cfg["tmpfs"] == {"/tmp": "rw,noexec,nosuid,size=64m"}
    assert cfg["restart_policy"] == {"Name": "no"}
    assert cfg["pids_limit"] == sb.PIDS_LIMIT
    assert cfg["mem_limit"] == "512m" and cfg["memswap_limit"] == "512m"
    assert cfg["nano_cpus"] == 1_000_000_000
    assert cfg["detach"] is True


def test_config_custom_caps(client):
    start(client, cpu=0.5, mem="1g")
    cfg = client.containers.create_calls[0]
    assert cfg["nano_cpus"] == 500_000_000 and cfg["mem_limit"] == "1g"


@pytest.mark.parametrize(
    "kw",
    [{"cpu": 0}, {"cpu": 9}, {"mem": "lots"}, {"mem": "0m"}, {"idle_timeout_s": 0}, {"port": 0}, {"port": 70000}],
)
def test_config_rejects_bad_limits(client, kw):
    with pytest.raises(ValueError):
        start(client, **kw)
    assert client.containers.create_calls == []


def test_network_default_and_override(client, monkeypatch):
    start(client)
    assert client.containers.create_calls[0]["network"] == "susanoo_sandbox"
    monkeypatch.setenv(sb.NETWORK_ENV, "other_net")
    start(client, run_id="two")
    assert client.containers.create_calls[1]["network"] == "other_net"
    assert [n["name"] for n in client.networks.created] == ["susanoo_sandbox", "other_net"]


def test_sandbox_network_created_once_and_not_internal(client):
    s = sb.Sandbox(client)
    s.start_app("r1", "gen:abc", env={}, now=NOW)
    s.start_app("r2", "gen:abc", env={}, now=NOW)
    assert len(client.networks.created) == 1
    assert client.networks.created[0]["internal"] is False
    client.networks.existing.add("susanoo_sandbox")
    sb.Sandbox(client).start_app("r3", "gen:abc", env={}, now=NOW)
    assert len(client.networks.created) == 1  # already existed: not recreated


def test_labels_and_name_follow_proposed_convention(client):
    h = start(client)
    cfg = client.containers.create_calls[0]
    assert cfg["name"] == "susanoo-run-abc-123"
    assert cfg["labels"] == {
        "susanoo.managed": "true",
        "susanoo.run_id": "abc-123",
        "susanoo.preview_host": "run-abc-123.susanoo.jugaadchhabra.dev",
        "susanoo.port": "8000",
        "susanoo.expires_at": (NOW + timedelta(seconds=900)).isoformat(),
    }
    assert h.preview_host == "run-abc-123.susanoo.jugaadchhabra.dev"
    assert h.url == "https://run-abc-123.susanoo.jugaadchhabra.dev"
    assert h.internal_url == "http://susanoo-run-abc-123:8000"
    assert h.container_id == "cid1"


def test_preview_domain_from_env(client, monkeypatch):
    monkeypatch.setenv(sb.PREVIEW_DOMAIN_ENV, "Preview.Example.com")
    assert start(client).preview_host == "run-abc-123.preview.example.com"
    monkeypatch.setenv(sb.PREVIEW_DOMAIN_ENV, "bad domain; rm")
    with pytest.raises(ValueError):
        start(client, run_id="x2")


@pytest.mark.parametrize(
    "run_id", ["", "UPPER", "has_underscore", "a.b", "a/b", "../x", "-lead", "trail-", "a b", "x" * 41, "a;b", "ä"]
)
def test_run_id_validation_rejects(client, run_id):
    with pytest.raises(ValueError):
        start(client, run_id=run_id)
    with pytest.raises(ValueError):
        sb.container_name(run_id)
    assert client.containers.create_calls == []


@pytest.mark.parametrize("run_id", ["a", "abc-123", "0", "x" * 40, "a-b-c"])
def test_run_id_validation_accepts(run_id):
    assert sb.validate_run_id(run_id) == run_id


@pytest.mark.parametrize("image", ["", "UPPER", "bad image", "img;rm -rf /", "-flag"])
def test_image_validation(client, image):
    with pytest.raises(ValueError):
        sb.Sandbox(client).start_app("r1", image, env={})


# ---------------------------------------------------------------------------------------- credentials


def test_env_only_via_create_call(client):
    start(client, env={"GITHUB_TOKEN": SECRET, "GITHUB_REPO": "acme/api"})
    cfg = client.containers.create_calls[0]
    assert cfg["environment"] == {"GITHUB_TOKEN": SECRET, "GITHUB_REPO": "acme/api", "PORT": "8000"}
    # the secret appears in exactly one create kwarg and nowhere in labels/name/image
    carriers = [k for k, v in cfg.items() if SECRET in repr(v)]
    assert carriers == ["environment"]


def test_env_cannot_override_port(client):
    start(client, env={"PORT": "9999"}, port=8000)
    assert client.containers.create_calls[0]["environment"]["PORT"] == "8000"


@pytest.mark.parametrize("env", [{"bad key": "v"}, {"1BAD": "v"}, {"K": "a\x00b"}, {"K": 5}])
def test_env_validation(client, env):
    with pytest.raises(ValueError):
        start(client, env=env)
    assert client.containers.create_calls == []


def test_env_values_never_logged_keys_only(client, caplog):
    with caplog.at_level(logging.DEBUG):
        start(client)
        h = start(client, run_id="abc-123")  # restart path, removes the stale one
        sb.Sandbox(client).stop_app(h)
    assert SECRET not in caplog.text
    assert "GITHUB_TOKEN" in caplog.text


def test_start_failure_error_has_no_env_values_and_cleans_up(client, caplog):
    client.containers.fail_start = True
    with caplog.at_level(logging.DEBUG), pytest.raises(sb.SandboxError) as exc:
        start(client)
    assert SECRET not in str(exc.value) and SECRET not in caplog.text
    assert exc.value.__cause__ is not None  # type name only in message; cause kept for debugging
    assert client.containers.by_name == {}  # created-but-unstarted container removed


# ------------------------------------------------------------------------------ build / stop / restart


def test_build_image(client, tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    assert sb.Sandbox(client).build_image(tmp_path, "gen:abc") == "gen:abc"
    build = client.images.builds[0]
    assert build["path"] == str(tmp_path) and build["tag"] == "gen:abc"
    assert not {"buildargs", "volumes"} & set(build)


def test_build_image_errors(client, tmp_path):
    with pytest.raises(sb.SandboxError, match="no Dockerfile"):
        sb.Sandbox(client).build_image(tmp_path, "gen:abc")
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    with pytest.raises(ValueError):
        sb.Sandbox(client).build_image(tmp_path, "bad tag!")
    client.images.error = RuntimeError("daemon says no")
    with pytest.raises(sb.SandboxError, match="Building the app image failed"):
        sb.Sandbox(client).build_image(tmp_path, "gen:abc")


def test_start_runs_container_and_stop_removes_it(client):
    s = sb.Sandbox(client)
    h = s.start_app("r1", "gen:abc", env={}, now=NOW)
    c = client.containers.by_name["susanoo-run-r1"]
    assert c.started
    s.stop_app(h)
    assert c.stopped and c.removed
    assert c.remove_kwargs == {"v": True, "force": True}
    s.stop_app(h)  # already gone: no error


def test_start_replaces_stale_container_for_same_run(client):
    s = sb.Sandbox(client)
    s.start_app("r1", "gen:abc", env={}, now=NOW)
    old = client.containers.by_name["susanoo-run-r1"]
    s.start_app("r1", "gen:abc", env={}, now=NOW)
    assert old.removed
    assert client.containers.by_name["susanoo-run-r1"] is not old


# --------------------------------------------------------------------------------------- idle reaping


def test_reap_idle_tracked_app_respects_timeout_and_touch(client):
    s = sb.Sandbox(client)
    s.start_app("r1", "gen:abc", env={}, idle_timeout_s=60, now=NOW)
    s.start_app("r2", "gen:abc", env={}, idle_timeout_s=60, now=NOW)
    assert s.reap_idle(NOW + timedelta(seconds=59)) == []
    s.touch("r2", NOW + timedelta(seconds=50))
    assert s.reap_idle(NOW + timedelta(seconds=61)) == ["r1"]
    assert "susanoo-run-r1" not in client.containers.by_name
    assert "susanoo-run-r2" in client.containers.by_name
    assert s.reap_idle(NOW + timedelta(seconds=111)) == ["r2"]
    assert client.containers.by_name == {}


def test_reap_idle_untracked_uses_expires_at_label(client):
    sb.Sandbox(client).start_app("old", "gen:abc", env={}, idle_timeout_s=60, now=NOW)
    fresh = sb.Sandbox(client)  # a new process: no in-memory registry
    assert fresh.reap_idle(NOW + timedelta(seconds=30)) == []
    assert fresh.reap_idle(NOW + timedelta(seconds=60)) == ["old"]
    assert client.containers.by_name == {}


def test_reap_idle_ignores_unmanaged_and_malformed(client):
    c = client.containers
    c.create(name="other", labels={"unrelated": "x"}, environment={})
    c.create(name="susanoo-run-bad", labels={"susanoo.managed": "true", "susanoo.run_id": "bad"}, environment={})
    assert sb.Sandbox(client).reap_idle(NOW + timedelta(days=9)) == []
    assert set(c.by_name) == {"other", "susanoo-run-bad"}


def test_reap_idle_naive_label_treated_as_utc(client):
    client.containers.create(
        name="susanoo-run-n",
        labels={"susanoo.managed": "true", "susanoo.run_id": "n", "susanoo.expires_at": "2026-10-11T12:00:00"},
        environment={},
    )
    assert sb.Sandbox(client).reap_idle(NOW) == ["n"]


# ----------------------------------------------------------------------------- module-level / no docker


def test_module_functions_report_missing_docker_package(monkeypatch):
    import builtins

    monkeypatch.setattr(sb, "_default", None)
    real = builtins.__import__

    def no_docker(name, *a, **k):
        if name == "docker":
            raise ImportError
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_docker)
    with pytest.raises(sb.SandboxError, match="not installed"):
        sb.reap_idle()


# -------------------------------------------------------------------------------- image / volume defences


def test_build_refuses_image_with_volumes_and_removes_it(client, tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    client.images.config = {"Volumes": {"/data": {}}}
    with pytest.raises(sb.SandboxError, match="volume"):
        sb.Sandbox(client).build_image(tmp_path, "gen:abc")
    assert client.images.removed == ["gen:abc"]


def test_start_refuses_image_with_volumes_without_creating_anything(client):
    client.images.config = {"Volumes": {"/data": {}}}
    with pytest.raises(sb.SandboxError, match="volume"):
        start(client)
    assert client.containers.create_calls == []
    assert client.images.removed == []  # not ours to delete at start time


@pytest.mark.parametrize("key", ["susanoo.run_id", "Susanoo.port", "caddy", "caddy_0.reverse_proxy", "caddy.labels"])
def test_image_with_routing_labels_is_refused(client, tmp_path, key):
    client.images.config = {"Labels": {"maintainer": "x", key: "evil"}}
    with pytest.raises(sb.SandboxError, match="label"):
        start(client)
    assert client.containers.create_calls == []
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    with pytest.raises(sb.SandboxError, match="label"):
        sb.Sandbox(client).build_image(tmp_path, "gen:abc")
    assert client.images.removed == ["gen:abc"]


def test_harmless_image_labels_and_empty_volumes_are_fine(client):
    client.images.config = {"Labels": {"org.opencontainers.image.source": "x", "susanoo-test": "1"}, "Volumes": None}
    assert start(client).container_id == "cid1"


def test_container_with_any_mount_is_removed_and_never_started(client):
    client.containers.mounts = [{"Type": "volume", "Destination": "/data"}]
    with pytest.raises(sb.SandboxError, match="volume mounted"):
        start(client)
    assert client.containers.by_name == {}


def test_image_inspect_failure_is_a_sandbox_error(client):
    def boom(tag):
        raise RuntimeError("daemon down")

    client.images.get = boom
    with pytest.raises(sb.SandboxError, match="inspect"):
        start(client)


def test_reap_removes_volumes_too(client):
    s = sb.Sandbox(client)
    s.start_app("r1", "gen:abc", env={}, idle_timeout_s=10, now=NOW)
    c = client.containers.by_name["susanoo-run-r1"]
    s.reap_idle(NOW + timedelta(seconds=11))
    assert c.remove_kwargs == {"v": True, "force": True}


# ------------------------------------------------------------------------------------------ reap_forever


async def test_reap_forever_runs_a_tick_and_survives_errors(client):
    s = sb.Sandbox(client)
    s.start_app("r1", "gen:abc", env={}, idle_timeout_s=1, now=datetime.now(UTC) - timedelta(seconds=30))
    task = asyncio.create_task(sb.reap_forever(0.01, sandbox=s))
    for _ in range(100):
        if not client.containers.by_name:
            break
        await asyncio.sleep(0.01)
    assert client.containers.by_name == {}

    calls = []

    def flaky(now=None):
        calls.append(1)
        raise RuntimeError("daemon hiccup")

    s.reap_idle = flaky  # type: ignore[method-assign]
    for _ in range(100):
        if len(calls) >= 2:
            break
        await asyncio.sleep(0.01)
    assert len(calls) >= 2  # kept looping after the error
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
