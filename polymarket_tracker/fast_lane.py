"""Ранние сигналы из блокчейна: заметить агрессивный вход до Data API.

Что делает
----------
Слушает сделки из цепочки (см. onchain_listener) и на крупных покупках
считает единственный признак, который у нас измерен и доступен мгновенно, —
удар по цене: насколько сделка прошла выше медианы недавних покупок.

    переплата > +20%  -> дрейф через час +17.6%, перевес +9.9 пп (n=117)
    около нуля        -> дрейф  +0.1%,           перевес -1.8 пп (n=1710)

Остальные признаки здесь недоступны: накопление, кластер и история кошелька
требуют записей, которых в базе ещё нет — сделка только что произошла. Это
осознанный размен: одна проверка вместо семи, зато на пять минут раньше.

Полная оценка никуда не девается: та же сделка придёт через Data API и
пройдёт обычный скоринг со всеми признаками. Ранний сигнал её не заменяет,
а опережает.

Почему опорная цена берётся из базы, а не из цепочки
---------------------------------------------------
Медиана считается по нашим записям, которые отстают на ~5 минут. Для окна
в 30 минут это значит, что 25 минут из 30 у нас есть — достаточно, чтобы
опора была устойчивой. Тянуть историю из цепочки ради пяти минут значило бы
сотни запросов на каждую сделку.

Дедупликация
------------
Одна сделка может прийти дважды: как OrderFilled мейкера и как встречное
событие. Плюс при переподключении вебсокет может повторить последние
события. Поэтому помним недавно отправленные (tx, кошелёк, токен).
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Optional

log = logging.getLogger(__name__)

# Сколько последних сигналов помним, чтобы не отправить один дважды.
SEEN_LIMIT = 5000

# Окно для опорной цены — то же, что у признака price_impact в скоринге.
REFERENCE_WINDOW_SEC = 1800


class FastLane:
    """Потребитель ончейн-потока: фильтрует и шлёт ранние сигналы."""

    def __init__(self, listener, storage, market_ctx, notifier, config):
        self.listener = listener
        self.storage = storage
        self.market_ctx = market_ctx
        self.notifier = notifier
        self.config = config
        self._seen: OrderedDict = OrderedDict()
        self.stats = {"seen": 0, "checked": 0, "alerted": 0, "no_reference": 0}

    async def run(self) -> None:
        cfg = self.config
        log.info(
            "Быстрая полоса: сделки от $%.0f, переплата от +%.0f%%",
            cfg.onchain_min_usdc, cfg.onchain_min_impact * 100,
        )
        try:
            async for trade in self.listener.stream():
                try:
                    await self._handle(trade)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 — одна сделка не роняет поток
                    log.warning("Быстрая полоса: ошибка обработки: %s", e)
        except asyncio.CancelledError:
            log.info("Быстрая полоса остановлена")
            raise

    async def _handle(self, trade) -> None:
        cfg = self.config
        self.stats["seen"] += 1

        if trade.side != "buy":
            return  # интересует открытие позиции
        if trade.usdc_amount < cfg.onchain_min_usdc:
            return

        key = (trade.tx_hash, trade.maker.lower(), trade.token_id)
        if key in self._seen:
            return
        self._remember(key)

        # Опора — по нашим записям. Момент отсчёта берём как "сейчас": сделка
        # только что произошла, а часы блока нам в логе не пришли.
        now = int(time.time())
        reference = self.storage.market_reference_price(
            trade.token_id, now, REFERENCE_WINDOW_SEC
        )
        self.stats["checked"] += 1
        if not reference:
            self.stats["no_reference"] += 1
            return  # по двум сделкам опору не строят — молчим, а не гадаем

        impact = (trade.price - reference) / reference
        if impact < cfg.onchain_min_impact:
            return

        market = await self.market_ctx.get_by_token_id(trade.token_id)
        await self._alert(trade, impact, reference, market)

    def _remember(self, key) -> None:
        self._seen[key] = True
        while len(self._seen) > SEEN_LIMIT:
            self._seen.popitem(last=False)

    async def _alert(self, trade, impact: float, reference: float, market) -> None:
        self.stats["alerted"] += 1
        slug = getattr(market, "event_slug", "") or getattr(market, "slug", "") or ""
        question = getattr(market, "question", "") or "рынок неизвестен"
        url = f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com"

        reason = (
            f"Ранний сигнал из цепочки: ${trade.usdc_amount:,.0f} по {trade.price:.3f} "
            f"при недавней {reference:.3f} (+{impact*100:.0f}%)"
        )
        signal_id = self.storage.save_signal(
            ts=int(time.time()),
            signal_type="onchain_early",
            maker=trade.maker,
            token_id=trade.token_id,
            market_slug=slug or None,
            usdc_amount=trade.usdc_amount,
            price=trade.price,
            reason=reason,
            tx_hash=trade.tx_hash,
            side="buy",
        )
        self.storage.init_outcome_record(signal_id, int(time.time()))

        text = (
            f"⚡ <b>Ранний сигнал из блокчейна</b>\n"
            f"{question[:120]}\n\n"
            f"<b>${trade.usdc_amount:,.0f}</b> по <b>{trade.price:.3f}</b>\n"
            f"Недавняя цена рынка: {reference:.3f} — переплата <b>+{impact*100:.0f}%</b>\n\n"
            f"Виден на ~5 минут раньше, чем через Data API. "
            f"Агрессивный вход: снимал ликвидность, а не ждал.\n"
            f'<a href="{url}">Открыть рынок</a>'
        )
        await self.notifier.send_status(text)
        log.info("Ранний сигнал: $%.0f @ %.3f (+%.0f%%) %s",
                 trade.usdc_amount, trade.price, impact * 100, slug)
