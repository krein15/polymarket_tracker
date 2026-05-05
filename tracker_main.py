"""Entry point для трекера.

Запуск:
    python tracker_main.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

from polymarket_tracker.core import PolymarketTracker


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


async def main() -> None:
    log_level = os.getenv("LOG_LEVEL", "INFO")
    setup_logging(log_level)
    log = logging.getLogger("main")

    try:
        tracker = PolymarketTracker.from_env()
    except ValueError as e:
        log.error(str(e))
        log.error("Скопируй .env.example в .env и заполни обязательные поля")
        sys.exit(1)

    # Graceful shutdown по SIGINT/SIGTERM
    stop_event = asyncio.Event()

    def _handler(*_):
        log.info("Получен сигнал остановки")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handler)
        except NotImplementedError:
            # Windows — add_signal_handler не работает, Ctrl+C всё равно поймается
            pass

    runner = asyncio.create_task(tracker.run())

    # Ждём либо завершения runner, либо сигнала остановки
    done, pending = await asyncio.wait(
        [runner, asyncio.create_task(stop_event.wait())],
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
    for task in done:
        if task is runner:
            exc = task.exception()
            if exc:
                log.exception("Tracker упал:", exc_info=exc)
                sys.exit(2)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
