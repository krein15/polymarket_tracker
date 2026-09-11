"""Настройка записи: размер пачки коммитов и снос лишнего индекса.

Замеры, на которых всё держится (11.09.2026)
--------------------------------------------
Живой трекер: база 9.1 ГБ, 11.2 млн сделок, процесс пишет ~1 ГБ в час при
приросте самой базы 2 ГБ в сутки.

Синтетика, 40 000 вставок, WAL + synchronous=NORMAL:

    коммит раз в   50 записей ... 23.8 с
    коммит раз в 1000 записей ...  6.8 с
    все три индекса ............ 23.4 с
    без idx_trades_maker_cond .. 11.6 с

Про кеш страниц вышла поучительная история. На коротких транзакциях он не
меняет ничего (22.9 против 22.8 с), и первый замер дал вывод "не нужен".
Но после перехода на пачку 500/10с запись на живом трекере выросла с
1 ГБ/ч до 6 ГБ/ч: за десять секунд накапливается около 2 МБ изменённых
страниц — ровно размер кеша по умолчанию, — он переполняется и сбрасывает
их посреди транзакции, а при коммите те же страницы пишутся заново.

Вывод: размер пачки и размер кеша — одна настройка, а не две. По
отдельности каждая выглядит безобидной.
"""
from __future__ import annotations

import time

from polymarket_tracker.storage import (
    CACHE_MB,
    COMMIT_EVERY,
    COMMIT_INTERVAL_SEC,
    Storage,
)

NOW = 1_788_000_000


def write(st, i):
    st.save_trade(
        tx_hash=f"0x{i:064x}", log_index=0, ts=NOW + i, block_number=i,
        maker="0x" + f"{i % 50:038x}", token_id=f"tok{i % 20}",
        side="buy", usdc_amount=100.0, price=0.5, condition_id=f"0xc{i % 7}",
    )


