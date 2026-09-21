"""Чёрный список рынков — один на все ветки сигналов.

Откуда тест
-----------
Правило жило методом внутри детектора, и спрашивала его только ветка
score. Быстрая полоса и подтверждение по погоне не фильтровали ничего: из
466 киберспортивных сигналов 357 пришли через score, а ещё 109 — мимо
фильтра, двумя другими путями. Отключение категории в .env срезало бы
только первые.

Замер, из-за которого это стало важно (425 сделок с ценой входа и исходом):

    киберспорт внутри матча   n=308   ROI -15.2%   перевес -6.2 пп
    всё остальное             n=117   ROI  -6.3%   перевес +0.1 пп

В киберспорте перевес отрицательный у САМОГО трейдера (-3.8 пп по его
цене) — копировать нечего.
"""
from __future__ import annotations

from polymarket_tracker.market_filter import is_ignored, is_ignored_for, market_tags


class Market:
    def __init__(self, category="", tags=()):
        self.category = category
        self.tags = frozenset(tags) if tags else None


class Cfg:
    def __init__(self, allowed=(), ignored=()):
        self.allowed_tags = set(allowed)
        self.ignored_categories = set(ignored)


class TestTags:
    def test_категория_и_теги_вместе(self):
        """Рынок LoL приходит с тегами {esports, games, sports}: по одной
        категории фильтр 'sports' его не поймает."""
        m = Market("esports", {"league-of-legends", "games", "sports"})
        assert market_tags(m) == {"esports", "league-of-legends", "games", "sports"}

    def test_рынка_нет(self):
        assert market_tags(None) == set()

    def test_пустые_теги_не_ломают(self):
        assert market_tags(Market("politics", None)) == {"politics"}


class TestIgnore:
    def test_киберспорт_отсекается_по_тегу_sports(self):
        m = Market("esports", {"esports", "games", "sports"})
        assert is_ignored(m, allowed_tags=set(), ignored_categories={"sports"})

    def test_явное_разрешение_перевешивает(self):
        """Иначе киберспорт нельзя вернуть, не открыв весь обычный спорт."""
        m = Market("esports", {"esports", "sports"})
        assert not is_ignored(m, {"esports"}, {"sports"})

    def test_политика_проходит(self):
        m = Market("politics", {"politics", "elections"})
        assert not is_ignored(m, set(), {"sports", "crypto"})

    def test_неизвестный_рынок_не_отсекается(self):
        """Gamma не ответила — молчать из-за недоступности справочника хуже,
        чем прислать лишний сигнал."""
        assert not is_ignored(None, set(), {"sports"})
        assert not is_ignored(Market("", ()), set(), {"sports"})

    def test_пустой_чёрный_список_пропускает_всё(self):
        m = Market("esports", {"sports"})
        assert not is_ignored(m, set(), set())

    def test_работает_прямо_с_конфигом(self):
        m = Market("esports", {"esports", "sports"})
        assert is_ignored_for(m, Cfg(allowed=(), ignored={"sports"}))
        assert not is_ignored_for(m, Cfg(allowed={"esports"}, ignored={"sports"}))

    def test_конфиг_без_полей_не_роняет(self):
        class Bare:
            pass

        assert not is_ignored_for(Market("esports", {"sports"}), Bare())


class TestКиберспортОтдельноОтСпорта:
    """Фильтровать надо по тегу esports, а не по sports.

    Правка 15.09 поставила IGNORED_CATEGORIES=crypto,sports и заодно
    выключила обычный спорт: погоня упала с 20 сигналов в сутки до нуля,
    быстрая полоса — с 18 до 0.3. Замер на 541 сигнале с исходом, с
    порогом входа 0.50:

        обычный спорт  n=131  ROI  +3.5%  перевес трейдера  +9.8 пп
        киберспорт     n=248  ROI  -8.7%  перевес трейдера  +2.3 пп

    И главное: ранние ончейн-сигналы — 66 из 78 — это обычный спорт. Без
    него единственная ветка, входящая по цене трейдера, почти пустеет.

    Теги ниже сняты с живых рынков Polymarket 21.09.2026 (include_tag=true):
    esports стоит на всех трёх дисциплинах и ни на одном обычном спорте.
    """

    IGNORED = {"crypto", "esports"}

    КИБЕР = {
        "lol": {"esports", "games", "league-of-legends", "sports"},
        "cs2": {"counter-strike-2", "esports", "games", "sports"},
        "dota2": {"dota-2", "esports", "games", "sports"},
    }
    ОБЫЧНЫЙ = {
        "nfl": {"games", "nfl", "nfl-gameday", "sports"},
        "mlb": {"baseball", "games", "mlb", "sports"},
        "cfb": {"cfb", "cfb-gameday", "games", "sports"},
        "atp": {"games", "sports", "tennis"},
        "epl": {"epl", "games", "premier-league", "soccer", "sports"},
        "ucl": {"games", "soccer", "sports", "ucl", "ucl-matchday"},
    }

    def test_киберспорт_отсекается(self):
        for имя, tags in self.КИБЕР.items():
            assert is_ignored(Market("", tags), set(), self.IGNORED), имя

    def test_обычный_спорт_проходит(self):
        for имя, tags in self.ОБЫЧНЫЙ.items():
            assert not is_ignored(Market("", tags), set(), self.IGNORED), имя

    def test_крипта_по_прежнему_отсекается(self):
        assert is_ignored(Market("crypto", {"crypto", "bitcoin"}),
                          set(), self.IGNORED)

    def test_старое_правило_ловило_весь_спорт(self):
        """Ради чего правка: sports стоит и на киберспорте, и на NFL."""
        старое = {"crypto", "sports"}
        assert is_ignored(Market("", self.КИБЕР["lol"]), set(), старое)
        assert is_ignored(Market("", self.ОБЫЧНЫЙ["nfl"]), set(), старое)
