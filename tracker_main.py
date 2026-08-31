"""Entry point для трекера.

Запуск:
    python tracker_main.py
"""
from __future__ import annotations

import asyncio
import atexit
import logging
import os
import signal
import sys
from pathlib import Path

from polymarket_tracker.core import PolymarketTracker


# Файл-замок: с автозапуском через Планировщик легко получить вторую копию
# поверх запущенной вручную. Две копии дерутся за getUpdates (каждая забирает
# часть команд) и дублируют работу по одной базе.
LOCK_PATH = Path("data") / "tracker.lock"
_lock_handle = None


def acquire_single_instance_lock() -> bool:
    """Захватить замок. False — трекер уже запущен."""
    global _lock_handle
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = open(LOCK_PATH, "a+")
    except OSError:
        return True  # не смогли открыть файл — не мешаем запуску

    try:
        if os.name == "nt":
            import msvcrt
            # ВАЖНО: locking блокирует байт на ТЕКУЩЕЙ позиции. В режиме "a+"
            # у второго процесса она оказывается в конце непустого файла, и
            # он спокойно берёт замок на другом байте. Всегда байт нулевой.
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False

    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    _lock_handle = handle
    atexit.register(_release_lock)
    return True


def _release_lock() -> None:
    global _lock_handle
    if _lock_handle is None:
        return
    try:
        if os.name == "nt":
            import msvcrt
            _lock_handle.seek(0)
            msvcrt.locking(_lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    finally:
        _lock_handle.close()
        _lock_handle = None


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

    if not acquire_single_instance_lock():
        log.error(
            "Трекер уже запущен (замок %s занят). Вторая копия будет драться "
            "за команды Telegram и дублировать работу — выхожу.", LOCK_PATH,
        )
        sys.exit(3)

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
