"""SQLite хранилище: известные кошельки, сделки, сигналы, чекпоинт.

ВНИМАНИЕ: с переходом на Data API схема trades изменилась.
PRIMARY KEY теперь (tx_hash, maker, token_id) вместо (tx_hash, log_index).
Старые БД от V1-листенера несовместимы — удалите data/tracker.db перед первым
запуском новой версии.
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets (
    address TEXT PRIMARY KEY,
    first_seen_ts INTEGER NOT NULL,
    last_seen_ts INTEGER NOT NULL,
    trade_count INTEGER NOT NULL DEFAULT 0,
    total_volume_usdc REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS trades (
    tx_hash TEXT NOT NULL,
    log_index INTEGER NOT NULL DEFAULT 0,
    ts INTEGER NOT NULL,
    block_number INTEGER NOT NULL DEFAULT 0,
    maker TEXT NOT NULL,
    token_id TEXT NOT NULL,
    side TEXT NOT NULL,
    usdc_amount REAL NOT NULL,
    price REAL NOT NULL,
    PRIMARY KEY (tx_hash, maker, token_id)
);
CREATE INDEX IF NOT EXISTS idx_trades_maker_ts ON trades(maker, ts);
CREATE INDEX IF NOT EXISTS idx_trades_token_ts ON trades(token_id, ts);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    signal_type TEXT NOT NULL,
    maker TEXT NOT NULL,
    token_id TEXT NOT NULL,
    market_slug TEXT,
    usdc_amount REAL NOT NULL,
    price REAL NOT NULL,
    reason TEXT,
    tx_hash TEXT,
    telegram_msg_id INTEGER,
    user_feedback TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);

CREATE TABLE IF NOT EXISTS checkpoint (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


SCHEMA_OUTCOMES = """
CREATE TABLE IF NOT EXISTS signal_outcomes (
    signal_id INTEGER PRIMARY KEY,
    price_1h REAL,
    price_24h REAL,
    price_7d REAL,
    max_price_reached REAL,
    min_price_reached REAL,
    market_resolved INTEGER NOT NULL DEFAULT 0,
    settled_price REAL,
    trader_was_right INTEGER,
    roi_if_followed REAL,
    hours_to_resolve REAL,
    last_checked_ts INTEGER,
    created_ts INTEGER NOT NULL,
    FOREIGN KEY (signal_id) REFERENCES signals(id)
);
CREATE INDEX IF NOT EXISTS idx_outcomes_check_queue
    ON signal_outcomes(market_resolved, last_checked_ts);
"""


# Shadow tracker (пункт 0.3): отдельная таблица для ВСЕХ buy >= MIN_TRADE_USDC
# на рынках ниже широкого порога ликвидности, без боевых фильтров. Объединяет
# данные сделки и поля резолва в одной таблице (в отличие от signals +
# signal_outcomes) — shadow рождается сразу с outcome-полями, болванки не нужны.
SCHEMA_SHADOW = """
CREATE TABLE IF NOT EXISTS shadow_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tx_hash TEXT NOT NULL,
    maker TEXT NOT NULL,
    token_id TEXT NOT NULL,
    ts INTEGER NOT NULL,
    side TEXT NOT NULL,
    usdc_amount REAL NOT NULL,
    price REAL NOT NULL,
    market_slug TEXT,
    category TEXT,
    volume_24h REAL,
    -- решение боевого детектора по этой сделке (для анализа false negatives):
    -- passed_filters = 1, если сделка породила сигнал Ветки A
    -- (suspicious_entry / cluster); 0 — если была бы отброшена.
    passed_filters INTEGER NOT NULL DEFAULT 0,
    signal_types TEXT,
    -- поля резолва (мирроринг signal_outcomes)
    price_1h REAL,
    price_24h REAL,
    price_7d REAL,
    max_price_reached REAL,
    min_price_reached REAL,
    market_resolved INTEGER NOT NULL DEFAULT 0,
    settled_price REAL,
    trader_was_right INTEGER,
    roi_if_followed REAL,
    hours_to_resolve REAL,
    last_checked_ts INTEGER,
    created_ts INTEGER NOT NULL,
    UNIQUE (tx_hash, maker, token_id)
);
CREATE INDEX IF NOT EXISTS idx_shadow_check_queue
    ON shadow_trades(market_resolved, last_checked_ts);
