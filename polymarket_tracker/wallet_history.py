"""История кошелька из Data API — вместо ожидания, пока накопится своя.

Зачем
-----
Признаки кошелька (новизна, пробуждение, кластер новых) — это 50 баллов из
150, то есть треть скоринга. Раньше они считались по локальной базе и были
выключены правилом холодного старта: пока своей истории меньше трёх суток,
«новый кошелёк» означает лишь «мы его ещё не видели». Замер на живых данных
за сутки: эти три признака не сработали ни разу, а достижимый максимум балла
составлял 70 при пороге 58 — сигнал требовал почти идеальной комбинации.

Здесь та же история берётся у Polymarket напрямую и сразу честная.

Сколько это стоит
-----------------
Кошельков в потоке ~1000 в час — столько запросов не сделать. Но признаки
считаются только для сделок дороже SCORING_MIN_TRADE_USDC, а таких порядка
80 в час. Плюс кэш: повторную сделку того же кошелька не перезапрашиваем.

Одна страница /activity (500 событий) отвечает на всё, что нужно:
  * уместилась целиком (пришло меньше 500) — знаем точный возраст и число
    сделок;
  * не уместилась — кошелёк заведомо активный, значит НЕ новый, и этого
    достаточно: точная цифра не нужна, чтобы отклонить.

Отказ API не ломает ничего: get() возвращает None, и скоринг честно
откатывается на локальные данные, как раньше.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import aiohttp

log = logging.getLogger(__name__)

ACTIVITY_URL = "https://data-api.polymarket.com/activity"
PAGE_LIMIT = 500

# Сколько последних отметок времени храним для признака «пробуждение».
# Нужна только предыдущая сделка, запас — на случай расхождения часов.
RECENT_KEEP = 8


@dataclass(frozen=True)
class WalletHistory:
    """Слепок истории кошелька на момент запроса."""

    address: str
    first_trade_ts: int       # самая старая сделка на странице
    trades_on_page: int       # сколько сделок пришло
    complete: bool            # вся история уместилась в страницу
    recent_ts: Tuple[int, ...]  # последние отметки времени, по убыванию
    fetched_at: float

    def age_days(self, now: float) -> float:
        """Возраст кошелька. Если история не уместилась — это нижняя оценка."""
        return max(0.0, (now - self.first_trade_ts) / 86400.0)

    def is_new(self, max_trades: int, max_age_days: float, now: float) -> bool:
        """Новый ли кошелёк по критериям конфига.

        Неполная история сразу означает «не новый»: раз не уместилось
        500 сделок, порог по их числу заведомо превышен.
        """
        if not self.complete:
            return False
        return self.trades_on_page <= max_trades and self.age_days(now) <= max_age_days

    def prev_trade_ts(self, before_ts: int) -> Optional[int]:
        """Предыдущая сделка строго до указанного момента — для «пробуждения»."""
        earlier = [ts for ts in self.recent_ts if ts < before_ts]
        return max(earlier) if earlier else None


class WalletHistoryProvider:
    """Кэширующий клиент /activity. Одна страница на кошелёк."""

    def __init__(
        self,
        ttl_seconds: float = 6 * 3600,
        max_entries: int = 5000,
        timeout_seconds: float = 20.0,
        max_concurrency: int = 3,
    ):
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self.timeout = timeout_seconds
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: Dict[str, WalletHistory] = {}
        # Отдельно помним неудачи, чтобы не долбить API по кругу на одном
        # и том же кошельке: адрес -> когда пробовали.
        self._failed: Dict[str, float] = {}
        self._sem = asyncio.Semaphore(max_concurrency)
        self.stats = {"hit": 0, "miss": 0, "fail": 0}

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                headers={"Accept": "application/json"},
            )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def get(self, address: str, now: Optional[float] = None) -> Optional[WalletHistory]:
        """История кошелька или None, если API недоступен."""
        now = now if now is not None else time.time()
        key = address.lower()

        cached = self._cache.get(key)
        if cached and now - cached.fetched_at < self.ttl:
            self.stats["hit"] += 1
            return cached

        failed_at = self._failed.get(key)
        if failed_at is not None and now - failed_at < 300:
            return None  # недавно не получилось — не молотим API повторно

        history = await self._fetch(key, now)
        if history is None:
            self.stats["fail"] += 1
            self._failed[key] = now
            return None

        self.stats["miss"] += 1
        self._remember(key, history)
        return history

    def _remember(self, key: str, history: WalletHistory) -> None:
        if len(self._cache) >= self.max_entries:
            # Простая эвикция: выбрасываем самые старые по времени запроса.
            oldest = sorted(self._cache.items(), key=lambda kv: kv[1].fetched_at)
            for k, _ in oldest[: max(1, self.max_entries // 10)]:
                self._cache.pop(k, None)
        self._cache[key] = history

    async def _fetch(self, address: str, now: float) -> Optional[WalletHistory]:
        if self._session is None or self._session.closed:
            await self.start()
        params = {"user": address, "limit": str(PAGE_LIMIT), "offset": "0"}
        try:
            async with self._sem:
                async with self._session.get(ACTIVITY_URL, params=params) as resp:
                    if resp.status != 200:
                        log.debug("activity %s: HTTP %d", address[:10], resp.status)
                        return None
                    data = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.debug("activity %s: %s", address[:10], type(e).__name__)
            return None
        except Exception as e:  # noqa: BLE001 — фон не должен ронять обработку сделки
            log.warning("activity %s: неожиданная ошибка %s", address[:10], e)
            return None

        if not isinstance(data, list):
            return None

        trade_ts = sorted(
            (int(a.get("timestamp") or 0) for a in data if a.get("type") == "TRADE"),
            reverse=True,
        )
        if not trade_ts:
            return None

        return WalletHistory(
            address=address,
            first_trade_ts=trade_ts[-1],
            trades_on_page=len(trade_ts),
            complete=len(data) < PAGE_LIMIT,
            recent_ts=tuple(trade_ts[:RECENT_KEEP]),
            fetched_at=now,
        )
