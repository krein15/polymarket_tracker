"""Отбой: рынок пошёл ПРОТИВ трейдера по сделке, о которой мы уже написали.

Замер на 4985 сделках с посчитанной погоней (10.09.2026):

    рынок пошёл за ним (>= +15%)   n=394   перевес +25.0 пп, ROI +50.4%
    рынок пошёл против (<= -15%)   n=306   перевес -29.0 пп, ROI -54.0%

Среди сделок, по которым сигнал реально ушёл в Telegram, отбойная группа
ещё хуже: винрейт 21.4% при безубытке 53.2%, ROI -60.4%.

Признак зеркален подтверждению и по порогу, и по силе — и это довод в его
пользу: односторонняя находка чаще всего оказывается случайностью выборки.
"""
from __future__ import annotations

import asyncio

from conftest import NOW
from test_confirmation import HERO, TOKEN, Cfg, FakeNotifier, buy, candidate

from polymarket_tracker.confirmation import ChaseConfirmer


def run(storage, notifier, cfg=None):
    conf = ChaseConfirmer(storage, notifier, cfg or Cfg())
    asyncio.run(conf._pass())
    return conf


def sent_signal(storage, ts=NOW, usdc=5000.0):
    """Отметить, что по этой сделке сигнал уже уходил пользователю."""
    storage.save_signal(
        ts=ts, signal_type="score", maker=HERO, token_id=TOKEN,
        market_slug="market-x", usdc_amount=usdc, price=0.40,
        reason="тест", tx_hash=f"0x{HERO[-4:]}{ts}{int(usdc)}", side="buy",
    )


def head(storage, ts):
    """Сдвинуть голову данных вперёд: без этого окно наблюдения не закрыто."""
    buy(storage, "0x" + "9" * 40, 0.40, 1.0, ts, token_id="head")


class TestRetraction:
    def test_разворот_по_отправленному_сигналу_даёт_отбой(self, storage):
        candidate(storage, price=0.40)
        sent_signal(storage)
        buy(storage, "0x" + "2" * 40, 0.30, 6000.0, NOW + 300)   # -25% к его цене
        head(storage, NOW + 3000)
        n = FakeNotifier()
        conf = run(storage, n)
        assert len(n.sent) == 1
        assert "ОТБОЙ" in n.sent[0]
        assert conf.stats["retracted"] == 1

    def test_без_отправленного_сигнала_молчим(self, storage):
        """Про эту сделку мы ничего не писали — сообщать не о чем.

        Это главное ограничение отбоя: без него он давал бы два десятка
        сообщений в сутки вместо одного.
        """
        candidate(storage, price=0.40)
        buy(storage, "0x" + "2" * 40, 0.30, 6000.0, NOW + 300)
        head(storage, NOW + 3000)
        n = FakeNotifier()
        conf = run(storage, n)
        assert n.sent == []
        assert conf.stats["retracted"] == 0

    def test_мелкий_разворот_не_отбой(self, storage):
        candidate(storage, price=0.40)
        sent_signal(storage)
        buy(storage, "0x" + "2" * 40, 0.38, 6000.0, NOW + 300)   # -5% при пороге -15%
        head(storage, NOW + 3000)
        n = FakeNotifier()
        run(storage, n)
        assert n.sent == []

    def test_отбой_идёт_красным_каналом(self, storage):
        """Цвета разведены: жёлтый ранний, зелёный обычный, красный сбой."""
        candidate(storage, price=0.40)
        sent_signal(storage)
        buy(storage, "0x" + "2" * 40, 0.30, 6000.0, NOW + 300)
        head(storage, NOW + 3000)

        class Tagging(FakeNotifier):
            def __init__(self):
                super().__init__()
                self.channels = []

            async def send_html(self, text):
                self.channels.append("html")
                return await super().send_html(text)

            async def send_alert(self, text):
                self.channels.append("alert")
                return await super().send_alert(text)

        n = Tagging()
        run(storage, n)
        assert n.channels == ["alert"]

    def test_поток_денег_не_фильтруется(self, storage):
        """Порог по деньгам ослабляет признак — замерено: -29.0 пп без него
        против -13.5 пп при пороге $5000. Поэтому мелкий поток тоже в счёт."""
        candidate(storage, price=0.40)
        sent_signal(storage)
        buy(storage, "0x" + "2" * 40, 0.30, 100.0, NOW + 300)
        head(storage, NOW + 3000)
        n = FakeNotifier()
        run(storage, n)
        assert len(n.sent) == 1

    def test_накопленное_после_простоя_молчит(self, storage):
        """Трекер простоял сутки (так и было 09-10.09): вываливать отбои
        задним числом незачем, но отметку поставить надо."""
        cfg = Cfg()
        cfg.chase_fresh_minutes = 5.0
        candidate(storage, price=0.40)
        sent_signal(storage)
        buy(storage, "0x" + "2" * 40, 0.30, 6000.0, NOW + 300)
        head(storage, NOW + 8000)
        n = FakeNotifier()
        conf = run(storage, n, cfg)
        assert n.sent == []
        assert conf.stats["checked"] == 1

    def test_у_отбоя_свой_предел_в_час(self, storage):
        """Отбои не должны вытесняться подтверждениями из общей квоты."""
        cfg = Cfg()
        cfg.chase_retract_max_per_hour = 1
        for i in range(3):
            ts, tx, maker, tok = NOW + i * 10, f"0xr{i}", f"0x{i:040x}", f"tok{i}"
            storage.save_trade(tx_hash=tx, log_index=0, ts=ts, block_number=0,
                               maker=maker, token_id=tok, side="buy",
                               usdc_amount=5000.0, price=0.40, condition_id="0xc")
            storage.save_shadow_trade(
                tx_hash=tx, maker=maker, token_id=tok, ts=ts, side="buy",
                usdc_amount=5000.0, price=0.40, market_slug=f"m{i}",
                category="politics", volume_24h=10000.0, passed_filters=False,
                signal_types=None, now_ts=ts, score=50.0, score_parts=None,
            )
            storage.save_signal(ts=ts, signal_type="score", maker=maker,
                                token_id=tok, market_slug=f"m{i}", usdc_amount=5000.0,
                                price=0.40, reason="t", tx_hash=tx, side="buy")
            buy(storage, "0x" + "e" * 40, 0.30, 9000.0, ts + 300, token_id=tok)
        head(storage, NOW + 3000)
        n = FakeNotifier()
        conf = run(storage, n, cfg)
        assert len(n.sent) == 1
        assert conf.stats["skipped_rate"] == 2


class TestSentSignalLookup:
    def test_находит_только_свои_типы(self, storage):
        """Ранний ончейн-сигнал приходит раньше, чем окно погони закрылось,
        а подтверждение — это вывод по той же самой погоне. Отбой не про них."""
        for kind in ("onchain_early", "chase"):
            storage.save_signal(ts=NOW, signal_type=kind, maker=HERO, token_id=TOKEN,
                                market_slug="m", usdc_amount=1.0, price=0.4,
                                reason="t", tx_hash="0xzz", side="buy")
        assert storage.sent_signal_for_trade("0xzz") is None

        storage.save_signal(ts=NOW, signal_type="score", maker=HERO, token_id=TOKEN,
                            market_slug="m", usdc_amount=1.0, price=0.4,
                            reason="t", tx_hash="0xzz", side="buy")
        assert storage.sent_signal_for_trade("0xzz") is not None

    def test_пустой_хеш_не_ломает(self, storage):
        assert storage.sent_signal_for_trade("") is None
