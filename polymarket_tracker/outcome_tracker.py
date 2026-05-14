"""Outcome tracker — фоновая задача, измеряющая качество сигналов.

Раз в N минут берёт незакрытые signal_outcomes из БД и для каждого:
  1. Дёргает Gamma API за актуальной ценой (минуя кэш — нужна свежая).
  2. Обновляет min/max и snapshot-поля (price_1h/24h/7d), если соответствующее
     окно с момента сигнала уже прошло и поле ещё не заполнено.
  3. Если рынок зарезолвлен (closed=True) — фиксирует settled_price,
     trader_was_right и roi_if_followed.

При первом запуске прогоняет backfill — заводит болванки signal_outcomes
для всех старых сигналов без записи (миграция с 1.1).

Throttling:
  - молодые сигналы (< 30 дней) — проверяем каждые POLL_INTERVAL_SEC
  - старые незакрытые (> 30 дней) — проверяем не чаще раза в сутки
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .market_context import MarketContext
    from .storage import Storage

log = logging.getLogger(__name__)


# Интервал между проходами цикла (сек). 5 минут — компромисс между
# свежестью price_1h-снапшотов и нагрузкой на Gamma.
POLL_INTERVAL_SEC = 300

# Сколько сигналов обрабатываем за один проход. С 65+ старых сигналов и
# одним проходом за 5 минут — за час разгребаем ~360 сигналов с запасом.
BATCH_LIMIT = 30

# Пауза между HTTP-запросами внутри батча (сек). 0.3с * 30 = ~9с на батч;
# Gamma лимит — 300/10s, мы далеко от него.
INTRA_BATCH_DELAY = 0.3

# Задержка перед самым первым проходом — даёт main loop'у спокойно
# поднять листенер и обработать первую пачку сделок без конкуренции.
INITIAL_DELAY_SEC = 30

# Старые незакрытые сигналы проверяем не чаще чем раз в сутки.
STALE_AGE_SEC = 30 * 86400
STALE_RECHECK_SEC = 86400

# Окна для snapshot-полей.
SNAPSHOT_WINDOWS = [
    ("has_price_1h", "set_price_1h", 3600),
    ("has_price_24h", "set_price_24h", 86400),
    ("has_price_7d", "set_price_7d", 7 * 86400),
]


class OutcomeTracker:
    def __init__(self, storage: "Storage", market_ctx: "MarketContext"):
        self.storage = storage
        self.market_ctx = market_ctx

    async def run(self) -> None:
        """Главный цикл. Работает до отмены извне (CancelledError)."""
        # Бэкфилл при старте — заводим болванки для legacy-сигналов.
        try:
            backfilled = self.storage.backfill_outcome_records()
            if backfilled > 0:
                log.info("Outcome backfill: создано %d болванок для старых сигналов", backfilled)
        except Exception as e:
            log.exception("Backfill упал: %s", e)

        # Пауза перед первым проходом — пусть main loop поднимет листенер и
        # обработает первую пачку сделок без конкуренции за HTTP.
        try:
            await asyncio.sleep(INITIAL_DELAY_SEC)
        except asyncio.CancelledError:
            raise

        while True:
            try:
                await self._run_pass()
            except asyncio.CancelledError:
                log.info("OutcomeTracker остановлен")
                raise
            except Exception as e:
                log.exception("Outcome pass упал: %s", e)

            try:
                await asyncio.sleep(POLL_INTERVAL_SEC)
            except asyncio.CancelledError:
                raise

    async def _run_pass(self) -> None:
        """Один проход: запросить кандидатов, обновить каждый."""
        candidates = self.storage.get_outcomes_to_update(limit=BATCH_LIMIT)
        if not candidates:
            return

        now = int(time.time())
        # Отфильтруем "старые незакрытые": их трогаем не чаще раза в сутки.
        actionable = []
        for c in candidates:
            age = now - c["signal_ts"]
            if age > STALE_AGE_SEC:
                last = c["last_checked_ts"] or 0
                if now - last < STALE_RECHECK_SEC:
                    continue  # ещё рано перепроверять
            actionable.append(c)

        if not actionable:
            return

        log.info("Outcome pass: обновляю %d сигналов", len(actionable))
        updated = 0
        resolved = 0
        api_misses = 0

        for c in actionable:
            try:
                result = await self._update_one(c, now)
                if result == "resolved":
                    resolved += 1
                elif result == "updated":
                    updated += 1
                elif result == "miss":
                    api_misses += 1
            except Exception as e:
                log.warning("Не смог обновить outcome signal_id=%s: %s", c["signal_id"], e)
            await asyncio.sleep(INTRA_BATCH_DELAY)

        if resolved or updated or api_misses:
            log.info(
                "Outcome pass: updated=%d resolved=%d api_misses=%d",
                updated, resolved, api_misses,
            )

    async def _update_one(self, c: dict, now: int) -> str:
        """Обновить один outcome. Возвращает 'resolved' | 'updated' | 'miss'."""
        signal_id = c["signal_id"]
        token_id = c["token_id"]
        side = c["side"] or "buy"
        price_at_signal = c["price_at_signal"]
        signal_ts = c["signal_ts"]

        info = await self.market_ctx.fetch_fresh(token_id)
        if info is None:
            self.storage.update_outcome_snapshots(
                signal_id=signal_id, now_ts=now, current_price=None,
            )
            return "miss"

        # Резолв?
        if info.closed and info.settled_price is not None:
            settled = info.settled_price
            trader_was_right, roi = self._compute_outcome(side, price_at_signal, settled)
            hours_to_resolve = max(0.0, (now - signal_ts) / 3600.0)
            self.storage.finalize_outcome(
                signal_id=signal_id,
                settled_price=settled,
                trader_was_right=trader_was_right,
                roi_if_followed=roi,
                hours_to_resolve=hours_to_resolve,
                now_ts=now,
            )
            log.debug(
                "Resolved signal_id=%d settled=%.3f side=%s right=%s roi=%+.2f%%",
                signal_id, settled, side, trader_was_right, roi * 100,
            )
            return "resolved"

        # Не закрыт — обновляем snapshots.
        current_price = info.last_trade_price
        if current_price is None:
            self.storage.update_outcome_snapshots(
                signal_id=signal_id, now_ts=now, current_price=None,
            )
            return "miss"

        age = now - signal_ts
        kwargs = {}
        for has_key, set_key, window in SNAPSHOT_WINDOWS:
            # Заполняем поле только если: окно прошло И поле ещё не заполнено.
            if age >= window and not c[has_key]:
                kwargs[set_key] = True

        self.storage.update_outcome_snapshots(
            signal_id=signal_id,
            now_ts=now,
            current_price=current_price,
            **kwargs,
        )
        return "updated"

    @staticmethod
    def _compute_outcome(
        side: str, price_at_signal: float, settled_price: float
    ) -> tuple[bool, float]:
        """Посчитать trader_was_right и roi_if_followed.

        BUY:  купил по price_at_signal; настоящая цена в момент резолва =
              settled_price (0 или 1 для бинарного). ROI как у покупки.
        SELL: продал по price_at_signal; "правильно" = настоящая цена ниже
              цены продажи (продал на пике). ROI трактуется как доля
              сохранённого капитала относительно "если бы держал до резолва".
        """
        if price_at_signal <= 0:
            # Защита от деления на ноль; не должно встречаться (есть фильтр в listener).
            return False, 0.0

        if side == "buy":
            trader_was_right = settled_price >= 0.5
            roi = (settled_price - price_at_signal) / price_at_signal
        else:  # sell
            trader_was_right = settled_price < price_at_signal
            roi = (price_at_signal - settled_price) / price_at_signal

        return trader_was_right, roi
