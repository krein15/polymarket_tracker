"""SQLite хранилище: известные кошельки, сделки, сигналы, чекпоинт.

ВНИМАНИЕ: с переходом на Data API схема trades изменилась.
PRIMARY KEY теперь (tx_hash, maker, token_id) вместо (tx_hash, log_index).
Старые БД от V1-листенера несовместимы — удалите data/tracker.db перед первым
запуском новой версии.
"""
from __future__ import annotations

import sqlite3
import threading
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
    -- Рынок целиком. token_id — это ОДИН исход (YES либо NO), а хедж
    -- распознаётся только по общему conditionId обоих исходов.
    condition_id TEXT,
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
-- Отбой ищет по сделке, был ли по ней уже отправлен сигнал.
CREATE INDEX IF NOT EXISTS idx_signals_tx ON signals(tx_hash);

-- Цена, по которой мы РЕАЛЬНО могли бы войти по сигналу.
--
-- Отдельная таблица, а не колонки в signals: замер добавился поздно, у
-- старых сигналов его нет и не будет, и путать "не мерили" с "не налилось"
-- нельзя. Плюс signals остаётся нетронутой.
CREATE TABLE IF NOT EXISTS signal_entries (
    signal_id INTEGER PRIMARY KEY,
    measured_ts INTEGER NOT NULL,
    delay_sec INTEGER NOT NULL,
    best_ask REAL,
    fill_500 REAL,
    fill_2000 REAL,
    fill_5000 REAL,
    depth_usdc REAL
);

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


# Снимки цены САМОГО РЫНКА, без привязки к чьим-либо сделкам.
#
# Зачем отдельная таблица. Всё, что мы мерили до сих пор, — это попытка
# повторить за инсайдером, и выборка везде условна на том, что кто-то
# совершил сделку. Но у предсказательных рынков есть давно известное
# смещение: фавориты недооценены, аутсайдеры переоценены. На 38 669
# теневых покупок оно видно:
#
#     цена 0.10-0.20   винрейт  8.4%   перевес -6.7 пп   ROI -44.4%
#     цена 0.60-0.70   винрейт 68.3%   перевес +3.9 пп   ROI  +5.9%
#
# Возражение к этим числам одно и то же: это сделки людей, а не цены.
# Информированные покупатели кучкуются там, где у них перевес, и часть
# +5.9% может быть их правотой, а не ошибкой рынка. Здесь отбора нет
# вообще: берём рынки из списка Gamma подряд и записываем цену, торговал
# там кто-нибудь или нет.
#
# Оба исхода рынка пишутся отдельными строками. Иначе вышел бы перекос:
# у вопросов "случится ли X?" сторона Yes почти всегда дешёвая, и
# выборка только по Yes была бы выборкой аутсайдеров.
SCHEMA_PRICE_SAMPLES = """
CREATE TABLE IF NOT EXISTS price_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    token_id TEXT NOT NULL,
    condition_id TEXT,
    market_slug TEXT,
    outcome TEXT,
    -- mid — честная оценка "во что рынок оценивает исход";
    -- best_ask и fill_2000 — то, что мы РЕАЛЬНО заплатили бы.
    -- Разница между ними и есть ответ на вопрос "съест ли спред перевес".
    mid REAL,
    best_bid REAL,
    best_ask REAL,
    fill_2000 REAL,
    depth_usdc REAL,
    volume_24h REAL,
    liquidity REAL,
    end_date_ts INTEGER,
    category TEXT,
    -- поля резолва: та же форма, что у shadow_trades, ради общего
    -- обработчика в outcome_tracker
    price_1h REAL,
    price_24h REAL,
    price_7d REAL,
    max_price_reached REAL,
    min_price_reached REAL,
    market_resolved INTEGER NOT NULL DEFAULT 0,
    settled_price REAL,
    won INTEGER,
    roi_at_entry REAL,
    hours_to_resolve REAL,
    last_checked_ts INTEGER,
    created_ts INTEGER NOT NULL,
    UNIQUE (token_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_price_samples_queue
    ON price_samples(market_resolved, last_checked_ts);
CREATE INDEX IF NOT EXISTS idx_price_samples_token
    ON price_samples(token_id, ts);
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


# Отложенный коммит: фиксируем накопленное раз в COMMIT_EVERY записей или
# раз в COMMIT_INTERVAL секунд — что наступит раньше. Замер в
# docs/PERFORMANCE.md: коммит на каждую операцию стоил 82 КБ записи на диск
# на одну сделку в 300 байт (~190 ГБ в сутки), пачками — 2 КБ.
#
# Размер пачки поднят 11.09.2026 после замера на синтетике (40 000 вставок,
# три индекса, WAL + synchronous=NORMAL):
#
#     коммит раз в   50    23.8 с
#     коммит раз в 1000     6.8 с
#
# При живом потоке в ~14 сделок в секунду прежний интервал в 2 секунды
# срабатывал раньше счётчика, то есть частоту коммитов задавал именно он.
# Подняты оба, иначе смысла нет.
#
# Чем платим: при жёстком падении теряется до COMMIT_INTERVAL секунд сделок.
# Это безопасно по устройству — чекпоинт лежит в той же транзакции, что и
# сами сделки, поэтому назад откатываются оба сразу, и догрузка после
# перезапуска просто перечитает этот кусок из Data API.
# Сколько строк удаляем за одну транзакцию при чистке истории.
# Одним запросом 5.9 млн строк не проходят: журнал вырастает до гигабайтов,
# и прерванная работа откатывается целиком.
# Кеш страниц SQLite, МБ. Держать его надо согласованно с размером пачки
# коммитов: за COMMIT_INTERVAL секунд набирается столько изменённых страниц,
# сколько должно поместиться сюда целиком.
CACHE_MB = 64

PRUNE_CHUNK = 50_000

COMMIT_EVERY = 500
COMMIT_INTERVAL_SEC = 10.0


# Насколько поздно ещё можно взять снимок цены "через час".
#
# Снимок, снятый через десять дней, называется price_1h, но числом является
# совсем другим — и молча портит любой замер дрейфа. Поэтому просроченные
# сверх этого срока не берём вовсе: честный пропуск лучше тихой подмены.
# Допуск равен самому окну (для часа — до двух часов), это компромисс между
# точностью и объёмом собранных данных.
SNAPSHOT_GRACE_FACTOR = 2

_SHADOW_FIELDS = """
    id AS shadow_id,
    token_id,
    side,
    price AS price_at_signal,
    ts AS signal_ts,
    last_checked_ts,
    (price_1h IS NOT NULL) AS has_price_1h,
    (price_24h IS NOT NULL) AS has_price_24h,
    (price_7d IS NOT NULL) AS has_price_7d
