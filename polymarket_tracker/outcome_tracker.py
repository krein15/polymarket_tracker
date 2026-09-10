"""Outcome tracker — фоновая задача, измеряющая качество сигналов.

Раз в N минут берёт незакрытые исходы и для каждого:
  1. Дёргает Gamma API за актуальной ценой (минуя кэш — нужна свежая).
  2. Обновляет min/max и snapshot-поля (price_1h/24h/7d), если соответствующее
     окно с момента сигнала уже прошло и поле ещё не заполнено.
  3. Если рынок зарезолвлен (closed=True) — фиксирует settled_price,
     trader_was_right и roi_if_followed.

Отслеживает ДВЕ таблицы исходов:
  * signal_outcomes — боевые сигналы      → _run_pass()
  * shadow_trades   — теневая выборка 0.3 → _run_shadow_pass()
Логика обновления одна и та же — отличаются лишь запрос кандидатов и методы
записи, поэтому вынесены в _OutcomeTarget и общий обработчик _run_target_pass.

Каждый цикл прогоняются оба прохода. Если shadow выключен (SHADOW_ENABLED=0),
таблица shadow_trades просто не наполняется со стороны core — тогда
_run_shadow_pass находит пустую очередь и сразу выходит (почти бесплатно).

При первом запуске прогоняет backfill боевых сигналов — заводит болванки
signal_outcomes для legacy-сигналов без записи. shadow_trades в backfill
не нуждается: строки рождаются сразу с outcome-полями.

Throttling:
  - молодые исходы (< 30 дней) — проверяем каждые POLL_INTERVAL_SEC
  - старые незакрытые (> 30 дней) — проверяем не чаще раза в сутки
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Callable

from .storage import SNAPSHOT_GRACE_FACTOR

if TYPE_CHECKING:
    from .market_context import MarketContext
    from .storage import Storage

log = logging.getLogger(__name__)


# Интервал между проходами цикла (сек). 5 минут — компромисс между
# свежестью price_1h-снапшотов и нагрузкой на Gamma.
POLL_INTERVAL_SEC = 300

# Сколько боевых сигналов обрабатываем за один проход.
BATCH_LIMIT = 30

# Shadow-выборка — надмножество боевой, строк в ней заметно больше; берём
# чуть больший батч, чтобы очередь резолва не отставала.
SHADOW_BATCH_LIMIT = 40

# Пауза между HTTP-запросами внутри батча (сек).
INTRA_BATCH_DELAY = 0.3

# Задержка перед самым первым проходом — даёт main loop'у спокойно
# поднять листенер и обработать первую пачку сделок без конкуренции.
INITIAL_DELAY_SEC = 30

# Старые незакрытые исходы проверяем не чаще чем раз в сутки.
STALE_AGE_SEC = 30 * 86400
STALE_RECHECK_SEC = 86400

# Окна для snapshot-полей.
SNAPSHOT_WINDOWS = [
    ("has_price_1h", "set_price_1h", 3600),
    ("has_price_24h", "set_price_24h", 86400),
    ("has_price_7d", "set_price_7d", 7 * 86400),
]


class _OutcomeTarget:
    """Одна отслеживаемая таблица исходов (боевые сигналы либо shadow).

    Инкапсулирует то, чем signal- и shadow-трекинг отличаются:
      * id_key           — имя ключа id в dict кандидата;
      * get_candidates   — storage-метод запроса незакрытых исходов;
      * update_snapshots — storage-метод обновления snapshot-полей;
      * finalize         — storage-метод финализации резолва;
      * batch_limit      — сколько строк брать за проход.

    Сигнатуры storage-методов у обеих таблиц совпадают по форме (id первым
    позиционным аргументом), поэтому общий обработчик зовёт их единообразно.
    """

    def __init__(
        self,
        name: str,
        id_key: str,
        get_candidates: Callable,
        update_snapshots: Callable,
        finalize: Callable,
        batch_limit: int,
    ):
        self.name = name
        self.id_key = id_key
        self.get_candidates = get_candidates
        self.update_snapshots = update_snapshots
        self.finalize = finalize
        self.batch_limit = batch_limit


class OutcomeTracker:
    def __init__(self, storage: "Storage", market_ctx: "MarketContext"):
        self.storage = storage
        self.market_ctx = market_ctx

        self._signal_target = _OutcomeTarget(
            "signal", "signal_id",
            storage.get_outcomes_to_update,
            storage.update_outcome_snapshots,
            storage.finalize_outcome,
            BATCH_LIMIT,
        )
        self._shadow_target = _OutcomeTarget(
            "shadow", "shadow_id",
            storage.get_shadow_to_update,
            storage.update_shadow_snapshots,
            storage.finalize_shadow_outcome,
            SHADOW_BATCH_LIMIT,
        )

    async def run(self) -> None:
        """Главный цикл. Работает до отмены извне (CancelledError)."""
        # Бэкфилл боевых сигналов — болванки для legacy-сигналов.
        # shadow_trades в бэкфилле не нуждается (рождается с outcome-полями).
        try:
            backfilled = self.storage.backfill_outcome_records()
            if backfilled > 0:
                log.info("Outcome backfill: создано %d болванок для старых сигналов", backfilled)
        except Exception as e:
            log.exception("Backfill упал: %s", e)

        # Пауза перед первым проходом — пусть main loop поднимет листенер.
        try:
            await asyncio.sleep(INITIAL_DELAY_SEC)
        except asyncio.CancelledError:
            raise

        while True:
            # Боевой и теневой проходы — независимо: падение одного не должно
            # мешать другому.
            for pass_fn in (self._run_pass, self._run_shadow_pass):
                try:
                    await pass_fn()
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
        """Проход по боевым сигналам (signal_outcomes)."""
        await self._run_target_pass(self._signal_target)

    async def _run_shadow_pass(self) -> None:
        """Проход по теневой выборке (shadow_trades, TODO 0.3)."""
        await self._run_target_pass(self._shadow_target)

    async def _run_target_pass(self, target: _OutcomeTarget) -> None:
        """Один проход по одной таблице: запросить кандидатов, обновить каждого."""
        candidates = target.get_candidates(limit=target.batch_limit)
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

        log.info("Outcome pass [%s]: обновляю %d исходов", target.name, len(actionable))
        updated = 0
        resolved = 0
        api_misses = 0

        for c in actionable:
            try:
                result = await self._update_one(target, c, now)
                if result == "resolved":
                    resolved += 1
                elif result == "updated":
                    updated += 1
                elif result == "miss":
                    api_misses += 1
            except Exception as e:
                log.warning(
                    "Не смог обновить %s id=%s: %s",
                    target.name, c.get(target.id_key), e,
                )
            await asyncio.sleep(INTRA_BATCH_DELAY)

        if resolved or updated or api_misses:
            log.info(
                "Outcome pass [%s]: updated=%d resolved=%d api_misses=%d",
                target.name, updated, resolved, api_misses,
            )

    async def _update_one(self, target: _OutcomeTarget, c: dict, now: int) -> str:
        """Обновить один исход. Возвращает 'resolved' | 'updated' | 'miss'."""
        row_id = c[target.id_key]
        token_id = c["token_id"]
        side = c["side"] or "buy"
        price_at_signal = c["price_at_signal"]
        signal_ts = c["signal_ts"]

        info = await self.market_ctx.fetch_fresh(token_id)
        if info is None:
            target.update_snapshots(row_id, now_ts=now, current_price=None)
            return "miss"

        # Резолв?
        if info.closed and info.settled_price is not None:
            settled = info.settled_price
            trader_was_right, roi = self._compute_outcome(side, price_at_signal, settled)
            hours_to_resolve = max(0.0, (now - signal_ts) / 3600.0)
            target.finalize(
                row_id,
                settled_price=settled,
                trader_was_right=trader_was_right,
                roi_if_followed=roi,
                hours_to_resolve=hours_to_resolve,
                now_ts=now,
            )
            log.debug(
                "Resolved [%s] id=%s settled=%.3f side=%s right=%s roi=%+.2f%%",
                target.name, row_id, settled, side, trader_was_right, roi * 100,
            )
            return "resolved"

        # Не закрыт — обновляем snapshots.
        current_price = info.last_trade_price
        if current_price is None:
            target.update_snapshots(row_id, now_ts=now, current_price=None)
            return "miss"

        age = now - signal_ts
        kwargs = {}
        for has_key, set_key, window in SNAPSHOT_WINDOWS:
            # Окно прошло, поле ещё пустое — и снимок ЕЩЁ ИМЕЕТ СМЫСЛ.
            #
            # Верхняя граница важнее, чем кажется. Без неё строка, до которой
            # очередь дошла через сутки, получала в price_1h цену суточной
            # давности: поле называется "через час", а числом является совсем
            # другим, и молча портит любой замер дрейфа. Честный пропуск
            # лучше тихой подмены.
            if not c[has_key] and window <= age <= window * SNAPSHOT_GRACE_FACTOR:
                kwargs[set_key] = True

        target.update_snapshots(
            row_id, now_ts=now, current_price=current_price, **kwargs
        )
        return "updated"

    @staticmethod
    def _compute_outcome(
        side: str, price_at_signal: float, settled_price: float
    ) -> tuple[bool, float]:
        """Посчитать trader_was_right и roi_if_followed.

        BUY:  купил по price_at_signal; цена в момент резолва = settled_price
              (0 или 1 для бинарного). ROI как у покупки.
        SELL: продал по price_at_signal; "правильно" = цена ниже цены продажи
              (продал на пике). ROI — доля сохранённого капитала.
        """
        if price_at_signal <= 0:
            # Защита от деления на ноль; не должно встречаться (фильтр в listener).
            return False, 0.0

        if side == "buy":
            trader_was_right = settled_price >= 0.5
            roi = (settled_price - price_at_signal) / price_at_signal
        else:  # sell
            trader_was_right = settled_price < price_at_signal
            roi = (price_at_signal - settled_price) / price_at_signal

        return trader_was_right, roi
