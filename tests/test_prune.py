"""Чистка старых сделок — порциями, а не одним запросом.

Почему переписано
-----------------
На живой базе (9.1 ГБ, 11.3 млн сделок) под удаление попали 5.9 млн строк.
Одним `DELETE` это не прошло: журнал WAL распух до 3.5 ГБ, работа не
уложилась в отведённое время и откатилась целиком — то есть впустую, да
ещё и оставила после себя гигабайты журнала.

Порции фиксируются по отдельности: журнал остаётся небольшим, а прерванная
уборка сохраняет сделанное и в следующий раз продолжается с того же места.

Код разрушительный, поэтому проверяется отдельно: ошибка здесь не всплывёт
в логах — просто однажды не окажется нужных данных.
"""
from __future__ import annotations

NOW = 1_788_000_000
DAY = 86400


def fill(storage, count, start_ts, step=1):
    for i in range(count):
        storage.save_trade(
            tx_hash=f"0x{start_ts}{i:050x}", log_index=0, ts=start_ts + i * step,
            block_number=i, maker="0x" + "a" * 40, token_id="tok",
            side="buy", usdc_amount=100.0, price=0.5, condition_id="0xc",
        )
    storage.flush()


class TestCutoff:
    def test_старое_удаляется_свежее_остаётся(self, storage):
        fill(storage, 20, NOW - 30 * DAY, step=DAY // 4)   # старьё
        fill(storage, 15, NOW - 2 * DAY, step=60)          # свежее
        deleted = storage.prune_old_trades(older_than_days=7, now=NOW)
        assert deleted == 20
        assert storage.count_trades() == 15

    def test_граница_считается_по_дням(self, storage):
        """Ровно на границе строка ещё живёт: cutoff строгий."""
        fill(storage, 1, NOW - 7 * DAY + 10)
        fill(storage, 1, NOW - 7 * DAY - 10)
        assert storage.prune_old_trades(older_than_days=7, now=NOW) == 1
        assert storage.count_trades() == 1

    def test_нечего_удалять(self, storage):
        fill(storage, 5, NOW - 60)
        assert storage.prune_old_trades(older_than_days=7, now=NOW) == 0
        assert storage.count_trades() == 5

    def test_пустая_таблица(self, storage):
        assert storage.prune_old_trades(older_than_days=7, now=NOW) == 0


class TestChunking:
    def test_удаляет_больше_одной_порции(self, storage):
        """Ровно то, что сломалось на живой базе: строк больше, чем влезает
        в одну транзакцию."""
        fill(storage, 25, NOW - 30 * DAY, step=60)
        deleted = storage.prune_old_trades(older_than_days=7, now=NOW, chunk=10)
        assert deleted == 25
        assert storage.count_trades() == 0

    def test_ход_работы_сообщается(self, storage):
        """Уборка идёт минутами — молчание тут неотличимо от зависания."""
        fill(storage, 25, NOW - 30 * DAY, step=60)
        seen = []
        storage.prune_old_trades(older_than_days=7, now=NOW, chunk=10,
                                 progress=seen.append)
        assert seen == [10, 20, 25]

    def test_порции_ложатся_на_диск_сразу(self, storage, tmp_path):
        """Иначе дробление бессмысленно: всё скопится в одной незаписанной
        пачке, и прерывание снова отменит работу целиком."""
        fill(storage, 30, NOW - 30 * DAY, step=60)
        stopped = []

        def stop_after_first(done):
            stopped.append(done)
            if len(stopped) == 1:
                raise KeyboardInterrupt

        try:
            storage.prune_old_trades(older_than_days=7, now=NOW, chunk=10,
                                     progress=stop_after_first)
        except KeyboardInterrupt:
            pass

        # Открываем базу заново — видим ли мы результат первой порции?
        from polymarket_tracker.storage import Storage
        again = Storage(storage.db_path)
        assert again.count_trades() == 20, "первая порция не записалась"
        again.close()

    def test_прерванная_уборка_продолжается_с_того_же_места(self, storage):
        """Главное свойство порционной чистки: второй запуск доделывает
        остаток, а не начинает всё заново."""
        fill(storage, 30, NOW - 30 * DAY, step=60)

        def stop_at_10(done):
            if done >= 10:
                raise KeyboardInterrupt

        try:
            storage.prune_old_trades(older_than_days=7, now=NOW, chunk=10,
                                     progress=stop_at_10)
        except KeyboardInterrupt:
            pass
        assert storage.count_trades() == 20

        deleted = storage.prune_old_trades(older_than_days=7, now=NOW, chunk=10)
        assert deleted == 20
        assert storage.count_trades() == 0
