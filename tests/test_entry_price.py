"""Достижимая цена входа: обход стакана и запись замера.

Зачем модуль вообще появился
----------------------------
Вся прибыль считалась от цены ТРЕЙДЕРА, а сигнал приходит после того, как
рынок за ним пошёл. Пересчёт на цену последователей (507 сделок с исходом):

    от его цены                 ROI +44.4%
    по цене последователей      ROI  +4.2%   [-1.7; +10.1]
    плюс проскальзывание 2%     ROI  +2.2%   [-3.7;  +8.1]

Перевес живёт в его цене входа, которой у нас нет. Пока достижимая цена не
записана, любой подсчёт прибыли — самообман.
"""
from __future__ import annotations

import asyncio

from conftest import NOW

from polymarket_tracker.entry_price import (
    EntryPriceTracker,
    fill_price,
    parse_book,
)

# Стакан с живого рынка: 0.59 на $3634, дальше дороже.
BOOK = [(0.59, 6160.0), (0.60, 16427.0), (0.61, 3076.0), (0.62, 308.0)]


class TestFillPrice:
    def test_мелкий_ордер_по_лучшей_цене(self):
        assert abs(fill_price(BOOK, 500.0) - 0.59) < 1e-9

    def test_крупный_ордер_съедает_уровни(self):
        """Верх книги — это цена первой сотни долларов. Сигнальные сделки
        измеряются тысячами и уходят вглубь."""
        price = fill_price(BOOK, 6000.0)
        assert 0.59 < price < 0.60

    def test_чем_больше_объём_тем_хуже_цена(self):
        """Если $5000 заметно дороже $500 — стратегия не масштабируется,
        и это надо видеть, а не усреднять."""
        prices = [fill_price(BOOK, s) for s in (500.0, 2000.0, 5000.0, 12000.0)]
        assert prices == sorted(prices)
        assert prices[0] < prices[-1]

    def test_не_хватило_глубины_честный_none(self):
        """Сделать вид, что налилось по последней доступной цене, значило бы
        приукрасить результат ровно там, где рынок нам его не даёт."""
        assert fill_price(BOOK, 10_000_000.0) is None

    def test_пустой_стакан(self):
        assert fill_price([], 500.0) is None

    def test_нулевой_объём_не_делит_на_ноль(self):
        assert fill_price(BOOK, 0.0) is None

    def test_порядок_уровней_не_важен(self):
        assert fill_price(list(reversed(BOOK)), 6000.0) == fill_price(BOOK, 6000.0)

    def test_битые_уровни_пропускаются(self):
        bad = [(0.0, 100.0), (0.59, 6160.0), (0.60, -5.0)]
        assert abs(fill_price(bad, 500.0) - 0.59) < 1e-9

    def test_средняя_взвешена_по_долям_а_не_по_уровням(self):
        """Простое среднее цен уровней дало бы 0.55 вместо 0.5025."""
        book = [(0.50, 1000.0), (0.60, 1000.0)]
        price = fill_price(book, 510.0)
        assert abs(price - 0.5025) < 0.002


class TestParseBook:
    def test_разбирает_ответ(self):
        payload = {"asks": [{"price": "0.59", "size": "100"},
                            {"price": "0.60", "size": "50"}]}
        assert parse_book(payload) == [(0.59, 100.0), (0.60, 50.0)]

    def test_кривой_уровень_не_теряет_стакан(self):
        payload = {"asks": [{"price": "0.59", "size": "100"},
                            {"price": "нет", "size": "50"},
                            {"size": "10"}]}
        assert parse_book(payload) == [(0.59, 100.0)]

    def test_пустой_и_битый_ответ(self):
        assert parse_book({}) == []
        assert parse_book(None) == []
        assert parse_book({"asks": None}) == []

    def test_биды_не_путаются_с_асками(self):
        """Ловушка, стоившая бы целого спреда: у Polymarket
        price?side=buy возвращает БИД (0.58), а покупаем мы по АСКУ (0.59).
        Здесь читаем только asks и ничего не угадываем."""
        payload = {"bids": [{"price": "0.58", "size": "999"}],
                   "asks": [{"price": "0.59", "size": "100"}]}
        assert parse_book(payload) == [(0.59, 100.0)]


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status
        self.calls = []

    def get(self, url, params=None):
        self.calls.append(params)
        return FakeResponse(self.payload, self.status)


