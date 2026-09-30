"""Снимки цены самих рынков — замер без привязки к чьим-либо сделкам.

Откуда взялась задача
---------------------
Всё, что трекер мерил до сих пор, условно на том, что кто-то совершил
сделку. И везде выходило одно: перевес живёт в цене трейдера, а мы
опаздываем от 2 до 29 минут.

Но на 38 669 теневых покупок с исходом видно смещение, которое не требует
ни скорости, ни инсайда:

    цена 0.10-0.20   винрейт  8.4%   перевес -6.7 пп   ROI -44.4%
    цена 0.40-0.60   винрейт 50.8%   перевес +0.4 пп   ROI   0.0%
    цена 0.60-0.70   винрейт 68.3%   перевес +3.9 пп   ROI  +5.9%

Возражение: это сделки людей, а не цены. Информированные покупатели
кучкуются там, где у них перевес. Здесь отбора нет — берём рынки из
списка подряд и пишем цену независимо от того, торговал там кто-то или
нет.
"""
from __future__ import annotations

import asyncio
import json

from conftest import NOW

from polymarket_tracker.price_sampler import (
    PriceSampler,
    balance_of,
    book_prices,
    mirror,
    parse_end_date,
    parse_market,
    stratum_of,
)

DAY = 86400


def gamma_market(**over):
    m = {
        "conditionId": "0xc",
        "slug": "will-x-happen",
        "clobTokenIds": json.dumps(["tok-yes", "tok-no"]),
        "outcomes": json.dumps(["Yes", "No"]),
        "closed": False,
        "active": True,
        "enableOrderBook": True,
        "acceptingOrders": True,
        "volume24hr": 12345.6,
        "liquidity": 6789.0,
        "endDateIso": "2026-10-01T04:59:00Z",
        "tags": [{"slug": "politics"}, {"slug": "elections"}],
    }
    m.update(over)
    return m


def book(bids=(), asks=()):
    return {
        "bids": [{"price": str(p), "size": str(s)} for p, s in bids],
        "asks": [{"price": str(p), "size": str(s)} for p, s in asks],
    }


class TestРазборРынка:
    def test_поля_вынимаются(self):
        p = parse_market(gamma_market())
        assert p["tokens"] == ["tok-yes", "tok-no"]
        assert p["outcomes"] == ["Yes", "No"]
        assert p["category"] == "politics"
        assert p["end_date_ts"] == parse_end_date("2026-10-01T04:59:00Z")

    def test_закрытый_рынок_пропускается(self):
        assert parse_market(gamma_market(closed=True)) is None

    def test_без_книги_заявок_пропускается(self):
        """Такой рынок нельзя купить ни по какой цене — это не отбор по цене."""
        assert parse_market(gamma_market(enableOrderBook=False)) is None
        assert parse_market(gamma_market(acceptingOrders=False)) is None

    def test_битые_токены_не_роняют(self):
        assert parse_market(gamma_market(clobTokenIds="не json")) is None
        assert parse_market(gamma_market(clobTokenIds=json.dumps(["один"]))) is None

    def test_исходов_и_токенов_поровну(self):
        assert parse_market(gamma_market(
            outcomes=json.dumps(["Yes", "No", "Draw"]))) is None


class TestДата:
    def test_iso_с_зулу(self):
        assert parse_end_date("2026-10-01T04:59:00Z") > 0

    def test_пусто(self):
        assert parse_end_date(None) is None
        assert parse_end_date("") is None

    def test_мусор_не_роняет(self):
        assert parse_end_date("когда-нибудь") is None


class TestЗеркало:
    def test_бид_на_yes_это_аск_на_no(self):
        """Yes + No = 1 по построению контракта."""
        assert mirror([(0.001, 500.0)]) == [(0.999, 500.0)]

    def test_крайние_уровни_отбрасываются(self):
        """Цена 0 или 1 — это не заявка, а мусор в книге."""
        assert mirror([(0.0, 10.0), (1.0, 10.0), (0.4, 10.0)]) == [(0.6, 10.0)]


