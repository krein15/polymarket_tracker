"""Признаки минуты 0 и модель, которая по ним предсказывает погоню.

Зачем это вообще
----------------
Весь перевес проекта сидит в одной метке. На 38 137 закрывшихся покупок,
по цене самого трейдера:

    все покупки                   +0.7 пп   ROI  +0.2%
    рынок пошёл за ним (>= +15%) +25.4 пп   ROI +51.6%
    рынок пошёл против           -31.0 пп   ROI -57.8%

Метка приходит через 20 минут наблюдения и на 29 минут позже сделки, а
цена к тому времени уходит на +59% медианы. Поэтому задача — предсказать
её в минуту 0: тогда вход идёт по цене трейдера с переплатой ~2%.
"""
from __future__ import annotations

import math

from conftest import NOW

from tools.chase_features import features, wilson
from tools.chase_model import auc, fit, predict, standardize

WINDOW = 1800
TOK = "tok-1"
HERO = "0x" + "a" * 40


class Row(dict):
    """Строка shadow_trades: sqlite3.Row умеет только чтение по ключу."""

    def __getitem__(self, k):
        return dict.get(self, k)


def shadow(price=0.50, usdc=5000.0, ts=NOW, maker=HERO, chase=0.2):
    return Row(id=1, ts=ts, maker=maker, token_id=TOK, usdc_amount=usdc,
               price=price, market_slug="m", category="sports",
               volume_24h=10000.0, chase=chase, chase_money=5000.0,
               score=50.0, market_resolved=0, roi_if_followed=None,
               trader_was_right=None)


def trade(storage, maker, price, usdc, ts, token=TOK, side="buy"):
    storage.save_trade(
        tx_hash=f"0x{maker[-6:]}{ts}{int(usdc)}{int(price*100)}", log_index=0,
        ts=ts, block_number=0, maker=maker, token_id=token, side=side,
        usdc_amount=usdc, price=price, condition_id="0xc")


class TestПризнакиМинутыНоль:
    def test_удар_по_цене_считается_от_медианы(self, storage):
        for i in range(5):
            trade(storage, "0x" + "b" * 40, 0.40, 1000.0, NOW - 600 - i)
        storage.flush()
        d = features(storage._conn().__enter__(), shadow(price=0.50))
        assert abs(d["impact"] - 0.25) < 1e-9

    def test_без_истории_удар_неизвестен(self, storage):
        """Пустое окно — это не "удара не было", а "не знаем". Ноль здесь
        соврал бы модели."""
        d = features(storage._conn().__enter__(), shadow())
        assert d["impact"] is None

    def test_окно_ограничено_получасом(self, storage):
        """Сделка часовой давности не должна задавать опорную цену."""
        trade(storage, "0x" + "b" * 40, 0.10, 1000.0, NOW - 3600)
        trade(storage, "0x" + "b" * 40, 0.40, 1000.0, NOW - 600)
        storage.flush()
        d = features(storage._conn().__enter__(), shadow(price=0.50))
        assert abs(d["impact"] - 0.25) < 1e-9
        assert d["recent_trades"] == 1

    def test_будущее_не_подглядывается(self, storage):
        """Признак, увидевший сделки ПОСЛЕ момента, обесценил бы всё."""
        trade(storage, "0x" + "b" * 40, 0.40, 1000.0, NOW - 600)
        trade(storage, "0x" + "c" * 40, 0.90, 9000.0, NOW + 60)
        storage.flush()
        d = features(storage._conn().__enter__(), shadow(price=0.50))
        assert d["recent_trades"] == 1
        assert d["recent_flow"] == 1000.0

    def test_первый_раз_в_рынке(self, storage):
        trade(storage, "0x" + "b" * 40, 0.40, 1000.0, NOW - 600)
        storage.flush()
        assert features(storage._conn().__enter__(), shadow())["first_in_token"] == 1
        trade(storage, HERO, 0.40, 1000.0, NOW - 900)
        storage.flush()
        assert features(storage._conn().__enter__(), shadow())["first_in_token"] == 0

    def test_метка_в_абсолютном_сдвиге(self, storage):
        """chase хранится относительным, а решает абсолютный сдвиг: при
        цене 0.90 рост на +15% упирается в единицу."""
        d = features(storage._conn().__enter__(), shadow(price=0.90, chase=0.10))
        assert abs(d["shift"] - 0.09) < 1e-9
        assert d["pos"] == 1
        d = features(storage._conn().__enter__(), shadow(price=0.20, chase=0.20))
        assert abs(d["shift"] - 0.04) < 1e-9
        assert d["pos"] == 0, "мелкий сдвиг в дешёвом рынке принят за погоню"

    def test_размер_относительно_потока(self, storage):
        trade(storage, "0x" + "b" * 40, 0.40, 15000.0, NOW - 600)
        storage.flush()
        d = features(storage._conn().__enter__(), shadow(usdc=5000.0))
        assert abs(d["size_vs_flow"] - 5000.0 / 15000.0) < 1e-9


class TestWilson:
    def test_ноль_наблюдений_не_роняет(self):
        assert wilson(0, 0) == (0.0, 0.0)

    def test_интервал_накрывает_долю(self):
        lo, hi = wilson(50, 100)
        assert lo < 0.5 < hi

    def test_на_малой_группе_не_схлопывается(self):
        """Обычная формула даёт [1.0; 1.0] и признак кажется идеальным."""
        lo, hi = wilson(5, 5)
        assert lo < 0.95 and hi <= 1.0


class TestAUC:
    def test_идеальное_разделение(self):
        assert auc([0, 0, 1, 1], [0.1, 0.2, 0.8, 0.9]) == 1.0

    def test_обратный_порядок(self):
        assert auc([1, 1, 0, 0], [0.1, 0.2, 0.8, 0.9]) == 0.0

    def test_совпадающие_оценки_это_монетка(self):
        assert auc([0, 1, 0, 1], [0.5] * 4) == 0.5

    def test_один_класс_не_определён(self):
        assert math.isnan(auc([1, 1, 1], [0.1, 0.5, 0.9]))


class TestСтандартизация:
    def test_среднее_ноль_разброс_единица(self):
        X = [[1.0], [3.0], [5.0]]
        Z, _ = standardize(X)
        col = [r[0] for r in Z]
        assert abs(sum(col) / 3) < 1e-12
        assert abs(math.sqrt(sum(x * x for x in col) / 2) - 1.0) < 1e-12

    def test_проверочная_половина_по_статистике_обучения(self):
        """Считать среднее по проверке — значит подглядеть в неё."""
        Xtr, st = standardize([[0.0], [2.0]])
        Xte, _ = standardize([[4.0]], st)
        assert Xte[0][0] > Xtr[1][0]

    def test_постоянный_признак_не_делит_на_ноль(self):
        Z, _ = standardize([[7.0], [7.0], [7.0]])
        assert all(r[0] == 0.0 for r in Z)


class TestОбучение:
    def test_учится_на_разделимых_данных(self):
        X = [[-2.0], [-1.0], [1.0], [2.0]] * 10
        y = [0, 0, 1, 1] * 10
        w, b = fit(X, y, epochs=400, lr=1.0)
        p = predict(X, w, b)
        assert p[0] < 0.5 < p[2]
        assert auc(y, p) == 1.0

    def test_на_шуме_не_выдумывает_порядок(self):
        """Признак без связи с меткой должен давать AUC около монетки."""
        X = [[float(i % 3)] for i in range(90)]
        y = [(i // 45) for i in range(90)]
        w, b = fit(X, y, epochs=200)
        assert 0.4 < auc(y, predict(X, w, b)) < 0.6
