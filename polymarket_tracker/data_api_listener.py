"""Listener событий через Polymarket Data API.

Заменяет onchain.py начиная с CTF Exchange V2 (28 апреля 2026).
Polymarket Data API (https://data-api.polymarket.com/trades) возвращает
уже декодированные сделки с обеих exchange-контрактов, прозрачно
адаптируясь к будущим апгрейдам контрактов. Мы избавляемся от RPC,
ABI-декодирования и зависимости от web3.

Стратегия polling:
    - GET /trades?limit=N — возвращает свежие сделки в DESC порядке по timestamp
    - запоминаем timestamp последней обработанной сделки
    - на каждом цикле берём всё с timestamp > checkpoint
    - дедуплицируем по (transactionHash, proxyWallet, asset)

API публичный, без авторизации. Rate limit нигде не задокументирован,
но 1 запрос / 3 секунды держит спокойно.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import AsyncIterator, Optional, Set, Tuple

import aiohttp

from .config import Config

log = logging.getLogger(__name__)

DATA_API_TRADES_URL = "https://data-api.polymarket.com/trades"


@dataclass
class Trade:
    """Стандартизированная сделка Polymarket из Data API.

    Сохраняем тот же интерфейс, что был у старого Trade из onchain.py,
    чтобы остальной код (anomaly_detector, storage, core) не пришлось трогать.
    Поля, которых нет в Data API (block_number, log_index, taker, exchange),
    заполняем разумными заглушками.
    """

    tx_hash: str
    log_index: int  # для совместимости со схемой storage; всегда 0
    block_number: int  # для совместимости; всегда 0 — у Data API нет блоков
    timestamp: int
    exchange: str  # "data_api" — единый источник вместо ctf/neg_risk

    maker: str  # proxyWallet из API — реальный трейдер
    taker: str  # для совместимости; всегда пустой

    side: str  # "buy" / "sell"
    token_id: str  # asset из API (decimal string outcome-токена)
    usdc_amount: float  # size * price
    shares: float  # size из API
    price: float  # price из API (0..1)

    # Бонусные поля от Data API (не было у onchain Trade — обогащаем сигналы)
    title: Optional[str] = None  # название рынка
    slug: Optional[str] = None  # слаг РЫНКА (для ссылки не годится)
    event_slug: Optional[str] = None  # слаг СОБЫТИЯ — из него строится ссылка
    outcome: Optional[str] = None  # "Yes" / "No" / название outcome
    condition_id: Optional[str] = None
    pseudonym: Optional[str] = None  # никнейм трейдера на Polymarket
    user_name: Optional[str] = None  # имя пользователя если есть

    def __str__(self) -> str:
        return (
            f"Trade[{self.side.upper()}] maker={self.maker[:10]} "
            f"asset={self.token_id[:12]}.. ${self.usdc_amount:.2f} @ {self.price:.3f}"
        )


class DataApiListener:
    """Polling Polymarket Data API. Yield-ит Trade объекты по мере появления."""

    def __init__(self, config: Config):
        self.config = config
        self._session: Optional[aiohttp.ClientSession] = None

        # Дедупликация: (tx_hash, maker, asset) — уникальный ключ сделки.
        # Храним только последние N — старее уже не пересекутся с новыми
        # из-за timestamp фильтра.
        self._seen: Set[Tuple[str, str, str]] = set()
        self._seen_max = 5000

        # Timestamp последней обработанной сделки.
        # 0 при первом запуске → возьмём весь limit самых свежих и стартуем.
        self._last_ts: int = 0

    def _make_session(self) -> aiohttp.ClientSession:
        """Создать новую aiohttp сессию."""
        return aiohttp.ClientSession(
            # Выборка на 10000 сделок весит ~8 МБ — 15 с на неё не хватает.
            timeout=aiohttp.ClientTimeout(total=60),
            headers={"Accept": "application/json"},
        )

    async def stream_trades(self, start_ts: Optional[int] = None) -> AsyncIterator[Trade]:
        """Основной цикл polling. Работает до отмены извне."""
        cfg = self.config

        if start_ts is not None and start_ts > 0:
            self._last_ts = start_ts
            log.info("DataApiListener стартует с timestamp=%d", start_ts)
        else:
            log.info("DataApiListener стартует без чекпоинта — берём свежие сделки")

        if self._session is None:
            self._session = self._make_session()

        consecutive_errors = 0

        while True:
            try:
                # Data API публикует сделки пачками раз в ~5 минут: "голова"
                # стоит на месте, потом прыгает на +300 сек. Качать мегабайты
                # каждые несколько секунд бессмысленно — сначала дешёвый запрос
                # на одну сделку, и только если голова сдвинулась, тянем пачку.
                head_ts = await self._fetch_head_ts()
                if head_ts is not None and head_ts <= self._last_ts:
                    await asyncio.sleep(cfg.data_api_poll_interval)
                    continue

                trades = await self._fetch_recent_trades(cfg.data_api_batch_limit)

                # API отдаёт DESC по timestamp; разворачиваем для хронологии.
                # Фильтруем по checkpoint и дедупу.
                fresh: list[Trade] = []
                for t in reversed(trades):
                    if t.timestamp <= self._last_ts:
                        continue
                    key = (t.tx_hash, t.maker, t.token_id)
                    if key in self._seen:
                        continue
                    self._seen.add(key)
                    fresh.append(t)

                # Обрезаем _seen чтобы не разрастался
                if len(self._seen) > self._seen_max:
                    # Грубо: при превышении сбрасываем — checkpoint всё равно
                    # защищает от повторов из прошлого окна.
                    self._seen.clear()

                # Если вся выборка оказалась новой — значит окно упёрлось в
                # потолок и часть сделок между чекпоинтом и самой старой
                # записью пачки мы не увидели. Молча терять их нельзя.
                if trades and len(fresh) >= len(trades) and self._last_ts > 0:
                    log.warning(
                        "Выборка забита под потолок (%d из %d новых): между чекпоинтом "
                        "и пачкой возможны пропуски — увеличь DATA_API_BATCH_LIMIT",
                        len(fresh), cfg.data_api_batch_limit,
                    )

                for t in fresh:
                    self._last_ts = max(self._last_ts, t.timestamp)
                    yield t

                consecutive_errors = 0
                await asyncio.sleep(cfg.data_api_poll_interval)

            except asyncio.CancelledError:
                raise
            except aiohttp.ClientError as e:
                consecutive_errors += 1
                backoff = min(60.0, 5.0 * consecutive_errors)
                log.warning(
                    "Data API ошибка (%d подряд): %s — пересоздаю сессию, retry через %.0fс",
                    consecutive_errors, e, backoff,
                )
                await self.close()
                self._session = None
                await asyncio.sleep(backoff)
                self._session = self._make_session()
            except Exception as e:
                consecutive_errors += 1
                backoff = min(60.0, 5.0 * consecutive_errors)
                log.exception("Неожиданная ошибка в listener: %s — пересоздаю сессию, retry через %.0fс", e, backoff)
                await self.close()
                self._session = None
                await asyncio.sleep(backoff)
                self._session = self._make_session()

    async def _fetch_head_ts(self) -> Optional[int]:
        """Timestamp самой свежей сделки у API — один запрос на одну запись.

        Нужен, чтобы не тянуть многомегабайтную выборку, пока API не
        опубликовал новую пачку. None — если ответ невалидный: тогда
        вызывающий код идёт за полной выборкой, как раньше.
        """
        if self._session is None:
            self._session = self._make_session()
        try:
            async with self._session.get(DATA_API_TRADES_URL, params={"limit": "1"}) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
        except Exception:
            return None  # ошибку разберёт основная выборка ниже
        if not isinstance(data, list) or not data:
            return None
        try:
            return int(data[0].get("timestamp") or 0) or None
        except (TypeError, ValueError):
            return None

    async def _fetch_recent_trades(self, limit: int) -> list[Trade]:
        """Один GET к /trades. Возвращает массив Trade в порядке от API (DESC по ts)."""
        if self._session is None:
            self._session = self._make_session()
        params = {"limit": str(limit)}

        async with self._session.get(DATA_API_TRADES_URL, params=params) as resp:
            if resp.status != 200:
                body = await resp.text()
                log.warning("Data API status=%d, body=%s", resp.status, body[:300])
                return []
            data = await resp.json()

        if not isinstance(data, list):
            log.warning("Data API вернул не массив: %s", type(data).__name__)
            return []

        trades: list[Trade] = []
        for item in data:
            t = self._parse_trade(item)
            if t:
                trades.append(t)
        return trades

    def _parse_trade(self, item: dict) -> Optional[Trade]:
        """Распарсить один JSON-объект из ответа API в Trade."""
        try:
            tx = item.get("transactionHash")
            maker = item.get("proxyWallet")
            asset = item.get("asset")
            if not tx or not maker or not asset:
                # Без этих полей сделку не идентифицируем — пропускаем
                return None

            side_raw = (item.get("side") or "").upper()
            if side_raw == "BUY":
                side = "buy"
            elif side_raw == "SELL":
                side = "sell"
            else:
                return None

            size = float(item.get("size") or 0)
            price = float(item.get("price") or 0)
            ts = int(item.get("timestamp") or 0)

            if size <= 0 or price <= 0 or ts <= 0:
                return None
            if not 0 < price <= 1.001:
                # Битая цена
                return None

            usdc_amount = size * price

            return Trade(
                tx_hash=tx.lower(),
                log_index=0,
                block_number=0,
                timestamp=ts,
                exchange="data_api",
                maker=maker.lower(),
                taker="",
                side=side,
                token_id=str(asset),
                usdc_amount=usdc_amount,
                shares=size,
                price=price,
                title=item.get("title"),
                slug=item.get("slug"),
                event_slug=item.get("eventSlug"),
                outcome=item.get("outcome"),
                condition_id=item.get("conditionId"),
                pseudonym=item.get("pseudonym"),
                user_name=item.get("name"),
            )
        except (ValueError, TypeError, AttributeError) as e:
            log.warning("Не смог распарсить trade: %s (item=%s)", e, str(item)[:200])
            return None

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None
