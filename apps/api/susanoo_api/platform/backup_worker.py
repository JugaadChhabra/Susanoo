"""Private deployment's daily encrypted backups and enforced 30-day expiration."""

import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from .recovery import backup, expire_backups

log = logging.getLogger(__name__)


def main():
    directory = Path(os.environ.get("PLATFORM_BACKUP_DIR", ".platform/backups"))
    while True:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            expire_backups(directory)
            destination = directory / (datetime.now(UTC).date().isoformat() + ".susanoobak")
            if not destination.exists():
                backup(destination)
        except Exception as exc:
            log.error("Private backup failed (%s); operator attention required", type(exc).__name__)
        time.sleep(3600)


if __name__ == "__main__":
    main()
