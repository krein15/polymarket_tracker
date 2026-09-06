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
    data_api_batch_limit: int = 200
    # Сколько страниц догружать назад, пока не дойдём до чекпоинта. Одна
    # страница в 10000 сделок покрывает ~8 минут потока, а простои бывают
    # длиннее — без догрузки разрыв отсекался молча.
    data_api_max_pages: int = 6  # размер выборки на один запрос (max 10000)

    # Signal filters
    min_trade_usdc: float = 500.0
    max_trade_price: float = 0.95  # отсекаем "почти решённые" рынки (ROI<спред)
    max_market_volume_24h: float = 100_000.0
    new_wallet_max_trades: int = 20
    new_wallet_max_age_days: int = 30
    cluster_min_wallets: int = 2
    # Минимальный вклад участника, чтобы попасть в кластер. Без него кластер
    # считал любых участников подряд (см. count_cluster_participants).
    cluster_min_participant_usdc: float = 500.0
    cluster_window_seconds: int = 3600

    # Category filter
    ignored_categories: Set[str] = field(default_factory=lambda: {"crypto", "sports"})
    # Явное разрешение, перевешивает ignored_categories. Нужно, потому что
    # киберспортивные рынки Polymarket несут теги {esports, sports, ...}
    # одновременно: без исключения их режет фильтр "sports".
    allowed_tags: Set[str] = field(default_factory=set)

    # Scoring (взвешенная оценка вместо жёсткой цепочки И, см. scoring.py).
    # Пороговый балл — стартовая гипотеза: калибруется по shadow-выборке,
    # когда наберётся статистика (tools/shadow_report.py --by-score).
    scoring_enabled: bool = True
    # Порог сигнала: берём БОЛЬШЕЕ из абсолютного пола и доли от достижимого
    # максимума. Одного абсолютного мало — набор доступных признаков меняется
    # (история кошелька из API может быть недоступна, адрес может быть вне
    # whitelist), и тогда фиксированное число означает разную строгость.
    score_threshold: float = 50.0
    score_threshold_ratio: float = 0.5
    # Ниже этой суммы сигнал не отправляем, даже если балл набран: сделка на
    # $300 не действие, а шум. Считается по НАКОПЛЕННОМУ за окно, а не по
    # одной покупке, — иначе потеряем тех, кто набирает позицию частями.
    signal_min_usdc: float = 2000.0
    # Дешёвый предфильтр входа в скоринг. Заметно ниже min_trade_usdc: иначе
    # не увидим тех, кто набирает позицию частями. Замер на живых данных:
    # при $200 это ~0.7 сделок/с и ~4 запроса/с к SQLite — приемлемо.
    scoring_min_trade_usdc: float = 200.0

    # История кошелька из Data API вместо ожидания локальной (см.
    # wallet_history.py). Запрашивается только для сделок-кандидатов.
    wallet_history_enabled: bool = True
    wallet_history_ttl_seconds: float = 6 * 3600
    wallet_history_concurrency: int = 3

    # Сигнал жизни: без него смерть трекера замечается через сутки (см.
    # heartbeat.py). Порог простоя — 20 минут, это четыре пропущенные
    # пятиминутные пачки Data API подряд.
    # Подтверждение погоней (см. confirmation.py). Пороги взяты по замеру:
    # при погоне свыше +15% и деньгах последователей от $2000 перевес
    # составил +35.7 пп на 88 закрытых исходах.
    # Быстрая полоса из блокчейна (см. fast_lane.py). Голова цепочки
    # отстаёт на ~1 с против 250-380 с у Data API, а в группе "рынок
    # побежал" за первые пять минут уходит четверть движения.
    onchain_enabled: bool = False       # включается наличием ALCHEMY_WSS_URL
    alchemy_wss_url: str = ""
    alchemy_http_url: str = ""
    etherscan_api_key: str = ""
    onchain_min_usdc: float = 5000.0
    onchain_min_impact: float = 0.20    # тот же порог, что у price_impact

    chase_enabled: bool = True
    chase_window_minutes: float = 20.0
    chase_min_ratio: float = 0.15
    chase_min_money_usdc: float = 2000.0
    # Насколько глубоко в прошлое разбирать неотмеченных кандидатов после
    # простоя. Дальше смысла нет: рынок уже ушёл.
    chase_max_age_minutes: float = 180.0
    # Сколько минут считается "свежим": более старых кандидатов разбираем
    # молча. Иначе перезапуск вываливает всю накопленную очередь разом.
    chase_fresh_minutes: float = 30.0
    # Жёсткий предел отправок в час. Страховка от ошибки калибровки: порог,
    # обещавший 14 сигналов в сутки, на живом потоке дал около 580.
    chase_max_per_hour: int = 3

    heartbeat_enabled: bool = True
    heartbeat_interval_hours: float = 24.0
    stall_alert_minutes: float = 20.0
    # Окно, в котором покупки одного кошелька по одному исходу считаются
    # набором одной позиции. 30 минут — компромисс между дроблением ордера
    # и склейкой независимых заходов.
    accumulation_window_seconds: int = 1800

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
        allowed = _str("ALLOWED_TAGS", "").lower()
        allowed_set = {c.strip() for c in allowed.split(",") if c.strip()}

        cfg = cls(
            telegram_bot_token=_str("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=_str("TELEGRAM_CHAT_ID", ""),
            data_api_poll_interval=_float("DATA_API_POLL_INTERVAL", 3.0),
            data_api_batch_limit=_int("DATA_API_BATCH_LIMIT", 200),
            data_api_max_pages=_int("DATA_API_MAX_PAGES", 6),
            min_trade_usdc=_float("MIN_TRADE_USDC", 500.0),
            max_trade_price=_float("MAX_TRADE_PRICE", 0.95),
            max_market_volume_24h=_float("MAX_MARKET_VOLUME_24H", 100_000.0),
            new_wallet_max_trades=_int("NEW_WALLET_MAX_TRADES", 20),
            new_wallet_max_age_days=_int("NEW_WALLET_MAX_AGE_DAYS", 30),
            cluster_min_wallets=_int("CLUSTER_MIN_WALLETS", 2),
            cluster_min_participant_usdc=_float("CLUSTER_MIN_PARTICIPANT_USDC", 500.0),
            cluster_window_seconds=_int("CLUSTER_WINDOW_SECONDS", 3600),
            ignored_categories=ignored_set,
            allowed_tags=allowed_set,
            scoring_enabled=_bool("SCORING_ENABLED", True),
            score_threshold=_float("SCORE_THRESHOLD", 50.0),
            score_threshold_ratio=_float("SCORE_THRESHOLD_RATIO", 0.5),
            signal_min_usdc=_float("SIGNAL_MIN_USDC", 2000.0),
            scoring_min_trade_usdc=_float("SCORING_MIN_TRADE_USDC", 200.0),
            wallet_history_enabled=_bool("WALLET_HISTORY_ENABLED", True),
            wallet_history_ttl_seconds=_float("WALLET_HISTORY_TTL_SECONDS", 6 * 3600),
            wallet_history_concurrency=_int("WALLET_HISTORY_CONCURRENCY", 3),
            alchemy_wss_url=_str("ALCHEMY_WSS_URL", ""),
            alchemy_http_url=_str("ALCHEMY_HTTP_URL", ""),
            etherscan_api_key=_str("ETHERSCAN_API_KEY", ""),
            onchain_enabled=_bool("ONCHAIN_ENABLED", True),
            onchain_min_usdc=_float("ONCHAIN_MIN_USDC", 5000.0),
            onchain_min_impact=_float("ONCHAIN_MIN_IMPACT", 0.20),
            chase_enabled=_bool("CHASE_ENABLED", True),
            chase_window_minutes=_float("CHASE_WINDOW_MINUTES", 20.0),
            chase_min_ratio=_float("CHASE_MIN_RATIO", 0.15),
            chase_min_money_usdc=_float("CHASE_MIN_MONEY_USDC", 2000.0),
            chase_max_age_minutes=_float("CHASE_MAX_AGE_MINUTES", 180.0),
            chase_fresh_minutes=_float("CHASE_FRESH_MINUTES", 30.0),
            chase_max_per_hour=_int("CHASE_MAX_PER_HOUR", 3),
            heartbeat_enabled=_bool("HEARTBEAT_ENABLED", True),
            heartbeat_interval_hours=_float("HEARTBEAT_INTERVAL_HOURS", 24.0),
            stall_alert_minutes=_float("STALL_ALERT_MINUTES", 20.0),
            accumulation_window_seconds=_int("ACCUMULATION_WINDOW_SECONDS", 1800),
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
        if self.accumulation_window_seconds < 60:
            errors.append("ACCUMULATION_WINDOW_SECONDS < 60 — окно накопления слишком узкое")
        return errors
