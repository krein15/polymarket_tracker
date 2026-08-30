"""Отложенный коммит: одно соединение, пачки записей, видимость снаружи.

Смысл механизма — в docs/PERFORMANCE.md: коммит на каждую операцию стоил
82 КБ записи на диск на одну сделку в 300 байт.
"""
from __future__ import annotations

import sqlite3

from conftest import NOW

from polymarket_tracker.storage import Storage


def other_process_view(path: str) -> sqlite3.Connection:
    """Соединение «как из другого процесса» — видит только закоммиченное."""
    return sqlite3.connect(path)


def add_trade(storage: Storage, i: int) -> bool:
    return storage.save_trade(
        tx_hash=f"0x{i:064x}", log_index=0, ts=NOW + i, block_number=0,
        maker="0x" + "a" * 40, token_id="token-1", side="buy",
        usdc_amount=100.0, price=0.5,
    )


class TestDeferredCommit:
    def test_записи_копятся_и_не_видны_снаружи_до_коммита(self, tmp_path):
        path = str(tmp_path / "t.db")
        s = Storage(path, commit_every=50, commit_interval=3600)
        for i in range(5):
            add_trade(s, i)
        outside = other_process_view(path)
        assert outside.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0
        outside.close()

    def test_flush_делает_их_видимыми(self, tmp_path):
        path = str(tmp_path / "t.db")
        s = Storage(path, commit_every=50, commit_interval=3600)
        for i in range(5):
            add_trade(s, i)
        s.flush()
        outside = other_process_view(path)
        assert outside.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 5
        outside.close()

    def test_пачка_коммитится_сама_при_достижении_порога(self, tmp_path):
        path = str(tmp_path / "t.db")
        s = Storage(path, commit_every=10, commit_interval=3600)
        for i in range(10):
            add_trade(s, i)
        outside = other_process_view(path)
        assert outside.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 10
        outside.close()

    def test_close_дописывает_остаток(self, tmp_path):
        path = str(tmp_path / "t.db")
        s = Storage(path, commit_every=1000, commit_interval=3600)
        for i in range(3):
            add_trade(s, i)
        s.close()
        outside = other_process_view(path)
        assert outside.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 3
        outside.close()

    def test_чтения_не_дёргают_коммит(self, tmp_path):
        """Счётчик пачки растёт только на записях — иначе любой запрос
        сбрасывал бы её на диск и смысл пакетирования терялся."""
        path = str(tmp_path / "t.db")
        s = Storage(path, commit_every=3, commit_interval=3600)
        add_trade(s, 0)
        for _ in range(10):
            s.count_trades()
            s.get_wallet("0x" + "a" * 40)
        outside = other_process_view(path)
        assert outside.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0
        outside.close()


class TestConsistencyInsideBatch:
    def test_дедуп_работает_внутри_незакоммиченной_пачки(self, tmp_path):
        s = Storage(str(tmp_path / "t.db"), commit_every=1000, commit_interval=3600)
        assert add_trade(s, 1) is True
        assert add_trade(s, 1) is False  # тот же tx_hash

    def test_свои_записи_видны_себе_же_до_коммита(self, tmp_path):
        """Признаки скоринга читают только что записанную сделку — если бы
        она была не видна до коммита, накопление считалось бы неверно."""
        s = Storage(str(tmp_path / "t.db"), commit_every=1000, commit_interval=3600)
        for i in range(4):
            add_trade(s, i)
        total, n = s.sum_wallet_buys_for_token("0x" + "a" * 40, "token-1", NOW - 1)
        assert (total, n) == (400.0, 4)

    def test_агрегаты_кошелька_накапливаются_в_пачке(self, tmp_path):
        s = Storage(str(tmp_path / "t.db"), commit_every=1000, commit_interval=3600)
        for i in range(3):
            stats = s.upsert_wallet_trade("0x" + "b" * 40, NOW + i, 10.0)
        assert stats.trade_count == 3
        assert stats.total_volume_usdc == 30.0


class TestWalMode:
    def test_включён_wal(self, tmp_path):
        """WAL нужен, чтобы tools/*.py читали базу, пока трекер пишет."""
        path = str(tmp_path / "t.db")
        s = Storage(path)
        s.flush()
        outside = other_process_view(path)
        mode = outside.execute("PRAGMA journal_mode").fetchone()[0]
        outside.close()
        assert mode.lower() == "wal"

    def test_читатель_из_другого_процесса_не_блокируется_писателем(self, tmp_path):
        path = str(tmp_path / "t.db")
        s = Storage(path, commit_every=1000, commit_interval=3600)
        add_trade(s, 0)          # транзакция открыта и не закоммичена
        outside = other_process_view(path)
        outside.execute("PRAGMA busy_timeout=2000")
        # Не должно быть 'database is locked'
        assert outside.execute("SELECT COUNT(*) FROM wallets").fetchone()[0] == 0
        outside.close()