class TestЦеныИзСтакана:
    def test_своя_книга(self):
        p = book_prices(book(bids=[(0.64, 4000)], asks=[(0.66, 5000)]))
        assert abs(p["mid"] - 0.65) < 1e-9
        assert p["best_bid"] == 0.64 and p["best_ask"] == 0.66

    def test_противоположная_книга_достраивает_сторону(self):
        """Главная причина объединять. На первом живом прогоне из 16
        снимков у 10 не считалась середина, и все 10 — крайние цены. Это
        пропуск, СВЯЗАННЫЙ С ЦЕНОЙ, то есть ровно то смещение, ради
        борьбы с которым замер и затевался.

        Направление важно: аск на No берётся из БИДА на Yes (кто готов
        купить Yes по 0.001, тот продаёт No по 0.999), а не из аска.
        """
        yes = book(bids=[(0.001, 100000)], asks=[(0.002, 50000)])
        no = book()                                   # своей книги нет вовсе
        p = book_prices(no, yes)
        assert p["best_ask"] == 0.999, "аск на No не достроен из бида на Yes"
        assert p["best_bid"] == 0.998, "бид на No не достроен из аска на Yes"
        assert abs(p["mid"] - 0.9985) < 1e-9

    def test_односторонний_стакан_остаётся_односторонним(self):
        """Если на Yes нет бидов, у No неоткуда взяться аску: такую
        позицию действительно нельзя купить, и выдумывать цену нельзя."""
        yes = book(asks=[(0.001, 100000)])            # только продавцы
        p = book_prices(book(), yes)
        assert p["best_bid"] == 0.999
        assert p["best_ask"] is None and p["mid"] is None

    def test_без_противоположной_книги_как_раньше(self):
        p = book_prices(book(asks=[(0.001, 100000)]))
        assert p["mid"] is None and p["best_ask"] == 0.001

    def test_пустой_стакан(self):
        p = book_prices({})
        assert p["mid"] is None and p["fill_2000"] is None

    def test_цена_исполнения_обходит_уровни(self):
        p = book_prices(book(bids=[(0.64, 100)],
                             asks=[(0.66, 1000), (0.70, 10000)]))
        # $660 по 0.66, остаток по 0.70 — средняя между ними.
        assert 0.66 < p["fill_2000"] < 0.70


class FakeStorage:
    def __init__(self, sampled=()):
        self.sampled = set(sampled)
        self.saved = []
        self.checkpoints = {}

    def price_sampled_since(self, token_ids, since_ts):
        return {t for t in token_ids if t in self.sampled}

    def get_checkpoint(self, key):
        return self.checkpoints.get(key)

    def set_checkpoint(self, key, value):
        self.checkpoints[key] = value

    def save_price_sample(self, **kw):
        self.saved.append(kw)
        return len(self.saved)


class Cfg:
    price_sample_batch = 3
    price_sample_max_days = 30


class TestОтбор:
    def _sampler(self, storage):
        return PriceSampler(storage, Cfg())

    def test_берёт_не_больше_батча(self):
        s = self._sampler(FakeStorage())
        raw = []
        for i in range(10):
            raw.append(gamma_market(
                slug=f"m{i}",
                clobTokenIds=json.dumps([f"y{i}", f"n{i}"])))
        assert len(s._pick(raw)) == 3

    def test_дальние_рынки_отсекаются(self):
        """Годовой рынок даст исход через год — данных не дождаться."""
        s = self._sampler(FakeStorage())
        raw = [gamma_market(endDateIso="2028-01-01T00:00:00Z")]
        assert s._pick(raw) == []
        assert s.stats["skipped_far"] == 1

    def test_уже_закрывшиеся_отсекаются(self):
        s = self._sampler(FakeStorage())
        assert s._pick([gamma_market(endDateIso="2020-01-01T00:00:00Z")]) == []

    def test_недавно_снятый_рынок_пропускается(self):
        """Повторные снимки одного рынка не независимы: выборка
        перекосилась бы в пользу долгоживущих."""
        s = self._sampler(FakeStorage(sampled={"tok-yes"}))
        assert s._pick([gamma_market()]) == []
        assert s.stats["skipped_cooldown"] == 1

    def test_отбор_симметричен_по_сторонам(self):
        """Дешёвый Yes и дорогой Yes — это один и тот же рынок с разных
        сторон, и попадать в выборку он должен одинаково.

        С 30.09 отбор расслоён по цене намеренно (см.
        TestРасслоение), но слой считается от min(цена, 1-цена) —
        "расстояния до определённости". Поэтому рынок 0.02/0.98 и рынок
        0.98/0.02 неразличимы, как и должно быть: обе строки всё равно
        попадут в выборку.
        """
        дешёвые = [gamma_market(slug=f"m{i}", bestBid=0.01, bestAsk=0.02,
                                outcomePrices=json.dumps(["0.01", "0.99"]),
                                clobTokenIds=json.dumps([f"y{i}", f"n{i}"]))
                   for i in range(6)]
        дорогие = [gamma_market(slug=f"m{i}", bestBid=0.97, bestAsk=0.98,
                                outcomePrices=json.dumps(["0.98", "0.02"]),
                                clobTokenIds=json.dumps([f"y{i}", f"n{i}"]))
                   for i in range(6)]
        a = [p["slug"] for p in self._sampler(FakeStorage())._pick(дешёвые)]
        b = [p["slug"] for p in self._sampler(FakeStorage())._pick(дорогие)]
        assert a == b and len(a) == 3


