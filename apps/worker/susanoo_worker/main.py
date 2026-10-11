"""Queue poller that runs the graph. Placeholder until the graph lands (Muaaz)."""

import asyncio
import logging

logger = logging.getLogger("susanoo_worker")

POLL_SECONDS = 5


async def run() -> None:
    logger.warning("Worker started; the run queue is not wired up yet, so it is idle.")
    while True:
        await asyncio.sleep(POLL_SECONDS)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())


if __name__ == "__main__":
    main()
