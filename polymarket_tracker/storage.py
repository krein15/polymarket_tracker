"""SQLite хранилище: известные кошельки, сделки, сигналы, чекпоинт.

ВНИМАНИЕ: с переходом на Data API схема trades изменилась.
PRIMARY KEY теперь (tx_hash, maker, token_id) вместо (tx_hash, log_index).
Старые БД от V1-листенера несовместимы — удалите tracker.db перед первым
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
    ) -> int:
        with self._conn() as c:
            cur = c.execute(
                """
                INSERT INTO signals
                (ts, signal_type, maker, token_id, market_slug, usdc_amount, price, reason, tx_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (ts, signal_type, maker.lower(), token_id, market_slug, usdc_amount, price, reason, tx_hash),
            )
            return cur.lastrowid or 0

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
