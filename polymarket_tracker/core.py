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
from .outcome_tracker import OutcomeTracker
from .storage import Storage
from .telegram_commands import TelegramCommandHandler
from .telegram_notifier import TelegramNotifier
from .wallet_analyzer import WalletAnalyzer
from .config import CTF_EXCHANGE_V2, NEG_RISK_CTF_EXCHANGE_V2
from .confirmation import ChaseConfirmer
from .entry_price import EntryPriceTracker
from .fast_lane import FastLane
from .onchain_listener import OnchainListener
from .heartbeat import Heartbeat, TrackerStats
from .wallet_history import WalletHistoryProvider
from .watchlist import Watchlist

log = logging.getLogger(__name__)

# Чекпоинт пишем не на каждую сделку, а раз в CHECKPOINT_EVERY: это была треть
# всех записей в БД. Потеря последних секунд безопасна — листенер перечитает
# сделки от последнего сохранённого ts, а save_trade отсеет дубликаты.
CHECKPOINT_EVERY = 25


class _TokenRedactor(logging.Filter):
    """Вырезает токен бота из логов.

    aiohttp вкладывает полный URL запроса в текст своих исключений, а URL
    Telegram Bot API содержит токен целиком. Без этого фильтра любой таймаут
    к api.telegram.org печатает секрет в консоль и в файл лога — а логи
    пересылают, когда просят помочь разобраться.

    Фильтр вешается на обработчики root-логгера, поэтому накрывает и
    сообщения, и аргументы, и текст трейсбеков.
    """

    MASK = "<TOKEN>"

    def __init__(self, token: str):
        super().__init__()
        self.token = token or ""

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.token:
            return True
        if isinstance(record.msg, str) and self.token in record.msg:
            record.msg = record.msg.replace(self.token, self.MASK)
        if record.args:
            args = record.args if isinstance(record.args, tuple) else (record.args,)
            masked = []
            for a in args:
                text = str(a)
                masked.append(text.replace(self.token, self.MASK) if self.token in text else a)
            record.args = tuple(masked) if isinstance(record.args, tuple) else masked[0]
        if record.exc_info:
            # Текст исключения формируется позже — подставляем очищенный заранее.
            record.exc_text = logging.Formatter().formatException(
                record.exc_info
            ).replace(self.token, self.MASK)
        return True


def _install_token_redaction(token: str) -> None:
    """Повесить редактор токена на все обработчики root-логгера (идемпотентно)."""
    if not token:
        return
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, _TokenRedactor) for f in handler.filters):
            handler.addFilter(_TokenRedactor(token))


