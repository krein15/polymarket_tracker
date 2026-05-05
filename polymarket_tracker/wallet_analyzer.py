"""Анализатор кошельков: "новый трейдер" / "опытный".

Стратегия двух источников:
1. Локальная БД (storage) — сколько раз мы видели кошелёк и когда впервые.
2. Polymarket Data API — история активности. Даёт более точный возраст.

В MVP используем ТОЛЬКО локальную БД — это надёжно, без зависимостей от
внешнего API и без rate limit проблем. Data API можно подключить позже,
это даст точный возраст при первой встрече кошелька.

Ограничение локального подхода: первые 2-4 недели работы трекера ВСЕ
кошельки будут выглядеть "новыми" (мы их впервые видим). После этого
система откалибруется сама.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .config import Config
from .storage import Storage, WalletStats

log = logging.getLogger(__name__)


@dataclass
class WalletAssessment:
    """Оценка кошелька по нашим фильтрам."""

    address: str
    is_new: bool  # trade_count < max И age < max_days
    is_known_veteran: bool  # trade_count > 100 (опытный) - не сигнал сам по себе
    first_seen_days_ago: float
    trade_count: int
    total_volume_usdc: float
    reason: str  # человекочитаемое объяснение


class WalletAnalyzer:
    def __init__(self, storage: Storage, config: Config):
        self.storage = storage
        self.config = config

    def assess(self, stats: WalletStats) -> WalletAssessment:
        """Оценить кошелёк по его статистике."""
        now = int(time.time())
        age_days = stats.age_days(now)

        is_new = (
            stats.trade_count <= self.config.new_wallet_max_trades
            and age_days <= self.config.new_wallet_max_age_days
        )
        is_veteran = stats.trade_count > 100

        if is_new:
            reason = f"новый ({stats.trade_count} сделок, {age_days:.1f}д)"
        elif is_veteran:
            reason = f"ветеран ({stats.trade_count} сделок)"
        else:
            reason = f"обычный ({stats.trade_count} сделок)"

        return WalletAssessment(
            address=stats.address,
            is_new=is_new,
            is_known_veteran=is_veteran,
            first_seen_days_ago=age_days,
            trade_count=stats.trade_count,
            total_volume_usdc=stats.total_volume_usdc,
            reason=reason,
        )
