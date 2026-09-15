"""Отбор рынков-кандидатов для агента-оценщика.

Почему отдельный модуль, а не по функции в каждом инструменте
-------------------------------------------------------------
Ровно из-за того, что случилось: правила отбора были написаны в пилоте и
не написаны в проверке связности. В результате прогон начался с рынков
"What price will Bitcoin hit in September" — тех самых, где у агента
заведомо нет шансов, и которые я перед этим специально отфильтровал в
соседнем файле. Деньги на них тратились, пока прогон не остановили.

Что здесь отбирается и почему именно так
----------------------------------------
* Категории. Крипта и живой спорт исключены. Про спорт замерено: перевес
  отрицательный даже у самого трейдера. Про крипту показал пилот — цена
  публична, непрерывна и меняется быстрее, чем поиск возвращает ответ:
  две модели нашли разные "текущие" цены биткойна и разошлись зеркально.

* Теги запрашиваются явно (include_tag=true). Без этого Gamma не отдаёт
  их ВООБЩЕ, поле приходит пустым, и фильтру нечего проверять — так крипта
  и проходила в первый раз.

* Цена 0.10-0.90. Снизу — потому что обе модели упираются в собственный
  пол 0.01-0.02 и завышают аутсайдеров в 10-20 раз; любой "перевес" там
  артефакт. Сверху — потому что на почти решённом рынке нечего выигрывать.

* Ликвидность $5k-400k. Снизу — чтобы ордер вообще налился. Сверху —
  потому что на крупных рынках нас уже опередили: там сидят те, кто
  занимается этим полный день.

* Есть описание. Без правил расчёта оценивать нечего: исход решают они.
"""
from __future__ import annotations

import collections
import datetime
import json
import logging
from typing import Optional

import requests

log = logging.getLogger(__name__)

GAMMA_URL = "https://gamma-api.polymarket.com/markets"

IGNORED_TAGS = {"sports", "crypto"}
MIN_LIQUIDITY = 5_000.0
MAX_LIQUIDITY = 400_000.0
MIN_PRICE = 0.10
MAX_PRICE = 0.90
MIN_DAYS = 1
MAX_DAYS = 21
MIN_RULES_CHARS = 80


def tags_of(market: dict) -> set:
    """Слаги тегов. Gamma отдаёт их списком словарей — но только если
    попросить include_tag=true, иначе поля нет вовсе."""
    out = set()
    for t in market.get("tags") or []:
        slug = t.get("slug") if isinstance(t, dict) else t
        if slug:
            out.add(str(slug))
    return out


def yes_price(market: dict) -> Optional[float]:
    try:
        return float(json.loads(market.get("outcomePrices") or "[]")[0])
    except (ValueError, TypeError, IndexError):
        return None


def is_binary(market: dict) -> bool:
    try:
        return json.loads(market.get("outcomes") or "[]") == ["Yes", "No"]
    except (ValueError, TypeError):
        return False


def days_left(market: dict, now: datetime.datetime) -> Optional[int]:
    end = market.get("endDate")
    if not end:
        return None
    try:
        return (datetime.datetime.fromisoformat(
            end.replace("Z", "+00:00")) - now).days
    except (ValueError, AttributeError):
        return None


def event_slug(market: dict) -> str:
    events = market.get("events") or []
    if events and isinstance(events[0], dict):
        return str(events[0].get("slug") or "")
    return ""


def rejection_reason(market: dict, now: datetime.datetime) -> Optional[str]:
    """Почему рынок не годится, или None если годится.

    Возвращаем причину, а не просто False: на отладке прогона важно видеть,
    что именно отсеялось, иначе пустой список выглядит поломкой сети.
    """
    if not is_binary(market):
        return "не бинарный"
    price = yes_price(market)
    if price is None:
        return "нет цены"
    if not (MIN_PRICE <= price <= MAX_PRICE):
        return f"цена {price:.3f} вне {MIN_PRICE}-{MAX_PRICE}"
    liq = float(market.get("liquidity") or 0)
    if liq < MIN_LIQUIDITY:
        return "мало ликвидности"
    if liq > MAX_LIQUIDITY:
        return "слишком крупный рынок"
    days = days_left(market, now)
    if days is None:
        return "нет даты закрытия"
    if not (MIN_DAYS <= days <= MAX_DAYS):
        return f"закрытие через {days} дн."
    tags = tags_of(market)
    if not tags:
        # Без тегов не отличить крипту от политики. Пропустить такой рынок
        # безопаснее, чем оценивать вслепую.
        return "теги не пришли"
    if tags & IGNORED_TAGS:
        return f"категория {sorted(tags & IGNORED_TAGS)}"
    if len(str(market.get("description") or "")) < MIN_RULES_CHARS:
        return "нет правил расчёта"
    return None


def fetch_markets(pages: int = 3, session=None) -> list:
    """Открытые рынки с тегами, по убыванию суточного оборота."""
    http = session or requests
    out = []
    for page in range(pages):
        try:
            r = http.get(GAMMA_URL, params={
                "closed": "false", "limit": "100", "offset": str(page * 100),
                "order": "volume24hr", "ascending": "false",
                "include_tag": "true",
            }, timeout=30)
        except Exception as e:  # noqa: BLE001
            log.warning("Gamma недоступна: %s", e)
            break
        if r.status_code != 200:
            break
        batch = r.json()
        if not batch:
            break
        out += batch
    return out


def pick_candidates(limit: int = 20, pages: int = 3,
                    markets: Optional[list] = None,
                    now: Optional[datetime.datetime] = None) -> list:
    """Рынки, годные для оценки агентом."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    pool = markets if markets is not None else fetch_markets(pages)
    out = []
    for m in pool:
        if rejection_reason(m, now) is None:
            m["_price_yes"] = yes_price(m)
            m["_days"] = days_left(m, now)
            out.append(m)
            if len(out) >= limit:
                break
    return out


# Сумма цен исчерпывающего набора исходов равна примерно единице. Заметно
# больше — значит исходы НЕ взаимоисключающие, а вложенные пороги вроде
# "нефть достигнет 100 / 105 / 110": там нефть, достигшая 110, достигла и
# 100. Проверять связность на таком наборе бессмысленно — сумма обязана
# быть больше единицы и у рынка, и у агента.
MAX_EXCLUSIVE_SUM = 1.15


def group_by_event(markets: list, min_size: int = 3,
                   max_per_event: int = 5,
                   exclusive_only: bool = True) -> list:
    """Сгруппировать по событию — для проверки связности.

    Внутри события берём самые дорогие исходы: на них у модели есть шанс,
    и именно они определяют картину.
    """
    groups = collections.defaultdict(list)
    for m in markets:
        slug = event_slug(m)
        if slug:
            groups[slug].append(m)
    usable = []
    for slug, items in groups.items():
        if len(items) < min_size:
            continue
        if exclusive_only:
            total = sum(yes_price(x) or 0 for x in items)
            if total > MAX_EXCLUSIVE_SUM:
                continue     # вложенные пороги, а не взаимоисключающие исходы
        items = sorted(items, key=lambda x: -(yes_price(x) or 0))[:max_per_event]
        usable.append((slug, items))
    return sorted(usable, key=lambda kv: -len(kv[1]))