class TestОчередьРезолва:
    def test_снимок_с_аском_но_без_середины_остаётся(self, storage):
        """Самый хвост шкалы: на аутсайдера по 0.001 покупателей нет
        вовсе, середины не существует — но купить его можно."""
        storage.save_price_sample(
            ts=NOW, token_id="tok", condition_id="0xc", market_slug="m",
            outcome="Yes", mid=None, best_bid=None, best_ask=0.001,
            fill_2000=0.002, depth_usdc=1e6, volume_24h=1.0, liquidity=1.0,
            end_date_ts=NOW + DAY, category="politics", now_ts=NOW)
        q = storage.get_price_samples_to_update(limit=10, now_ts=NOW + 60)
        assert len(q) == 1
        assert abs(q[0]["price_at_signal"] - 0.001) < 1e-12

    def test_снимок_совсем_без_цены_в_очередь_не_идёт(self, storage):
        storage.save_price_sample(
            ts=NOW, token_id="tok2", condition_id=None, market_slug="m",
            outcome="No", mid=None, best_bid=0.999, best_ask=None,
            fill_2000=None, depth_usdc=0.0, volume_24h=None, liquidity=None,
            end_date_ts=None, category=None, now_ts=NOW)
        assert storage.get_price_samples_to_update(
            limit=10, now_ts=NOW + 60) == []

    def test_повтор_не_дублируется(self, storage):
        kw = dict(ts=NOW, token_id="tok3", condition_id=None, market_slug="m",
                  outcome="Yes", mid=0.5, best_bid=0.49, best_ask=0.51,
                  fill_2000=0.51, depth_usdc=1.0, volume_24h=None,
                  liquidity=None, end_date_ts=None, category=None, now_ts=NOW)
        assert storage.save_price_sample(**kw) is not None
        assert storage.save_price_sample(**kw) is None

    def test_резолв_фиксируется(self, storage):
        sid = storage.save_price_sample(
            ts=NOW, token_id="tok4", condition_id=None, market_slug="m",
            outcome="Yes", mid=0.65, best_bid=0.64, best_ask=0.66,
            fill_2000=0.66, depth_usdc=1.0, volume_24h=None, liquidity=None,
            end_date_ts=None, category=None, now_ts=NOW)
        storage.finalize_price_sample(
            sid, settled_price=1.0, trader_was_right=True,
            roi_if_followed=0.538, hours_to_resolve=5.0, now_ts=NOW + 100)
        assert storage.count_price_samples(resolved_only=True) == 1
        assert storage.get_price_samples_to_update(limit=10) == []


class TestПроход:
    def test_смещение_страницы_запоминается(self, monkeypatch):
        """Без этого каждый проход брал бы одну и ту же первую страницу."""
        st = FakeStorage()
        s = PriceSampler(st, Cfg())

        async def fake_page(session):
            return None

        monkeypatch.setattr(s, "_fetch_page", fake_page)
        asyncio.run(s.run_once())
        assert st.checkpoints == {}    # страница не пришла — смещение не двигаем


class TestСлучайноеСмещение:
    """Страница берётся случайная, а не следующая по порядку.

    Первый живой проход записал 53 строки — и все 53 по одной категории:
    список Gamma идёт блоками, и подряд лежали 27 рынков одного события
    (кандидаты на выборах в Бразилии). Последовательный обход означал бы,
    что несколько часов подряд выборка состоит из одного события, а это
    не независимые наблюдения: они делят и исход, и настроение рынка.
    """

    class Session:
        def __init__(self, empty_from=None):
            self.offsets = []
            self.empty_from = empty_from

        def get(self, url, params=None):
            self.offsets.append(int(params["offset"]))
            page = ([] if (self.empty_from is not None
                           and params["offset"] >= self.empty_from)
                    else [gamma_market(slug=f"m{params['offset']}")] * 100)
            return _Resp(page)

    def _fetch(self, storage, session):
        s = PriceSampler(storage, Cfg())
        return asyncio.run(s._fetch_page(session))

    def test_смещения_разные(self):
        st = FakeStorage()
        ses = self.Session()
        for _ in range(12):
            self._fetch(st, ses)
        assert len(set(ses.offsets)) > 1, "смещение не меняется"

    def test_пустая_страница_сужает_границу(self):
        """Иначе выборка вечно била бы в пустоту за краем списка."""
        st = FakeStorage()
        st.checkpoints["price_sampler_offset"] = "10000"
        ses = self.Session(empty_from=0)
        assert self._fetch(st, ses) is None
        assert int(st.checkpoints["price_sampler_offset"]) < 10000

    def test_граница_не_опускается_ниже_страницы(self):
        """Иначе randrange(0, 0) уронил бы проход."""
        st = FakeStorage()
        st.checkpoints["price_sampler_offset"] = "0"
        ses = self.Session()
        assert self._fetch(st, ses) is not None


