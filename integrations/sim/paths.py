"""Keep machine-specific runtime data outside the shareable integration source."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]
STATE = Path(os.environ.get("SUSANOO_SIM_STATE_DIR", str(REPO / ".platform/sim-local"))).resolve()
PRIVATE = STATE / ".private"


def credentials_file():
    explicit = os.environ.get("PLATFORM_BROWSER_CREDENTIALS")
    if explicit:
        return Path(explicit)
    files = list((REPO / ".platform").glob("demo-credentials-*.json"))
    if len(files) != 1:
        raise RuntimeError("Set PLATFORM_BROWSER_CREDENTIALS to the intended local synthetic account file")
    return files[0]


def ensure_private_state():
    from susanoo_runner.local_security import secure_local_path

    STATE.mkdir(parents=True, exist_ok=True)
    PRIVATE.mkdir(exist_ok=True)
    secure_local_path(STATE)
    secure_local_path(PRIVATE)
