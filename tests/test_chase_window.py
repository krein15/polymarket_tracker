"""Пересчёт погони на разных окнах наблюдения.

Зачем инструмент появился
-------------------------
Метка погони делит сделки надёжно: за которыми рынок пошёл — перевес
+20.2 пп, против которых — −25.5 пп. И знание там настоящее: даже по цене
последователей (VWAP 0.737 против его 0.603) остаётся +6.9 пп на 8 938
сделках.

Но метка складывается за 20 минут, сигнал уходит через 29 минут медианы,
и наша цена оказывается на 37–56% выше его. Отсюда вопрос: а если
смотреть пять минут вместо двадцати? Метка станет шумнее, но и цена не
успеет уйти.

Ответ оказался отрицательным: на старой половине окно 10 минут давало
+15.2% [+4.0; +26.4] по цене на конце окна, на свежей — +2.1%
[−6.3; +10.5]. Не воспроизводится.

Логику окна всё равно надо держать проверенной: на ней построен вывод,
которым закрывается всё направление.
"""
from __future__ import annotations

import sqlite3

from conftest import NOW

from tools.chase_window import MIN_END_PRICE, WINDOWS_MIN, mean_ci, windows_for

HERO = "0x" + "a" * 40
OTHER = "0x" + "b" * 40
TOK = "tok-1"


def db(trades):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE trades (ts INTEGER, maker TEXT, token_id TEXT, "
              "side TEXT, price REAL, usdc_amount REAL)")
    c.executemany("INSERT INTO trades VALUES (?,?,?,?,?,?)", trades)
    return c


def cand(price=0.40, ts=NOW, maker=HERO):
    return {"ts": ts, "price": price, "maker": maker, "token_id": TOK}


class TestОкно:
    def test_поток_считается_по_своему_окну(self):
        c = db([
            (NOW + 60, OTHER, TOK, "buy", 0.50, 1000.0),      # в 5 мин
            (NOW + 600, OTHER, TOK, "buy", 0.60, 2000.0),     # в 10 мин
            (NOW + 1500, OTHER, TOK, "buy", 0.70, 4000.0),    # в 30 мин
        ])
        w = windows_for(c, cand())
        assert w[5]["money"] == 1000.0
        assert w[10]["money"] == 3000.0
        assert w[30]["money"] == 7000.0

    def test_своя_сделка_в_поток_не_идёт(self):
        """Иначе трейдер гонится сам за собой."""
        c = db([(NOW + 60, HERO, TOK, "buy", 0.90, 9000.0)])
        assert windows_for(c, cand())[5]["money"] == 0.0

    def test_продажи_в_поток_не_идут(self):
        c = db([(NOW + 60, OTHER, TOK, "sell", 0.90, 9000.0)])
        assert windows_for(c, cand())[5]["money"] == 0.0

    def test_сдвиг_абсолютный(self):
        """Цены живут в (0,1], и процент к цене недостижим на дорогих
        рынках — на этом ветка погони однажды ослепла на 30% рынков."""
        c = db([(NOW + 60, OTHER, TOK, "buy", 0.50, 1000.0)])
        w = windows_for(c, cand(price=0.40))
        assert abs(w[5]["shift"] - 0.10) < 1e-9

    def test_vwap_взвешен_по_долям(self):
        c = db([
            (NOW + 60, OTHER, TOK, "buy", 0.50, 1000.0),   # 2000 долей
            (NOW + 120, OTHER, TOK, "buy", 1.00, 1000.0),  # 1000 долей
        ])
        w = windows_for(c, cand())
        assert abs(w[5]["vwap"] - 2000.0 / 3000.0) < 1e-9

    def test_цена_на_конце_это_последняя_сделка_окна(self):
        """Главная колонка отчёта: по ней мы могли бы войти."""
        c = db([
            (NOW + 60, OTHER, TOK, "buy", 0.50, 100.0),
            (NOW + 290, OTHER, TOK, "buy", 0.55, 100.0),
            (NOW + 400, OTHER, TOK, "buy", 0.80, 100.0),    # уже вне 5 мин
        ])
        w = windows_for(c, cand())
        assert w[5]["end_price"] == 0.55
        assert w[10]["end_price"] == 0.80

    def test_цена_на_конце_учитывает_и_свои_и_продажи(self):
        """Поток последователей считается только по чужим покупкам, а
        цена на рынке — по всем сделкам: купить мы будем по рыночной."""
        c = db([
            (NOW + 60, OTHER, TOK, "buy", 0.50, 100.0),
            (NOW + 120, HERO, TOK, "sell", 0.62, 100.0),
        ])
        w = windows_for(c, cand())
        assert w[5]["end_price"] == 0.62
        assert w[5]["money"] == 100.0

    def test_без_сделок_цена_остаётся_его(self):
        """Пустое окно — не повод выдумать цену."""
        w = windows_for(db([]), cand(price=0.40))
        assert w[5]["end_price"] == 0.40
        assert w[5]["vwap"] is None and w[5]["shift"] is None

    def test_сделки_до_кандидата_не_считаются(self):
        """Признак, заглянувший в прошлое, — это не погоня."""
        c = db([(NOW - 60, OTHER, TOK, "buy", 0.90, 9000.0)])
        assert windows_for(c, cand())[5]["money"] == 0.0

    def test_все_окна_возвращаются(self):
        w = windows_for(db([]), cand())
        assert sorted(w) == sorted(WINDOWS_MIN)


class TestСреднее:
    def test_интервал_накрывает_среднее(self):
        m, lo, hi = mean_ci([0.1, 0.2, 0.3, 0.4])
        assert lo < m < hi

    def test_одно_наблюдение_не_роняет(self):
        assert mean_ci([0.5]) == (0.5, 0.5, 0.5)


class TestПорогЦены:
    def test_порог_отсекает_копеечные_цены(self):
        """Деление на 0.01 давало ROI в сотни процентов от одного исхода:
        на окне 20 минут без фильтра по деньгам вышло +217.8% с
        интервалом [-77.9; +513.5]. Среднее там ничего не значит."""
        assert MIN_END_PRICE >= 0.02
