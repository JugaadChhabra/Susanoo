"""Static cache-stage coverage; hosted CI separately performs actual Docker builds."""

from pathlib import Path
import subprocess
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("dockerfile", ["apps/api/Dockerfile", "apps/worker/Dockerfile"])
def test_legacy_dependency_stage_contains_every_workspace_member(dockerfile):
    members = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["uv"]["workspace"]["members"]
    dependency_stage = (ROOT / dockerfile).read_text(encoding="utf-8").split("uv sync", 1)[0]
    for member in members:
        metadata = f"{member}/pyproject.toml"
        assert f"COPY {metadata} {metadata}" in dependency_stage, metadata


def test_runner_secrets_are_ignored_by_git_and_all_root_docker_contexts():
    paths = [".susanoo/platform/synthetic.sqlite", ".susanoo/approved-profile.json"]
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", *paths], cwd=ROOT, capture_output=True, text=True, check=True
    )
    assert set(result.stdout.splitlines()) == set(paths)
    for dockerignore in [".dockerignore", "deploy/api.Dockerfile.dockerignore", "deploy/web.Dockerfile.dockerignore"]:
        entries = (ROOT / dockerignore).read_text(encoding="utf-8").splitlines()
        assert ".susanoo" in entries
