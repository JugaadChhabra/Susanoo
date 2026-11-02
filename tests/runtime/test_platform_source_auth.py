import asyncio
import base64
import subprocess
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from susanoo_connectors import source as acquisition
from susanoo_connectors.source import GitSource
from susanoo_contracts import SourceSpec
from susanoo_runner.journal import JournalError
from susanoo_runner.platform_source_auth import source_auth


def fixture():
    spec = SourceSpec(
        repository="synthetic/agent",
        commit="a" * 40,
        installation_id=101,
        trusted=True,
        recipe_hash="b" * 64,
        lockfile_hash="c" * 64,
    )
    claim = SimpleNamespace(
        lease_token="local-test-lease",
        execution_manifest=SimpleNamespace(versions=[SimpleNamespace(kind="source", id=uuid4(), data=spec)]),
    )
    calls = []

    def post(endpoint, body):
        calls.append((endpoint, body))
        if endpoint.endswith("/revoke"):
            return {"revocation_queued": True}
        return {
            "token": "synthetic-private-git-canary",
            "repository": spec.repository,
            "source_commit": spec.commit,
            "read_only": True,
            "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        }

    journal = SimpleNamespace(endpoint="runner/attempts/local", control=lambda: None, client=SimpleNamespace(post=post))
    source = GitSource(url="https://github.com/synthetic/agent.git", commit=spec.commit)
    return claim, journal, source, calls


def test_source_grant_revokes_after_success_and_acquisition_failure():
    async def run(fail):
        claim, journal, source, calls = fixture()
        try:
            async with source_auth(claim, journal, source, enabled=True) as header:
                assert base64.b64decode(header.split(" ")[-1]) == b"x-access-token:synthetic-private-git-canary"
                if fail:
                    raise RuntimeError("Synthetic fetch failure")
        except RuntimeError:
            assert fail
        assert [call[0].rsplit("/", 1)[-1] for call in calls] == ["source-credential", "revoke"]
        assert "synthetic-private-git-canary" not in repr(calls)

    asyncio.run(run(False))
    asyncio.run(run(True))


def test_source_grant_cannot_be_used_for_another_repository():
    async def run():
        claim, journal, source, calls = fixture()
        other = source.model_copy(update={"url": "https://github.com/synthetic/other.git"})
        with pytest.raises(JournalError, match="exact approved"):
            async with source_auth(claim, journal, other, enabled=True):
                pytest.fail("No grant for the wrong repository")
        assert calls == []

    asyncio.run(run())


def test_git_auth_is_url_scoped_and_never_written_or_passed_in_argv(tmp_path, monkeypatch):
    source = GitSource(url="https://github.com/synthetic/agent.git", commit="a" * 40)
    header = "Authorization: Basic synthetic-canary"
    checked = []

    def git(args, cwd, env, **kwargs):
        assert header not in repr(args)
        value = subprocess.run(
            ["git", "config", "--get-urlmatch", "http.extraHeader", source.url + "/info/refs"],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        assert value.stdout.strip() == header
        other = subprocess.run(
            ["git", "config", "--get-urlmatch", "http.extraHeader", "https://github.com/synthetic/other.git"],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert header not in other.stdout
        checked.append(args[0])
        return source.commit if args[0] == "rev-parse" else ""

    monkeypatch.setattr(acquisition, "_git", git)
    result = acquisition.acquire_git(source, tmp_path / "snapshot", auth_header=header)
    assert result.commit == source.commit and "fetch" in checked
    assert all(
        header.encode() not in path.read_bytes() for path in (tmp_path / "snapshot").rglob("*") if path.is_file()
    )
