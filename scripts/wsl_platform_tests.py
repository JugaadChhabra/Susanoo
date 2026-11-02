"""Run Linux qualification against a disposable role/database in the local WSL PostgreSQL service."""

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4


def main():
    if os.name != "nt":
        raise SystemExit("This wrapper runs from Windows; use scripts/test_platform.py directly on Linux.")
    root = Path(__file__).resolve().parents[1]
    linux_root = "/mnt/" + root.drive[0].lower() + root.as_posix()[2:]
    role, password = "susanoo_test_" + uuid4().hex, uuid4().hex + uuid4().hex
    admin = ["wsl", "-d", "Ubuntu", "-u", "postgres", "--", "psql", "-d", "postgres", "-v", "ON_ERROR_STOP=1"]
    created = False
    try:
        subprocess.run(
            admin,
            input=f"CREATE ROLE {role} LOGIN CREATEDB CREATEROLE PASSWORD '{password}';\n"
            f"GRANT pg_signal_backend TO {role};\n"
            f"COMMENT ON ROLE {role} IS 'Susanoo disposable WSL qualification role';\n",
            text=True,
            check=True,
            capture_output=True,
        )
        created = True
        # Credentials are passed through stdin to avoid exposing them in process command lines.
        payload = json.dumps({"root": linux_root, "role": role, "password": password, "args": sys.argv[1:]})
        code = (
            "import json,os,subprocess,sys; p=json.load(sys.stdin); "
            "env={**os.environ,'PLATFORM_TEST_ADMIN_URL':'postgresql://'+p['role']+':'+p['password']"
            "+'@127.0.0.1:5432/postgres','PLATFORM_PG_BIN':'/usr/bin',"
            "'PYTHONPYCACHEPREFIX':'/home/aarav/.cache/susanoo-bridge-pycache',"
            "'UV_PROJECT_ENVIRONMENT':'/home/aarav/.cache/susanoo-bridge-venv'}; "
            "sys.exit(subprocess.run(['uv','run','--no-sync','--project',p['root'],'--directory',p['root'],"
            "'python','scripts/test_platform.py',*p['args']],env=env).returncode)"
        )
        return subprocess.run(
            ["wsl", "-d", "Ubuntu", "--", "python3", "-c", code], input=payload, text=True, check=False
        ).returncode
    finally:
        if created:
            cleanup = (
                "SELECT format('DROP DATABASE %I WITH (FORCE)',datname) FROM pg_database "
                f"WHERE datdba=(SELECT oid FROM pg_roles WHERE rolname='{role}') "
                "AND datname ~ '^susanoo_test_[0-9a-f]{32}$'\\gexec\n"
                f"DROP ROLE {role};\n"
            )
            subprocess.run(admin, input=cleanup, text=True, check=True, capture_output=True)


if __name__ == "__main__":
    sys.exit(main())
