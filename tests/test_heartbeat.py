"""Сигнал жизни: сводка и сторож потока.

Два разных отказа: процесс умер (тогда тревогой служит отсутствие сводки) и
процесс жив, но поток иссяк (тогда нужно активное предупреждение).
"""
from __future__ import annotations

import asyncio

from polymarket_tracker.heartbeat import (
    Heartbeat,
    TrackerStats,
    format_digest,
    format_recovery,
    format_stall,
)


def stats(last_trade_age_min=5.0):
    return TrackerStats(
        uptime_hours=6.2, trades_processed=120_000, signals_sent=7,
        last_trade_age_min=last_trade_age_min, trades_total=531_080,
        signals_total=63, resolved_total=40, wallets_total=27_167,
    )


class FakeNotifier:
    """Двойник нотифаера. Три метода — потому что у сообщений разная природа:
    служебный текст экранируется, готовая разметка нет, сбои идут красным."""

    def __init__(self):
        self.sent = []

    async def send_status(self, text):
        self.sent.append(text)
        return 1

    async def send_html(self, text):
        self.sent.append(text)
        return 1

    async def send_alert(self, text):
        self.sent.append(text)
        return 1


class FakeConfig:
    heartbeat_interval_hours = 24.0
    stall_alert_minutes = 20.0


class TestFormatting:
    def test_разряды_не_съедают_запятые_в_тексте(self):
        """Первая версия делала сплошную замену запятых и ломала предложения."""
        text = format_digest(stats())
        assert "531 080 сделок, 27 167 кошельков".replace(" ", " ", 0)
        assert "сделок," in text and "исходов закрыто" in text

    def test_числа_разделены_не_запятой(self):
        assert "531,080" not in format_digest(stats())

    def test_возраст_последней_сделки_показан(self):
        assert "мин назад" in format_digest(stats())

    def test_без_сделок_строка_опускается(self):
        assert "мин назад" not in format_digest(stats(last_trade_age_min=None))

    def test_тревога_называет_простой(self):
        assert "37" in format_stall(37.0)

    def test_восстановление_называет_длительность(self):
        assert "12" in format_recovery(12.0)


class TestStallWatch:
    def test_норма_молчит(self):
        n = FakeNotifier()
        hb = Heartbeat(n, lambda: stats(5.0), FakeConfig())
        asyncio.run(hb._check_stall(stats(5.0), 20.0))
        assert n.sent == []

    def test_простой_поднимает_тревогу(self):
        n = FakeNotifier()
        hb = Heartbeat(n, lambda: stats(40.0), FakeConfig())
        asyncio.run(hb._check_stall(stats(40.0), 20.0))
        assert len(n.sent) == 1
        assert "остановился" in n.sent[0]

    def test_тревога_шлётся_один_раз(self):
        """Иначе при часовом простое придёт три десятка одинаковых сообщений."""
        n = FakeNotifier()
        hb = Heartbeat(n, lambda: stats(40.0), FakeConfig())
        for _ in range(5):
            asyncio.run(hb._check_stall(stats(40.0), 20.0))
        assert len(n.sent) == 1

    def test_восстановление_сообщается(self):
        n = FakeNotifier()
        hb = Heartbeat(n, lambda: stats(40.0), FakeConfig())
        asyncio.run(hb._check_stall(stats(40.0), 20.0))
        asyncio.run(hb._check_stall(stats(3.0), 20.0))
        assert len(n.sent) == 2
        assert "восстановился" in n.sent[1]

    def test_после_восстановления_тревога_возможна_снова(self):
        n = FakeNotifier()
        hb = Heartbeat(n, lambda: stats(), FakeConfig())
        asyncio.run(hb._check_stall(stats(40.0), 20.0))
        asyncio.run(hb._check_stall(stats(3.0), 20.0))
        asyncio.run(hb._check_stall(stats(50.0), 20.0))
        assert sum("остановился" in m for m in n.sent) == 2

    def test_пустая_база_не_вызывает_тревогу(self):
        """На старте сделок ещё нет — это не поломка."""
        n = FakeNotifier()
        hb = Heartbeat(n, lambda: stats(None), FakeConfig())
        asyncio.run(hb._check_stall(stats(None), 20.0))
        assert n.sent == []