CREATE INDEX IF NOT EXISTS idx_shadow_ts ON shadow_trades(ts);
"""


@dataclass
class WalletStats:
    address: str
    first_seen_ts: int
    last_seen_ts: int
    trade_count: int
    total_volume_usdc: float

    def age_days(self, now: Optional[int] = None) -> float:
        now = now or int(time.time())
        return (now - self.first_seen_ts) / 86400.0


class Storage:
    """Потокобезопасный SQLite wrapper (writes сериализуются через lock)."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)
            c.executescript(SCHEMA_OUTCOMES)
            c.executescript(SCHEMA_SHADOW)
            # Идемпотентная миграция: добавить signals.side, если ещё нет.
            # Все старые сигналы — это buy (другая сторона ранее не реализовывалась).
            try:
                c.execute("ALTER TABLE signals ADD COLUMN side TEXT DEFAULT 'buy'")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ───────── Wallets ─────────

    def get_wallet(self, address: str) -> Optional[WalletStats]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM wallets WHERE address = ?", (address.lower(),)
            ).fetchone()
            if not row:
                return None
            return WalletStats(
                address=row["address"],
                first_seen_ts=row["first_seen_ts"],
                last_seen_ts=row["last_seen_ts"],
                trade_count=row["trade_count"],
                total_volume_usdc=row["total_volume_usdc"],
            )

    def upsert_wallet_trade(self, address: str, ts: int, usdc: float) -> WalletStats:
        """Атомарное обновление: UPSERT + инкремент."""
        address = address.lower()
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO wallets (address, first_seen_ts, last_seen_ts, trade_count, total_volume_usdc)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(address) DO UPDATE SET
                    last_seen_ts = excluded.last_seen_ts,
                    trade_count = trade_count + 1,
                    total_volume_usdc = total_volume_usdc + excluded.total_volume_usdc
                """,
                (address, ts, ts, usdc),
            )
            row = c.execute(
                "SELECT * FROM wallets WHERE address = ?", (address,)
            ).fetchone()
        return WalletStats(
            address=row["address"],
            first_seen_ts=row["first_seen_ts"],
            last_seen_ts=row["last_seen_ts"],
            trade_count=row["trade_count"],
            total_volume_usdc=row["total_volume_usdc"],
        )

    # ───────── Trades ─────────

    def save_trade(
        self,
        tx_hash: str,
        log_index: int,
        ts: int,
        block_number: int,
        maker: str,
        token_id: str,
        side: str,
        usdc_amount: float,
        price: float,
    ) -> bool:
        """Возвращает True если сохранено (не дубликат).

        log_index и block_number сохраняем для совместимости со схемой;
        при работе через Data API оба = 0.
        """
        with self._conn() as c:
            try:
                c.execute(
                    """
                    INSERT INTO trades
                    (tx_hash, log_index, ts, block_number, maker, token_id, side, usdc_amount, price)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (tx_hash, log_index, ts, block_number, maker.lower(), token_id, side, usdc_amount, price),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def count_recent_new_wallets_for_token(
        self, token_id: str, since_ts: int, max_trades: int
    ) -> int:
        """Сколько уникальных кошельков с trade_count <= max_trades торговали
        этот токен после since_ts. Используется для кластерного сигнала."""
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT DISTINCT t.maker
                FROM trades t
                JOIN wallets w ON w.address = t.maker
                WHERE t.token_id = ? AND t.ts >= ? AND w.trade_count <= ?
                """,
                (token_id, since_ts, max_trades),
            ).fetchall()
            return len(rows)

    # ───────── Signals ─────────

    def save_signal(
        self,
        ts: int,
        signal_type: str,
        maker: str,
        token_id: str,
        market_slug: Optional[str],
        usdc_amount: float,
        price: float,
        reason: str,
        tx_hash: Optional[str] = None,
        side: str = "buy",
    ) -> int:
        with self._conn() as c:
            cur = c.execute(
                """
                INSERT INTO signals
                (ts, signal_type, maker, token_id, market_slug,
                 usdc_amount, price, reason, tx_hash, side)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (ts, signal_type, maker.lower(), token_id, market_slug,
                 usdc_amount, price, reason, tx_hash, side),
            )
            return cur.lastrowid or 0

    def init_outcome_record(self, signal_id: int, now_ts: int) -> None:
        """Создать пустую запись для трекинга исхода. Идемпотентно."""
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO signal_outcomes (signal_id, created_ts)
                VALUES (?, ?)
                ON CONFLICT(signal_id) DO NOTHING
                """,
                (signal_id, now_ts),
            )

    def update_signal_telegram(self, signal_id: int, msg_id: int) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE signals SET telegram_msg_id = ? WHERE id = ?",
                (msg_id, signal_id),
            )

    # ───────── Checkpoint ─────────

    def get_checkpoint(self, key: str) -> Optional[str]:
        with self._conn() as c:
            row = c.execute(
                "SELECT value FROM checkpoint WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else None

    def set_checkpoint(self, key: str, value: str) -> None:
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO checkpoint (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    # ───────── Outcomes ─────────

    def backfill_outcome_records(self) -> int:
        """Создать болванки signal_outcomes для всех старых signals без записи.

        Идемпотентно: при повторных вызовах ничего не делает.
        Возвращает количество новосозданных записей.
        """
        with self._conn() as c:
            cur = c.execute(
                """
                INSERT INTO signal_outcomes (signal_id, created_ts)
                SELECT s.id, s.ts
                FROM signals s
                LEFT JOIN signal_outcomes o ON o.signal_id = s.id
                WHERE o.signal_id IS NULL
                """
            )
            return cur.rowcount or 0

    def get_outcomes_to_update(self, limit: int = 100) -> list[dict]:
        """Получить незакрытые исходы для проверки.

        Возвращает список dict-ов с полями нужными outcome_tracker'у:
        signal_id, token_id, side, price_at_signal, signal_ts, created_ts,
        last_checked_ts, has_price_1h, has_price_24h, has_price_7d.

        Сортировка: дольше всех не проверявшиеся первыми (NULL last_checked_ts
        — самые приоритетные).
        """
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT
                    o.signal_id,
                    s.token_id,
                    s.side,
                    s.price AS price_at_signal,
                    s.ts AS signal_ts,
                    o.created_ts,
                    o.last_checked_ts,
                    (o.price_1h IS NOT NULL) AS has_price_1h,
                    (o.price_24h IS NOT NULL) AS has_price_24h,
                    (o.price_7d IS NOT NULL) AS has_price_7d
                FROM signal_outcomes o
                JOIN signals s ON s.id = o.signal_id
                WHERE o.market_resolved = 0
                ORDER BY
                    CASE WHEN o.last_checked_ts IS NULL THEN 0 ELSE 1 END,
                    o.last_checked_ts ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def update_outcome_snapshots(
        self,
        signal_id: int,
        now_ts: int,
        current_price: Optional[float],
        set_price_1h: bool = False,
        set_price_24h: bool = False,
        set_price_7d: bool = False,
    ) -> None:
        """Обновить snapshot-поля для незакрытого исхода.

        Если current_price передана — обновляет min/max и заполняет нужные
        price_Xh поля (если set_price_Xh=True). last_checked_ts обновляется
        всегда — даже если current_price=None (значит, мы пытались, но Gamma
        промахнулась).
        """
        with self._conn() as c:
            if current_price is None:
                c.execute(
                    "UPDATE signal_outcomes SET last_checked_ts = ? WHERE signal_id = ?",
                    (now_ts, signal_id),
                )
                return

            sets = ["last_checked_ts = ?"]
            params: list = [now_ts]

            # min/max обновляем атомарно через COALESCE
            sets.append(
                "max_price_reached = CASE "
                "WHEN max_price_reached IS NULL OR ? > max_price_reached THEN ? "
                "ELSE max_price_reached END"
            )
            params.extend([current_price, current_price])
            sets.append(
                "min_price_reached = CASE "
                "WHEN min_price_reached IS NULL OR ? < min_price_reached THEN ? "
                "ELSE min_price_reached END"
            )
            params.extend([current_price, current_price])

            if set_price_1h:
                sets.append("price_1h = COALESCE(price_1h, ?)")
                params.append(current_price)
            if set_price_24h:
                sets.append("price_24h = COALESCE(price_24h, ?)")
                params.append(current_price)
            if set_price_7d:
                sets.append("price_7d = COALESCE(price_7d, ?)")
                params.append(current_price)

            params.append(signal_id)
            c.execute(
                f"UPDATE signal_outcomes SET {', '.join(sets)} WHERE signal_id = ?",
                params,
            )

    def finalize_outcome(
        self,
        signal_id: int,
        settled_price: float,
        trader_was_right: bool,
        roi_if_followed: float,
        hours_to_resolve: float,
        now_ts: int,
    ) -> None:
        """Зафиксировать рынок как зарезолвленный."""
        with self._conn() as c:
            c.execute(
                """
                UPDATE signal_outcomes SET
                    market_resolved = 1,
                    settled_price = ?,
                    trader_was_right = ?,
                    roi_if_followed = ?,
                    hours_to_resolve = ?,
                    last_checked_ts = ?
                WHERE signal_id = ?
                """,
                (
                    settled_price,
                    1 if trader_was_right else 0,
                    roi_if_followed,
                    hours_to_resolve,
                    now_ts,
                    signal_id,
                ),
            )

    # ───────── Maintenance (пункт 0.2 TODO) ─────────

    def count_trades(self) -> int:
        """Сколько строк в trades — для отчётности обслуживания."""
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM trades").fetchone()[0]

    def prune_old_trades(
        self, older_than_days: int = 7, now: Optional[int] = None
    ) -> int:
        """Удалить строки trades старше older_than_days дней.

        Таблица trades нужна только для cluster-детекции и подсчёта свежих
        кошельков на токене — оба смотрят максимум на последний час
        (cluster_window_seconds). Историю можно безопасно удалять:

          * агрегаты в wallets (trade_count, first_seen_ts, total_volume_usdc)
            хранятся отдельно и НЕ пересчитываются из trades;
          * signals / signal_outcomes таблицу trades не читают.

        VACUUM здесь НЕ вызывается — место на диске вернёт отдельный vacuum()
        (его нельзя запускать внутри транзакции). Возвращает число удалённых
        строк.
        """
        now = now if now is not None else int(time.time())
        cutoff = now - older_than_days * 86400
        with self._conn() as c:
            cur = c.execute("DELETE FROM trades WHERE ts < ?", (cutoff,))
            return cur.rowcount or 0

    def vacuum(self) -> None:
        """Дефрагментировать БД и вернуть свободные страницы ОС.

        VACUUM не может выполняться внутри транзакции, поэтому открываем
        отдельное соединение в autocommit-режиме (isolation_level=None).
        Требует эксклюзивного доступа — запускать при ОСТАНОВЛЕННОМ трекере,
        иначе sqlite3 кинет 'database is locked'.
        """
        conn = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        try:
            conn.execute("VACUUM")
        finally:
            conn.close()

    # ───────── Shadow tracker (пункт 0.3) ─────────

    def save_shadow_trade(
        self,
        tx_hash: str,
        maker: str,
        token_id: str,
        ts: int,
        side: str,
        usdc_amount: float,
        price: float,
        market_slug: Optional[str],
        category: Optional[str],
        volume_24h: Optional[float],
        passed_filters: bool,
        signal_types: Optional[str],
        now_ts: int,
    ) -> bool:
        """Записать сделку в shadow_trades. Возвращает True если новая (не дубль).

        passed_filters — породила ли сделка сигнал Ветки A (suspicious_entry /
        cluster). signal_types — comma-joined список типов сигналов или None.
        volume_24h может быть 0/None для рынков, по которым Gamma промахнулась.
        """
        with self._conn() as c:
            try:
                c.execute(
                    """
                    INSERT INTO shadow_trades
                    (tx_hash, maker, token_id, ts, side, usdc_amount, price,
                     market_slug, category, volume_24h, passed_filters,
                     signal_types, created_ts)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tx_hash, maker.lower(), token_id, ts, side, usdc_amount,
                        price, market_slug, category, volume_24h,
                        1 if passed_filters else 0, signal_types, now_ts,
                    ),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def get_shadow_to_update(self, limit: int = 100) -> list[dict]:
        """Незакрытые shadow-сделки для проверки резолва.

        Ключи dict-ов совместимы с get_outcomes_to_update (id назван
        shadow_id, цена входа — price_at_signal, ts сделки — signal_ts),
        чтобы outcome_tracker мог переиспользовать общий обработчик батча.
        Сортировка: дольше всех не проверявшиеся первыми.
        """
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT
                    id AS shadow_id,
                    token_id,
                    side,
                    price AS price_at_signal,
                    ts AS signal_ts,
                    last_checked_ts,
                    (price_1h IS NOT NULL) AS has_price_1h,
                    (price_24h IS NOT NULL) AS has_price_24h,
                    (price_7d IS NOT NULL) AS has_price_7d
                FROM shadow_trades
                WHERE market_resolved = 0
                ORDER BY
                    CASE WHEN last_checked_ts IS NULL THEN 0 ELSE 1 END,
                    last_checked_ts ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def update_shadow_snapshots(
        self,
        shadow_id: int,
        now_ts: int,
        current_price: Optional[float],
        set_price_1h: bool = False,
        set_price_24h: bool = False,
        set_price_7d: bool = False,
    ) -> None:
        """Обновить snapshot-поля незакрытой shadow-сделки.

        Мирроринг update_outcome_snapshots для таблицы shadow_trades.
        last_checked_ts обновляется всегда, даже при current_price=None.
        """
        with self._conn() as c:
            if current_price is None:
                c.execute(
                    "UPDATE shadow_trades SET last_checked_ts = ? WHERE id = ?",
                    (now_ts, shadow_id),
                )
                return

            sets = ["last_checked_ts = ?"]
            params: list = [now_ts]

            sets.append(
                "max_price_reached = CASE "
                "WHEN max_price_reached IS NULL OR ? > max_price_reached THEN ? "
                "ELSE max_price_reached END"
            )
            params.extend([current_price, current_price])
            sets.append(
                "min_price_reached = CASE "
                "WHEN min_price_reached IS NULL OR ? < min_price_reached THEN ? "
                "ELSE min_price_reached END"
            )
            params.extend([current_price, current_price])

            if set_price_1h:
                sets.append("price_1h = COALESCE(price_1h, ?)")
                params.append(current_price)
            if set_price_24h:
                sets.append("price_24h = COALESCE(price_24h, ?)")
                params.append(current_price)
            if set_price_7d:
                sets.append("price_7d = COALESCE(price_7d, ?)")
                params.append(current_price)

            params.append(shadow_id)
            c.execute(
                f"UPDATE shadow_trades SET {', '.join(sets)} WHERE id = ?",
                params,
            )

    def finalize_shadow_outcome(
        self,
        shadow_id: int,
        settled_price: float,
        trader_was_right: bool,
        roi_if_followed: float,
        hours_to_resolve: float,
        now_ts: int,
    ) -> None:
        """Зафиксировать shadow-сделку как зарезолвленную.

        Мирроринг finalize_outcome для таблицы shadow_trades.
        """
        with self._conn() as c:
            c.execute(
                """
                UPDATE shadow_trades SET
                    market_resolved = 1,
                    settled_price = ?,
                    trader_was_right = ?,
                    roi_if_followed = ?,
                    hours_to_resolve = ?,
                    last_checked_ts = ?
                WHERE id = ?
                """,
                (
                    settled_price,
                    1 if trader_was_right else 0,
                    roi_if_followed,
                    hours_to_resolve,
                    now_ts,
                    shadow_id,
                ),
            )

    def count_shadow_trades(self) -> int:
        """Сколько строк в shadow_trades — для статистики/отчётности."""
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM shadow_trades").fetchone()[0]
