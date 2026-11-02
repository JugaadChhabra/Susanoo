from types import SimpleNamespace

import pytest
from susanoo_contracts import DataPolicy, ExecutionManifest, RunLimits
from susanoo_runner.journal import JournalError
from susanoo_runner.platform_jobs import approval_key, owned_storage_bytes


def test_local_advanced_approval_pins_policy_and_limits_not_only_versions():
    manifest = ExecutionManifest(policy=DataPolicy(), limits=RunLimits(), timezone="Asia/Kolkata")
    claim = SimpleNamespace(kind="build", execution_manifest=manifest)
    initial = approval_key(claim)
    manifest.limits.max_reference_calls += 1
    assert approval_key(claim) != initial
    second = approval_key(claim)
    manifest.policy.report_upload_allowed = True
    assert approval_key(claim) != second


def test_owned_storage_includes_nested_source_and_child_journals(tmp_path):
    path = tmp_path / "attempt.sqlite"
    path.write_bytes(b"journal")
    child = tmp_path / "attempt.sqlite.source"
    child.mkdir()
    (child / "source.py").write_bytes(b"source")
    (tmp_path / "unrelated").write_bytes(b"private-user-file")
    assert owned_storage_bytes(SimpleNamespace(path=path)) == len(b"journalsource")


def test_owned_storage_does_not_follow_symlinks(tmp_path):
    target = tmp_path / "user-file"
    target.write_bytes(b"untouched")
    link = tmp_path / "attempt.sqlite.link"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("This host does not allow test symlink creation")
    with pytest.raises(JournalError, match="not safely bounded"):
        owned_storage_bytes(SimpleNamespace(path=tmp_path / "attempt.sqlite"))
    assert target.read_bytes() == b"untouched"
