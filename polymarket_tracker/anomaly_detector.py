"""Детектор — решает, какая сделка достойна сигнала.

Схема после перехода на скоринг (29.08.2026):

Ветка B — "Whitelist" (без изменений):
    - maker ∈ whitelist
    - размер сделки ≥ whitelist_min_usdc

Ветка S — "Score" (заменила прежнюю Ветку A):
    Жёсткие ворота (то, что бессмысленно взвешивать):
        - только покупки
        - рынок не закрыт
        - категория/теги не в ignored_categories (с учётом allowed_tags)
        - размер сделки ≥ scoring_min_trade_usdc — дешёвый предфильтр,
          чтобы не гонять запросы к БД на каждую мелочь
    Дальше признаки складываются в балл (см. scoring.py), сигнал уходит
    при score ≥ score_threshold.

Ветка A — прежняя цепочка И — ПРОДОЛЖАЕТ считаться, но сигналов больше не
шлёт: её вердикт пишется в shadow_trades.passed_filters. Это даёт прямое
сравнение старой и новой методики на одних и тех же сделках. Выкидывать её
можно будет, когда накопится статистика и станет видно, кто кого.

Ветки независимы: одна сделка может дать и whitelist-сигнал, и score-сигнал.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from .config import Config
from .data_api_listener import Trade
from .market_context import MarketInfo
from .scoring import FeatureExtractor, Score, compute_score
from .storage import Storage
from .wallet_analyzer import WalletAssessment
from .watchlist import Watchlist

log = logging.getLogger(__name__)


@dataclass
class Signal:
    """Сигнал для отправки в Telegram."""

    signal_type: str  # "score" | "whitelist" | (legacy: "suspicious_entry" | "cluster")
    trade: Trade
    market: MarketInfo
    wallet: WalletAssessment
    reason: str  # что именно сработало
    cluster_size: int = 0  # для кластерного сигнала
    score: Optional[Score] = None  # заполнен у сигналов Ветки S


@dataclass
class EvaluationResult:
    """Итог разбора одной сделки.

    Кроме сигналов несёт то, что нужно теневой выборке: балл (даже когда он
    ниже порога — иначе порог не на чем калибровать) и вердикт старой Ветки A.
    """

    signals: list = field(default_factory=list)
    score: Optional[Score] = None
    legacy_passed: bool = False  # сработала бы прежняя цепочка И
    legacy_types: list = field(default_factory=list)


class AnomalyDetector:
    def __init__(self, config: Config, storage: Storage, watchlist: Watchlist):
        self.config = config
        self.storage = storage
        self.watchlist = watchlist
        self.features = FeatureExtractor(storage, config)

    def evaluate(
        self,
        trade: Trade,
        market: Optional[MarketInfo],
        wallet: WalletAssessment,
    ) -> EvaluationResult:
        """Прогнать сделку через все ветки."""
        result = EvaluationResult()

        # ── Ветка B: Whitelist ──
        # Первая, т.к. не требует ни метаданных рынка, ни запросов к БД.
        entry = self.watchlist.get(trade.maker)
        if entry is not None and market and entry.signals_on_its_own:
            # Персональный порог: 90-й процентиль покупок этого кошелька.
            # $200 от того, кто обычно ставит $5000, — шум; $2000 от того,
            # кто обычно ставит $200, — редкая уверенность.
            threshold = max(self.config.whitelist_min_usdc, entry.big_usdc)
            # Хедж не копируем: если он уже взял противоположный исход этого
            # же рынка, сделка не выражает мнения о результате. Ветка B
            # раньше этого не проверяла вовсе — смотрела только на сумму.
            hedged = self.storage.wallet_bought_both_outcomes(
                trade.maker,
                trade.condition_id or "",
                trade.timestamp - self.config.accumulation_window_seconds,
            )
            if hedged:
                log.debug(
                    "Whitelist %s: пропускаю, куплены оба исхода рынка %s",
                    trade.maker[:10], trade.condition_id,
                )
            if trade.usdc_amount >= threshold and not hedged:
                personal = (
                    f", крупно для него (порог ${entry.big_usdc:,.0f})"
                    if entry.big_usdc > self.config.whitelist_min_usdc else ""
                )
                result.signals.append(
                    Signal(
                        signal_type="whitelist",
                        trade=trade,
                        market=market,
                        wallet=wallet,
                        reason=(
                            f"Whitelist {entry.nickname or trade.maker[:10]}, "
                            f"${trade.usdc_amount:.0f}{personal}"
                        ),
                    )
                )

        if market is None:
            return result

        # ── Ветка A (legacy): считаем вердикт, но не шлём ──
        result.legacy_types = self._legacy_branch_a_types(trade, market, wallet)
        result.legacy_passed = bool(result.legacy_types)

        # ── Ветка S: скоринг ──
        if not self.config.scoring_enabled:
            return result
        if not self._passes_hard_gates(trade, market):
            return result

        features = self.features.extract(
            trade, market, wallet,
            whitelist_tier=entry.tier if entry is not None else "",
        )
        score = compute_score(features, self.config)
        result.score = score

        if score.total >= self.config.score_threshold:
            result.signals.append(
                Signal(
                    signal_type="score",
                    trade=trade,
                    market=market,
                    wallet=wallet,
                    reason=f"Балл {score.total:.0f}: {score.summary()}",
                    cluster_size=features.cluster_new_wallets,
                    score=score,
                )
            )

        return result

    # ───────── Ворота Ветки S ─────────

    def _passes_hard_gates(self, trade: Trade, market: MarketInfo) -> bool:
        """Условия, которые бессмысленно взвешивать — либо да, либо нет."""
        cfg = self.config

        if trade.side != "buy":
            return False  # интересует открытие позиции, не выход

        if market.closed:
            return False

        # Дешёвый предфильтр: на каждую сделку признаки не считаем, это
        # несколько запросов к SQLite. Порог заметно ниже сигнального —
        # иначе не увидим тех, кто набирает позицию частями.
        if trade.usdc_amount < cfg.scoring_min_trade_usdc:
            return False

        return not self._is_ignored_category(market)

    def _is_ignored_category(self, market: MarketInfo) -> bool:
        """Категория рынка в чёрном списке (с учётом явных разрешений).

        Сверяем и category, и теги: рынок LoL приходит с тегами
        {esports, league-of-legends, games, sports} — по одному лишь
        category="esports" фильтр "sports" его не поймает.
        """
        cfg = self.config
        market_tags = {market.category} | set(market.tags)
        # ALLOWED_TAGS перевешивает: киберспорт помечен и как sports, вернуть
        # его иначе нельзя, не открыв заодно весь обычный спорт.
        if cfg.allowed_tags & market_tags:
            return False
        return bool(cfg.ignored_categories & market_tags)

    # ───────── Ветка A: прежняя логика, только для сравнения ─────────

    def _legacy_branch_a_types(
        self, trade: Trade, market: MarketInfo, wallet: WalletAssessment
    ) -> list:
        """Что прислала бы прежняя цепочка И. Сигналы отсюда не отправляются —
        вердикт нужен теневой выборке, чтобы сравнить старую методику с новой.
        """
        cfg = self.config

        if trade.side != "buy":
            return []
        if trade.usdc_amount < cfg.min_trade_usdc:
            return []
        if trade.price >= cfg.max_trade_price:
            return []
        if market.closed:
            return []
        if self._is_ignored_category(market):
            return []
        if market.volume_24h > cfg.max_market_volume_24h:
            return []

        cluster_size = self.storage.count_recent_new_wallets_for_token(
            token_id=trade.token_id,
            since_ts=trade.timestamp - cfg.cluster_window_seconds,
            max_trades=cfg.new_wallet_max_trades,
        )
        if cluster_size >= cfg.cluster_min_wallets:
            return ["cluster"]
        if wallet.is_new:
            return ["suspicious_entry"]
        return []
