"""Общие фикстуры и конструкторы тестовых объектов.

Тесты не ходят в сеть и не трогают боевую БД: Storage создаётся на временном
файле (sqlite3 в памяти не подходит — Storage открывает соединение на каждую
операцию, и in-memory база умирала бы между вызовами).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polymarket_tracker.config import Config  # noqa: E402
from polymarket_tracker.data_api_listener import Trade  # noqa: E402
from polymarket_tracker.market_context import MarketInfo  # noqa: E402
from polymarket_tracker.storage import Storage  # noqa: E402
from polymarket_tracker.wallet_analyzer import WalletAssessment  # noqa: E402

NOW = 1_788_000_000


@pytest.fixture
def storage(tmp_path) -> Storage:
    return Storage(str(tmp_path / "test.db"))


@pytest.fixture
def config() -> Config:
    """Конфиг с явными значениями — не зависит от .env разработчика."""
    return Config(
        telegram_bot_token="t",
        telegram_chat_id="1",
        min_trade_usdc=2000.0,
        max_trade_price=0.95,
        max_market_volume_24h=50_000.0,
        new_wallet_max_trades=20,
        new_wallet_max_age_days=30,
        cluster_min_wallets=3,
        cluster_window_seconds=3600,
        ignored_categories={"crypto", "sports"},
        allowed_tags=set(),
        scoring_enabled=True,
        score_threshold=50.0,
        scoring_min_trade_usdc=200.0,
        accumulation_window_seconds=1800,
    )


def make_trade(
    usdc: float = 5000.0,
    price: float = 0.5,
    side: str = "buy",
    maker: str = "0x" + "1" * 40,
    token_id: str = "token-1",
    ts: int = NOW,
    tx_hash: str = "0xabc",
    condition_id: str = "",
) -> Trade:
    return Trade(
        condition_id=condition_id,
        tx_hash=tx_hash,
        log_index=0,
        block_number=0,
        timestamp=ts,
        exchange="data_api",
        maker=maker,
        taker="",
        side=side,
        token_id=token_id,
        usdc_amount=usdc,
        shares=usdc / max(price, 0.001),
        price=price,
    )


def make_market(
    category: str = "politics",
    tags=("politics", "elections"),
    volume_24h: float = 10_000.0,
    closed: bool = False,
) -> MarketInfo:
    return MarketInfo(
        condition_id="cond",
        question="Вопрос?",
        slug="market-slug",
        category=category,
        volume_24h=volume_24h,
        volume_total=100_000.0,
        liquidity=5_000.0,
        end_date_iso=None,
        outcome="Yes",
        closed=closed,
        tags=frozenset(tags),
        event_slug="event-slug",
    )


def make_wallet(is_new: bool = True, trade_count: int = 3) -> WalletAssessment:
    return WalletAssessment(
        address="0x" + "1" * 40,
        is_new=is_new,
        is_known_veteran=False,
        first_seen_days_ago=1.0,
        trade_count=trade_count,
        total_volume_usdc=1000.0,
        reason="новый" if is_new else "обычный",
    )
