"""Снимки цены САМИХ РЫНКОВ — без привязки к чьим-либо сделкам.

Зачем это, если есть теневая выборка
------------------------------------
Всё, что трекер мерил до сих пор, — попытка повторить за инсайдером. И
везде получался один ответ: перевес живёт в цене трейдера, а мы опаздываем
от 2 до 29 минут.

Но у предсказательных рынков есть смещение, которое не требует ни скорости,
ни инсайда: фавориты недооценены, аутсайдеры переоценены. На 38 669
теневых покупок с исходом оно видно невооружённым глазом:

    цена 0.10-0.20   винрейт  8.4%   перевес -6.7 пп   ROI -44.4%
    цена 0.40-0.60   винрейт 50.8%   перевес +0.4 пп   ROI   0.0%
    цена 0.60-0.70   винрейт 68.3%   перевес +3.9 пп   ROI  +5.9%

Возражение к этим числам одно, и оно серьёзное: это СДЕЛКИ ЛЮДЕЙ, а не
цены. Информированные покупатели кучкуются там, где у них перевес, и часть
+5.9% может быть их правотой, а не ошибкой рынка.

Здесь отбора нет вообще. Берём рынки из списка Gamma подряд, страницу за
страницей, и записываем цену — торговал там кто-нибудь или нет.

Что именно пишем
----------------
    mid        во что рынок оценивает исход — для проверки смещения
    best_ask   что мы заплатили бы за первую сотню долларов
    fill_2000  что мы заплатили бы за ордер в $2000, с обходом стакана

Разница между mid и fill_2000 и есть ответ на главное возражение к затее:
съест ли стоимость входа тот перевес, который найдётся. Смешивать их в
одном числе нельзя — это два разных вопроса.

Оба исхода рынка пишутся отдельными строками. Иначе вышел бы перекос: у
вопросов "случится ли X?" сторона Yes почти всегда дешёвая, и выборка
только по ней была бы выборкой аутсайдеров.

Чего этот замер НЕ отвечает
---------------------------
Рынки берутся только те, что закрываются в ближайший MAX_DAYS_TO_END.
Иначе данных не дождаться: годовой рынок даст исход через год. Внутри
этой рамки отбор не зависит от цены, поэтому калибровку мерить можно —
но переносить вывод на долгие рынки нельзя, и end_date пишется в строку
именно ради этой проверки.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp

from .config import GAMMA_API_BASE
from .entry_price import fill_price, parse_book
from .market_context import _category_from_tags

log = logging.getLogger(__name__)

MARKETS_URL = f"{GAMMA_API_BASE}/markets"
BOOK_URL = "https://clob.polymarket.com/book"
HTTP_TIMEOUT_SEC = 20

# Раз в час берём одну страницу списка и снимаем с неё до BATCH_MARKETS
# рынков. За сутки это ~700 рынков: выборка растёт быстрее, чем боевые
# сигналы, и не зависит от того, торговал ли там кто-нибудь.
INTERVAL_SEC = 3600
FIRST_RUN_DELAY_SEC = 300
PAGE_SIZE = 100
BATCH_MARKETS = 30

# Один рынок — один снимок в неделю. Повторные снимки одного рынка не
# независимы, и без этого выборка перекосилась бы в пользу долгоживущих.
COOLDOWN_SEC = 7 * 86400

# Дальний горизонт не берём: исход по нему придёт позже, чем нужен ответ.
MAX_DAYS_TO_END = 30

# На какие размеры считаем цену исполнения.
#
# Размер здесь не подробность, а половина вопроса. $2000 по цене 0.02 —
# это 100 000 долей, и книга столько не отдаёт. На живой выборке ордер
# $2000 проходил с переплатой меньше 5% лишь у 23% рынков, и почти все
# они дороже 0.80; у случайного рынка медианная переплата за размер
# 30.8% против 0.6% у рынка, где только что прошла крупная сделка.
#
# Поэтому меряем лесенкой: где перевес переживёт $200, но не $2000, это
# тоже ответ — просто про другой масштаб позиции.
FILL_SIZES_USDC = (200.0, 500.0, 2000.0)
FILL_SIZE_USDC = 2000.0

# Пауза между запросами стакана. Рынков за проход немного, спешить некуда.
REQUEST_PAUSE_SEC = 0.15

# Верхняя граница случайного смещения. Сколько рынков закрывается в
# горизонте, заранее неизвестно, поэтому начинаем с оценки и учим по
# ответам API.
START_BOUND = 1000

OFFSET_KEY = "price_sampler_offset"


def _iso(dt: datetime) -> str:
    """Дата в том виде, в каком её принимает Gamma."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_end_date(raw: Optional[str]) -> Optional[int]:
    """ISO-дата окончания рынка в unix-время. None, если разобрать нельзя."""
    if not raw:
        return None
    try:
        return int(datetime.fromisoformat(
            raw.replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None


def parse_market(m: dict) -> Optional[dict]:
    """Рынок из списка Gamma в вид, пригодный для снимка.

    None — если снимать нечего: нет токенов, нет книги заявок, рынок
    закрыт или не принимает ордера. Это не отбор по цене: такой рынок
    нельзя купить ни по какой цене.
    """
    if not m or m.get("closed") or not m.get("active"):
        return None
    if not m.get("enableOrderBook") or not m.get("acceptingOrders"):
        return None
    try:
        tokens = json.loads(m.get("clobTokenIds") or "[]")
        outcomes = json.loads(m.get("outcomes") or "[]")
    except (ValueError, TypeError):
        return None
    if len(tokens) < 2 or len(tokens) != len(outcomes):
        return None

    tag_slugs = frozenset(
        (t.get("slug") or t.get("label") or "").lower()
        for t in (m.get("tags") or []) if t
    )
    return {
        "condition_id": m.get("conditionId"),
        "slug": m.get("slug"),
        "tokens": [str(t) for t in tokens],
        "outcomes": [str(o) for o in outcomes],
        "volume_24h": _num(m.get("volume24hr")),
        "liquidity": _num(m.get("liquidity")),
        "end_date_ts": parse_end_date(m.get("endDateIso") or m.get("endDate")),
        "category": _category_from_tags(tag_slugs),
    }


def _num(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def mirror(levels: list) -> list:
    """Заявки по противоположному исходу, пересчитанные на этот.

    На бинарном рынке Yes + No = 1 по построению контракта. Значит бид на
    Yes по 0.001 — это ровно то же самое, что аск на No по 0.999, и
    наоборот. Уровни зеркалятся один в один.
    """
    return [(1.0 - price, size) for price, size in levels
            if 0.0 < price < 1.0]


def book_prices(payload: dict, other: dict = None) -> dict:
    """Цены одного исхода с учётом стакана противоположного.

    Зачем объединять. У крайних рынков своя сторона книги часто пуста:
    аутсайдер по 0.001 не имеет бидов, а его фаворит по 0.999 — асков. На
    первом же живом прогоне из 16 снимков у 10 не считалась середина, и
    все 10 — крайние цены. Это пропуск, СВЯЗАННЫЙ С ЦЕНОЙ, то есть ровно
    то смещение, ради борьбы с которым замер и затевался: калибровка
    считалась бы только по серединке шкалы.

    Объединение его убирает и заодно честнее описывает рынок: купить No
    можно и через книгу самого No, и продав Yes.
    """
    asks = parse_book(payload, "asks")
    bids = parse_book(payload, "bids")
    if other is not None:
        # Бид на противоположном исходе = аск на этом, и наоборот.
        asks = asks + mirror(parse_book(other, "bids"))
        bids = bids + mirror(parse_book(other, "asks"))
    best_ask = min((p for p, _ in asks), default=None)
    best_bid = max((p for p, _ in bids), default=None)
    mid = None
    if best_ask is not None and best_bid is not None:
        mid = (best_ask + best_bid) / 2.0
    out = {
        "mid": mid,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "depth_usdc": sum(p * s for p, s in asks),
    }
    for size in FILL_SIZES_USDC:
        out["fill_{:.0f}".format(size)] = fill_price(asks, size)
    return out


class PriceSampler:
    """Фоновая задача: обход списка рынков и снимки их цен."""

    def __init__(self, storage, config=None):
        self.storage = storage
        self.max_days = getattr(config, "price_sample_max_days",
                                MAX_DAYS_TO_END)
        self.batch = getattr(config, "price_sample_batch", BATCH_MARKETS)
        self.stats = {"pages": 0, "seen": 0, "skipped_cooldown": 0,
                      "skipped_far": 0, "saved": 0, "no_book": 0}

    async def run(self) -> None:
        log.info(
            "Снимки цены рынков: раз в %d мин, до %d рынков за проход, "
            "горизонт %d дней",
            INTERVAL_SEC // 60, self.batch, self.max_days,
        )
        await asyncio.sleep(FIRST_RUN_DELAY_SEC)
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001 — фон не роняет трекер
                log.warning("Снимки цены: ошибка прохода: %s", e)
            try:
                await asyncio.sleep(INTERVAL_SEC)
            except asyncio.CancelledError:
                break

    async def run_once(self) -> int:
        """Один проход: страница списка рынков, снимки с неё. Вернёт число строк."""
        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SEC)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            markets = await self._fetch_page(session)
            if markets is None:
                return 0
            candidates = self._pick(markets)
            saved = 0
            for m in candidates:
                saved += await self._snapshot(session, m)
        if saved:
            log.info("Снимки цены: записано %d строк по %d рынкам",
                     saved, len(candidates))
        return saved

    async def _fetch_page(self, session) -> Optional[list]:
        """Случайная страница списка рынков.

        Почему случайная, а не следующая по порядку. Первый живой проход
        записал 53 строки — и все 53 по одной категории: список идёт
        блоками, и подряд лежали 27 рынков одного события (кандидаты на
        выборах в Бразилии). Последовательный обход означал бы, что
        несколько часов подряд выборка состоит из одного события, а это
        не независимые наблюдения: они делят и исход, и настроение рынка.

        Верхнюю границу смещения не знаем заранее и учим по ответам:
        пустая страница означает, что дальше ничего нет, полная — что
        может быть ещё. Так граница сама подстраивается под то, сколько
        рынков сейчас закрывается в горизонте.
        """
        bound = int(self.storage.get_checkpoint(OFFSET_KEY) or START_BOUND)
        bound = max(PAGE_SIZE, bound)
        offset = random.randrange(0, bound)
        now = datetime.now(timezone.utc)
        # Рамку задаёт сам Gamma, а не мы постфактум. Без этих двух
        # параметров список идёт в своём порядке, и первая же страница
        # оказалась сплошь рынками 2027-2028 годов: сто из ста улетали в
        # отсев. Фильтр по дате от цены не зависит, поэтому калибровку он
        # не портит — он только сужает горизонт, и это записано в строке.
        params = {
            "closed": "false", "active": "true", "include_tag": "true",
            "limit": PAGE_SIZE, "offset": offset,
            "end_date_min": _iso(now),
            "end_date_max": _iso(now + timedelta(days=self.max_days)),
        }
        try:
            async with session.get(MARKETS_URL, params=params) as r:
                if r.status != 200:
                    log.warning("Снимки цены: Gamma ответила %d", r.status)
                    return None
                data = await r.json()
        except Exception as e:  # noqa: BLE001 — сеть, не сбой логики
            log.warning("Снимки цены: список рынков недоступен: %s", e)
            return None

        if not data:
            # Дальше этого места рынков нет — сузить границу.
            self.storage.set_checkpoint(
                OFFSET_KEY, str(max(PAGE_SIZE, offset)))
            return None
        if len(data) >= PAGE_SIZE and offset + 2 * PAGE_SIZE > bound:
            # Страница полная у самого края — возможно, есть ещё.
            self.storage.set_checkpoint(OFFSET_KEY, str(offset + 2 * PAGE_SIZE))
        self.stats["pages"] += 1
        return data

    def _pick(self, raw: list) -> list:
        """Отобрать рынки для снимка. Отбор не зависит от цены."""
        now = int(time.time())
        horizon = now + self.max_days * 86400
        parsed = []
        for m in raw:
            self.stats["seen"] += 1
            p = parse_market(m)
            if p is None:
                continue
            end = p["end_date_ts"]
            if end is not None and (end > horizon or end <= now):
                self.stats["skipped_far"] += 1
                continue
            parsed.append(p)

        tokens = [t for p in parsed for t in p["tokens"]]
        fresh = self.storage.price_sampled_since(tokens, now - COOLDOWN_SEC)
        out = []
        for p in parsed:
            if any(t in fresh for t in p["tokens"]):
                self.stats["skipped_cooldown"] += 1
                continue
            out.append(p)
            if len(out) >= self.batch:
                break
        return out

    async def _snapshot(self, session, m: dict) -> int:
        """Снимок одного рынка: строка на каждый исход.

        Стаканы обоих исходов берём заранее: цена каждого считается с
        учётом противоположного (см. book_prices).
        """
        now = int(time.time())
        saved = 0
        books = []
        for token in m["tokens"]:
            books.append(await self._fetch_book(session, token))
            await asyncio.sleep(REQUEST_PAUSE_SEC)
        binary = len(m["tokens"]) == 2

        for i, (token, outcome) in enumerate(zip(m["tokens"], m["outcomes"])):
            payload = books[i]
            # Зеркалить можно только на бинарном рынке: у события с тремя
            # исходами "не этот" не сводится к одному другому.
            other = books[1 - i] if binary else None
            empty = {"mid": None, "best_bid": None, "best_ask": None,
                     "depth_usdc": None}
            empty.update({"fill_{:.0f}".format(x): None
                          for x in FILL_SIZES_USDC})
            prices = (book_prices(payload, other) if payload is not None
                      else empty)
            if prices["mid"] is None:
                self.stats["no_book"] += 1
            row = self.storage.save_price_sample(
                ts=now, token_id=token, condition_id=m["condition_id"],
                market_slug=m["slug"], outcome=outcome,
                mid=prices["mid"], best_bid=prices["best_bid"],
                best_ask=prices["best_ask"],
                fill_200=prices["fill_200"], fill_500=prices["fill_500"],
                fill_2000=prices["fill_2000"],
                depth_usdc=prices["depth_usdc"],
                volume_24h=m["volume_24h"], liquidity=m["liquidity"],
                end_date_ts=m["end_date_ts"], category=m["category"],
                now_ts=now,
            )
            if row is not None:
                saved += 1
                self.stats["saved"] += 1
        return saved

    async def _fetch_book(self, session, token_id: str) -> Optional[dict]:
        try:
            async with session.get(BOOK_URL, params={"token_id": token_id}) as r:
                if r.status != 200:
                    return None
                return await r.json()
        except Exception:  # noqa: BLE001 — рынок мог закрыться, это не сбой
            return None
