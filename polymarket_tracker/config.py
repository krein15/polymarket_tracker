"""Загрузка конфигурации из .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Set

from dotenv import load_dotenv


# ═══════════════════════════════════════════════════════════════════
# Polymarket Data API — основной источник данных с CTFv2 миграции (28.04.2026)
# ═══════════════════════════════════════════════════════════════════

DATA_API_BASE = "https://data-api.polymarket.com"
DATA_API_TRADES_URL = f"{DATA_API_BASE}/trades"

# Polymarket Gamma API — метаданные рынков
GAMMA_API_BASE = "https://gamma-api.polymarket.com"


# ═══════════════════════════════════════════════════════════════════
# Legacy ончейн-константы (V1, до 28.04.2026)
# Оставлены на случай возврата к ончейн-листингу. На V2 не используются.
# ═══════════════════════════════════════════════════════════════════

# V1 (mertв с 28.04.2026)
CTF_EXCHANGE_V1 = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_CTF_EXCHANGE_V1 = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# V2 (28.04.2026+) — для будущего ончейн-листингa если понадобится
CTF_EXCHANGE_V2 = "0xE111180000d2663C0091e4f400237545B87B996B"
NEG_RISK_CTF_EXCHANGE_V2 = "0xe2222d279d744050d28e00520010520000310F59"
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"

# Алиасы для обратной совместимости с любым кодом, который мог их импортировать
CTF_EXCHANGE = CTF_EXCHANGE_V1
NEG_RISK_CTF_EXCHANGE = NEG_RISK_CTF_EXCHANGE_V1
USDC = USDC_E
USDC_DECIMALS = 6


# ═══════════════════════════════════════════════════════════════════
# Config dataclass
# ═══════════════════════════════════════════════════════════════════


@dataclass
class Config:
    """Конфигурация трекера. Все значения из .env."""

    # Telegram (обязательные)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Data API (заменили ончейн-листенер)
    data_api_poll_interval: float = 3.0  # сек между запросами к /trades
    data_api_batch_limit: int = 200  # размер выборки на один запрос (max 10000)

    # Signal filters
    min_trade_usdc: float = 500.0
    max_trade_price: float = 0.95  # отсекаем "почти решённые" рынки (ROI<спред)
    max_market_volume_24h: float = 100_000.0
    new_wallet_max_trades: int = 20
    new_wallet_max_age_days: int = 30
    cluster_min_wallets: int = 2
    cluster_window_seconds: int = 3600

    # Category filter
    ignored_categories: Set[str] = field(default_factory=lambda: {"crypto", "sports"})

    # Whitelist
    whitelist_file: str = "data/whitelist.txt"
    whitelist_min_usdc: float = 200.0

    # Shadow tracker (TODO 0.3): измерение false negatives фильтров Ветки A.
    # Пишем ВСЕ покупки >= min_trade_usdc на рынках с volume_24h ниже широкого
    # порога, без боевых фильтров, и сравниваем исход отброшенных с пропущенными.
    # Порог должен быть >= max_market_volume_24h, иначе shadow-выборка не
    # накроет все боевые сигналы.
    shadow_enabled: bool = True
    shadow_max_volume_24h: float = 500_000.0

    # Storage
    db_path: str = "data/tracker.db"

    # Logging
    log_level: str = "INFO"

    # Legacy (не используется в Data API режиме, оставлено чтобы не падало
    # на старых .env с этими полями)
    polygon_rpc_url: str = ""
    start_blocks_back: int = 50
    poll_interval: float = 3.0
    batch_blocks: int = 100

    @classmethod
    def from_env(cls, env_path: str | Path = ".env") -> "Config":
        """Загрузить из .env-файла."""
        load_dotenv(env_path)

        def _float(key: str, default: float) -> float:
            val = os.getenv(key)
            return float(val) if val else default

        def _int(key: str, default: int) -> int:
            val = os.getenv(key)
            return int(val) if val else default

        def _str(key: str, default: str) -> str:
            return os.getenv(key, default)

        def _bool(key: str, default: bool) -> bool:
            val = os.getenv(key)
            if val is None or val.strip() == "":
                return default
            return val.strip().lower() in ("1", "true", "yes", "on", "да")

        ignored = _str("IGNORED_CATEGORIES", "crypto,sports").lower()
        ignored_set = {c.strip() for c in ignored.split(",") if c.strip()}

        cfg = cls(
            telegram_bot_token=_str("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=_str("TELEGRAM_CHAT_ID", ""),
            data_api_poll_interval=_float("DATA_API_POLL_INTERVAL", 3.0),
            data_api_batch_limit=_int("DATA_API_BATCH_LIMIT", 200),
            min_trade_usdc=_float("MIN_TRADE_USDC", 500.0),
            max_trade_price=_float("MAX_TRADE_PRICE", 0.95),
            max_market_volume_24h=_float("MAX_MARKET_VOLUME_24H", 100_000.0),
            new_wallet_max_trades=_int("NEW_WALLET_MAX_TRADES", 20),
            new_wallet_max_age_days=_int("NEW_WALLET_MAX_AGE_DAYS", 30),
            cluster_min_wallets=_int("CLUSTER_MIN_WALLETS", 2),
            cluster_window_seconds=_int("CLUSTER_WINDOW_SECONDS", 3600),
            ignored_categories=ignored_set,
            whitelist_file=_str("WHITELIST_FILE", "data/whitelist.txt"),
            whitelist_min_usdc=_float("WHITELIST_MIN_USDC", 200.0),
            shadow_enabled=_bool("SHADOW_ENABLED", True),
            shadow_max_volume_24h=_float("SHADOW_MAX_VOLUME_24H", 500_000.0),
            db_path=_str("DB_PATH", "data/tracker.db"),
            log_level=_str("LOG_LEVEL", "INFO"),
            # Legacy (просто чтобы старые .env не ломались)
            polygon_rpc_url=_str("POLYGON_RPC_URL", ""),
            start_blocks_back=_int("START_BLOCKS_BACK", 50),
            poll_interval=_float("POLL_INTERVAL", 3.0),
            batch_blocks=_int("BATCH_BLOCKS", 100),
        )
        return cfg

    def validate(self) -> list[str]:
        """Возвращает список ошибок (если пусто — конфиг ок)."""
        errors = []
        if not self.telegram_bot_token:
            errors.append("TELEGRAM_BOT_TOKEN не задан")
        if not self.telegram_chat_id:
            errors.append("TELEGRAM_CHAT_ID не задан")
        if self.data_api_poll_interval < 1.0:
            errors.append("DATA_API_POLL_INTERVAL < 1.0 сек — слишком агрессивно")
        if self.data_api_batch_limit < 1 or self.data_api_batch_limit > 10000:
            errors.append("DATA_API_BATCH_LIMIT должен быть в диапазоне [1, 10000]")
        if not 0 < self.max_trade_price <= 1.0:
            errors.append(f"MAX_TRADE_PRICE должен быть в (0, 1.0], сейчас {self.max_trade_price}")
        return errors
