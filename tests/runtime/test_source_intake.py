"""I01/I02 source intake: pinned git, safe archives, isolated snapshots, approved builds [Spec §9; T19-T24]."""

import os
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from susanoo_connectors.source import (
    ArchiveLimits,
    BuildRecipe,
    GitSource,
    SourceError,
    acquire_git,
    detect_runtime,
    import_archive,
    import_directory,
    run_build,
    validate_git_url,
)

COMMIT = "a" * 40


def git(cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Synthetic",
        "GIT_AUTHOR_EMAIL": "synthetic@example.invalid",
        "GIT_COMMITTER_NAME": "Synthetic",
        "GIT_COMMITTER_EMAIL": "synthetic@example.invalid",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "upstream"
    root.mkdir()
    git(root, "init", "--quiet", "-b", "main")
    (root / "pyproject.toml").write_text("[project]\nname='agent'\n")
    (root / "agent.py").write_text("print('v1')\n")
    (root / ".gitattributes").write_text("*.py filter=evil\n")
    git(root, "add", ".")
    git(root, "commit", "--quiet", "-m", "v1")
    first = git(root, "rev-parse", "HEAD").strip()
    (root / "agent.py").write_text("print('v2')\n")
    git(root, "commit", "--quiet", "-am", "v2")
    return root, first


@pytest.mark.parametrize(
    "url,code",
    [
        ("--upload-pack=touch /tmp/pwned", "unsafe_url"),
        ("ext::sh -c touch% /tmp/pwned", "unsafe_url"),
        ("http://github.com/acme/agent.git", "unsafe_url"),
        ("https://user:token@github.com/acme/agent.git", "credentials_in_url"),
        ("https://evil.example/acme/agent.git", "host_not_allowed"),
        ("file:///tmp/repo", "unsafe_url"),
    ],
)
def test_git_url_policy(url, code):
    with pytest.raises(SourceError) as error:
        validate_git_url(GitSource(url=url, commit=COMMIT))
    assert error.value.code == code


def test_git_acquisition_pins_the_commit_and_disables_filters(repo, tmp_path):
    root, first = repo
    hook_marker = tmp_path / "hook-ran"
    hooks = root / ".git" / "hooks"
    (hooks / "post-checkout").write_text(f"#!/bin/sh\ntouch {hook_marker}\n")
    (hooks / "post-checkout").chmod(0o755)
    snapshot = acquire_git(GitSource(url=f"file://{root}", commit=first, allow_local=True), tmp_path / "snap")
    assert snapshot.commit == first
    assert (tmp_path / "snap" / "agent.py").read_text() == "print('v1')\n"  # the pinned commit, not main
    assert not hook_marker.exists()
    again = acquire_git(GitSource(url=f"file://{root}", commit=first, allow_local=True), tmp_path / "snap2")
    assert again.digest == snapshot.digest


def test_unknown_commit_fails(repo, tmp_path):
    root, _ = repo
    with pytest.raises(SourceError):
        acquire_git(GitSource(url=f"file://{root}", commit="b" * 40, allow_local=True), tmp_path / "snap")


def zip_with(tmp_path: Path, entries: list[tuple[str, bytes, int | None]]) -> Path:
    path = tmp_path / "upload.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, data, mode in entries:
            info = zipfile.ZipInfo(name)
            if mode is not None:
                info.external_attr = mode << 16
            bundle.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED)
    return path


@pytest.mark.parametrize(
    "entries,code",
    [
        ([("../escape.py", b"x", None)], "path_traversal"),
        ([("/etc/passwd", b"x", None)], "path_traversal"),
        ([("link", b"/etc/passwd", stat.S_IFLNK | 0o777)], "link_refused"),
        ([("bomb.txt", b"0" * 2_000_000, None)], "decompression_bomb"),
    ],
)
def test_archive_rejections(tmp_path, entries, code):
    with pytest.raises(SourceError) as error:
        import_archive(zip_with(tmp_path, entries), tmp_path / "out")
    assert error.value.code == code
    assert not (tmp_path / "escape.py").exists()


def test_archive_limits_and_success(tmp_path):
    ok = zip_with(tmp_path, [("src/agent.py", b"print('hi')\n", None), ("package.json", b"{}", None)])
    snapshot = import_archive(ok, tmp_path / "out")
    assert snapshot.files == 2 and (tmp_path / "out" / "src" / "agent.py").exists()
    with pytest.raises(SourceError) as error:
        import_archive(ok, tmp_path / "out2", ArchiveLimits(max_files=1))
    assert error.value.code == "too_many_files"


def test_directory_snapshot_never_touches_the_original(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "agent.py").write_text("print('hi')\n")
    (project / "outside").symlink_to("/etc")
    before = sorted(p.name for p in project.iterdir())
    snapshot = import_directory(project, tmp_path / "snap")
    (tmp_path / "snap" / "agent.py").write_text("tampered")
    assert (project / "agent.py").read_text() == "print('hi')\n"
    assert sorted(p.name for p in project.iterdir()) == before
    assert not (tmp_path / "snap" / "outside").exists()
    assert snapshot.files == 1


def test_runtime_detection_only_suggests(tmp_path):
    (tmp_path / "pyproject.toml").write_text("")
    (tmp_path / "Dockerfile").write_text("")
    suggestions = detect_runtime(tmp_path)
    assert {(s.kind, s.status) for s in suggestions} == {("python", "suggestion"), ("container", "suggestion")}


def test_builds_need_the_approved_recipe_and_classify_failures(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "agent.py").write_text("print('built')\n")
    snapshot = import_directory(project, tmp_path / "snap")
    ok = BuildRecipe(runtime="python", steps=[[sys.executable, "agent.py"]])
    with pytest.raises(SourceError):
        run_build(snapshot, ok, approved_digest="0" * 64)
    assert run_build(snapshot, ok, approved_digest=ok.digest).status == "succeeded"
    install = BuildRecipe(runtime="python", steps=[[sys.executable, "-c", "import sys; sys.exit(1)", "install"]])
    assert run_build(snapshot, install, approved_digest=install.digest).status == "dependency_install_failed"
    needs = BuildRecipe(runtime="python", steps=[[sys.executable, "agent.py"]], env={"TOKEN": "secret:API_TOKEN"})
    assert run_build(snapshot, needs, approved_digest=needs.digest).status == "missing_secret"
    leak = BuildRecipe(
        runtime="python",
        steps=[[sys.executable, "-c", "import os; print(os.environ['TOKEN'])"]],
        env={"TOKEN": "secret:API_TOKEN"},
    )
    result = run_build(snapshot, leak, approved_digest=leak.digest, secrets={"API_TOKEN": "tok-canary-123"})
    assert result.status == "succeeded" and "tok-canary-123" not in result.log_tail and "[redacted]" in result.log_tail
    slow = BuildRecipe(
        runtime="python", steps=[[sys.executable, "-c", "import time; time.sleep(5)"]], timeout_seconds=1
    )
    assert run_build(snapshot, slow, approved_digest=slow.digest).status == "timed_out"
    assert (project / "agent.py").read_text() == "print('built')\n"
