"""Автоматическая чистка истории и индекс по времени сделки.

Откуда
------
19.09 трекер заметно грузил диск. Причина нашлась в запросах, которые
отвечают на вопрос "какая последняя сделка":

    MAX(ts)   28 с   подтверждение погони и сторож простоя, раз в 2 мин
    MIN(ts)   22 с   скоринг, прямо в цикле приёма сделок, раз в 5 мин

Индекса по времени не было, и оба запроса читали индекс по токену целиком.
Больше половины времени диск был занят этими сканами, а синхронный SQLite
на эти 22-28 секунд останавливал весь трекер.

Второе: база за неделю выросла с 3.5 до 8.7 ГБ, потому что ретеншн был
только ручным. Вместе с таблицей растут все индексы, и каждый скан
дорожает пропорционально.
"""
from __future__ import annotations

import asyncio

from polymarket_tracker.retention import RetentionTask
from polymarket_tracker.storage import Storage

NOW = 1_788_000_000
DAY = 86400


def fill(storage, count, start_ts, step=60):
    for i in range(count):
        storage.save_trade(
            tx_hash=f"0x{start_ts}{i:050x}", log_index=0, ts=start_ts + i * step,
            block_number=i, maker="0x" + "a" * 40, token_id=f"tok{i % 5}",
            side="buy", usdc_amount=100.0, price=0.5, condition_id="0xc",
        )
    storage.flush()


class TestTsIndex:
    def test_индекс_по_времени_создаётся(self, tmp_path):
        st = Storage(str(tmp_path / "t.db"))
        with st._conn() as c:
            names = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='index'")}
        st.close()
        assert "idx_trades_ts" in names

    def test_последняя_сделка_берётся_через_индекс(self, tmp_path):
        """Без индекса план — чтение индекса по токену целиком. Проверяем
        именно план: на маленькой тестовой базе скан быстр и по времени
        разницы не видно, а на 8.7 ГБ он шёл 28 секунд."""
        st = Storage(str(tmp_path / "t.db"))
        with st._conn() as c:
            for sql in ("SELECT MAX(ts) FROM trades", "SELECT MIN(ts) FROM trades"):
                plan = " ".join(r[-1] for r in c.execute("EXPLAIN QUERY PLAN " + sql))
                assert "idx_trades_ts" in plan, f"{sql}: {plan}"
        st.close()

    def test_поиск_старых_тоже_через_индекс(self, tmp_path):
        """Иначе каждая порция чистки сканировала бы таблицу."""
        st = Storage(str(tmp_path / "t.db"))
        with st._conn() as c:
            plan = " ".join(r[-1] for r in c.execute(
                "EXPLAIN QUERY PLAN SELECT rowid FROM trades WHERE ts < ? LIMIT 10",
                (NOW,)))
        st.close()
        assert "idx_trades_ts" in plan

    def test_индекс_появляется_на_старой_базе(self, tmp_path):
        """На работающей базе его нет — миграция должна добавить."""
        db = tmp_path / "t.db"
        st = Storage(str(db))
        with st._conn() as c:
            c.execute("DROP INDEX IF EXISTS idx_trades_ts")
        st.flush()
        st.close()
        again = Storage(str(db))
        with again._conn() as c:
            names = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='index'")}
        again.close()
        assert "idx_trades_ts" in names


class TestPruneLimit:
    def test_предел_на_вызов(self, storage):
        fill(storage, 30, NOW - 30 * DAY)
        assert storage.prune_old_trades(older_than_days=7, now=NOW, max_rows=12) == 12
        assert storage.count_trades() == 18

    def test_предел_больше_хвоста(self, storage):
        fill(storage, 5, NOW - 30 * DAY)
        assert storage.prune_old_trades(older_than_days=7, now=NOW, max_rows=100) == 5

    def test_нулевой_предел_ничего_не_трогает(self, storage):
        fill(storage, 5, NOW - 30 * DAY)
        assert storage.prune_old_trades(older_than_days=7, now=NOW, max_rows=0) == 0
        assert storage.count_trades() == 5


class TestRetentionTask:
    def test_удаляет_старое_оставляет_свежее(self, storage, monkeypatch):
        import time
        now = int(time.time())
        fill(storage, 20, now - 30 * DAY)
        fill(storage, 15, now - 2 * DAY)
        task = RetentionTask(storage, days=7)
        deleted = asyncio.run(task.run_once(pause=0))
        assert deleted == 20
        assert storage.count_trades() == 15

    def test_потолок_на_проход(self, storage):
        """После долгого простоя первая чистка не должна забрать диск на
        полчаса — хвост разбирается за несколько проходов."""
        import time
        now = int(time.time())
        fill(storage, 40, now - 30 * DAY)
        task = RetentionTask(storage, days=7, max_rows=25)
        assert asyncio.run(task.run_once(pause=0)) == 25
        assert storage.count_trades() == 15
        assert asyncio.run(task.run_once(pause=0)) == 15

    def test_между_порциями_отдаёт_цикл(self, storage, monkeypatch):
        """Главное свойство: SQLite синхронный, и удаление одним куском
        остановило бы приём сделок — ровно то, от чего лечим."""
        import time
        import polymarket_tracker.retention as ret
        now = int(time.time())
        fill(storage, 35, now - 30 * DAY)
        monkeypatch.setattr(ret, "SLICE_ROWS", 10)
        pauses = []

        async def fake_sleep(sec):
            pauses.append(sec)

        monkeypatch.setattr(ret.asyncio, "sleep", fake_sleep)
        asyncio.run(RetentionTask(storage, days=7).run_once(pause=0.5))
        # 35 строк порциями по 10: после каждой полной порции — пауза.
        assert pauses == [0.5, 0.5, 0.5]

    def test_нечего_удалять(self, storage):
        import time
        fill(storage, 5, int(time.time()) - 60)
        assert asyncio.run(RetentionTask(storage, days=7).run_once(pause=0)) == 0

    def test_срок_хранения_с_запасом_для_скоринга(self):
        """Скорингу нужно HISTORY_MIN_DAYS локальной истории — иначе
        признаки новизны и кластера отключаются."""
        from polymarket_tracker.retention import RETENTION_DAYS
        from polymarket_tracker.scoring import HISTORY_MIN_DAYS
        assert RETENTION_DAYS >= HISTORY_MIN_DAYS * 2
