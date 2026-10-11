"""Unit tests for guards/patch_size.py."""

import subprocess
from pathlib import Path

import pytest

from susanoo.guards import patch_size as ps


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(ps.PATCH_MAX_LINES_ENV, raising=False)


# ---- numstat parsing -------------------------------------------------------


def test_empty_numstat_is_zero():
    assert ps.count_from_numstat("") == 0
    assert ps.count_from_numstat("\n") == 0


def test_numstat_sums_added_and_removed():
    assert ps.count_from_numstat("3\t1\ta.py\n10\t0\tb.py\n0\t7\tc.py\n") == 21


def test_numstat_binary_counts_as_fixed_large_number():
    assert ps.count_from_numstat("-\t-\timg.png\n2\t3\ta.py\n") == ps.BINARY_FILE_LINES + 5


def test_numstat_pure_rename_is_zero():
    assert ps.count_from_numstat("0\t0\told.py => new.py\n") == 0
    assert ps.count_from_numstat("0\t0\tdir/{old => new}/x.py\n") == 0


def test_numstat_rename_with_edits_counts_the_edits():
    assert ps.count_from_numstat("2\t1\tdir/{old => new}/x.py\n") == 3


def test_numstat_path_with_spaces():
    assert ps.count_from_numstat("1\t1\tmy file.py\n") == 2


def test_numstat_garbage_raises():
    with pytest.raises(ValueError):
        ps.count_from_numstat("not a numstat line\n")
    with pytest.raises(ValueError):
        ps.count_from_numstat("x\ty\tfile\n")


# ---- cap config ------------------------------------------------------------


def test_default_cap():
    assert ps.patch_max_lines() == ps.MAX_LINES_CHANGED == 60


def test_env_overrides_cap(monkeypatch):
    monkeypatch.setenv(ps.PATCH_MAX_LINES_ENV, "5")
    assert ps.patch_max_lines() == 5


@pytest.mark.parametrize("bad", ["abc", "0", "-3", "1.5"])
def test_bad_env_is_rejected(monkeypatch, bad):
    monkeypatch.setenv(ps.PATCH_MAX_LINES_ENV, bad)
    with pytest.raises(ValueError):
        ps.patch_max_lines()


# ---- real git repo ---------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(repo),
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("".join(f"line {i}\n" for i in range(20)))
    (tmp_path / "big.txt").write_text("".join(f"keep {i}\n" for i in range(30)))
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def _commit(repo: Path) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change")
    return _git(repo, "rev-parse", "HEAD")


def test_count_lines_changed_on_real_repo(repo):
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "a.txt").write_text("".join(f"line {i}\n" for i in range(20) if i != 3) + "extra\n")  # -1 +1
    (repo / "new.txt").write_text("1\n2\n3\n")  # +3
    head = _commit(repo)
    assert ps.count_lines_changed(repo, base, head) == 5


def test_binary_file_counts_large(repo):
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "blob.bin").write_bytes(b"\x00\x01\x02\xff" * 10)
    head = _commit(repo)
    assert ps.count_lines_changed(repo, base, head) == ps.BINARY_FILE_LINES


def test_pure_rename_is_free(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "mv", "big.txt", "renamed.txt")
    head = _commit(repo)
    assert ps.count_lines_changed(repo, base, head) == 0


def test_cap_edge_equal_ok_and_plus_one_fails(repo):
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "new.txt").write_text("".join(f"n{i}\n" for i in range(10)))
    head = _commit(repo)
    assert ps.check(repo, base, head, max_lines=10) == 10
    with pytest.raises(ps.PatchTooLarge) as exc:
        ps.check(repo, base, head, max_lines=9)
    assert (exc.value.lines, exc.value.cap) == (10, 9)
    assert "10 lines" in str(exc.value)
    assert "9-line limit" in str(exc.value)


def test_check_uses_env_cap_by_default(repo, monkeypatch):
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "new.txt").write_text("1\n2\n3\n")
    head = _commit(repo)
    monkeypatch.setenv(ps.PATCH_MAX_LINES_ENV, "3")
    assert ps.check(repo, base, head) == 3
    monkeypatch.setenv(ps.PATCH_MAX_LINES_ENV, "2")
    with pytest.raises(ps.PatchTooLarge):
        ps.check(repo, base, head)


def test_default_cap_rejects_61_and_accepts_60(repo):
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "new.txt").write_text("x\n" * 60)
    ok = _commit(repo)
    assert ps.check(repo, base, ok) == 60
    (repo / "new2.txt").write_text("x\n" * 1)
    (repo / "new.txt").write_text("x\n" * 61)
    big = _commit(repo)
    with pytest.raises(ps.PatchTooLarge) as exc:
        ps.check(repo, base, big)
    assert exc.value.cap == 60


def test_bad_revision_is_a_clear_error(repo):
    with pytest.raises(ps.GitDiffError, match="Could not measure the patch"):
        ps.count_lines_changed(repo, "deadbeef", "HEAD")


@pytest.mark.parametrize("ref", ["--output=/tmp/x", "HEAD", "main", "-p", "abc", ""])
def test_refs_that_are_not_commit_ids_are_refused(tmp_path, ref):
    with pytest.raises(ps.GitDiffError, match="not a commit id"):
        ps.count_lines_changed(tmp_path, ref, "a" * 40)
