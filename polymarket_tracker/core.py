"""Главный оркестратор трекера.

Собирает все модули и крутит главный цикл:
    trade ← DataApiListener (Polymarket Data API)
      → Storage.upsert_wallet_trade / save_trade
      → MarketContext.get_by_token_id (Gamma API enrichment)
      → WalletAnalyzer.assess
      → AnomalyDetector.evaluate → signals
      → TelegramNotifier.send_signal для каждого сигнала
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from .anomaly_detector import AnomalyDetector, Signal
from .config import Config
from .data_api_listener import DataApiListener, Trade
from .market_context import MarketContext
from .storage import Storage
from .telegram_notifier import TelegramNotifier
from .wallet_analyzer import WalletAnalyzer
from .watchlist import Watchlist

log = logging.getLogger(__name__)


class PolymarketTracker:
    def __init__(self, config: Config):
        self.config = config
        self.storage = Storage(config.db_path)
        self.watchlist = Watchlist(config.whitelist_file)
        self.listener = DataApiListener(config)
        self.market_ctx = MarketContext()
        self.wallet_analyzer = WalletAnalyzer(self.storage, config)
        self.detector = AnomalyDetector(config, self.storage, self.watchlist)
        self.notifier = TelegramNotifier(config.telegram_bot_token, config.telegram_chat_id)

        # Счётчики для периодической статистики
        self._stats_trades = 0
        self._stats_signals = 0
        self._stats_started_at = time.time()

    @classmethod
    def from_env(cls, env_path: str = ".env") -> "PolymarketTracker":
        cfg = Config.from_env(env_path)
        errors = cfg.validate()
        if errors:
            raise ValueError("Ошибки конфигурации:\n" + "\n".join(f"  - {e}" for e in errors))
        return cls(cfg)

    async def run(self) -> None:
        """Основной цикл. Работает до Ctrl+C."""
        await self.market_ctx.start()
        await self.notifier.start()

        # Возобновление с последнего сохранённого timestamp
        start_ts = self._get_start_ts()

        await self.notifier.send_status(
            f"Трекер запущен (Data API). Whitelist: {len(self.watchlist)} адресов. "
            f"MIN_TRADE=${self.config.min_trade_usdc:.0f}, "
            f"MAX_VOL24H=${self.config.max_market_volume_24h:,.0f}, "
            f"poll={self.config.data_api_poll_interval:.1f}с"
        )
        log.info("=== Tracker started ===")

        # Запускаем периодическую статистику в фоне
        stats_task = asyncio.create_task(self._stats_loop())

        try:
            async for trade in self.listener.stream_trades(start_ts=start_ts):
                try:
                    await self._process_trade(trade)
                except Exception as e:
                    log.exception("Ошибка обработки trade %s: %s", trade.tx_hash, e)
        except asyncio.CancelledError:
            log.info("Остановка трекера...")
        finally:
            stats_task.cancel()
            await self.market_ctx.close()
            await self.listener.close()
            await self.notifier.close()

    def _get_start_ts(self) -> int:
        """Возобновление с последнего сохранённого timestamp сделки."""
        saved = self.storage.get_checkpoint("last_trade_ts")
        if saved:
            try:
                return int(saved)
            except ValueError:
                pass
        return 0  # 0 → listener возьмёт самые свежие сделки

    async def _process_trade(self, trade: Trade) -> None:
        """Обработка одной сделки: обновить БД, оценить, отправить сигналы."""
        self._stats_trades += 1

        # 1. Сохранить сделку (дубликаты — False)
        saved = self.storage.save_trade(
            tx_hash=trade.tx_hash,
            log_index=trade.log_index,
            ts=trade.timestamp,
            block_number=trade.block_number,
            maker=trade.maker,
            token_id=trade.token_id,
            side=trade.side,
            usdc_amount=trade.usdc_amount,
            price=trade.price,
        )
        if not saved:
            return  # уже видели

        # 2. Обновить стату кошелька
        wallet_stats = self.storage.upsert_wallet_trade(
            address=trade.maker,
            ts=trade.timestamp,
            usdc=trade.usdc_amount,
        )
        assessment = self.wallet_analyzer.assess(wallet_stats)

        # 3. Обновить чекпоинт
        self.storage.set_checkpoint("last_trade_ts", str(trade.timestamp))

        # 4. Быстрый фильтр: если сделка мелкая И не whitelist — не тратим запрос к Gamma
        is_whitelisted = self.watchlist.is_whitelisted(trade.maker)
        if (
            not is_whitelisted
            and trade.usdc_amount < self.config.min_trade_usdc
        ):
            return

        # 5. Получить метаданные рынка (Gamma API). Если Data API уже вернул
        # title/slug/outcome — используем их и подтягиваем только volume24h из Gamma.
        market = await self.market_ctx.get_by_token_id(trade.token_id)

        # Обогащаем market данными из Trade (Data API даёт title/slug/outcome
        # сразу, без необходимости лезть в Gamma — это защита если Gamma
        # вернёт None для нового рынка)
        if market is None and trade.title and trade.slug:
            from .market_context import MarketInfo
            market = MarketInfo(
                condition_id=trade.condition_id or "",
                question=trade.title,
                slug=trade.slug,
                category="",  # без Gamma не знаем
                volume_24h=0.0,
                volume_total=0.0,
                liquidity=0.0,
                end_date_iso=None,
                outcome=trade.outcome or "?",
                closed=False,
            )
            log.debug("Market metadata из Trade (Gamma промахнулся): %s", trade.slug)

        # 6. Прогнать через детектор
        signals = self.detector.evaluate(trade, market, assessment)

        if signals:
            log.info("📡 %d сигнал(ов) на %s: %s", len(signals), trade, [s.signal_type for s in signals])

        for signal in signals:
            await self._emit_signal(signal)

    async def _emit_signal(self, signal: Signal) -> None:
        """Сохранить сигнал в БД и отправить в Telegram."""
        self._stats_signals += 1

        signal_id = self.storage.save_signal(
            ts=signal.trade.timestamp,
            signal_type=signal.signal_type,
            maker=signal.trade.maker,
            token_id=signal.trade.token_id,
            market_slug=signal.market.slug,
            usdc_amount=signal.trade.usdc_amount,
            price=signal.trade.price,
            reason=signal.reason,
            tx_hash=signal.trade.tx_hash,
        )

        msg_id = await self.notifier.send_signal(signal)
        if msg_id:
            self.storage.update_signal_telegram(signal_id, msg_id)

    async def _stats_loop(self) -> None:
        """Раз в час пишем в лог сводку."""
        while True:
            try:
                await asyncio.sleep(3600)
                uptime = (time.time() - self._stats_started_at) / 3600.0
                log.info(
                    "📊 Статистика: %d сделок обработано, %d сигналов, uptime=%.1fч",
                    self._stats_trades, self._stats_signals, uptime,
                )
            except asyncio.CancelledError:
                break
