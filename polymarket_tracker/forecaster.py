"""Оценка вероятности исхода языковой моделью, независимо от цены рынка.

Зачем
-----
Весь трекер до сих пор отвечал на вопрос "кто-то знает больше рынка?",
копируя чужие сделки. Замер на 425 сделках показал, что так мы теряем
12.8%: перевес живёт в цене входа трейдера, которой у нас нет.

Здесь другой подход — оценить вероятность самим и сравнить с ценой. Не
"найти вероятного победителя" (это и есть цена, её видно бесплатно), а
найти рынок, где цена не учитывает того, что уже известно.

Главное решение конструкции: МОДЕЛЬ НЕ ВИДИТ ЦЕНУ
-------------------------------------------------
Покажи ей 0.72 — она напишет "около 70%", и мы измерим послушность, а не
знание. Цена подставляется снаружи, уже после оценки. Без этого правила
весь замер бессмысленен, поэтому цена не попадает в промпт ни в каком виде.

Второе: в модель уходят ПРАВИЛА РАСЧЁТА (поле description у Gamma), а не
заголовок. "Will X happen by Y" почти всегда имеет оговорки про источник и
срок, и именно на них теряют деньги.

Чего этот модуль НЕ делает
--------------------------
Не торгует и не решает, входить ли. Он выдаёт число и объяснение; дальше
оценка идёт тем же путём, что и остальные сигналы, — в теневую запись, к
замеру достижимой цены входа и к paper_pnl. Первый результат, который нас
интересует, — не прибыль, а калибровка: сбывается ли 70% в 70% случаев.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

# Цены за миллион токенов (документация Anthropic на 06.2026). Нужны только
# для оценки стоимости прогона — точный расход всё равно берём из usage.
PRICES_USD_PER_MTOK = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

# Веб-поиск — серверный инструмент: модель ищет сама, отдельный поисковый
# сервис не нужен.
#
# Предел на число поисков — главный рычаг стоимости. Выдача поиска и есть
# основной объём входа: без поиска запрос весит 529 токенов, с поиском
# 41 000-249 000. Разница в цене — два порядка.
WEB_SEARCH_TOOL = {
    "type": "web_search_20260209",
    "name": "web_search",
    "max_uses": 3,
}

# Уровень усилий. Ниже "high" модель ПЕРЕСТАЁТ ИСКАТЬ вовсе — проверено:
# при effort="medium" тот же запрос дал ноль поисков и ответ целиком из
# памяти модели, с устаревшими данными и расхождением с рынком в 0.19.
#
# Это самая опасная поломка в модуле: она не падает и не видна в ответе.
# Модель уверенно пишет вероятность, обоснование выглядит разумным, а
# свежих данных в нём нет. Поэтому усилия не опускаем и проверяем факт
# поиска в самом результате (см. Forecast.web_searches).
DEFAULT_EFFORT = "high"

SYSTEM_PROMPT = """Ты оцениваешь вероятность исходов для вопросов с рынка предсказаний.

Порядок работы:
1. Прочитай КРИТЕРИИ РАСЧЁТА целиком. Именно они решают исход, а не
   заголовок вопроса. Ищи в них: какой источник считается официальным, до
   какой даты и по какому времени, что считается частичным выполнением.
2. Найди свежие новости по теме. Сегодняшняя дата указана в запросе —
   твои внутренние знания могут быть устаревшими, полагайся на найденное.
3. Назови вероятность.

О калибровке. Твоя оценка проверяется по факту: из вопросов, где ты
сказал 70%, сбыться должно примерно 70%. Поэтому:
- не округляй до уверенных чисел, когда свидетельств мало;
- 0 и 1 не бывает — всегда оставляй место неожиданности;
- если тема тебе незнакома и поиск ничего не дал, ставь confidence "low"
  и вероятность ближе к середине. Признать незнание полезнее, чем угадать.

Ты НЕ видишь рыночную цену и не должен её угадывать. Оценивай вопрос по
существу."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "probability": {
            "type": "number",
            "description": "Вероятность исхода YES, от 0.01 до 0.99",
        },
        "confidence": {
            "type": "string",
            "enum": ["low", "medium", "high"],
            "description": "Насколько надёжны свидетельства",
        },
        "key_facts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Факты, на которых стоит оценка, с датами",
        },
        "resolution_note": {
            "type": "string",
            "description": "Что в критериях расчёта может удивить: источник, "
                           "срок, трактовка частичного выполнения",
        },
        "reasoning": {
            "type": "string",
            "description": "Короткое объяснение, 2-4 предложения",
        },
    },
    "required": ["probability", "confidence", "key_facts",
                 "resolution_note", "reasoning"],
    "additionalProperties": False,
}


