"""Отбор рынков для агента: что берём и, главное, что не берём.

Почему это отдельный модуль с тестами
-------------------------------------
Правила отбора сначала жили внутри пилота. Проверка связности их не знала
и начала прогон с рынков "What price will Bitcoin hit in September" —
ровно тех, что пилот специально отсеивал. Деньги тратились, пока прогон не
остановили вручную.

Дублирование правил и было причиной. Теперь они в одном месте, а тесты
закрепляют каждое: у любого фильтра здесь есть цена ошибки в долларах.
"""
from __future__ import annotations

import datetime

from polymarket_tracker.market_scanner import (
    IGNORED_TAGS,
    MAX_PRICE,
    MIN_PRICE,
    group_by_event,
    pick_candidates,
    rejection_reason,
    tags_of,
)

NOW = datetime.datetime(2026, 9, 16, tzinfo=datetime.timezone.utc)


def market(**over):
    m = {
        "question": "Will X happen?",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.40", "0.60"]',
        "liquidity": 50_000,
        "endDate": "2026-09-25T00:00:00Z",
        "tags": [{"slug": "politics"}, {"slug": "elections"}],
        "description": "x" * 200,
        "events": [{"slug": "some-event"}],
    }
    m.update(over)
    return m


class TestTags:
    def test_теги_из_словарей(self):
        assert tags_of(market()) == {"politics", "elections"}

    def test_теги_строками_тоже(self):
        assert tags_of(market(tags=["politics"])) == {"politics"}

    def test_тегов_нет(self):
        assert tags_of(market(tags=None)) == set()


class TestRejection:
    def test_годный_рынок_проходит(self):
        assert rejection_reason(market(), NOW) is None

    def test_крипта_отсекается(self):
        """Цена биткойна публична и меняется быстрее, чем поиск возвращает
        ответ: две модели нашли разные 'текущие' цены и разошлись зеркально."""
        m = market(tags=[{"slug": "crypto"}, {"slug": "bitcoin"}])
        assert "категория" in rejection_reason(m, NOW)

    def test_спорт_отсекается(self):
        """Замерено: перевес отрицательный даже у самого трейдера."""
        m = market(tags=[{"slug": "sports"}])
        assert "категория" in rejection_reason(m, NOW)

    def test_без_тегов_не_берём(self):
        """Gamma не отдаёт теги без include_tag=true. Отличить крипту от
        политики нечем — оценивать вслепую дороже, чем пропустить."""
        assert rejection_reason(market(tags=[]), NOW) == "теги не пришли"

    def test_дешёвые_аутсайдеры_отсекаются(self):
        """Обе модели упираются в пол 0.01-0.02 и завышают такие исходы в
        10-20 раз. Любой перевес там — артефакт, а не находка."""
        m = market(outcomePrices='["0.01", "0.99"]')
        assert "вне" in rejection_reason(m, NOW)

    def test_почти_решённые_отсекаются(self):
        m = market(outcomePrices='["0.97", "0.03"]')
        assert "вне" in rejection_reason(m, NOW)

    def test_границы_цены_включительно(self):
        assert rejection_reason(
            market(outcomePrices=f'["{MIN_PRICE}", "0.9"]'), NOW) is None
        assert rejection_reason(
            market(outcomePrices=f'["{MAX_PRICE}", "0.1"]'), NOW) is None

    def test_неликвидный_отсекается(self):
        assert rejection_reason(market(liquidity=100), NOW) == "мало ликвидности"

    def test_слишком_крупный_отсекается(self):
        """На таких рынках сидят те, кто занимается этим полный день."""
        m = market(liquidity=5_000_000)
        assert rejection_reason(m, NOW) == "слишком крупный рынок"

    def test_далёкое_закрытие_отсекается(self):
        m = market(endDate="2027-06-01T00:00:00Z")
        assert "закрытие через" in rejection_reason(m, NOW)

    def test_уже_закрывшийся_отсекается(self):
        m = market(endDate="2026-09-01T00:00:00Z")
        assert "закрытие через" in rejection_reason(m, NOW)

    def test_без_правил_расчёта_не_берём(self):
        """Исход решают правила, а не заголовок."""
        assert rejection_reason(market(description="коротко"), NOW) == \
            "нет правил расчёта"

    def test_многоисходный_не_берём(self):
        m = market(outcomes='["A", "B", "C"]')
        assert rejection_reason(m, NOW) == "не бинарный"

    def test_битые_данные_не_роняют(self):
        assert rejection_reason(market(outcomePrices="мусор"), NOW) is not None
        assert rejection_reason(market(endDate="вчера"), NOW) is not None


