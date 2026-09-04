"""Догрузка страниц до чекпоинта и неблокирующая история кошелька.

Обе правки сделаны по одному замеру: база стабильно отставала от API на
8-10 минут, а одна выборка в 10000 сделок покрывает при 20 сделках в
секунду всего восемь минут. Разрыв был больше окна — и всё, что не влезло,
отсекалось чекпоинтом молча.
"""
from __future__ import annotations

import asyncio

from conftest import NOW

from polymarket_tracker.config import Config
from polymarket_tracker.data_api_listener import DataApiListener, Trade
from polymarket_tracker.wallet_history import WalletHistory, WalletHistoryProvider


def make_page(start_ts, count, step=1):
    """Страница сделок, по убыванию времени — как отдаёт API."""
    return [
        Trade(
            tx_hash=f"0x{start_ts}_{i}", log_index=0, block_number=0,
            timestamp=start_ts - i * step, exchange="data_api",
            maker="0x" + "1" * 40, taker="", side="buy", token_id="t",
            usdc_amount=100.0, shares=1.0, price=0.5,
        )
        for i in range(count)
    ]


class FakeListener(DataApiListener):
    """Листенер с подменённой выборкой: страницы задаём вручную."""

    def __init__(self, pages, config=None):
        super().__init__(config or Config(telegram_bot_token="t", telegram_chat_id="1"))
        self._pages = pages
        self.requested = []

    async def _fetch_recent_trades(self, limit: int, offset: int = 0):
        self.requested.append(offset)
        idx = offset // limit
        return self._pages[idx] if idx < len(self._pages) else []


class TestCatchUp:
    def test_одна_страница_если_чекпоинт_внутри_неё(self):
        pages = [make_page(NOW, 100)]
        li = FakeListener(pages)
        li._last_ts = NOW - 50  # чекпоинт внутри первой страницы
        got = asyncio.run(li._fetch_until_checkpoint(limit=100, max_pages=6))
        assert li.requested == [0]
        assert len(got) == 100

    def test_догружает_пока_не_дойдёт_до_чекпоинта(self):
        """Разрыв шире одной страницы — раньше остаток терялся молча."""
        pages = [make_page(NOW, 100), make_page(NOW - 100, 100), make_page(NOW - 200, 100)]
        li = FakeListener(pages)
        li._last_ts = NOW - 250  # глубже двух страниц
        got = asyncio.run(li._fetch_until_checkpoint(limit=100, max_pages=6))
        assert li.requested == [0, 100, 200]
        assert len(got) == 300

    def test_упирается_в_ограничение_страниц(self):
        """Защита от бесконечной догрузки после долгого простоя."""
        pages = [make_page(NOW - i * 100, 100) for i in range(10)]
        li = FakeListener(pages)
        li._last_ts = 0  # чекпоинта фактически нет
        got = asyncio.run(li._fetch_until_checkpoint(limit=100, max_pages=3))
        assert li.requested == [0, 100, 200]
        assert len(got) == 300

    def test_короткая_страница_останавливает_догрузку(self):
        """История кончилась — дальше ходить незачем."""
        pages = [make_page(NOW, 100), make_page(NOW - 100, 40)]
        li = FakeListener(pages)
        li._last_ts = 0
        got = asyncio.run(li._fetch_until_checkpoint(limit=100, max_pages=6))
        assert li.requested == [0, 100]
        assert len(got) == 140

    def test_пустой_ответ_не_ломает(self):
        li = FakeListener([[]])
        li._last_ts = 0
        assert asyncio.run(li._fetch_until_checkpoint(limit=100, max_pages=6)) == []


class TestNonBlockingHistory:
    """Цикл приёма последовательный: ожидание сети превращается в отставание."""

    def _provider(self, delay=0.05):
        p = WalletHistoryProvider()

        async def fake_fetch(address, now):
            await asyncio.sleep(delay)
            return WalletHistory(
                address=address, first_trade_ts=int(now) - 86400, trades_on_page=5,
                complete=True, recent_ts=(int(now),), fetched_at=now,
            )

        p._fetch = fake_fetch
        return p

    def test_кэш_пуст_значит_не_ждём(self):
        p = self._provider()
        assert p.cached("0x" + "a" * 40) is None

    def test_подкачка_наполняет_кэш(self):
        p = self._provider()
        addr = "0x" + "a" * 40

        async def scenario():
            p.prefetch(addr)
            assert p.cached(addr) is None  # сразу — ещё нет, и это нормально
            await asyncio.sleep(0.2)       # фоновая задача успевает
            return p.cached(addr)

        assert asyncio.run(scenario()) is not None

    def test_повторная_подкачка_не_дублирует_запрос(self):
        p = self._provider(delay=0.1)
        calls = []
        orig = p._fetch

        async def counting(address, now):
            calls.append(address)
            return await orig(address, now)

        p._fetch = counting
        addr = "0x" + "b" * 40

        async def scenario():
            for _ in range(5):
                p.prefetch(addr)
            await asyncio.sleep(0.3)

        asyncio.run(scenario())
        assert len(calls) == 1