class TestBatchSize:
    def test_пачка_заметно_больше_интервала_потока(self):
        """Поток ~14 сделок в секунду. Если COMMIT_EVERY меньше, чем сделок
        за интервал, частоту задаёт счётчик, и поднимать интервал бессмысленно
        — ровно этим и была прежняя пара 50/2с."""
        assert COMMIT_EVERY > 14 * COMMIT_INTERVAL_SEC

    def test_окно_потери_ограничено(self):
        """При падении теряются сделки за интервал. Догрузка их перечитает,
        но окно должно оставаться обозримым."""
        assert COMMIT_INTERVAL_SEC <= 30

    def test_записи_не_теряются_в_пачке(self, tmp_path):
        """Главный риск отложенного коммита: данные есть в памяти, но не на
        диске. flush обязан их дописать."""
        db = tmp_path / "t.db"
        st = Storage(str(db))
        for i in range(COMMIT_EVERY // 2):
            write(st, i)
        st.flush()
        st.close()

        again = Storage(str(db))
        assert again.count_trades() == COMMIT_EVERY // 2
        again.close()

    def test_пачка_фиксируется_по_счётчику(self, tmp_path):
        """Дойдя до COMMIT_EVERY, пачка должна лечь на диск сама, без flush."""
        db = tmp_path / "t.db"
        st = Storage(str(db), commit_every=10, commit_interval=3600.0)
        for i in range(10):
            write(st, i)
        reader = Storage(str(db))
        assert reader.count_trades() == 10
        reader.close()
        st.close()

    def test_пачка_фиксируется_по_времени(self, tmp_path):
        """Редкий поток не должен висеть в памяти до заполнения счётчика."""
        db = tmp_path / "t.db"
        st = Storage(str(db), commit_every=10_000, commit_interval=0.01)
        write(st, 1)
        time.sleep(0.05)
        write(st, 2)
        reader = Storage(str(db))
        assert reader.count_trades() >= 1
        reader.close()
        st.close()


class TestIndexes:
    def test_лишний_индекс_не_создаётся(self, tmp_path):
        st = Storage(str(tmp_path / "t.db"))
        with st._conn() as c:
            names = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='index'")}
        st.close()
        assert "idx_trades_maker_cond" not in names

    def test_старый_индекс_сносится_при_открытии(self, tmp_path):
        """На уже работающей базе он есть — миграция должна его убрать."""
        db = tmp_path / "t.db"
        st = Storage(str(db))
        with st._conn() as c:
            c.execute("CREATE INDEX IF NOT EXISTS idx_trades_maker_cond "
                      "ON trades(maker, condition_id, ts)")
        st.flush()
        st.close()

        again = Storage(str(db))
        with again._conn() as c:
            names = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='index'")}
        again.close()
        assert "idx_trades_maker_cond" not in names

    def test_индекс_по_кошельку_остался(self, tmp_path):
        """Именно он теперь обслуживает проверку хеджа."""
        st = Storage(str(tmp_path / "t.db"))
        with st._conn() as c:
            names = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='index'")}
        st.close()
        assert "idx_trades_maker_ts" in names

    def test_проверка_хеджа_работает_без_него(self, tmp_path):
        """Смысл признака не должен зависеть от того, каким индексом он
        обслуживается."""
        st = Storage(str(tmp_path / "t.db"))
        for i, token in enumerate(("yes", "no")):
            st.save_trade(
                tx_hash=f"0xh{i}", log_index=0, ts=NOW + i, block_number=0,
                maker="0x" + "a" * 40, token_id=token, side="buy",
                usdc_amount=500.0, price=0.5, condition_id="0xcond",
            )
        st.flush()
        assert st.wallet_bought_both_outcomes("0x" + "a" * 40, "0xcond", NOW - 10)
        assert not st.wallet_bought_both_outcomes("0x" + "b" * 40, "0xcond", NOW - 10)
        st.close()


class TestCache:
    """Кеш страниц и размер пачки — одна настройка, а не две.

    Замерено на живом трекере: пачка 500/10с при кеше по умолчанию (2 МБ)
    подняла запись с 1 ГБ/ч до 6 ГБ/ч. За 10 секунд накапливается около
    2 МБ изменённых страниц — кеш переполняется и сбрасывает их посреди
    транзакции, а при коммите те же страницы пишутся заново.

    По отдельности ни один замер этого не показывает: на коротких
    транзакциях кеш не влияет вовсе (22.9 против 22.8 с). Именно поэтому
    первая попытка мерить кеш в отрыве от пачки дала ложный вывод "не
    нужен".
    """

    def test_кеш_выставлен(self, tmp_path):
        st = Storage(str(tmp_path / "t.db"))
        with st._conn() as c:
            got = c.execute("PRAGMA cache_size").fetchone()[0]
        st.close()
        # Отрицательное значение у SQLite означает килобайты, а не страницы.
        assert got == -CACHE_MB * 1024
        assert got != -2000, "это умолчание SQLite, PRAGMA не применилась"

    def test_кеш_вмещает_пачку(self, tmp_path):
        """Груба оценка: около 5 изменённых страниц по 4 КБ на сделку при
        ~14 сделках в секунду. Кеш должен покрывать весь интервал с запасом."""
        dirty_mb = 14 * COMMIT_INTERVAL_SEC * 5 * 4096 / 1024 / 1024
        assert CACHE_MB > dirty_mb * 4

    def test_кеш_настраивается(self, tmp_path):
        st = Storage(str(tmp_path / "t.db"), cache_mb=8)
        with st._conn() as c:
            assert c.execute("PRAGMA cache_size").fetchone()[0] == -8 * 1024
        st.close()

    def test_бессмысленное_значение_не_ломает(self, tmp_path):
        """Ноль или минус означали бы у SQLite совсем другое."""
        st = Storage(str(tmp_path / "t.db"), cache_mb=0)
        with st._conn() as c:
            assert c.execute("PRAGMA cache_size").fetchone()[0] < 0
        st.close()

    def test_режим_журнала_не_сбит(self, tmp_path):
        st = Storage(str(tmp_path / "t.db"))
        with st._conn() as c:
            assert c.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
            assert c.execute("PRAGMA synchronous").fetchone()[0] == 1
        st.close()
