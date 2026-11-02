"""Protect generated local secrets on POSIX and with real Windows DACLs."""

import csv
import io
import os
import re
import subprocess
from functools import cache
from pathlib import Path


@cache
def current_windows_sid():
    output = subprocess.check_output(["whoami", "/user", "/fo", "csv", "/nh"], text=True)
    sid = next(csv.reader(io.StringIO(output)))[1]
    if not re.fullmatch(r"S-\d+(?:-\d+)+", sid):
        raise RuntimeError("Cannot determine the local security principal")
    return sid


def secure_local_path(path):
    path = Path(path)
    if os.name != "nt":
        os.chmod(path, 0o700 if path.is_dir() else 0o600)
        return
    flags = "(OI)(CI)F" if path.is_dir() else "F"
    subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r", f"*{current_windows_sid()}:{flags}", f"*S-1-5-18:{flags}"],
        check=True,
        capture_output=True,
        timeout=15,
    )