"""

# Поля для общего обработчика исходов. Имена те же, что у теневой
# выборки: outcome_tracker зовёт обе таблицы одним кодом.
#
# price_at_signal — середина, а если её нет, то аск. Односторонний
# стакан у крайних рынков не редкость: на аутсайдера по 0.001 покупателей
# нет вовсе, есть только продавцы. Середины там не существует, но купить
# его можно — значит наблюдение есть, и терять его нельзя: это самый
# хвост шкалы, ради которого замер и затевался.
#
# ROI по цене исполнения ($2000 с обходом стакана) считается в анализе из
# fill_2000: смещение рынка и стоимость входа — два разных вопроса, и
# смешивать их в одном числе нельзя.
_PRICE_SAMPLE_FIELDS = """
    id AS sample_id,
    token_id,
    'buy' AS side,
    COALESCE(mid, best_ask) AS price_at_signal,
    ts AS signal_ts,
    last_checked_ts,
    (price_1h IS NOT NULL) AS has_price_1h,
    (price_24h IS NOT NULL) AS has_price_24h,
    (price_7d IS NOT NULL) AS has_price_7d
"""



class Storage:
    """SQLite-хранилище: одно долгоживущее соединение, отложенный коммит.

    Раньше каждая операция открывала своё соединение и коммитила отдельно —
    три транзакции на сделку, 46 IO-операций, ~190 ГБ записи в сутки. Теперь
    соединение одно, а коммит откладывается до COMMIT_EVERY записей или
    COMMIT_INTERVAL_SEC секунд.

    Чем платим: при аварийном завершении теряется последняя незакоммиченная
    пачка — секунды данных. Для трекера это безопасно: чекпоинт лежит в той же
    транзакции, поэтому после перезапуска листенер перечитает те же сделки, а
    save_trade отсеет их как дубликаты. По той же причине откат при ошибке
    (rollback) теряет всю пачку целиком, а не одну запись, — данные вернутся
    со следующим проходом листенера.

    Доступ сериализуется блокировкой: соединение общее для всех задач
    asyncio-цикла. Режим WAL позволяет читателям из ДРУГИХ процессов
    (tools/stats.py и прочие) работать, пока трекер пишет.
    """

    def __init__(
        self,
        db_path: str,
        commit_every: int = COMMIT_EVERY,
        commit_interval: float = COMMIT_INTERVAL_SEC,
        cache_mb: int = CACHE_MB,
    ):
        self.db_path = db_path
        self._commit_every = commit_every
        self._commit_interval = commit_interval
        self._cache_mb = max(1, int(cache_mb))
        self._lock = threading.RLock()
        self._connection: Optional[sqlite3.Connection] = None
        self._pending = 0
        self._last_commit = time.monotonic()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)
            c.executescript(SCHEMA_OUTCOMES)
            c.executescript(SCHEMA_SHADOW)
            c.executescript(SCHEMA_PRICE_SAMPLES)
            # Идемпотентная миграция: добавить signals.side, если ещё нет.
            # Все старые сигналы — это buy (другая сторона ранее не реализовывалась).
            for ddl in (
                "ALTER TABLE signals ADD COLUMN side TEXT DEFAULT 'buy'",
                # Скоринг: балл и его разбивка по признакам. Разбивка нужна,
                # чтобы потом измерить, какой признак несёт alpha, — ради этого
                # скоринг и вводился.
                "ALTER TABLE signals ADD COLUMN score REAL",
                "ALTER TABLE signals ADD COLUMN score_parts TEXT",
                # В теневой выборке балл считаем для ВСЕХ подходящих сделок,
                # а не только для сигнальных: без этого не подобрать порог.
                "ALTER TABLE shadow_trades ADD COLUMN score REAL",
                "ALTER TABLE shadow_trades ADD COLUMN score_parts TEXT",
                # Хедж (покупка обоих исходов) без conditionId не ловится.
                "ALTER TABLE trades ADD COLUMN condition_id TEXT",
                # Подтверждение погоней: насколько последователи переплатили
                # относительно кандидата и сколько денег занесли.
                "ALTER TABLE shadow_trades ADD COLUMN chase REAL",
                "ALTER TABLE shadow_trades ADD COLUMN chase_money REAL",
                "ALTER TABLE shadow_trades ADD COLUMN chase_checked_ts INTEGER",
            ):
                try:
                    c.execute(ddl)
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e).lower():
                        raise
            # Индексы по добавленным колонкам — строго ПОСЛЕ ALTER: на уже
            # существующей БД колонки в момент CREATE TABLE ещё нет, и
            # CREATE INDEX по ней падает с "no such column".
            # idx_trades_maker_cond удалён 11.09.2026. Он заводился под
            # проверку хеджа, но замер на живой базе показал, что обычный
            # idx_trades_maker_ts справляется не хуже: 10.02 мс против
            # 10.58 мс на запрос (150 проверок x 6 раундов с чередованием
            # порядка, чтобы прогрев кеша не достался одному варианту).
            # При этом лишний индекс стоил половины времени вставки:
            # 23.4 с против 11.6 с на 40 000 строк.
            c.execute("DROP INDEX IF EXISTS idx_trades_maker_cond")
            # Индекс по времени сделки. Без него "какая последняя сделка"
            # (MAX(ts)) и "с какого дня история" (MIN(ts)) читали индекс
            # целиком: на базе в 8.7 ГБ — 28 и 22 секунды. MAX(ts) звали
            # подтверждение погони и сторож простоя, каждые две минуты;
            # MIN(ts) — скоринг прямо в цикле приёма сделок. Больше половины
            # времени диск был занят этими сканами, а синхронный SQLite на
            # это время останавливал весь трекер.
            #
            # На вставке индекс почти бесплатен — замерено: 2.0 с против
            # 2.1 с на 60 000 строк. Время растёт монотонно, поэтому новая
            # запись ложится в самый правый лист, а он всегда горячий. Этим он
            # и отличается от удалённого maker_cond с произвольным доступом.
            c.execute("CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts)")
        # Схема должна лечь на диск сразу, а не ждать пачку.
        self.flush()

    def _open(self) -> sqlite3.Connection:
        """Создать соединение и выставить режимы. Ленивая инициализация."""
        conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # WAL: читатель из другого процесса не блокируется писателем.
        # synchronous=NORMAL при WAL теряет максимум последнюю транзакцию при
        # отключении питания, но не рушит базу — размен, который для трекера
        # оправдан (см. docs/PERFORMANCE.md).
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        # Кеш страниц. Для чтений не нужен — их держит файловый кеш
        # системы, запросы и так укладываются в доли миллисекунды. Нужен
        # под длинную транзакцию: при пачке в 500 записей больший кеш
        # ускорил вставку 60 000 сделок с 6.7 с до 4.9 с, а при пачке в 50
        # не изменил ничего. Дальше 64 МБ выигрыш не растёт.
        #
        # На ОБЪЁМ записи кеш заметно не влияет: 46.5 КБ на сделку — тот же
        # порядок, что и без него. Держим ради процессорного времени, не
        # ради диска.
        conn.execute(f"PRAGMA cache_size=-{self._cache_mb * 1024}")
        return conn

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._connection is None:
                self._connection = self._open()
            conn = self._connection
            # total_changes — счётчик изменённых строк за всё время соединения.
            # По нему отличаем запись от чтения: in_transaction для этого не
            # годится, он остаётся истинным после первой же записи, и тогда
            # каждое последующее чтение накручивало бы пачку (а их в скоринге
            # по шесть на сделку).
            changes_before = conn.total_changes
            try:
                yield conn
            except Exception:
                # Откат отменяет всю незакоммиченную пачку — см. докстринг класса.
                conn.rollback()
                self._pending = 0
                self._last_commit = time.monotonic()
                raise
            if conn.total_changes != changes_before:
                self._pending += 1
                if (
                    self._pending >= self._commit_every
                    or time.monotonic() - self._last_commit >= self._commit_interval
                ):
                    self._commit_locked()

    def _commit_locked(self) -> None:
        """Коммит. Вызывать только под self._lock."""
        if self._connection is not None and self._connection.in_transaction:
            self._connection.commit()
        self._pending = 0
        self._last_commit = time.monotonic()

    def flush(self) -> None:
        """Зафиксировать отложенное немедленно.

        Вызывается при остановке трекера и перед операциями, которым нужен
        эксклюзивный доступ к файлу (vacuum).
        """
        with self._lock:
            self._commit_locked()

    def close(self) -> None:
        """Дописать пачку, ужать WAL и закрыть соединение.

        Про WAL. Файл журнала растёт до размера самой крупной пачки записей
        и обратно сам не сжимается: после догона суточного простоя он занял
        427 МБ и остался таким. Место не течёт — файл переиспользуется, — но
        427 МБ лежат мёртвым грузом, и каждая контрольная точка их обходит.
        TRUNCATE при остановке возвращает место; если чекпоинт не пройдёт
        (например, база занята), молча идём дальше — это уборка, а не
        обязанность.
        """
        with self._lock:
            if self._connection is not None:
                self._commit_locked()
                try:
                    self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.Error:
                    pass
                self._connection.close()
                self._connection = None

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
        condition_id: str = "",
    ) -> bool:
        """Возвращает True если сохранено (не дубликат).

        log_index и block_number сохраняем для совместимости со схемой;
        при работе через Data API оба = 0. condition_id нужен, чтобы видеть
        покупку обоих исходов одного рынка (хедж).
        """
        with self._conn() as c:
            try:
                c.execute(
                    """
                    INSERT INTO trades
                    (tx_hash, log_index, ts, block_number, maker, token_id,
                     condition_id, side, usdc_amount, price)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (tx_hash, log_index, ts, block_number, maker.lower(), token_id,
                     condition_id or None, side, usdc_amount, price),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def follower_flow(
        self, token_id: str, exclude_maker: str, since_ts: int, until_ts: int
    ) -> tuple:
        """Деньги и средневзвешенная цена ЧУЖИХ покупок после сделки.

        Возвращает (сумма USDC, VWAP). VWAP = 0.0, если следом никто не зашёл.
        Средневзвешенная, а не средняя: одна мелкая покупка по нелепой цене
        не должна перевешивать реальный поток.
        """
        with self._conn() as c:
            row = c.execute(
                "SELECT COALESCE(SUM(usdc_amount), 0) AS money, "
                "       COALESCE(SUM(usdc_amount * price), 0) AS vp "
                "FROM trades "
                "WHERE token_id = ? AND ts > ? AND ts <= ? AND side = 'buy' "
                "  AND maker <> ?",
                (token_id, since_ts, until_ts, exclude_maker.lower()),
            ).fetchone()
        money = float(row["money"] or 0.0)
        vwap = (float(row["vp"]) / money) if money > 0 else 0.0
        return money, vwap

    def candidates_awaiting_chase(
        self, oldest_ts: int, newest_ts: int, limit: int
    ) -> list:
        """Кандидаты, у которых окно наблюдения закрылось, а погоня не считана."""
        with self._conn() as c:
            return c.execute(
                "SELECT id, tx_hash, maker, token_id, ts, price, usdc_amount, market_slug "
                "FROM shadow_trades "
                "WHERE chase_checked_ts IS NULL AND side = 'buy' "
                "  AND ts >= ? AND ts <= ? "
                "ORDER BY ts LIMIT ?",
                (oldest_ts, newest_ts, limit),
            ).fetchall()

    def signals_awaiting_entry(
        self, ready_before: int, oldest: int, limit: int
    ) -> list:
        """Сигналы, у которых пора снять цену входа, а замера ещё нет.

        Отсчёт идёт от времени ОТПРАВКИ сообщения, а не от времени сделки.
        Разница принципиальная: у подтверждения по погоне в signals.ts лежит
        время сделки трейдера, которой к моменту отправки уже 20-30 минут.
        Пока отсчёт шёл по нему, нижняя граница ("не старше получаса")
        выбрасывала погоню почти целиком — замер получили 73 сигнала из 221.

        Время отправки берём из signal_outcomes.created_ts: запись заводится
        ровно в момент отправки. Если её почему-то нет, падаем на signals.ts.
        """
        with self._conn() as c:
            return c.execute(
                "SELECT s.id, COALESCE(o.created_ts, s.ts) AS ts, "
                "       s.token_id, s.price, s.signal_type, "
                "       s.market_slug, s.telegram_msg_id "
                "FROM signals s "
                "LEFT JOIN signal_entries e ON e.signal_id = s.id "
                "LEFT JOIN signal_outcomes o ON o.signal_id = s.id "
                "WHERE e.signal_id IS NULL "
                "  AND COALESCE(o.created_ts, s.ts) <= ? "
                "  AND COALESCE(o.created_ts, s.ts) >= ? "
                "ORDER BY COALESCE(o.created_ts, s.ts) LIMIT ?",
                (ready_before, oldest, limit),
            ).fetchall()

    def save_signal_entry(
        self,
        signal_id: int,
        measured_ts: int,
        delay_sec: int,
        best_ask: Optional[float],
        fill_500: Optional[float],
        fill_2000: Optional[float],
        fill_5000: Optional[float],
        depth_usdc: Optional[float],
    ) -> None:
        """Записать замер. Пустые цены — это тоже результат: значит стакан
        был слишком тонким, чтобы налить такой объём."""
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO signal_entries "
                "(signal_id, measured_ts, delay_sec, best_ask, "
                " fill_500, fill_2000, fill_5000, depth_usdc) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (signal_id, measured_ts, delay_sec, best_ask,
                 fill_500, fill_2000, fill_5000, depth_usdc),
            )

    def sent_signal_for_trade(self, tx_hash: str):
        """Сигнал, уже отправленный по этой сделке, или None.

        Нужен отбою: сообщать "рынок пошёл против" имеет смысл только по
        тем сделкам, о которых мы уже написали. Про остальные мы молчали —
        и молчать надо дальше, иначе отбой сам станет потоком сигналов.

        Ранние ончейн-сигналы и подтверждения сюда не попадают: первые
        приходят раньше, чем окно погони вообще закрылось, вторые — это
        уже вывод по той же самой погоне.
        """
        if not tx_hash:
            return None
        with self._conn() as c:
            return c.execute(
                "SELECT id, signal_type, ts, market_slug, price, usdc_amount, maker "
                "FROM signals WHERE tx_hash = ? "
                "  AND (signal_type = 'score' OR signal_type = 'whitelist') "
                "ORDER BY ts LIMIT 1",
                (tx_hash,),
            ).fetchone()

    def save_chase(
        self, shadow_id: int, chase, money: float, checked_ts: int
    ) -> None:
        """Записать результат проверки погони — в том числе отрицательный.

        Отметку ставим всегда, иначе кандидат будет перепроверяться вечно.
        """
        with self._conn() as c:
            c.execute(
                "UPDATE shadow_trades SET chase = ?, chase_money = ?, "
                "chase_checked_ts = ? WHERE id = ?",
                (chase, money, checked_ts, shadow_id),
            )

    def wallet_track_record(self, maker: str, min_resolved: int = 3) -> Optional[dict]:
        """Как этот кошелёк отработал по НАШИМ наблюдениям.

        Считаем по теневой выборке: она пишет все крупные покупки подряд,
        поэтому цифра не отобрана нашими же фильтрами. Возвращает None,
        пока закрытых исходов меньше min_resolved — по двум сделкам
        winrate не бывает.
        """
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n, "
                "       AVG(CAST(trader_was_right AS REAL)) AS wr, "
                "       AVG(roi_if_followed) AS roi "
                "FROM shadow_trades "
                "WHERE maker = ? AND market_resolved = 1 "
                "  AND trader_was_right IS NOT NULL",
                (maker.lower(),),
            ).fetchone()
        if not row or (row["n"] or 0) < min_resolved:
            return None
        return {
            "resolved": int(row["n"]),
            "winrate": float(row["wr"] or 0.0),
            "roi": float(row["roi"] or 0.0),
        }

    def market_reference_price(
        self, token_id: str, before_ts: int, window_sec: int, min_trades: int = 3
    ) -> Optional[float]:
        """Медианная цена покупок этого исхода за окно ДО указанного момента.

        Опорная точка для признака "удар по цене": насколько трейдер
        переплатил относительно того, где рынок только что торговался.
        Медиана, а не среднее — устойчивее к одиночному выбросу.

        None, если сделок меньше min_trades: по двум точкам опоры не строят.
        """
        with self._conn() as c:
            rows = c.execute(
                "SELECT price FROM trades "
                "WHERE token_id = ? AND ts >= ? AND ts < ? AND side = 'buy' "
                "ORDER BY price",
                (token_id, before_ts - window_sec, before_ts),
            ).fetchall()
        if len(rows) < min_trades:
            return None
        prices = [float(r["price"]) for r in rows]
        mid = len(prices) // 2
        value = prices[mid] if len(prices) % 2 else (prices[mid - 1] + prices[mid]) / 2.0
        return value if value > 0 else None

    def count_cluster_participants(
        self, token_id: str, since_ts: int, min_usdc: float
    ) -> int:
        """Сколько РАЗНЫХ кошельков купили этот исход на сумму от min_usdc.

        Пришло на смену подсчёту "новых" кошельков. Тот опирался на локальный
        trade_count, а на молодой базе 86% адресов имеют меньше 20 сделок —
        то есть признак означал просто "несколько участников" и срабатывал
        почти везде: 82% сигналов держались на нём.

        Деньги — честный признак толпы: скинуться по $500 на один исход за
        час случайно не выходит.
        """
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(DISTINCT maker) AS n FROM trades "
                "WHERE token_id = ? AND ts >= ? AND side = 'buy' AND usdc_amount >= ?",
                (token_id, since_ts, min_usdc),
            ).fetchone()
            return int(row["n"] or 0)

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

    # ───────── Признаки для скоринга ─────────

    def sum_wallet_buys_for_token(
        self, maker: str, token_id: str, since_ts: int
    ) -> tuple[float, int]:
        """Сколько кошелёк набрал по токену за окно: (сумма USDC, число сделок).

        Нужно для признака "накопление позиции": порог на одну сделку не видит
        того, кто набирает ту же сумму двадцатью мелкими покупками.
        """
        with self._conn() as c:
            row = c.execute(
                """
                SELECT COALESCE(SUM(usdc_amount), 0.0) AS total, COUNT(*) AS n
                FROM trades
                WHERE maker = ? AND token_id = ? AND ts >= ? AND side = 'buy'
                """,
                (maker.lower(), token_id, since_ts),
            ).fetchone()
            return float(row["total"] or 0.0), int(row["n"] or 0)

    def wallet_prev_trade_ts(self, maker: str, before_ts: int) -> Optional[int]:
        """Время предыдущей сделки кошелька строго до before_ts.

        Нужно для признака "пробуждение": кошелёк молчал месяцами и вдруг
        берёт крупно. Считаем по trades, а не по wallets.last_seen_ts, —
        последний уже обновлён текущей сделкой к моменту оценки.
        """
        with self._conn() as c:
            row = c.execute(
                "SELECT MAX(ts) AS prev FROM trades WHERE maker = ? AND ts < ?",
                (maker.lower(), before_ts),
            ).fetchone()
            return int(row["prev"]) if row and row["prev"] is not None else None

    def market_volume_since(self, token_id: str, since_ts: int) -> float:
        """Оборот по токену с момента since_ts (USDC)."""
        with self._conn() as c:
            row = c.execute(
                "SELECT COALESCE(SUM(usdc_amount), 0.0) AS v FROM trades "
                "WHERE token_id = ? AND ts >= ?",
                (token_id, since_ts),
            ).fetchone()
            return float(row["v"] or 0.0)

    def market_hourly_baseline(
        self, token_id: str, now_ts: int, window_hours: int, min_hours: int
    ) -> Optional[float]:
        """Средний часовой оборот рынка за окно — база для сравнения.

        Смысл признака: важен не абсолютный размер сделки, а во сколько раз
        она больше того, чем этот рынок живёт обычно. Рынок с оборотом
        $300/день и рынок с $40000/день нельзя мерить одной константой.

        None — если истории меньше min_hours (на свежей БД это норма, тогда
        вызывающий код падает обратно на volume24h из Gamma).
        """
        since = now_ts - window_hours * 3600
        with self._conn() as c:
            row = c.execute(
                """
                SELECT COALESCE(SUM(usdc_amount), 0.0) AS total,
                       MIN(ts) AS first_ts
                FROM trades WHERE token_id = ? AND ts >= ?
                """,
                (token_id, since),
            ).fetchone()
        if not row or row["first_ts"] is None:
            return None
        covered_hours = max(1.0, (now_ts - int(row["first_ts"])) / 3600.0)
        if covered_hours < min_hours:
            return None
        return float(row["total"] or 0.0) / covered_hours

    def history_days(self, now_ts: int) -> float:
        """Сколько дней локальной истории накоплено.

        Нужно для холодного старта: пока история короче возраста "нового"
        кошелька, признак новизны ничего не значит — в свежей БД новыми
        выглядят почти все, кого мы просто ещё не видели.
        """
        with self._conn() as c:
            row = c.execute("SELECT MIN(ts) AS first_ts FROM trades").fetchone()
        if not row or row["first_ts"] is None:
            return 0.0
        return max(0.0, (now_ts - int(row["first_ts"])) / 86400.0)

    def count_distinct_wallets_for_token(self, token_id: str, since_ts: int) -> int:
        """Сколько РАЗНЫХ кошельков торговали токен после since_ts.

        В отличие от count_recent_new_wallets_for_token не требует новизны:
        синхронный заход нескольких старых адресов — тоже кластер.
        """
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(DISTINCT maker) AS n FROM trades "
                "WHERE token_id = ? AND ts >= ?",
                (token_id, since_ts),
            ).fetchone()
            return int(row["n"] or 0)

    def wallet_bought_both_outcomes(
        self, maker: str, condition_id: str, since_ts: int
    ) -> bool:
        """Покупал ли кошелёк ОБА исхода одного рынка (YES и NO) за окно.

        Это и есть хедж или арбитраж: ставка на оба исхода не несёт мнения о
        результате, копировать её бессмысленно. Ловится только по
        condition_id — у YES и NO разные token_id, и по токену такую пару
        не увидеть.

        Пустой condition_id (старые записи до миграции) — False: судить не
        по чему, а ложное срабатывание хуже пропуска.
        """
        if not condition_id:
            return False
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(DISTINCT token_id) AS n FROM trades "
                "WHERE maker = ? AND condition_id = ? AND side = 'buy' AND ts >= ?",
                (maker.lower(), condition_id, since_ts),
            ).fetchone()
            return int(row["n"] or 0) > 1

    def wallet_traded_both_sides(self, maker: str, token_id: str) -> bool:
        """Покупал И продавал один и тот же токен — то есть входил и выходил.

        ВНИМАНИЕ: это НЕ про ставку на оба исхода рынка — для неё есть
        wallet_bought_both_outcomes. Здесь про оборот по одному исходу:
        признак скальпера или маркет-мейкера.
        """
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(DISTINCT side) AS n FROM trades "
                "WHERE maker = ? AND token_id = ?",
                (maker.lower(), token_id),
            ).fetchone()
            return int(row["n"] or 0) > 1

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
        score: Optional[float] = None,
        score_parts: Optional[str] = None,
    ) -> int:
        with self._conn() as c:
            cur = c.execute(
                """
                INSERT INTO signals
                (ts, signal_type, maker, token_id, market_slug,
                 usdc_amount, price, reason, tx_hash, side, score, score_parts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (ts, signal_type, maker.lower(), token_id, market_slug,
                 usdc_amount, price, reason, tx_hash, side, score, score_parts),
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

    # ───────── Снимки цены рынка (калибровка) ─────────

    def save_price_sample(
        self,
        ts: int,
        token_id: str,
        condition_id: Optional[str],
        market_slug: Optional[str],
        outcome: Optional[str],
        mid: Optional[float],
        best_bid: Optional[float],
        best_ask: Optional[float],
        fill_2000: Optional[float],
        depth_usdc: Optional[float],
        volume_24h: Optional[float],
        liquidity: Optional[float],
        end_date_ts: Optional[int],
        category: Optional[str],
        now_ts: int,
    ) -> Optional[int]:
        """Записать снимок цены. None, если такой уже есть (тот же токен и ts)."""
        with self._conn() as c:
            cur = c.execute(
                """
                INSERT INTO price_samples (
                    ts, token_id, condition_id, market_slug, outcome,
                    mid, best_bid, best_ask, fill_2000, depth_usdc,
                    volume_24h, liquidity, end_date_ts, category, created_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(token_id, ts) DO NOTHING
                """,
                (ts, token_id, condition_id, market_slug, outcome, mid,
                 best_bid, best_ask, fill_2000, depth_usdc, volume_24h,
                 liquidity, end_date_ts, category, now_ts),
            )
            return cur.lastrowid if cur.rowcount else None

    def price_sampled_since(self, token_ids: list, since_ts: int) -> set:
        """Какие из токенов уже снимались после since_ts.

        Нужно, чтобы один и тот же рынок не попадал в выборку каждый час:
        повторные снимки одного рынка — не независимые наблюдения, и
        калибровку они перекосили бы в пользу долгоживущих рынков.
        """
        if not token_ids:
            return set()
        out = set()
        with self._conn() as c:
            for i in range(0, len(token_ids), 400):
                chunk = list(token_ids[i:i + 400])
                marks = ",".join("?" * len(chunk))
                rows = c.execute(
                    "SELECT DISTINCT token_id FROM price_samples "
                    "WHERE ts >= ? AND token_id IN (" + marks + ")",
                    [since_ts] + chunk,
                ).fetchall()
                out.update(r[0] for r in rows)
        return out

    def get_price_samples_to_update(
        self, limit: int = 100, now_ts: Optional[int] = None
    ) -> list:
        """Незакрытые снимки для проверки резолва.

        Форма та же, что у get_shadow_to_update: половина батча отдана
        просроченным снимкам цены через час, половина — обычному LRU.
        """
        now_ts = int(time.time()) if now_ts is None else now_ts
        half = max(1, limit // 2)
        with self._conn() as c:
            overdue = c.execute(
                """
                SELECT """ + _PRICE_SAMPLE_FIELDS + """
                FROM price_samples
                WHERE market_resolved = 0
                  AND COALESCE(mid, best_ask) IS NOT NULL
                  AND price_1h IS NULL
                  AND ? - ts >= 3600
                  AND ? - ts <= 3600 * ?
                ORDER BY ts ASC
                LIMIT ?
                """,
                (now_ts, now_ts, SNAPSHOT_GRACE_FACTOR, half),
            ).fetchall()
            seen = {r["sample_id"] for r in overdue}
            rest = c.execute(
                """
                SELECT """ + _PRICE_SAMPLE_FIELDS + """
                FROM price_samples
                WHERE market_resolved = 0
                  AND COALESCE(mid, best_ask) IS NOT NULL
                ORDER BY
                    CASE WHEN last_checked_ts IS NULL THEN 0 ELSE 1 END,
                    last_checked_ts ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        out = [dict(r) for r in overdue]
        for r in rest:
            if len(out) >= limit:
                break
            if r["sample_id"] not in seen:
                out.append(dict(r))
        return out

    def update_price_sample_snapshots(
        self,
        sample_id: int,
        now_ts: int,
        current_price: Optional[float],
        set_price_1h: bool = False,
        set_price_24h: bool = False,
        set_price_7d: bool = False,
    ) -> None:
        """Мирроринг update_shadow_snapshots для price_samples."""
        with self._conn() as c:
            if current_price is None:
                c.execute(
                    "UPDATE price_samples SET last_checked_ts = ? WHERE id = ?",
                    (now_ts, sample_id),
                )
                return
            sets = ["last_checked_ts = ?",
                    "max_price_reached = MAX(COALESCE(max_price_reached, ?), ?)",
                    "min_price_reached = MIN(COALESCE(min_price_reached, ?), ?)"]
            args = [now_ts, current_price, current_price,
                    current_price, current_price]
            for flag, col in ((set_price_1h, "price_1h"),
                              (set_price_24h, "price_24h"),
                              (set_price_7d, "price_7d")):
                if flag:
                    sets.append(col + " = ?")
                    args.append(current_price)
            args.append(sample_id)
            c.execute(
                "UPDATE price_samples SET " + ", ".join(sets) + " WHERE id = ?",
                args,
            )

    def finalize_price_sample(
        self,
        sample_id: int,
        settled_price: float,
        trader_was_right: bool,
        roi_if_followed: float,
        hours_to_resolve: float,
        now_ts: int,
    ) -> None:
        """Зафиксировать резолв снимка.

        Имена аргументов — общие с другими таблицами, иначе обработчик
        исходов не смог бы звать их одинаково. По смыслу здесь никакого
        трейдера нет: trader_was_right — это "исход сыграл", а
        roi_if_followed — доход при покупке по опорной цене (середина,
        а у одностороннего стакана — аск).
        """
        with self._conn() as c:
            c.execute(
                """
                UPDATE price_samples SET
                    market_resolved = 1,
                    settled_price = ?,
                    won = ?,
                    roi_at_entry = ?,
                    hours_to_resolve = ?,
                    last_checked_ts = ?
                WHERE id = ?
                """,
                (settled_price, 1 if trader_was_right else 0,
                 roi_if_followed, hours_to_resolve, now_ts, sample_id),
            )

    def count_price_samples(self, resolved_only: bool = False) -> int:
        sql = "SELECT COUNT(*) FROM price_samples"
        if resolved_only:
            sql += " WHERE market_resolved = 1"
        with self._conn() as c:
            return c.execute(sql).fetchone()[0]

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

    def last_trade_ts(self) -> Optional[int]:
        """Время самой свежей сделки — для сторожа потока в heartbeat."""
        with self._conn() as c:
            row = c.execute("SELECT MAX(ts) AS ts FROM trades").fetchone()
            return int(row["ts"]) if row and row["ts"] is not None else None

    def count_signals(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM signals").fetchone()[0]

    def count_wallets(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM wallets").fetchone()[0]

    def count_resolved_outcomes(self) -> int:
        with self._conn() as c:
            return c.execute(
                "SELECT COUNT(*) FROM signal_outcomes WHERE market_resolved = 1"
            ).fetchone()[0]

    def count_trades(self) -> int:
        """Сколько строк в trades — для отчётности обслуживания."""
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM trades").fetchone()[0]

    def prune_old_trades(
        self,
        older_than_days: int = 7,
        now: Optional[int] = None,
        chunk: int = PRUNE_CHUNK,
        progress=None,
        max_rows: Optional[int] = None,
    ) -> int:
        """Удалить строки trades старше older_than_days дней — порциями.

        Таблица trades нужна только для cluster-детекции и подсчёта свежих
        кошельков на токене — оба смотрят максимум на последний час
        (cluster_window_seconds). Историю можно безопасно удалять:

          * агрегаты в wallets (trade_count, first_seen_ts, total_volume_usdc)
            хранятся отдельно и НЕ пересчитываются из trades;
          * signals / signal_outcomes таблицу trades не читают;
          * признак "пробуждения" берёт историю кошелька из API, а эту
            таблицу использует лишь как запасной путь.

        Почему порциями
        ---------------
        Одним запросом это не проходит. На живой базе под удаление попали
        5.9 млн строк: журнал WAL распух до 3.5 ГБ, работа не уложилась в
        отведённое время и откатилась целиком — то есть впустую.

        Порции по PRUNE_CHUNK строк фиксируются по отдельности: журнал
        остаётся небольшим, а прерванная уборка сохраняет уже сделанное и
        в следующий раз продолжится с того же места.

        VACUUM здесь НЕ вызывается — место на диске вернёт отдельный
        vacuum() (его нельзя запускать внутри транзакции). Возвращает число
        удалённых строк.
        """
        now = now if now is not None else int(time.time())
        cutoff = now - older_than_days * 86400
        deleted = 0
        while True:
            # Предел на вызов: фоновая чистка берёт понемногу, чтобы не
            # останавливать приём сделок — SQLite синхронный.
            take = chunk if max_rows is None else min(chunk, max_rows - deleted)
            if take <= 0:
                return deleted
            with self._conn() as c:
                cur = c.execute(
                    "DELETE FROM trades WHERE rowid IN ("
                    "  SELECT rowid FROM trades WHERE ts < ? LIMIT ?)",
                    (cutoff, take),
                )
                n = cur.rowcount or 0
            # Каждая порция ложится на диск сразу. Без этого смысл дробления
            # теряется: отложенный коммит собрал бы всё в одну пачку.
            self.flush()
            deleted += n
            if progress is not None:
                progress(deleted)
            if n < take:
                return deleted

    def vacuum(self) -> None:
        """Дефрагментировать БД и вернуть свободные страницы ОС.

        VACUUM не может выполняться внутри транзакции, поэтому открываем
        отдельное соединение в autocommit-режиме (isolation_level=None).
        Требует эксклюзивного доступа — запускать при ОСТАНОВЛЕННОМ трекере,
        иначе sqlite3 кинет 'database is locked'.
        """
        # Своё соединение держит файл — дописываем пачку и отпускаем,
        # иначе VACUUM упрётся в 'database is locked'.
        self.close()
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
        score: Optional[float] = None,
        score_parts: Optional[str] = None,
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
                     signal_types, created_ts, score, score_parts)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tx_hash, maker.lower(), token_id, ts, side, usdc_amount,
                        price, market_slug, category, volume_24h,
                        1 if passed_filters else 0, signal_types, now_ts,
                        score, score_parts,
                    ),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def get_shadow_to_update(
        self, limit: int = 100, now_ts: Optional[int] = None
    ) -> list[dict]:
        """Незакрытые shadow-сделки для проверки резолва.

        Ключи dict-ов совместимы с get_outcomes_to_update (id назван
        shadow_id, цена входа — price_at_signal, ts сделки — signal_ts),
        чтобы outcome_tracker мог переиспользовать общий обработчик батча.

        Очередь делится пополам, и вот почему
        -------------------------------------
        Раньше был чистый LRU — "дольше всех не проверявшиеся первыми". У
        свежей сделки last_checked_ts почти сейчас, поэтому она уходила в
        самый хвост очереди, а впереди стояли тысячи старых. Час истекал
        раньше, чем до неё доходил черёд; если за это время рынок успевал
        закрыться, снимок цены через час не брался уже никогда.

        Видно по данным: чем быстрее закрывается рынок, тем реже у сделки
        есть price_1h.

            рынок закрылся < 1 часа     726 сделок,  снимок есть у   0.0%
            1-3 часа                    435                          1.8%
            3-12 часов                 9992                         12.7%
            12-48 часов                6512                         35.0%
            больше 2 суток              507                         98.4%

        Для рынков быстрее часа снимка и не может быть. Но 3-12 часов живут
        заметно дольше часа — там 12.7% это уже потеря, причём смещённая:
        замеры дрейфа считались почти только по медленным рынкам, тогда как
        поток трекера — это быстрый спорт.

        Просроченные берутся только пока снимок ещё имеет смысл
        (SNAPSHOT_GRACE_FACTOR): иначе длинный хвост безнадёжно старых строк
        снова вытеснил бы свежие — ровно та беда, которую чиним.

        Поэтому половина батча отдаётся строкам, у которых снимок ПРОСРОЧЕН,
        и лишь вторая половина — обычному LRU. Нагрузка на Gamma та же:
        меняется только порядок, а не число запросов. Делить нужно именно
        пополам: отдать всю квоту просрочке значило бы остановить проверку
        резолва, пока разбирается накопленный хвост.
        """
        # Момент отсчёта берём сами: у парного метода для боевых сигналов
        # такого параметра нет, а общий обработчик зовёт оба одинаково.
        now_ts = int(time.time()) if now_ts is None else now_ts
        half = max(1, limit // 2)
        with self._conn() as c:
            overdue = c.execute(
                """
                SELECT """ + _SHADOW_FIELDS + """
                FROM shadow_trades
                WHERE market_resolved = 0
                  AND price_1h IS NULL
                  AND ? - ts >= 3600
                  AND ? - ts <= 3600 * ?
                ORDER BY ts ASC
                LIMIT ?
                """,
                (now_ts, now_ts, SNAPSHOT_GRACE_FACTOR, half),
            ).fetchall()
            seen = {r["shadow_id"] for r in overdue}
            rest = c.execute(
                """
                SELECT """ + _SHADOW_FIELDS + """
                FROM shadow_trades
                WHERE market_resolved = 0
                ORDER BY
                    CASE WHEN last_checked_ts IS NULL THEN 0 ELSE 1 END,
                    last_checked_ts ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        out = [dict(r) for r in overdue]
        for r in rest:
            if len(out) >= limit:
                break
            if r["shadow_id"] not in seen:
                out.append(dict(r))
        return out

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
