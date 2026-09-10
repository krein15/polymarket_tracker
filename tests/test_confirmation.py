"""Подтверждение погоней: рынок пошёл за трейдером и платит дороже него.

Замер, породивший признак (4362 теневых сделки, окно 20 минут):
    последователи платили выше на 15%+  -> перевес +35.7 пп, дрейф +83%
    платили примерно как он             -> перевес  -0.4 пп
    платили НИЖЕ него на 5%+            -> перевес -24.6 пп
"""
from __future__ import annotations

import asyncio

from conftest import NOW

from polymarket_tracker.confirmation import ChaseConfirmer

TOKEN = "token-1"
HERO = "0x" + "1" * 40


def buy(storage, maker, price, usdc, ts, token_id=TOKEN):
    storage.save_trade(
        tx_hash=f"0x{maker[-4:]}{ts}{int(usdc)}", log_index=0, ts=ts, block_number=0,
        maker=maker, token_id=token_id, side="buy", usdc_amount=usdc,
        price=price, condition_id="0xc",
    )


def candidate(storage, price=0.40, usdc=5000.0, ts=NOW):
    buy(storage, HERO, price, usdc, ts)
    storage.save_shadow_trade(
        tx_hash=f"0x{HERO[-4:]}{ts}{int(usdc)}", maker=HERO, token_id=TOKEN, ts=ts,
        side="buy", usdc_amount=usdc, price=price, market_slug="market-x",
        category="politics", volume_24h=10000.0, passed_filters=False,
        signal_types=None, now_ts=ts, score=50.0, score_parts=None,
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


class Cfg:
    chase_window_minutes = 20.0
    chase_min_ratio = 0.15
    chase_min_money_usdc = 2000.0
    chase_max_age_minutes = 180.0
    chase_fresh_minutes = 180.0   # в тестах шумим по всей очереди
    chase_max_per_hour = 100
    chase_retract_ratio = -0.15
    chase_retract_max_per_hour = 100


class TestFollowerFlow:
    def test_считает_только_чужие_покупки(self, storage):
        buy(storage, HERO, 0.40, 5000.0, NOW)
        buy(storage, HERO, 0.50, 3000.0, NOW + 60)          # его же — не считаем
        buy(storage, "0x" + "2" * 40, 0.60, 1000.0, NOW + 120)
        money, vwap = storage.follower_flow(TOKEN, HERO, NOW, NOW + 1200)
        assert money == 1000.0
        assert abs(vwap - 0.60) < 1e-9

    def test_цена_средневзвешенная(self, storage):
        """Одна мелкая покупка по нелепой цене не должна перевешивать поток."""
        buy(storage, "0x" + "2" * 40, 0.50, 9000.0, NOW + 60)
        buy(storage, "0x" + "3" * 40, 0.95, 100.0, NOW + 90)
        money, vwap = storage.follower_flow(TOKEN, HERO, NOW, NOW + 1200)
        assert money == 9100.0
        assert 0.50 < vwap < 0.52

    def test_окно_соблюдается(self, storage):
        buy(storage, "0x" + "2" * 40, 0.60, 1000.0, NOW + 5000)
        money, _ = storage.follower_flow(TOKEN, HERO, NOW, NOW + 1200)
        assert money == 0.0

    def test_продажи_не_считаются(self, storage):
        storage.save_trade(
            tx_hash="0xsell", log_index=0, ts=NOW + 60, block_number=0,
            maker="0x" + "2" * 40, token_id=TOKEN, side="sell",
            usdc_amount=5000.0, price=0.60, condition_id="0xc",
        )
        money, _ = storage.follower_flow(TOKEN, HERO, NOW, NOW + 1200)
        assert money == 0.0


class TestConfirmationPass:
    def _run(self, storage, notifier):
        conf = ChaseConfirmer(storage, notifier, Cfg())
        asyncio.run(conf._pass())
        return conf

    def test_погоня_даёт_подтверждение(self, storage):
        candidate(storage, price=0.40)
        buy(storage, "0x" + "2" * 40, 0.55, 6000.0, NOW + 300)   # +37% к его цене
        buy(storage, "0x" + "9" * 40, 0.40, 1.0, NOW + 3000)     # двигаем голову вперёд
        n = FakeNotifier()
        self._run(storage, n)
        assert len(n.sent) == 1
        assert "рынок пошёл следом" in n.sent[0]
        assert storage.count_signals() == 1

    def test_без_погони_молчим(self, storage):
        candidate(storage, price=0.40)
        buy(storage, "0x" + "2" * 40, 0.41, 6000.0, NOW + 300)   # цена не изменилась
        buy(storage, "0x" + "9" * 40, 0.40, 1.0, NOW + 3000)
        n = FakeNotifier()
        self._run(storage, n)
        assert n.sent == []

    def test_мало_денег_не_подтверждение(self, storage):
        """Одиночная мелкая покупка по высокой цене — не поток, а случайность."""
        candidate(storage, price=0.40)
        buy(storage, "0x" + "2" * 40, 0.60, 300.0, NOW + 300)
        buy(storage, "0x" + "9" * 40, 0.40, 1.0, NOW + 3000)
        n = FakeNotifier()
        self._run(storage, n)
        assert n.sent == []

    def test_кандидат_не_проверяется_дважды(self, storage):
        candidate(storage, price=0.40)
        buy(storage, "0x" + "2" * 40, 0.55, 6000.0, NOW + 300)
        buy(storage, "0x" + "9" * 40, 0.40, 1.0, NOW + 3000)
        n = FakeNotifier()
        self._run(storage, n)
        self._run(storage, n)
        assert len(n.sent) == 1

    def test_окно_ещё_не_закрылось_ждём(self, storage):
        """Пока с момента сделки не прошло окно наблюдения, судить рано."""
        candidate(storage, price=0.40, ts=NOW)
        buy(storage, "0x" + "2" * 40, 0.55, 6000.0, NOW + 60)
        n = FakeNotifier()
        self._run(storage, n)   # голова данных = NOW+60, окно 20 мин не прошло
        assert n.sent == []


class TestVolumeGuards:
    """Ограничители, добавленные после того, как бот залил Telegram: порог
    обещал 14 сигналов в сутки, а живой поток дал около 580."""

    def _candidates(self, storage, count, base_ts):
        for i in range(count):
            ts = base_ts + i * 10
            buy(storage, f"0x{i:040x}", 0.40, 5000.0, ts, token_id=f"tok{i}")
            storage.save_shadow_trade(
                tx_hash=f"0xc{i}{ts}", maker=f"0x{i:040x}", token_id=f"tok{i}", ts=ts,
                side="buy", usdc_amount=5000.0, price=0.40, market_slug=f"m{i}",
                category="politics", volume_24h=10000.0, passed_filters=False,
                signal_types=None, now_ts=ts, score=50.0, score_parts=None,
            )
            buy(storage, "0x" + "e" * 40, 0.60, 9000.0, ts + 300, token_id=f"tok{i}")

    def test_предел_в_час_ограничивает_поток(self, storage):
        cfg = Cfg(); cfg.chase_max_per_hour = 2
        self._candidates(storage, 6, NOW)
        buy(storage, "0x" + "9" * 40, 0.4, 1.0, NOW + 4000, token_id="head")
        n = FakeNotifier()
        conf = ChaseConfirmer(storage, n, cfg)
        asyncio.run(conf._pass())
        assert len(n.sent) == 2
        assert conf.stats["skipped_rate"] == 4

    def test_накопленное_разбирается_молча(self, storage):
        """Старые кандидаты помечаются проверенными, но сообщений не шлют:
        рынок по ним уже ушёл, а при перезапуске их сотни."""
        cfg = Cfg(); cfg.chase_fresh_minutes = 5.0
        self._candidates(storage, 4, NOW)
        buy(storage, "0x" + "9" * 40, 0.4, 1.0, NOW + 8000, token_id="head")
        n = FakeNotifier()
        conf = ChaseConfirmer(storage, n, cfg)
        asyncio.run(conf._pass())
        assert n.sent == []
        assert conf.stats["backfilled"] == 4

    def test_молчаливый_разбор_всё_равно_помечает(self, storage):
        """Иначе очередь не рассасывается и та же пачка крутится вечно."""
        cfg = Cfg(); cfg.chase_fresh_minutes = 5.0
        self._candidates(storage, 3, NOW)
        buy(storage, "0x" + "9" * 40, 0.4, 1.0, NOW + 8000, token_id="head")
        conf = ChaseConfirmer(storage, FakeNotifier(), cfg)
        asyncio.run(conf._pass())
        asyncio.run(conf._pass())
        assert conf.stats["checked"] == 3
