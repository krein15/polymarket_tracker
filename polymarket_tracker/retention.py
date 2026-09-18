"""Автоматическая чистка старых сделок.

Зачем
-----
Ретеншн был только ручным (tools/db_maintenance.py). 12.09 базу почистили
с 9.1 ГБ до 3.5 ГБ, а к 19.09 она снова выросла до 8.7 ГБ: сделок
приходит ~1.2 млн в сутки, и никто их не удалял.

Чем плох рост, кроме места: все индексы растут вместе с таблицей, и любой
запрос, читающий индекс целиком, дорожает пропорционально. Именно так
"последняя сделка" дошла до 28 секунд.

Почему можно чистить
--------------------
Сырые сделки нужны на окнах от 20 минут до часа: кластер, опорная цена,
погоня. Признак "пробуждения" берёт историю кошелька из API и лишь в запас
смотрит сюда. Скорингу нужно не меньше HISTORY_MIN_DAYS (3 дня) локальной
истории — семь дней с запасом.

Всё ценное (сигналы, исходы, теневые сделки, кошельки) лежит в других
таблицах и этой чисткой не затрагивается.

Как чистит
----------
Раз в час, порциями, с потолком на один проход. Потолок нужен, чтобы после
долгого простоя первая чистка не забрала диск на полчаса: хвост разберётся
за несколько проходов. Место на диске чистка не возвращает — освободившиеся
страницы SQLite использует заново, и база перестаёт расти. Вернуть место
целиком может только VACUUM, а он для работающего трекера слишком тяжёл.
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

# Сколько дней сырых сделок держать.
RETENTION_DAYS = 7

# Как часто чистить. Час: за это время копится ~50 000 строк — одна порция.
INTERVAL_SEC = 3600

# Первая чистка — не сразу после старта: трекер в это время догоняет
# пропущенное, и диск нужнее ему.
FIRST_RUN_DELAY_SEC = 600

# Потолок строк за один проход. После недели простоя хвост в миллионы строк
# разберётся за несколько часов, не отнимая диск у приёма сделок.
MAX_ROWS_PER_RUN = 500_000

# Порция и пауза между порциями. SQLite синхронный: пока идёт удаление,
# трекер стоит. Поэтому удаляем по 10 000 строк и между порциями отдаём
# цикл приёму сделок — иначе уборка сама стала бы тем, от чего лечим.
SLICE_ROWS = 10_000
SLICE_PAUSE_SEC = 1.0


class RetentionTask:
    """Фоновая задача: раз в час удалять сделки старше RETENTION_DAYS."""

    def __init__(self, storage, days: int = RETENTION_DAYS,
                 max_rows: int = MAX_ROWS_PER_RUN):
        self.storage = storage
        self.days = days
        self.max_rows = max_rows
        self.stats = {"runs": 0, "deleted": 0}

    async def run_once(self, pause: float = SLICE_PAUSE_SEC) -> int:
        """Один проход короткими порциями. Возвращает число удалённых строк."""
        deleted = 0
        while deleted < self.max_rows:
            want = min(SLICE_ROWS, self.max_rows - deleted)
            n = self.storage.prune_old_trades(
                older_than_days=self.days, max_rows=want)
            deleted += n
            if n < want:
                break          # старых больше нет
            await asyncio.sleep(pause)   # отдать цикл приёму сделок
        self.stats["runs"] += 1
        self.stats["deleted"] += deleted
        if deleted:
            log.info("Чистка истории: удалено %d сделок старше %d дн.",
                     deleted, self.days)
        return deleted

    async def run(self) -> None:
        log.info("Чистка истории: сделки старше %d дн., раз в %d мин",
                 self.days, INTERVAL_SEC // 60)
        try:
            await asyncio.sleep(FIRST_RUN_DELAY_SEC)
        except asyncio.CancelledError:
            return
        while True:
            try:
                await self.run_once()
                await asyncio.sleep(INTERVAL_SEC)
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001 — уборка не роняет трекер
                log.warning("Чистка истории: ошибка: %s", e)
                try:
                    await asyncio.sleep(INTERVAL_SEC)
                except asyncio.CancelledError:
                    break