class PolymarketTracker:
    def __init__(self, config: Config):
        self.config = config
        # До создания любых сетевых клиентов: их ошибки не должны светить токен.
        _install_token_redaction(config.telegram_bot_token)
        self.storage = Storage(config.db_path)
        self.watchlist = Watchlist(config.whitelist_file)
        self.listener = DataApiListener(config)
        self.market_ctx = MarketContext()
        self.wallet_analyzer = WalletAnalyzer(self.storage, config)
        self.detector = AnomalyDetector(config, self.storage, self.watchlist)
        self.notifier = TelegramNotifier(
            config.telegram_bot_token, config.telegram_chat_id,
            max_per_hour=config.telegram_max_per_hour,
        )
        # Отдельный MarketContext для outcome_tracker — изолирует HTTP-сессию.
        # Иначе таймаут в одном месте закрывает сессию посреди запроса в другом.
        self.outcome_market_ctx = MarketContext()
        self.outcome_tracker = OutcomeTracker(self.storage, self.outcome_market_ctx)
        self.commands = TelegramCommandHandler(config, self.storage)
        self.onchain = None
        # История кошелька из API — снимает холодный старт признаков.
        # Запрашивается только для сделок-кандидатов, с кэшем.
        self.wallet_history = (
            WalletHistoryProvider(
                ttl_seconds=config.wallet_history_ttl_seconds,
                max_concurrency=config.wallet_history_concurrency,
            )
            if config.wallet_history_enabled else None
        )

        # Счётчики для периодической статистики
        self._stats_trades = 0
        self._stats_signals = 0
        self._stats_started_at = time.time()

        # Отложенный чекпоинт
        self._checkpoint_ts = 0
        self._since_checkpoint = 0

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
        await self.outcome_market_ctx.start()
        if self.wallet_history is not None:
            await self.wallet_history.start()
        await self.notifier.start()

        # Возобновление с последнего сохранённого timestamp
        start_ts = self._get_start_ts()

        if (
            self.config.shadow_enabled
            and self.config.shadow_max_volume_24h < self.config.max_market_volume_24h
        ):
            log.warning(
                "SHADOW_MAX_VOLUME_24H ($%.0f) < MAX_MARKET_VOLUME_24H ($%.0f): "
                "shadow-выборка не накроет все боевые сигналы — увеличь порог",
                self.config.shadow_max_volume_24h, self.config.max_market_volume_24h,
            )

        shadow_str = (
            f"shadow≤${self.config.shadow_max_volume_24h:,.0f}"
            if self.config.shadow_enabled else "shadow=off"
        )
        await self.notifier.send_status(
            f"Трекер запущен (Data API). Whitelist: {len(self.watchlist)} адресов. "
            f"MIN_TRADE=${self.config.min_trade_usdc:.0f}, "
            f"MAX_VOL24H=${self.config.max_market_volume_24h:,.0f}, "
            f"poll={self.config.data_api_poll_interval:.1f}с, {shadow_str}"
        )
        log.info("=== Tracker started ===")

        # Запускаем периодические фоновые задачи
        stats_task = asyncio.create_task(self._stats_loop())
        outcome_task = asyncio.create_task(self.outcome_tracker.run())
        commands_task = asyncio.create_task(self.commands.run())
        # Быстрая полоса включается только при заданном вебсокете: без него
        # слушать нечего, и трекер работает как раньше.
        fast_lane_task = None
        if self.config.onchain_enabled and self.config.onchain_wss_urls:
            self.onchain = OnchainListener(
                self.config.onchain_wss_urls,
                [CTF_EXCHANGE_V2, NEG_RISK_CTF_EXCHANGE_V2],
                min_usdc=self.config.onchain_min_usdc,
            )
            await self.onchain.start()
            fast_lane_task = asyncio.create_task(
                FastLane(self.onchain, self.storage, self.market_ctx,
                         self.notifier, self.config).run()
            )
        elif self.config.onchain_enabled:
            log.info("Быстрая полоса выключена: ONCHAIN_WSS_URLS не задан")

        chase_task = (
            asyncio.create_task(
                ChaseConfirmer(self.storage, self.notifier, self.config).run()
            )
            if self.config.chase_enabled else None
        )
        entry_task = (
            asyncio.create_task(
                EntryPriceTracker(self.storage, self.config).run()
            )
            if self.config.entry_price_enabled else None
        )
        heartbeat_task = (
            asyncio.create_task(
                Heartbeat(self.notifier, self._collect_stats, self.config).run()
            )
            if self.config.heartbeat_enabled else None
        )

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
            outcome_task.cancel()
            commands_task.cancel()
            if heartbeat_task is not None:
                heartbeat_task.cancel()
            if chase_task is not None:
                chase_task.cancel()
            if fast_lane_task is not None:
                fast_lane_task.cancel()
            if entry_task is not None:
                entry_task.cancel()
            # Дать задачам корректно завершиться (подавляем CancelledError)
            for t in (stats_task, outcome_task, commands_task, heartbeat_task,
                      chase_task, fast_lane_task, entry_task):
                if t is None:
                    continue
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
            await self.market_ctx.close()
            await self.outcome_market_ctx.close()
            if self.wallet_history is not None:
                await self.wallet_history.close()
            if getattr(self, "onchain", None) is not None:
                await self.onchain.close()
            await self.listener.close()
            await self.notifier.close()
            await self.commands.close()
            # Коммит отложен пачками — без этого последние секунды работы
            # (включая чекпоинт) остались бы незаписанными.
            self._flush_checkpoint()
            self.storage.close()

    def _flush_checkpoint(self) -> None:
        """Записать накопленный чекпоинт, если есть что писать."""
        if self._checkpoint_ts and self._since_checkpoint:
            self.storage.set_checkpoint("last_trade_ts", str(self._checkpoint_ts))
            self._since_checkpoint = 0

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
            condition_id=trade.condition_id or "",
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

        # 3. Чекпоинт — пачкой, а не на каждую сделку (см. CHECKPOINT_EVERY)
        self._checkpoint_ts = max(self._checkpoint_ts, trade.timestamp)
        self._since_checkpoint += 1
        if self._since_checkpoint >= CHECKPOINT_EVERY:
            self._flush_checkpoint()

        # 4. Быстрый фильтр: если сделка мелкая И не whitelist — не тратим запрос к Gamma.
        # Порог берём минимальный из двух: скоринг работает с сделками заметно
        # мельче min_trade_usdc, чтобы видеть набор позиции частями.
        is_whitelisted = self.watchlist.is_whitelisted(trade.maker)
        min_interesting = (
            min(self.config.min_trade_usdc, self.config.scoring_min_trade_usdc)
            if self.config.scoring_enabled
            else self.config.min_trade_usdc
        )
        if not is_whitelisted and trade.usdc_amount < min_interesting:
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
                event_slug=trade.event_slug or "",
                category="",  # без Gamma не знаем
                volume_24h=0.0,
                volume_total=0.0,
                liquidity=0.0,
                end_date_iso=None,
                outcome=trade.outcome or "?",
                closed=False,
            )
            log.debug("Market metadata из Trade (Gamma промахнулся): %s", trade.slug)

        # 5.5. История кошелька из API. Только для кандидатов (мелочь сюда
        # не доходит — отсеяна быстрым фильтром выше), с кэшем и молчаливым
        # откатом на локальные данные при недоступности API.
        # Берём только готовую историю: ждать сеть здесь нельзя — цикл приёма
        # последовательный, и каждая секунда ожидания превращается в отставание
        # от API. Если истории ещё нет, ставим фоновую загрузку и считаем
        # признаки по локальным данным; к следующей сделке этого кошелька
        # история уже будет.
        history = None
        if self.wallet_history is not None:
            history = self.wallet_history.cached(trade.maker)
            if history is None:
                self.wallet_history.prefetch(trade.maker)

        # 6. Прогнать через детектор
        result = self.detector.evaluate(trade, market, assessment, history=history)
        signals = result.signals

        if signals:
            log.info("📡 %d сигнал(ов) на %s: %s", len(signals), trade, [s.signal_type for s in signals])

        for signal in signals:
            await self._emit_signal(signal)

        # 7. Shadow capture (TODO 0.3): фиксируем сделку для измерения
        #    false negatives — после отправки сигналов, чтобы запись в
        #    shadow не задерживала боевой сигнал.
        self._record_shadow_trade(trade, market, result)

    def _record_shadow_trade(self, trade: Trade, market, result) -> None:
        """Shadow capture: фиксируем ВСЕ покупки >= MIN_TRADE_USDC на
        неликвидных рынках — с баллом скоринга и с вердиктом прежней Ветки A.

        passed_filters — сработала бы прежняя цепочка И. Сигналов она больше
        не шлёт, но её вердикт пишется рядом с баллом: только так можно на
        одних и тех же сделках сравнить старую методику с новой.

        score/score_parts пишем всегда, когда балл посчитан, — даже если он
        ниже порога. Иначе порог не на чем калибровать: в выборке остались бы
        только те сделки, что и так прошли.

        Ограничение: выборка по-прежнему начинается с MIN_TRADE_USDC, поэтому
        накопление позиции мелкими покупками в неё не попадает — резолвить
        столько строк outcome_tracker не успеет.

        Любая ошибка здесь не должна ломать боевой путь — поэтому глушим.
        """
        cfg = self.config
        if not cfg.shadow_enabled:
            return
        if market is None:
            return  # без метаданных рынка не оценить volume — пропуск
        if trade.side != "buy":
            return  # shadow-выборка определена как buy-only (Ветка A — buy-only)
        if trade.usdc_amount < cfg.min_trade_usdc:
            return
        if market.volume_24h > cfg.shadow_max_volume_24h:
            return

        passed = result.legacy_passed
        types = sorted(set(result.legacy_types) | {s.signal_type for s in result.signals})
        score = result.score

        try:
            self.storage.save_shadow_trade(
                tx_hash=trade.tx_hash,
                maker=trade.maker,
                token_id=trade.token_id,
                ts=trade.timestamp,
                side=trade.side,
                usdc_amount=trade.usdc_amount,
                price=trade.price,
                market_slug=market.slug,
                category=market.category,
                volume_24h=market.volume_24h,
                passed_filters=passed,
                signal_types=",".join(types) if types else None,
                now_ts=int(time.time()),
                score=score.total if score else None,
                score_parts=score.parts_json() if score else None,
            )
        except Exception as e:
            log.warning("Shadow capture не удался для %s: %s", trade.tx_hash, e)

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
            side=signal.trade.side,
            score=signal.score.total if signal.score else None,
            score_parts=signal.score.parts_json() if signal.score else None,
        )

        # Заводим болванку для outcome-трекера (фаза 1.2)
        self.storage.init_outcome_record(signal_id, int(time.time()))

        msg_id = await self.notifier.send_signal(signal)
        if msg_id:
            self.storage.update_signal_telegram(signal_id, msg_id)

    def _collect_stats(self) -> TrackerStats:
        """Снимок для сводки. Синхронный: запросы к SQLite дешёвые."""
        last_ts = self.storage.last_trade_ts()
        return TrackerStats(
            uptime_hours=(time.time() - self._stats_started_at) / 3600.0,
            trades_processed=self._stats_trades,
            signals_sent=self._stats_signals,
            last_trade_age_min=(
                (time.time() - last_ts) / 60.0 if last_ts else None
            ),
            trades_total=self.storage.count_trades(),
            signals_total=self.storage.count_signals(),
            resolved_total=self.storage.count_resolved_outcomes(),
            wallets_total=self.storage.count_wallets(),
        )

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