class TestPick:
    def test_предел_соблюдается(self):
        pool = [market(question=f"Q{i}") for i in range(10)]
        assert len(pick_candidates(limit=3, markets=pool, now=NOW)) == 3

    def test_негодные_не_попадают(self):
        pool = [market(tags=[{"slug": "crypto"}]), market()]
        got = pick_candidates(limit=10, markets=pool, now=NOW)
        assert len(got) == 1
        assert tags_of(got[0]) & IGNORED_TAGS == set()

    def test_цена_и_срок_проставляются(self):
        got = pick_candidates(limit=1, markets=[market()], now=NOW)
        assert got[0]["_price_yes"] == 0.40
        assert got[0]["_days"] == 9   # с 16-го по 25-е

    def test_пустой_пул(self):
        assert pick_candidates(limit=5, markets=[], now=NOW) == []


class TestGroup:
    def test_группирует_по_событию(self):
        pool = [market(events=[{"slug": "e1"}]) for _ in range(3)]
        pool += [market(events=[{"slug": "e2"}])]
        groups = group_by_event(pool, min_size=3, exclusive_only=False)
        assert len(groups) == 1
        assert groups[0][0] == "e1"

    def test_предел_исходов_на_событие(self):
        """Без него событие с одиннадцатью кандидатами съедает бюджет."""
        pool = [market(events=[{"slug": "e"}]) for _ in range(11)]
        groups = group_by_event(pool, min_size=3, max_per_event=5,
                                exclusive_only=False)
        assert len(groups[0][1]) == 5

    def test_берутся_самые_дорогие_исходы(self):
        """На дешёвых модель упирается в свой пол — смысла в них нет."""
        pool = [market(events=[{"slug": "e"}],
                       outcomePrices=f'["{p}", "0.5"]')
                for p in ("0.15", "0.80", "0.45")]
        groups = group_by_event(pool, min_size=3, max_per_event=2,
                                exclusive_only=False)
        prices = [float(__import__("json").loads(m["outcomePrices"])[0])
                  for m in groups[0][1]]
        assert prices == [0.80, 0.45]

    def test_вложенные_пороги_не_берутся(self):
        """"Нефть достигнет 100 / 105 / 110" — не взаимоисключающие исходы:
        достигшая 110 достигла и 100. Сумма там больше единицы и у рынка,
        и у агента, проверять связность бессмысленно."""
        pool = [market(events=[{"slug": "oil"}],
                       outcomePrices=f'["{p}", "0.5"]')
                for p in ("0.60", "0.45", "0.30")]
        assert group_by_event(pool, min_size=3) == []

    def test_исчерпывающий_набор_берётся(self):
        """Сумма около единицы — исходы действительно взаимоисключающие."""
        pool = [market(events=[{"slug": "midterms"}],
                       outcomePrices=f'["{p}", "0.5"]')
                for p in ("0.50", "0.30", "0.19")]
        assert len(group_by_event(pool, min_size=3)) == 1

    def test_вложенные_можно_разрешить_явно(self):
        pool = [market(events=[{"slug": "oil"}],
                       outcomePrices=f'["{p}", "0.5"]')
                for p in ("0.60", "0.45", "0.30")]
        assert len(group_by_event(pool, min_size=3, exclusive_only=False)) == 1

    def test_исключительность_по_полному_набору(self):
        """Порядок действий, который уже был нарушен: у нефти шесть исходов
        с суммой 1.20, но после отсева дешёвых остаются четыре с суммой
        около 1.15 — и порог перестаёт их ловить. Считать надо ДО отсева."""
        pool = [market(events=[{"slug": "oil"}],
                       outcomePrices=f'["{p}", "0.5"]')
                for p in ("0.50", "0.35", "0.30", "0.05")]
        assert group_by_event(pool, min_size=3, now=NOW) == []

    def test_событий_без_группы_нет(self):
        assert group_by_event([market(events=[])], min_size=3) == []
