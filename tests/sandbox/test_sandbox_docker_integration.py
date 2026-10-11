"""Real-Docker checks: build/run the template image, and the VOLUME / label bypasses. Skipped without a daemon.

Everything created here is removed in the fixture teardown: containers named susanoo-run-it-*, images tagged
susanoo-sandbox-it:*, the throwaway network and any anonymous volume a test creates.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest

from susanoo.sandbox import docker as sb

docker = pytest.importorskip("docker")
TEMPLATE = Path(__file__).resolve().parents[2] / "templates" / "app-template"
BASE = "python:3.12-slim"
IT_LABEL = {"susanoo-it": "1"}  # hyphen, not a reserved "susanoo." key


class Env:
    def __init__(self, client, suffix):
        self.client = client
        self.suffix = suffix
        self.network = f"susanoo-it-net-{suffix}"
        self.tags: list[str] = []
        self.containers: list = []

    def tag(self, name):
        tag = f"susanoo-sandbox-it:{name}-{self.suffix}"
        self.tags.append(tag)
        return tag

    def volumes(self) -> set[str]:
        return {v.name for v in self.client.volumes.list()}

    def build_raw(self, dockerfile: str, tmp_path: Path, name: str) -> str:
        """Build an image straight through the SDK (bypassing Sandbox.build_image's checks)."""
        ctx = tmp_path / name
        ctx.mkdir()
        (ctx / "Dockerfile").write_text(dockerfile)
        tag = self.tag(name)
        self.client.images.build(path=str(ctx), tag=tag, rm=True, forcerm=True)
        return tag


@pytest.fixture
def env(monkeypatch):
    try:
        client = docker.from_env()
        client.ping()
        client.images.get(BASE)
    except docker.errors.ImageNotFound:
        pytest.skip(f"{BASE} not available locally")
    except Exception:
        pytest.skip("Docker daemon not reachable")
    e = Env(client, uuid.uuid4().hex[:8])
    monkeypatch.setenv(sb.NETWORK_ENV, e.network)
    yield e
    for c in client.containers.list(all=True, filters={"name": f"susanoo-run-it-{e.suffix}"}):
        c.remove(v=True, force=True)
    for c in e.containers:
        try:
            c.remove(v=True, force=True)
        except Exception:
            pass
    for tag in e.tags:
        try:
            client.images.remove(tag, force=True)
        except Exception:
            pass
    try:
        client.networks.get(e.network).remove()
    except Exception:
        pass


def test_template_image_builds_and_runs_locked_down(env, tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(TEMPLATE, repo)
    box = sb.Sandbox(env.client)
    run_id = f"it-{env.suffix}"
    tag = env.tag("template")
    volumes_before = env.volumes()
    handle = None
    try:
        box.build_image(repo, tag)
        handle = box.start_app(run_id, tag, env={"ANTHROPIC_APP_API_KEY": "not-a-real-key"})
        info = env.client.api.inspect_container(handle.container_id)
        host = info["HostConfig"]
        assert not info["Mounts"] and not host["Binds"]
        assert host["Privileged"] is False and host["CapDrop"] == ["ALL"] and host["ReadonlyRootfs"] is True
        assert host["Memory"] == 512 * 1024 * 1024
        assert host["NetworkMode"] == env.network  # dedicated network was created on demand
        assert info["Config"]["Labels"]["susanoo.run_id"] == run_id
    finally:
        if handle is not None:
            box.stop_app(handle)
    assert env.volumes() == volumes_before
    assert not env.client.containers.list(all=True, filters={"name": f"susanoo-run-{run_id}"})


def test_volume_image_is_refused_at_build_and_removed(env, tmp_path):
    ctx = tmp_path / "vol"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text(f"FROM {BASE}\nVOLUME /data\nUSER root\nCMD sleep 60\n")
    tag = env.tag("vol")
    before = env.volumes()
    with pytest.raises(sb.SandboxError, match="volume"):
        sb.Sandbox(env.client).build_image(ctx, tag)
    with pytest.raises(docker.errors.ImageNotFound):
        env.client.images.get(tag)  # the refused image was removed
    assert env.volumes() == before


def test_volume_image_built_elsewhere_is_refused_at_start(env, tmp_path):
    tag = env.build_raw(f"FROM {BASE}\nVOLUME /data\nUSER root\nCMD sleep 60\n", tmp_path, "vol2")
    before = env.volumes()
    with pytest.raises(sb.SandboxError, match="volume"):
        sb.Sandbox(env.client).start_app(f"it-{env.suffix}", tag, env={})
    assert not env.client.containers.list(all=True, filters={"name": f"susanoo-run-it-{env.suffix}"})
    assert env.volumes() == before  # no anonymous volume was ever created


def test_routing_label_image_is_refused(env, tmp_path):
    tag = env.build_raw(f"FROM {BASE}\nLABEL caddy=evil.example.com\nCMD sleep 60\n", tmp_path, "lbl")
    with pytest.raises(sb.SandboxError, match="label"):
        sb.Sandbox(env.client).start_app(f"it-{env.suffix}", tag, env={})
    assert not env.client.containers.list(all=True, filters={"name": f"susanoo-run-it-{env.suffix}"})


def test_removal_drops_anonymous_volumes(env):
    before = env.volumes()
    made = env.client.api.create_container(BASE, command="true", volumes=["/data"], labels=IT_LABEL)
    container = env.client.containers.get(made["Id"])
    env.containers.append(container)
    assert env.volumes() - before  # the anonymous volume exists
    sb.Sandbox(env.client)._force_remove(container)
    assert env.volumes() == before
