"""Supervisor — auto-restart and health monitoring."""

from __future__ import annotations

import asyncio
import os
import sys
import time
import signal
import structlog
from dotenv import load_dotenv

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load .env
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from bot.main import GabaBot

logger = structlog.get_logger()


class Supervisor:
    """Supervises the bot with auto-restart and health checks."""

    def __init__(
        self,
        max_restarts: int = 10,
        restart_window_s: float = 3600,
        min_restart_delay_s: float = 5.0,
        max_restart_delay_s: float = 300.0,
    ):
        self.max_restarts = max_restarts
        self.restart_window_s = restart_window_s
        self.min_restart_delay_s = min_restart_delay_s
        self.max_restart_delay_s = max_restart_delay_s

        self._restart_times: list[float] = []
        self._restart_delay = min_restart_delay_s
        self._running = True
        self._bot: GabaBot | None = None

    async def run(self):
        logger.info("supervisor_starting")

        # Signal handling (Unix only)
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._handle_signal)
            except NotImplementedError:
                pass  # Windows

        while self._running:
            if self._too_many_restarts():
                logger.critical("too_many_restarts",
                              count=len(self._restart_times),
                              window_s=self.restart_window_s)
                break

            try:
                logger.info("bot_starting_via_supervisor",
                           restart_count=len(self._restart_times))

                self._bot = GabaBot()
                await self._bot.run()

                logger.info("bot_exited_cleanly")
                break

            except KeyboardInterrupt:
                logger.info("supervisor_interrupted")
                break

            except Exception as e:
                logger.error("bot_crashed", error=str(e), exc_info=True)
                self._restart_times.append(time.time())

                if self._running:
                    logger.info("restarting_in", delay_s=self._restart_delay)
                    await asyncio.sleep(self._restart_delay)
                    self._restart_delay = min(
                        self._restart_delay * 2,
                        self.max_restart_delay_s,
                    )

        logger.info("supervisor_stopped")

    def _too_many_restarts(self) -> bool:
        now = time.time()
        self._restart_times = [
            t for t in self._restart_times
            if now - t < self.restart_window_s
        ]
        return len(self._restart_times) >= self.max_restarts

    def _handle_signal(self):
        logger.info("signal_received")
        self._running = False
        if self._bot:
            asyncio.create_task(self._bot.shutdown())


def main():
    supervisor = Supervisor()
    try:
        asyncio.run(supervisor.run())
    except KeyboardInterrupt:
        print("\nSupervisor stopped.")


if __name__ == "__main__":
    main()
