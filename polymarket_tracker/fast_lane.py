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
import html
import logging
import time

import aiohttp
from collections import OrderedDict, deque
from typing import Optional

from .trader_card import fetch_nickname, trader_lines

log = logging.getLogger(__name__)

# Сколько последних сигналов помним, чтобы не отправить один дважды.
SEEN_LIMIT = 5000

# Окно для опорной цены — то же, что у признака price_impact в скоринге.
REFERENCE_WINDOW_SEC = 1800

# Ник трейдера у Data API. В логе цепочки его нет — там только адрес, а по
# адресу не понять, кто это. Запрос делается ТОЛЬКО на отправку сигнала
# (несколько раз в час), поэтому на скорость полосы не влияет.
NICKNAME_URL = "https://data-api.polymarket.com/trades"
NICKNAME_TIMEOUT_SEC = 6


class FastLane:
    """Потребитель ончейн-потока: фильтрует и шлёт ранние сигналы."""

    def __init__(self, listener, storage, market_ctx, notifier, config):
        self.listener = listener
        self.storage = storage
        self.market_ctx = market_ctx
        self.notifier = notifier
        self.config = config
        self._seen: OrderedDict = OrderedDict()
        self._recent_alerts: deque = deque()
        self.stats = {"seen": 0, "checked": 0, "alerted": 0,
                      "no_reference": 0, "near_resolved": 0, "skipped_rate": 0}

    def _rate_ok(self, now: float) -> bool:
        limit = self.config.onchain_max_per_hour
        while self._recent_alerts and now - self._recent_alerts[0] > 3600:
            self._recent_alerts.popleft()
        if len(self._recent_alerts) >= limit:
            self.stats["skipped_rate"] += 1
            return False
        self._recent_alerts.append(now)
        return True

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

        # Почти решённый рынок: покупка по 0.999 — это расчёт по уже
        # известному исходу, а не мнение о будущем. Из 19 первых ранних
        # сигналов 11 оказались именно такими.
        if trade.price >= cfg.max_trade_price:
            self.stats["near_resolved"] += 1
            return

        market = await self.market_ctx.get_by_token_id(trade.token_id)
        if market is not None and getattr(market, "closed", False):
            self.stats["near_resolved"] += 1
            return

        if not self._rate_ok(time.time()):
            return
        await self._alert(trade, impact, reference, market)

    def _remember(self, key) -> None:
        self._seen[key] = True
        while len(self._seen) > SEEN_LIMIT:
            self._seen.popitem(last=False)

    async def _alert(self, trade, impact: float, reference: float, market) -> None:
        self.stats["alerted"] += 1
        slug = getattr(market, "event_slug", "") or getattr(market, "slug", "") or ""
        question = getattr(market, "question", "") or "рынок неизвестен"
        outcome = getattr(market, "outcome", "") or "?"
        category = getattr(market, "category", "") or "unknown"
        volume = getattr(market, "volume_24h", 0.0) or 0.0
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

        tx_url = f"https://polygonscan.com/tx/{trade.tx_hash}"
        trader_block = trader_lines(
            self.storage, trade.maker, await fetch_nickname(trade.maker)
        )

        lines = [
            "🟡 <b>РАННИЙ · из блокчейна</b> · 📈 BUY",
            "",
            f"<b>Рынок:</b> {html.escape(question[:160])}",
            f"<b>Outcome:</b> {html.escape(str(outcome))} @ {trade.price:.3f}",
            f"<b>Размер:</b> ${trade.usdc_amount:,.0f} ({trade.shares:,.0f} shares)",
            "",
            f"<b>Категория:</b> {html.escape(str(category))} | "
            f"<b>Volume 24h:</b> ${volume:,.0f}",
            "",
            *trader_block,
            "",
            f"<b>Причина:</b> переплатил <b>+{impact*100:.0f}%</b> к цене последних "
            f"30 минут ({reference:.3f}) — снимал ликвидность, а не ждал",
            "",
            "<i>Виден на ~5 минут раньше, чем через Data API. "
            "Полная оценка придёт обычным сигналом позже.</i>",
            "",
            f'<a href="{url}">Рынок</a> · <a href="{tx_url}">Транзакция</a>',
        ]
        await self.notifier.send_html(chr(10).join(lines))
        log.info("Ранний сигнал: $%.0f @ %.3f (+%.0f%%) %s",
                 trade.usdc_amount, trade.price, impact * 100, slug)
