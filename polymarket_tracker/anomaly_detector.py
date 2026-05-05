"""Детектор аномалий — главная логика.

Две независимые ветки детекции:

Ветка A — "Suspicious entry":
    Все фильтры должны совпасть одновременно:
    - размер сделки ≥ min_trade_usdc (отсекаем мелочь)
    - категория рынка НЕ в ignored_categories (crypto, sports)
    - volume_24h рынка < max_market_volume (малоликвидный)
    - maker считается "новым" (trade_count и age)
    - ИЛИ есть кластер из N+ новых кошельков на этом рынке за последний час

Ветка B — "Whitelist activity":
    - maker ∈ whitelist
    - размер сделки ≥ whitelist_min_usdc

Обе ветки работают параллельно — одна сделка может генерить оба сигнала.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .config import Config
from .data_api_listener import Trade
from .market_context import MarketInfo
from .storage import Storage
from .wallet_analyzer import WalletAssessment
from .watchlist import Watchlist

log = logging.getLogger(__name__)


@dataclass
class Signal:
    """Сигнал для отправки в Telegram."""

    signal_type: str  # "suspicious_entry" | "whitelist" | "cluster"
    trade: Trade
    market: MarketInfo
    wallet: WalletAssessment
    reason: str  # что именно сработало
    cluster_size: int = 0  # для кластерного сигнала


class AnomalyDetector:
    def __init__(self, config: Config, storage: Storage, watchlist: Watchlist):
        self.config = config
        self.storage = storage
        self.watchlist = watchlist

    def evaluate(
        self,
        trade: Trade,
        market: Optional[MarketInfo],
        wallet: WalletAssessment,
    ) -> list[Signal]:
        """Прогнать сделку через обе ветки. Может вернуть 0, 1 или 2 сигнала."""
        signals: list[Signal] = []

        # ── Ветка B: Whitelist ──
        # Обрабатывается первой, т.к. не требует market metadata
        if self.watchlist.is_whitelisted(trade.maker):
            if trade.usdc_amount >= self.config.whitelist_min_usdc and market:
                signals.append(
                    Signal(
                        signal_type="whitelist",
                        trade=trade,
                        market=market,
                        wallet=wallet,
                        reason=f"Whitelisted кошелёк, ${trade.usdc_amount:.0f}",
                    )
                )

        # ── Ветка A: Suspicious entry ──
        # Требуем market metadata
        if market is None:
            return signals

        if not self._passes_base_filters(trade, market):
            return signals

        # Проверяем кластер (несколько новых кошельков)
        cluster_size = self.storage.count_recent_new_wallets_for_token(
            token_id=trade.token_id,
            since_ts=trade.timestamp - self.config.cluster_window_seconds,
            max_trades=self.config.new_wallet_max_trades,
        )

        if cluster_size >= self.config.cluster_min_wallets:
            signals.append(
                Signal(
                    signal_type="cluster",
                    trade=trade,
                    market=market,
                    wallet=wallet,
                    reason=f"Кластер: {cluster_size} новых кошельков за час",
                    cluster_size=cluster_size,
                )
            )
        elif wallet.is_new:
            # Одиночный новый кошелёк — тоже сигнал (но слабее)
            signals.append(
                Signal(
                    signal_type="suspicious_entry",
                    trade=trade,
                    market=market,
                    wallet=wallet,
                    reason=(
                        f"Новый кошелёк (trades={wallet.trade_count}, "
                        f"age={wallet.first_seen_days_ago:.1f}д), "
                        f"малоликвидный рынок (vol24h=${market.volume_24h:.0f})"
                    ),
                )
            )

        return signals

    def _passes_base_filters(self, trade: Trade, market: MarketInfo) -> bool:
        """Базовые фильтры Ветки A (кроме новизны кошелька)."""
        cfg = self.config

        if trade.side != "buy":
            return False  # интересуют только покупки — открытие позиции

        if trade.usdc_amount < cfg.min_trade_usdc:
            return False

        if market.closed:
            return False

        if market.category in cfg.ignored_categories:
            return False

        if market.volume_24h > cfg.max_market_volume_24h:
            return False

        return True
