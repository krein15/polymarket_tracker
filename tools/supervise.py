#!/usr/bin/env python3
"""Надзиратель: поднимает трекер обратно, если тот упал.

Зачем
-----
`.bat` запускал трекер ровно один раз. Упала сеть, оборвался вебсокет,
процесс умер — и всё стоит до тех пор, пока человек не заметит. Так и
вышло 09-10.09: трекер простоял сутки, а обнаружилось это только при
разборе логов.

Теперь это дорого. Чтобы отличить прибыль от нуля, нужно порядка 700
закрывшихся сделок — около месяца. Каждые сутки простоя добавляются к
сроку напрямую.

Почему на Python, а не циклом в .bat
------------------------------------
Надзирателю нужно мерить, сколько процесс прожил: перезапускать после
получаса работы и после трёх секунд — это разные ситуации. Арифметика со
временем в batch превращается в нечитаемое месиво, а здесь она проверяется
тестами.

Что НЕ делает
-------------
Не лечит причину. Если трекер падает мгновенно и подряд — значит сломан
он, а не сеть, и надзиратель сдаётся, а не крутит вечный цикл, скрывая
поломку.

Запуск (обычно через scripts/start_tracker.bat):
    python tools/supervise.py
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Прожил столько — считаем запуск удавшимся, счётчик неудач обнуляем.
# Полчаса выбраны с запасом: обрывы сети случаются и через час работы, но
# такое падение не признак поломки.
STABLE_RUN_SEC = 1800

# Пауза перед перезапуском, по числу неудач подряд. Растёт, чтобы при
# устойчивой поломке не молотить впустую, но первые попытки быстрые:
# обычный обрыв сети лечится сам за секунды.
BACKOFF_SEC = (15, 30, 60, 120, 300, 600)

# Столько неудач подряд — и мы сдаёмся. Значит дело не в сети.
GIVE_UP_AFTER = 8

LOG_PATH = ROOT / "data" / "supervisor.log"

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass


def next_delay(consecutive_failures: int) -> int:
    """Сколько ждать перед следующей попыткой."""
    if consecutive_failures <= 0:
        return 0
    idx = min(consecutive_failures - 1, len(BACKOFF_SEC) - 1)
    return BACKOFF_SEC[idx]


def run_was_stable(duration_sec: float) -> bool:
    """Прожил достаточно, чтобы считать падение случайным, а не поломкой."""
    return duration_sec >= STABLE_RUN_SEC


def should_give_up(consecutive_failures: int) -> bool:
    return consecutive_failures >= GIVE_UP_AFTER


def log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"{stamp} {message}"
    print(line, flush=True)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass  # без файла надзиратель всё равно работает


def main() -> int:
    target = [sys.executable, str(ROOT / "tracker_main.py")]
    failures = 0
    log("Надзиратель запущен")

    while True:
        started = time.time()
        try:
            code = subprocess.call(target, cwd=str(ROOT))
        except KeyboardInterrupt:
            log("Остановлено с клавиатуры")
            return 0
        duration = time.time() - started

        if code == 0:
            log(f"Трекер завершился штатно за {duration / 60:.1f} мин — выходим")
            return 0

        if run_was_stable(duration):
            failures = 1   # проработал долго: это сбой, а не поломка
            log(f"Трекер упал (код {code}) после {duration / 60:.1f} мин работы")
        else:
            failures += 1
            log(f"Трекер упал (код {code}) через {duration:.0f} с, "
                f"неудач подряд: {failures}")

        if should_give_up(failures):
            log(f"Сдаюсь: {failures} падений подряд без нормальной работы. "
                f"Похоже, сломан трекер, а не сеть — смотри data/tracker.log")
            return 1

        delay = next_delay(failures)
        log(f"Перезапуск через {delay} с")
        try:
            time.sleep(delay)
        except KeyboardInterrupt:
            log("Остановлено с клавиатуры")
            return 0


if __name__ == "__main__":
    sys.exit(main())
