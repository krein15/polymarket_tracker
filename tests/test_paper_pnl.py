"""Порог цены входа в отчёте о бумажной прибыли.

Отчёт должен считать то же самое, что трекер реально разрешает. Вердикт
по цене входа запрещает вход дешевле ENTRY_MIN_PRICE, значит и в подсчёте
прибыли такие сделки участвовать не должны — иначе отчёт меряет стратегию,
которой уже нет.

Порог не выдуман: на 541 сигнале с замером и исходом вход ниже 0.50 — это
четверть сделок и 84% всего убытка, и там в минусе сам трейдер.
"""
from __future__ import annotations

import sqlite3

from conftest import NOW

from tools.paper_pnl import report


def signal(storage, sid_price, entry, settled, ts=NOW, stype="score"):
    """Сигнал с замером цены входа и закрывшимся исходом."""
    sid = storage.save_signal(
        ts=ts, signal_type=stype, maker="0x" + "a" * 40,
        token_id=f"tok{entry}{settled}{ts}", market_slug="m",
        usdc_amount=5000.0, price=sid_price, reason="t",
        tx_hash=f"0x{ts}{entry}{settled}", side="buy",
    )
    storage.save_signal_entry(
        signal_id=sid, measured_ts=ts + 120, delay_sec=120,
        best_ask=entry, fill_500=entry, fill_2000=entry, fill_5000=entry,
        depth_usdc=50_000.0,
    )
    storage.init_outcome_record(sid, ts)
    with storage._conn() as c:
        c.execute(
            "UPDATE signal_outcomes SET market_resolved = 1, settled_price = ? "
            "WHERE signal_id = ?", (settled, sid))
    storage.flush()
    return sid


def run(storage, capsys, **kw):
    storage.close()
    report(storage.db_path, 2000, kw.pop("stype", "all"), **kw)
    return capsys.readouterr().out


class TestПорогВхода:
    def test_без_порога_считаются_все(self, storage, capsys):
        signal(storage, 0.40, 0.42, 1.0)
        signal(storage, 0.30, 0.30, 0.0)
        out = run(storage, capsys)
        assert "прибыльных: 1/2" in out

    def test_порог_убирает_дешёвые(self, storage, capsys):
        signal(storage, 0.70, 0.72, 1.0)
        signal(storage, 0.30, 0.30, 0.0)
        out = run(storage, capsys, min_entry=0.50)
        assert "прибыльных: 1/1" in out
        assert "отсечено порогом 0.50: 1" in out

    def test_показан_убыток_отсечённых(self, storage, capsys):
        """Главное число отчёта: сколько мы сэкономили, а не сколько
        осталось. Иначе порог выглядит как потеря сигналов."""
        signal(storage, 0.70, 0.72, 1.0)
        signal(storage, 0.30, 0.30, 0.0)
        out = run(storage, capsys, min_entry=0.50)
        assert "их ROI, если бы вошли: -100.0%" in out

    def test_граница_включительно(self, storage, capsys):
        """0.50-0.60 на данных уже нейтральна — выбрасывать её незачем."""
        signal(storage, 0.48, 0.50, 1.0)
        out = run(storage, capsys, min_entry=0.50)
        assert "прибыльных: 1/1" in out

    def test_дата_отсекает_старое(self, storage, capsys):
        """Поток после правок другой, и мерить его надо отдельно."""
        signal(storage, 0.70, 0.72, 1.0, ts=NOW - 30 * 86400)
        signal(storage, 0.70, 0.72, 0.0, ts=NOW)
        import time
        since = time.strftime("%Y-%m-%d", time.localtime(NOW - 86400))
        out = run(storage, capsys, since=since)
        assert "прибыльных: 0/1" in out

    def test_тип_сигнала_фильтруется(self, storage, capsys):
        signal(storage, 0.70, 0.72, 1.0, stype="onchain_early")
        signal(storage, 0.70, 0.72, 0.0, stype="score")
        out = run(storage, capsys, stype="onchain_early")
        assert "прибыльных: 1/1" in out
