"""Portable setup must never overwrite an existing Sim encryption key."""

import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
INTEGRATION = ROOT / "integrations/sim"


def test_init_creates_private_secrets_and_refuses_rotation(tmp_path):
    state = tmp_path / "private state"
    env = {**os.environ, "SUSANOO_SIM_STATE_DIR": str(state)}
    command = [sys.executable, str(INTEGRATION / "init.py")]
    first = subprocess.run(command, env=env, cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert first.returncode == 0, first.stderr
    before = (state / ".env").read_bytes()
    values = dict(line.split("=", 1) for line in before.decode().splitlines())
    names = ["POSTGRES_PASSWORD", "BETTER_AUTH_SECRET", "ENCRYPTION_KEY", "INTERNAL_API_SECRET", "CRON_SECRET"]
    assert all(len(values[name]) == 64 for name in names)
    assert len({values[name] for name in names}) == len(names)
    assert all(values[name] not in first.stdout + first.stderr for name in names)
    assert (state / ".private").is_dir()
    if os.name != "nt":
        assert (state / ".env").stat().st_mode & 0o777 == 0o600
        assert (state / ".private").stat().st_mode & 0o777 == 0o700
    second = subprocess.run(command, env=env, cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert second.returncode != 0
    assert (state / ".env").read_bytes() == before


def test_paths_use_source_checkout_and_explicit_state_not_working_directory(tmp_path):
    state = tmp_path / "outside checkout"
    code = (
        "import sys,json; sys.path.insert(0,sys.argv[1]); import paths; "
        "print(json.dumps([str(paths.REPO),str(paths.STATE),str(paths.credentials_file())]))"
    )
    credentials = tmp_path / "selected-accounts.json"
    result = subprocess.run(
        [sys.executable, "-c", code, str(INTEGRATION)],
        env={**os.environ, "SUSANOO_SIM_STATE_DIR": str(state), "PLATFORM_BROWSER_CREDENTIALS": str(credentials)},
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [str(ROOT), str(state), str(credentials)]
    assert not state.exists(), "Importing path configuration must not initialize or mutate an install"
