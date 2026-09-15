"""Оценщик вероятностей: разбор ответа и запрет на подсказку цены.

Самая опасная поломка этого модуля молчит
-----------------------------------------
При effort="medium" модель ПЕРЕСТАЁТ ИСКАТЬ. Проверено на живом запросе:
ноль поисков, 529 входных токенов вместо 41 000, ответ целиком из памяти
модели — с устаревшими данными и уверенным расхождением с рынком в 0.19.

Ничего не падает. Вероятность приходит, обоснование выглядит разумным,
JSON валиден. Отличить такой ответ от настоящего можно только по числу
поисков, поэтому оно попадает в результат и проверяется здесь.

Второе правило, которое легко потерять при правке промпта: цена рынка в
запрос не попадает. Покажи её модели — она напишет "около того же", и мы
измерим послушность вместо знания.
"""
from __future__ import annotations

from polymarket_tracker.forecaster import (
    DEFAULT_EFFORT,
    OUTPUT_SCHEMA,
    SYSTEM_PROMPT,
    WEB_SEARCH_TOOL,
    build_question,
    estimate_cost,
    parse_forecast,
)

MARKET = {
    "question": "Will the Fed cut rates in September?",
    "description": "Resolves YES if the upper bound is lowered by exactly "
                   "25 bps per the official FOMC statement.",
    "endDate": "2026-09-17T00:00:00Z",
    "outcomePrices": '["0.735", "0.265"]',
    "_price_yes": 0.735,
    "liquidity": 120000.0,
}


class TestNoPriceLeak:
    def test_цена_не_попадает_в_запрос(self):
        """Главное правило конструкции. Модель, увидевшая 0.735, повторит
        её — и замер потеряет смысл."""
        q = build_question(MARKET, "2026-09-15")
        assert "0.735" not in q
        assert "0.265" not in q
        assert "outcomePrices" not in q
        assert "liquidity" not in q.lower()

    def test_правила_расчёта_передаются(self):
        """Исход решают они, а не заголовок."""
        q = build_question(MARKET, "2026-09-15")
        assert "official FOMC statement" in q
        assert "25 bps" in q

    def test_дата_передаётся(self):
        """Без неё модель считает актуальным своё обучение."""
        assert "2026-09-15" in build_question(MARKET, "2026-09-15")

    def test_срок_закрытия_передаётся(self):
        assert "2026-09-17" in build_question(MARKET, "2026-09-15")

    def test_длинные_правила_обрезаются(self):
        """Иначе один рынок с простынёй условий съест бюджет запроса."""
        m = dict(MARKET, description="x" * 20000)
        q = build_question(m, "2026-09-15")
        assert len(q) < 8000
        assert "обрезаны" in q

    def test_без_правил_не_падает(self):
        q = build_question({"question": "Q?"}, "2026-09-15")
        assert "не указаны" in q


class TestSettings:
    def test_усилия_не_ниже_high(self):
        """Ниже — модель не ищет. Это не вкусовая настройка."""
        assert DEFAULT_EFFORT in ("high", "xhigh", "max")

    def test_число_поисков_ограничено(self):
        """Главный рычаг стоимости: 2 поиска ~$0.03, 4 поиска ~$0.05."""
        assert 1 <= WEB_SEARCH_TOOL["max_uses"] <= 5

    def test_промпт_требует_искать(self):
        assert "новости" in SYSTEM_PROMPT.lower()

    def test_промпт_говорит_про_калибровку(self):
        """Без этого модель выдаёт круглые уверенные числа."""
        assert "калибров" in SYSTEM_PROMPT.lower()

    def test_схема_требует_все_поля(self):
        assert set(OUTPUT_SCHEMA["required"]) == {
            "probability", "confidence", "key_facts",
            "resolution_note", "reasoning"}
        assert OUTPUT_SCHEMA["additionalProperties"] is False


class TestParse:
    def test_чистый_json(self):
        assert parse_forecast('{"probability": 0.42}')["probability"] == 0.42

    def test_json_среди_текста(self):
        """При работе с инструментами текстовых блоков несколько, и нужный
        не обязательно приходит один."""
        text = 'Вот оценка:\n{"probability": 0.3, "confidence": "low"}\nГотово.'
        assert parse_forecast(text)["probability"] == 0.3

    def test_мусор_даёт_none(self):
        assert parse_forecast("извините, не могу") is None
        assert parse_forecast("") is None
        assert parse_forecast(None) is None

    def test_обрезанный_json_не_роняет(self):
        assert parse_forecast('{"probability": 0.5, "confid') is None


class TestCost:
    def test_дороже_модель_дороже_запрос(self):
        cheap = estimate_cost("claude-haiku-4-5", 10_000, 1_000)
        mid = estimate_cost("claude-sonnet-5", 10_000, 1_000)
        top = estimate_cost("claude-opus-5", 10_000, 1_000)
        assert cheap < mid < top

    def test_кеш_дешевле_обычного_входа(self):
        plain = estimate_cost("claude-sonnet-5", 10_000, 100)
        cached = estimate_cost("claude-sonnet-5", 0, 100, cache_read_tokens=10_000)
        assert cached < plain

    def test_незнакомая_модель_не_роняет(self):
        assert estimate_cost("claude-неизвестная", 1000, 100) > 0
