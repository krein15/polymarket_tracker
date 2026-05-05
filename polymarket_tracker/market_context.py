"""Gamma API wrapper — метаданные рынков Polymarket.

Получаем по token_id: название рынка, volume, категорию, end_date.
Без этого сигнал бесполезен — мы не знаем на какое событие ставит трейдер.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

import aiohttp
from cachetools import TTLCache

from .config import GAMMA_API_BASE

log = logging.getLogger(__name__)


@dataclass
class MarketInfo:
    """Метаданные рынка из Gamma API."""

    condition_id: str
    question: str
    slug: str
    category: str  # lowercase
    volume_24h: float
    volume_total: float
    liquidity: float
    end_date_iso: Optional[str]
    outcome: str  # "Yes" / "No" / название outcome соответствующее token_id
    closed: bool

    def url(self) -> str:
        return f"https://polymarket.com/event/{self.slug}"


class MarketContext:
    """Кэширующий клиент Gamma API. Rate limit 300 req/10s на /markets — хватит."""

    def __init__(self):
        # Метаданные рынка стабильны в рамках дня — TTL 5 минут достаточно
        self._cache: TTLCache[str, MarketInfo] = TTLCache(maxsize=2000, ttl=300)
        # Негативный кэш — короткий TTL чтобы повторить попытку через минуту
        self._negative_cache: TTLCache[str, float] = TTLCache(maxsize=500, ttl=60)
        self._session: Optional[aiohttp.ClientSession] = None

    def _make_session(self) -> aiohttp.ClientSession:
        return aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30, connect=15),
            headers={"Accept": "application/json"},
        )

    async def start(self) -> None:
        if self._session is None:
            self._session = self._make_session()

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def get_by_token_id(self, token_id: str) -> Optional[MarketInfo]:
        """Найти рынок по одному из clob_token_ids (UP или DOWN токен)."""
        if token_id in self._cache:
            return self._cache[token_id]
        if token_id in self._negative_cache:
            return None

        if self._session is None:
            self._session = self._make_session()

        url = f"{GAMMA_API_BASE}/markets"
        params = {"clob_token_ids": token_id, "limit": 1}

        # 3 попытки с пересозданием сессии при ошибке
        for attempt in range(3):
            try:
                async with self._session.get(url, params=params) as resp:
                    if resp.status != 200:
                        log.warning("Gamma /markets вернул %d для token=%s", resp.status, token_id[:12])
                        self._negative_cache[token_id] = 1
                        return None
                    data = await resp.json()
                break  # успех — выходим из цикла
            except asyncio.TimeoutError:
                log.warning("Gamma API таймаут (попытка %d/3) для token=%s — пересоздаю сессию", attempt + 1, token_id[:12])
                await self.close()
                self._session = self._make_session()
                if attempt < 2:
                    await asyncio.sleep(3)
                else:
                    return None  # все 3 попытки провалились
            except aiohttp.ClientError as e:
                log.warning("Gamma API ошибка (попытка %d/3): %s — пересоздаю сессию", attempt + 1, e)
                await self.close()
                self._session = self._make_session()
                if attempt < 2:
                    await asyncio.sleep(3)
                else:
                    return None

        if not data or not isinstance(data, list):
            self._negative_cache[token_id] = 1
            return None

        market = data[0]
        info = self._parse_market(market, token_id)
        if info:
            # Кэшируем оба token_id рынка
            self._cache[token_id] = info
            clob_ids = market.get("clobTokenIds", [])
            if isinstance(clob_ids, str):
                import json
                try:
                    clob_ids = json.loads(clob_ids)
                except json.JSONDecodeError:
                    clob_ids = []
            for other_id in clob_ids:
                if other_id and other_id != token_id:
                    self._cache[str(other_id)] = info
        else:
            self._negative_cache[token_id] = 1

        return info

    def _parse_market(self, m: dict, token_id: str) -> Optional[MarketInfo]:
        """Вынимаем из ответа Gamma только нужные поля."""
        try:
            clob_ids = m.get("clobTokenIds", [])
            if isinstance(clob_ids, str):
                import json
                try:
                    clob_ids = json.loads(clob_ids)
                except json.JSONDecodeError:
                    clob_ids = []

            outcomes = m.get("outcomes", [])
            if isinstance(outcomes, str):
                import json
                try:
                    outcomes = json.loads(outcomes)
                except json.JSONDecodeError:
                    outcomes = ["Yes", "No"]

            # Определяем какому outcome соответствует наш token_id
            outcome = "?"
            try:
                idx = [str(c) for c in clob_ids].index(str(token_id))
                if idx < len(outcomes):
                    outcome = outcomes[idx]
            except ValueError:
                pass

            # Категория — может быть в events[0].category или просто .category
            category = m.get("category", "") or ""
            if not category:
                events = m.get("events", [])
                if events and isinstance(events, list):
                    category = events[0].get("category", "") or ""
            category = category.lower().strip()

            return MarketInfo(
                condition_id=m.get("conditionId", ""),
                question=m.get("question", "")[:200],
                slug=m.get("slug", ""),
                category=category,
                volume_24h=float(m.get("volume24hr", 0) or 0),
                volume_total=float(m.get("volume", 0) or 0),
                liquidity=float(m.get("liquidity", 0) or 0),
                end_date_iso=m.get("endDate"),
                outcome=outcome,
                closed=bool(m.get("closed", False)),
            )
        except (KeyError, ValueError, TypeError) as e:
            log.warning("Не смог распарсить market: %s", e)
            return None