@dataclass
class Forecast:
    """Результат одной оценки."""

    probability: float
    confidence: str
    key_facts: list
    resolution_note: str
    reasoning: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    web_searches: int = 0
    cost_usd: float = 0.0
    error: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.error


def estimate_cost(model: str, input_tokens: int, output_tokens: int,
                  cache_read_tokens: int = 0) -> float:
    """Примерная стоимость запроса. Чтение из кеша считаем по десятой доле
    входной цены — порядок верный, точная ставка зависит от модели."""
    price_in, price_out = PRICES_USD_PER_MTOK.get(model, (5.0, 25.0))
    return (
        input_tokens * price_in / 1_000_000
        + cache_read_tokens * price_in * 0.1 / 1_000_000
        + output_tokens * price_out / 1_000_000
    )


def build_question(market: dict, today: str) -> str:
    """Запрос к модели. Цены здесь нет и быть не должно."""
    rules = str(market.get("description") or "").strip()
    if len(rules) > 6000:
        rules = rules[:6000] + "\n[правила обрезаны]"
    ends = str(market.get("endDate") or "неизвестно")
    return (
        f"Сегодня: {today}\n\n"
        f"ВОПРОС: {market.get('question')}\n\n"
        f"РЫНОК ЗАКРЫВАЕТСЯ: {ends}\n\n"
        f"КРИТЕРИИ РАСЧЁТА:\n{rules or '(не указаны)'}\n\n"
        f"Оцени вероятность исхода YES."
    )


def parse_forecast(text: str) -> Optional[dict]:
    """Разобрать ответ. Схема гарантирует JSON, но подстраховка дешевле
    потерянного запроса: при работе с инструментами текстовых блоков
    несколько, и нужный не обязательно первый."""
    text = (text or "").strip()
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except ValueError:
        return None


def _text_blocks(content) -> list:
    out = []
    for block in content or []:
        if getattr(block, "type", None) == "text":
            out.append(getattr(block, "text", "") or "")
    return out


def _count_searches(content) -> int:
    return sum(1 for b in (content or [])
               if getattr(b, "type", None) == "web_search_tool_result")


def forecast(client, market: dict, today: str, model: str,
             effort: str = DEFAULT_EFFORT,
             max_tokens: int = 8000) -> Forecast:
    """Оценить один рынок. Исключения не пробрасываем: в прогоне по
    десяткам рынков один отказ не должен ронять всю работу."""
    empty = Forecast(probability=0.0, confidence="", key_facts=[],
                     resolution_note="", reasoning="", model=model)
    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": SYSTEM_PROMPT,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": build_question(market, today)}],
            tools=[WEB_SEARCH_TOOL],
            output_config={
                "effort": effort,
                "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
            },
        )
    except Exception as e:  # noqa: BLE001
        empty.error = f"{type(e).__name__}: {e}"
        return empty

    if getattr(response, "stop_reason", "") == "refusal":
        empty.error = "модель отказалась отвечать"
        return empty

    data = None
    for text in reversed(_text_blocks(response.content)):
        data = parse_forecast(text)
        if data:
            break
    usage = getattr(response, "usage", None)
    inp = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    cached = getattr(usage, "cache_read_input_tokens", 0) or 0

    if not data or "probability" not in data:
        empty.error = "не удалось разобрать ответ"
        empty.input_tokens, empty.output_tokens = inp, out
        empty.cost_usd = estimate_cost(model, inp, out, cached)
        return empty

    try:
        prob = float(data["probability"])
    except (TypeError, ValueError):
        empty.error = "вероятность не число"
        return empty
    # Ноль и единица означали бы "исход уже известен" — на открытом рынке
    # так не бывает, и деление на такую цену позже дало бы бесконечность.
    prob = min(max(prob, 0.01), 0.99)

    return Forecast(
        probability=prob,
        confidence=str(data.get("confidence") or ""),
        key_facts=list(data.get("key_facts") or []),
        resolution_note=str(data.get("resolution_note") or ""),
        reasoning=str(data.get("reasoning") or ""),
        model=model,
        input_tokens=inp,
        output_tokens=out,
        cache_read_tokens=cached,
        web_searches=_count_searches(response.content),
        cost_usd=estimate_cost(model, inp, out, cached),
        raw=data,
    )
