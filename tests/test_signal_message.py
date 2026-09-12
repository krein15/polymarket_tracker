"""Что обязано быть в сообщении о сигнале.

Откуда тест
-----------
Подтверждение по погоне сообщало "рынок пошёл следом" — но не говорило, ЗА
КЕМ и на какой исход. По такому сигналу нечего проверить и не за кем
следить: ни ссылки на трейдера, ни его послужного списка, ни outcome.
В ранних сигналах из цепочки всё это было, то есть форматы разошлись.

Поэтому обязательные поля закреплены тестом, а сборка карточки трейдера
вынесена в общий модуль: разъехаться снова им теперь труднее.
"""
from __future__ import annotations

import asyncio

from conftest import NOW
from test_confirmation import HERO, TOKEN, Cfg, FakeNotifier, buy, candidate

from polymarket_tracker.confirmation import ChaseConfirmer


class FakeMarket:
    question = "Lakers vs Bilbao — ничья?"
    outcome = "Draw"
    event_slug = "lal-bil-elc-2026-09-12"
    slug = "lal-bil-elc-2026-09-12-draw"
    category = "sports"
    volume_24h = 120_000.0
    closed = False


class FakeCtx:
    def __init__(self, market=FakeMarket()):
        self.market = market
        self.asked = []

    async def get_by_token_id(self, token_id):
        self.asked.append(token_id)
        return self.market


def chase_message(storage, market_ctx=None, resolved=0):
    """Прогнать один проход погони и вернуть отправленный текст."""
    candidate(storage, price=0.330, usdc=2520.0)
    buy(storage, "0x" + "2" * 40, 0.599, 56_797.0, NOW + 300)
    buy(storage, "0x" + "9" * 40, 0.40, 1.0, NOW + 3000)

    for i in range(resolved):
        storage.save_shadow_trade(
            tx_hash=f"0xhist{i}", maker=HERO, token_id=f"t{i}", ts=NOW - 86400 * (i + 2),
            side="buy", usdc_amount=1000.0, price=0.4, market_slug="m",
            category="sports", volume_24h=1.0, passed_filters=False,
            signal_types=None, now_ts=NOW, score=1.0, score_parts=None,
        )
        storage.finalize_shadow_trade(
            shadow_id=i + 1, settled_price=1.0, trader_was_right=True,
            roi_if_followed=1.5, hours_to_resolve=1.0, now_ts=NOW,
        )

    n = FakeNotifier()
    conf = ChaseConfirmer(storage, n, Cfg(), market_ctx=market_ctx)
    asyncio.run(conf._pass())
    assert n.sent, "сигнал не отправлен"
    return n.sent[0]


class TestChaseMessage:
    def test_есть_исход_ставки(self, storage):
        """То, чего не хватало: на ЧТО поставлено."""
        text = chase_message(storage, FakeCtx())
        assert "Outcome:" in text
        assert "Draw" in text

    def test_есть_ссылка_на_трейдера(self, storage):
        """И то, ЗА КЕМ погоня."""
        text = chase_message(storage, FakeCtx())
        assert f"https://polymarket.com/profile/{HERO}" in text
        assert HERO[:8] in text

    def test_есть_послужной_список(self, storage):
        text = chase_message(storage, FakeCtx())
        assert "истории по нему у нас пока нет" in text

    def test_есть_название_рынка(self, storage):
        text = chase_message(storage, FakeCtx())
        assert "Lakers vs Bilbao" in text

    def test_есть_обе_цены_и_поток(self, storage):
        text = chase_message(storage, FakeCtx())
        assert "0.330" in text      # его цена
        assert "0.599" in text      # средняя последователей
        assert "56,797" in text     # деньги погони
        assert "2,520" in text      # его размер

    def test_ссылка_на_событие_а_не_на_рынок(self, storage):
        """У рынка внутри события свой слаг, и /event/<market_slug> даёт 404."""
        text = chase_message(storage, FakeCtx())
        assert "polymarket.com/event/lal-bil-elc-2026-09-12\"" in text

    def test_без_рынка_сигнал_всё_равно_уходит(self, storage):
        """Gamma может не ответить. Лучше сообщение без исхода, чем молчание."""
        text = chase_message(storage, market_ctx=None)
        assert "ПОГОНЯ" in text
        assert "Outcome:" not in text
        assert f"https://polymarket.com/profile/{HERO}" in text

    def test_отказ_gamma_не_роняет(self, storage):
        class Broken:
            async def get_by_token_id(self, token_id):
                raise RuntimeError("Gamma недоступна")

        text = chase_message(storage, Broken())
        assert "ПОГОНЯ" in text

    def test_запрашивается_нужный_токен(self, storage):
        ctx = FakeCtx()
        chase_message(storage, ctx)
        assert ctx.asked == [TOKEN]

    def test_разметка_не_экранирована(self, storage):
        """Сообщение уходит через send_html: теги должны остаться тегами,
        иначе пользователь видит <b> в тексте."""
        text = chase_message(storage, FakeCtx())
        assert "<b>" in text and "&lt;b&gt;" not in text
