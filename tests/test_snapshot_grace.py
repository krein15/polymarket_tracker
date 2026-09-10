"""Снимок цены не берётся, если брать его уже поздно.

Зачем ограничение
-----------------
Поле называется price_1h. Если очередь дошла до строки через сутки, старый
код всё равно записывал туда ТЕКУЩУЮ цену — суточной давности вместо часовой.
Ошибка не падает и не видна: колонка заполнена, замер дрейфа считается, числа
получаются уверенные и неверные.

Так и было на живых данных: у рынков, закрывшихся за 3-12 часов, снимок
через час есть лишь у 12.7%, а у живущих дольше двух суток — у 98.4%. То
есть дрейф мерился почти только по медленным рынкам, тогда как поток
трекера — быстрый спорт.

Теперь просроченное сверх допуска честно остаётся пустым.
"""
from __future__ import annotations

import asyncio

from conftest import NOW

from polymarket_tracker.outcome_tracker import (
    SNAPSHOT_GRACE_FACTOR,
    OutcomeTracker,
    _OutcomeTarget,
)

HOUR = 3600


class FakeMarketCtx:
    """Рынок жив и торгуется — значит дело дойдёт до снимков, а не до резолва."""

    def __init__(self, price=0.55):
        self.price = price

    async def fetch_fresh(self, token_id):
        class Info:
            closed = False
            settled_price = None
            last_trade_price = self.price

        return Info()


class Recorder:
    """Запоминает, какие поля трекер попросил заполнить."""

    def __init__(self):
        self.calls = []

    def update_snapshots(self, row_id, now_ts, current_price, **kwargs):
        self.calls.append(kwargs)

    def finalize(self, *a, **k):
        raise AssertionError("рынок не закрыт — финализировать нечего")


def run_one(age_sec: int, has_1h: bool = False):
    rec = Recorder()
    target = _OutcomeTarget(
        name="shadow", id_key="shadow_id",
        get_candidates=lambda limit: [],
        update_snapshots=rec.update_snapshots,
        finalize=rec.finalize, batch_limit=10,
    )
    tracker = OutcomeTracker.__new__(OutcomeTracker)
    tracker.market_ctx = FakeMarketCtx()
    row = {
        "shadow_id": 1, "token_id": "tok", "side": "buy",
        "price_at_signal": 0.40, "signal_ts": NOW - age_sec,
        "has_price_1h": has_1h, "has_price_24h": False, "has_price_7d": False,
    }
    asyncio.run(tracker._update_one(target, row, NOW))
    return rec.calls[0]


class TestSnapshotGrace:
    def test_вовремя_снимок_берётся(self):
        assert run_one(HOUR + 60).get("set_price_1h") is True

    def test_на_границе_допуска_ещё_берётся(self):
        assert run_one(HOUR * SNAPSHOT_GRACE_FACTOR).get("set_price_1h") is True

    def test_протухший_снимок_не_берётся(self):
        """Сутки спустя это уже не "цена через час", а другое число."""
        assert run_one(24 * HOUR).get("set_price_1h") is None

    def test_час_ещё_не_прошёл(self):
        assert run_one(600).get("set_price_1h") is None

    def test_уже_снятое_не_перезаписывается(self):
        assert run_one(HOUR + 60, has_1h=True).get("set_price_1h") is None

    def test_окна_независимы(self):
        """Через сутки с небольшим час безнадёжно просрочен, а суточное
        окно как раз наступило — и оно должно сработать."""
        kw = run_one(25 * HOUR)
        assert kw.get("set_price_1h") is None
        assert kw.get("set_price_24h") is True