class _Resp:
    def __init__(self, payload):
        self.status = 200
        self._payload = payload

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class TestРасслоение:
    """Квота на слой цены.

    За 8.5 дней набралось 738 закрывшихся наблюдений — и 566 из них в
    двух крайних полосах, а в середине по 10-22. Так вышло не случайно:
    рынков вида "кто из двадцати кандидатов" на Polymarket много больше,
    чем честных 50/50, и у каждого девятнадцать исходов — дешёвые
    аутсайдеры. При равномерном обходе середина шкалы набиралась бы
    месяцы.

    Расслоение оценку не портит: калибровка считается ВНУТРИ полосы, и
    квота на слой на это не влияет. Чего теперь нельзя — складывать
    полосы в одно число без весов.
    """

    def _pick(self, raw, batch=8):
        class C:
            price_sample_batch = batch
            price_sample_max_days = 30
        return PriceSampler(FakeStorage(), C())._pick(raw)

    def _m(self, i, price):
        return gamma_market(
            slug=f"m{i}", bestBid=price - 0.005, bestAsk=price + 0.005,
            clobTokenIds=json.dumps([f"y{i}", f"n{i}"]))

    def test_слой_считается_от_расстояния_до_определённости(self):
        assert abs(balance_of(self._m(0, 0.02)) - 0.02) < 1e-9
        assert abs(balance_of(self._m(0, 0.98)) - 0.02) < 1e-9
        assert abs(balance_of(self._m(0, 0.50)) - 0.50) < 1e-9

    def test_серединные_рынки_не_тонут_среди_аутсайдеров(self):
        """Главное свойство. 40 дешёвых и 4 серединных — без квоты
        серединные не попали бы в выборку вовсе."""
        raw = [self._m(i, 0.02) for i in range(40)]
        raw += [self._m(100 + i, 0.50) for i in range(4)]
        picked = self._pick(raw)
        middles = [p for p in picked if p["balance"] > 0.35]
        assert len(middles) == 4, "серединные рынки утонули"

    def test_квота_ограничивает_крайний_слой(self):
        raw = [self._m(i, 0.02) for i in range(40)]
        picked = self._pick(raw, batch=8)
        cheap = [p for p in picked if p["balance"] < 0.05]
        # Квота = batch // 4 = 2; остальное добирается из излишка, но
        # именно по квоте в слой попадают первые два.
        assert 2 <= len(cheap) <= 8

    def test_недобор_слоя_не_оставляет_проход_пустым(self):
        """На странице может не оказаться рынков нужной цены — тогда
        добираем чем есть, иначе час уйдёт впустую."""
        raw = [self._m(i, 0.02) for i in range(10)]
        assert len(self._pick(raw, batch=8)) == 8

    def test_рынок_без_цены_в_списке_берётся_последним(self):
        """Цена из списка нужна до того, как тратить запрос на книгу.
        Если её нет, слой неизвестен — такой рынок не должен съедать
        чужую квоту."""
        raw = [gamma_market(slug="без-цены",
                            clobTokenIds=json.dumps(["yX", "nX"]))]
        raw += [self._m(i, 0.50) for i in range(4)]
        picked = self._pick(raw, batch=4)
        assert all(p["slug"] != "без-цены" for p in picked)

    def test_все_слои_представлены(self):
        raw = []
        for i, price in enumerate((0.02, 0.10, 0.28, 0.48)):
            raw += [self._m(i * 10 + j, price) for j in range(5)]
        picked = self._pick(raw, batch=8)
        strata = {stratum_of(p["balance"]) for p in picked}
        assert len(strata) == 4, f"представлены только слои {strata}"
