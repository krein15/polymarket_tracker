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
    # Слаги тегов Gamma (esports, sports, politics, crypto, ...). Именно по ним
    # работает фильтр категорий: поле category у Gamma больше не заполняется.
    tags: frozenset = frozenset()
    # Поля для outcome-трекера (фаза 1.2)
    last_trade_price: Optional[float] = None  # текущая цена нашего token_id
    settled_price: Optional[float] = None  # финальная цена нашего token_id (если closed)

    def url(self) -> str:
        return f"https://polymarket.com/event/{self.slug}"


# Крупные категории в порядке приоритета: рынок обычно несёт несколько тегов
# (например {primaries, united-states, politics, elections, earn-4}), и для
# отчётов нужен один осмысленный. Теги вроде earn-* — промо-метки Polymarket,
# темы рынка они не описывают.
_MAJOR_CATEGORIES = (
    "sports", "crypto", "politics", "elections", "geopolitics", "world",
    "economy", "business", "tech", "science", "culture", "pop-culture",
)


def _category_from_tags(tag_slugs: frozenset) -> str:
    """Одна читаемая категория из набора тегов Gamma."""
    for major in _MAJOR_CATEGORIES:
        if major in tag_slugs:
            return major
    meaningful = sorted(s for s in tag_slugs if not s.startswith("earn"))
    return meaningful[0] if meaningful else ""


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
        """Найти рынок по одному из clob_token_ids (UP или DOWN токен).

        Использует кэш — подходит для основного цикла, где нужны статичные
        метаданные (название, категория, slug). Цены могут быть устаревшими
        до 5 минут.
        """
        if token_id in self._cache:
            return self._cache[token_id]
        if token_id in self._negative_cache:
            return None

        info, market = await self._fetch_from_api(token_id)
        if info and market:
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
        elif info is None:
            self._negative_cache[token_id] = 1

        return info

    async def fetch_fresh(self, token_id: str) -> Optional[MarketInfo]:
        """Получить актуальные данные минуя кэш (для outcome-трекера).

        Делает до двух попыток: сначала с дефолтным фильтром Gamma (активные
        рынки), потом с closed=true (зарезолвленные). Не пишет в основной
        кэш — иначе портила бы свежесть для других вызывающих.
        """
        info, _ = await self._fetch_from_api(token_id, include_closed=False)
        if info is None:
            info, _ = await self._fetch_from_api(token_id, include_closed=True)
        return info

    async def _fetch_from_api(
        self, token_id: str, include_closed: bool = False
    ) -> tuple[Optional[MarketInfo], Optional[dict]]:
        """Низкоуровневый запрос к Gamma. Возвращает (info, raw_market_dict).

        include_closed=True добавляет ?closed=true — Gamma по умолчанию
        возвращает только активные рынки, но для outcome-трекера нам нужны
        как раз закрытые (узнать settled_price).
        """
        if self._session is None:
            self._session = self._make_session()

        url = f"{GAMMA_API_BASE}/markets"
        # include_tag=true — иначе Gamma не отдаёт теги, а поле category у неё
        # давно пустое, и фильтр категорий оказывается мёртвым.
        params: dict = {"clob_token_ids": token_id, "limit": 1, "include_tag": "true"}
        if include_closed:
            params["closed"] = "true"

        data = None
        for attempt in range(3):
            try:
                async with self._session.get(url, params=params) as resp:
                    if resp.status != 200:
                        log.warning("Gamma /markets вернул %d для token=%s", resp.status, token_id[:12])
                        return None, None
                    data = await resp.json()
                break
            except asyncio.TimeoutError:
                log.warning("Gamma API таймаут (попытка %d/3) для token=%s — пересоздаю сессию", attempt + 1, token_id[:12])
                await self.close()
                self._session = self._make_session()
                if attempt < 2:
                    await asyncio.sleep(3)
                else:
                    return None, None
            except aiohttp.ClientError as e:
                log.warning("Gamma API ошибка (попытка %d/3): %s — пересоздаю сессию", attempt + 1, e)
                await self.close()
                self._session = self._make_session()
                if attempt < 2:
                    await asyncio.sleep(3)
                else:
                    return None, None

        if not data or not isinstance(data, list):
            return None, None

        market = data[0]
        info = self._parse_market(market, token_id)
        return info, market

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

            # Категория. Историческое поле category Gamma больше не заполняет
            # (проверено на живом API: null и у рынка, и у события), поэтому
            # основной источник — слаги тегов, отдаваемые при include_tag=true.
            tag_slugs = frozenset(
                str(tag.get("slug", "")).lower().strip()
                for tag in (m.get("tags") or [])
                if isinstance(tag, dict) and tag.get("slug")
            )
            category = m.get("category", "") or ""
            if not category:
                events = m.get("events", [])
                if events and isinstance(events, list):
                    category = events[0].get("category", "") or ""
            category = category.lower().strip()
            if not category and tag_slugs:
                category = _category_from_tags(tag_slugs)

            # Текущая цена нашего token_id. Gamma отдаёт массив outcomePrices
            # выровненный по clobTokenIds — если есть, берём по индексу нашего токена.
            # Иначе fallback на lastTradePrice.
            current_price: Optional[float] = None
            settled_price: Optional[float] = None
            closed = bool(m.get("closed", False))

            outcome_prices = m.get("outcomePrices", [])
            if isinstance(outcome_prices, str):
                import json
                try:
                    outcome_prices = json.loads(outcome_prices)
                except json.JSONDecodeError:
                    outcome_prices = []

            try:
                idx = [str(c) for c in clob_ids].index(str(token_id))
                if idx < len(outcome_prices):
                    current_price = float(outcome_prices[idx])
            except (ValueError, TypeError):
                pass

            if current_price is None:
                ltp = m.get("lastTradePrice")
                if ltp is not None:
                    try:
                        current_price = float(ltp)
                    except (ValueError, TypeError):
                        pass

            # Если рынок закрыт — то, что у нас в outcomePrices, это финальная цена.
            # Бинарный резолв: 1.0 для победителя, 0.0 для проигравшего.
            if closed and current_price is not None:
                settled_price = current_price

            return MarketInfo(
                condition_id=m.get("conditionId", ""),
                question=m.get("question", "")[:200],
                slug=m.get("slug", ""),
                category=category,
                tags=tag_slugs,
                volume_24h=float(m.get("volume24hr", 0) or 0),
                volume_total=float(m.get("volume", 0) or 0),
                liquidity=float(m.get("liquidity", 0) or 0),
                end_date_iso=m.get("endDate"),
                outcome=outcome,
                closed=closed,
                last_trade_price=current_price,
                settled_price=settled_price,
            )
        except (KeyError, ValueError, TypeError) as e:
            log.warning("Не смог распарсить market: %s", e)
            return None