class Cfg:
    entry_delay_sec = 120


def signal(storage, ts, token="tok-1", price=0.40):
    return storage.save_signal(
        ts=ts, signal_type="score", maker="0x" + "a" * 40, token_id=token,
        market_slug="m", usdc_amount=5000.0, price=price, reason="t",
        tx_hash=f"0x{ts}", side="buy",
    )


BOOK_PAYLOAD = {"asks": [{"price": "0.59", "size": "6160"},
                         {"price": "0.60", "size": "16427"}]}


class TestTrackerPass:
    def _run(self, storage, session, now):
        tracker = EntryPriceTracker(storage, Cfg())

        async def go():
            rows = storage.signals_awaiting_entry(
                ready_before=now - Cfg.entry_delay_sec, oldest=now - 1800, limit=20)
            for row in rows:
                await tracker._one(session, row, now)

        asyncio.run(go())
        return tracker

    def test_замер_записывается(self, storage):
        sid = signal(storage, NOW - 300)
        session = FakeSession(BOOK_PAYLOAD)
        self._run(storage, session, NOW)
        with storage._conn() as c:
            row = c.execute("SELECT * FROM signal_entries WHERE signal_id=?",
                            (sid,)).fetchone()
        assert row is not None
        assert abs(row["best_ask"] - 0.59) < 1e-9
        assert abs(row["fill_500"] - 0.59) < 1e-9
        assert row["delay_sec"] == 300

    def test_свежий_сигнал_ещё_не_замеряется(self, storage):
        """Раньше задержки снимать нечестно: так быстро человек не успеет."""
        signal(storage, NOW - 10)
        rows = storage.signals_awaiting_entry(
            ready_before=NOW - Cfg.entry_delay_sec, oldest=NOW - 1800, limit=20)
        assert rows == []

    def test_старый_сигнал_не_замеряется(self, storage):
        """После простоя цена получасовой давности не та, которую человек
        увидел бы по сигналу."""
        signal(storage, NOW - 7200)
        rows = storage.signals_awaiting_entry(
            ready_before=NOW - Cfg.entry_delay_sec, oldest=NOW - 1800, limit=20)
        assert rows == []

    def test_замер_не_повторяется(self, storage):
        signal(storage, NOW - 300)
        session = FakeSession(BOOK_PAYLOAD)
        self._run(storage, session, NOW)
        self._run(storage, session, NOW)
        assert len(session.calls) == 1

    def test_нет_стакана_замер_не_пишется(self, storage):
        """Рынок мог закрыться — это не сбой, но и не цена."""
        sid = signal(storage, NOW - 300)
        tracker = self._run(storage, FakeSession(None, status=404), NOW)
        with storage._conn() as c:
            assert c.execute("SELECT COUNT(*) FROM signal_entries WHERE signal_id=?",
                             (sid,)).fetchone()[0] == 0
        assert tracker.stats["no_book"] == 1

    def test_тонкий_стакан_пишет_пустые_цены(self, storage):
        """Пустая цена — тоже результат: объём не налился бы."""
        sid = signal(storage, NOW - 300)
        thin = {"asks": [{"price": "0.59", "size": "100"}]}   # всего $59
        self._run(storage, FakeSession(thin), NOW)
        with storage._conn() as c:
            row = c.execute("SELECT * FROM signal_entries WHERE signal_id=?",
                            (sid,)).fetchone()
        assert row["best_ask"] is not None
        assert row["fill_500"] is None and row["fill_5000"] is None
        assert row["depth_usdc"] < 100

    def test_запрашивается_нужный_токен(self, storage):
        signal(storage, NOW - 300, token="tok-важный")
        session = FakeSession(BOOK_PAYLOAD)
        self._run(storage, session, NOW)
        assert session.calls[0]["token_id"] == "tok-важный"
