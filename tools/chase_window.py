#!/usr/bin/env python3
"""Окно наблюдения за погоней: что теряется быстрее — метка или цена.

Вопрос
------
Метка погони делит сделки надёжно: за которыми рынок пошёл — перевес
+20.2 пп, против которых — −25.5 пп, остальные — ноль. И это не просто
движение цены: даже по цене последователей (VWAP 0.737 против его 0.603)
перевес остаётся +6.9 пп на 8 938 сделках. Знание настоящее.

Но метка складывается за 20 минут наблюдения, сигнал уходит через 29
минут медианы, и наша цена входа оказывается на 37–56% выше его. Это
0.83–0.94 при его 0.60 — далеко за точкой, где перевес ещё был. По
нашей цене погоня даёт −1.2%.

Отсюда единственная проверяемая мысль: а если смотреть пять минут вместо
двадцати? Метка станет шумнее — меньше денег успеет зайти. Но и цена не
успеет уйти. Что потеряется быстрее?

Как считаем
-----------
Для каждой закрывшейся покупки заново собираем поток последователей на
окнах 5/10/20/30 минут и смотрим три цены:

    его цена        сколько стоило знание тому, кто его имел
    VWAP окна       среднее по движению; купить по нему нельзя,
                    но это верхняя граница достижимого
    цена на конце   последняя сделка к моменту закрытия окна —
                    то, что мы увидели бы в момент сигнала

Решает третья колонка. Первые две — для понимания, куда делся перевес.

Чего эта оценка НЕ учитывает
----------------------------
Цена на конце окна берётся из ленты сделок, а покупать пришлось бы по
аску. По нашим замерам входа разрыв между ними заметный, поэтому любой
плюс в третьей колонке — оптимистичная граница, а не обещание. Если и
она в нуле, вопрос закрыт окончательно.

Запуск:
    python tools\\chase_window.py
"""
from __future__ import annotations

import argparse
import math
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DB = os.path.join(ROOT, "data", "tracker.db")
WINDOWS_MIN = (5, 10, 20, 30)
MAX_WINDOW_SEC = max(WINDOWS_MIN) * 60
POS_SHIFT = 0.05
MIN_GROUP = 40


def mean_ci(values):
    n = len(values)
    m = sum(values) / n
    if n < 2:
        return m, m, m
    se = math.sqrt(sum((x - m) ** 2 for x in values) / (n - 1) / n)
    return m, m - 1.96 * se, m + 1.96 * se


def candidates(conn):
    lo, hi = conn.execute("SELECT MIN(ts), MAX(ts) FROM trades").fetchone()
    return conn.execute(
        "SELECT id, tx_hash, maker, token_id, ts, price, usdc_amount, "
        "       settled_price, market_slug "
        "FROM shadow_trades "
        "WHERE market_resolved = 1 AND settled_price IS NOT NULL "
        "  AND side = 'buy' AND price > 0 AND price < 1 "
        "  AND ts >= ? AND ts <= ? "
        "ORDER BY ts",
        (lo, hi - MAX_WINDOW_SEC),
    ).fetchall()


def windows_for(conn, row):
    """Поток последователей и цена на конце каждого окна."""
    nearby = conn.execute(
        "SELECT ts, maker, side, price, usdc_amount FROM trades "
        "WHERE token_id = ? AND ts > ? AND ts <= ? ORDER BY ts",
        (row["token_id"], row["ts"], row["ts"] + MAX_WINDOW_SEC),
    ).fetchall()
    out = {}
    for w in WINDOWS_MIN:
        edge_ts = row["ts"] + w * 60
        money = shares = 0.0
        last = None
        for t in nearby:
            if t["ts"] > edge_ts:
                break
            last = t["price"]
            if t["maker"] == row["maker"] or t["side"] != "buy":
                continue
            if not t["price"]:
                continue
            money += t["usdc_amount"]
            shares += t["usdc_amount"] / t["price"]
        vwap = (money / shares) if shares > 0 else None
        out[w] = {
            "money": money,
            "vwap": vwap,
            "shift": (vwap - row["price"]) if vwap else None,
            "end_price": last if last else row["price"],
        }
    return out


def build(conn, limit=None, progress=None):
    rows = candidates(conn)
    if limit:
        rows = rows[:limit]
    out = []
    for i, r in enumerate(rows):
        if progress and i and i % 500 == 0:
            progress(i, len(rows))
        d = {"price": r["price"], "settled": r["settled_price"],
             "slug": r["market_slug"], "ts": r["ts"]}
        d["w"] = windows_for(conn, r)
        out.append(d)
    return out


# Цены ниже этой в колонке "на конце окна" не берём. Деление на 0.01
# даёт ROI в сотни процентов от одного исхода, и среднее перестаёт что-либо
# значить: на окне 20 минут без фильтра по деньгам так и вышло — +217.8%
# с интервалом [-77.9; +513.5].
MIN_END_PRICE = 0.05


def report(rows, money_floor, label=""):
    print()
    print("сделок пересчитано: {}{}".format(len(rows), label))
    print("порог потока последователей: ${:,.0f}".format(money_floor))
    print()
    print("Решает колонка «цена на конце окна» — по ней мы могли бы войти.")
    print("  {:<8}{:>8}{:>8}{:>26}{:>26}{:>26}".format(
        "окно", "меток", "в сутки", "по ЕГО цене",
        "по VWAP окна", "по цене на конце"))

    span_days = max((max(r["ts"] for r in rows)
                     - min(r["ts"] for r in rows)) / 86400.0, 0.5)

    for w in WINDOWS_MIN:
        pos = [r for r in rows
               if r["w"][w]["shift"] is not None
               and r["w"][w]["shift"] >= POS_SHIFT
               and r["w"][w]["money"] >= money_floor]
        if len(pos) < MIN_GROUP:
            print("  {:<8}{:>8}   мало".format("{} мин".format(w), len(pos)))
            continue

        def col(pcol):
            vals, wins, be = [], 0, 0.0
            for r in pos:
                p = r["price"] if pcol == "price" else r["w"][w][pcol]
                if not p or not (0 < p < 1):
                    continue
                if pcol == "end_price" and p < MIN_END_PRICE:
                    continue
                vals.append((r["settled"] - p) / p)
                wins += 1 if r["settled"] >= 0.5 else 0
                be += p
            if len(vals) < MIN_GROUP:
                return "мало"
            k = len(vals)
            m, lo, hi = mean_ci(vals)
            return "{:>+5.1f}пп {:>+6.1f}% [{:>+5.1f};{:>+5.1f}]".format(
                (wins / k - be / k) * 100, m * 100, lo * 100, hi * 100)

        print("  {:<8}{:>8}{:>8.1f}{:>26}{:>26}{:>26}".format(
            "{} мин".format(w), len(pos), len(pos) / span_days,
            col("price"), col("vwap"), col("end_price")))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=DB)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--money", type=float, default=25000.0,
                    help="минимальный поток последователей, как в боевом правиле")
    a = ap.parse_args()

    conn = sqlite3.connect("file:" + a.db + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    import time
    t0 = time.time()

    def prog(i, n):
        el = time.time() - t0
        print("    {}/{}, осталось ~{:.0f} с".format(i, n, el / i * (n - i)),
              flush=True)

    print("пересчитываю окна...", flush=True)
    rows = build(conn, limit=a.limit, progress=prog)
    print("готово за {:.0f} с".format(time.time() - t0))
    for floor in (0.0, a.money):
        report(rows, floor)

    # Проверка вне выборки. Красивый убывающий узор — ровно то, на чём
    # уже один раз обожглись: репутация кошелька давала +16.5% на
    # обучении и -59.4% на свежих данных.
    rows.sort(key=lambda r: r["ts"])
    cut = rows[len(rows) // 2]["ts"]
    print()
    print("=" * 72)
    print("ПРОВЕРКА ВНЕ ВЫБОРКИ: та же таблица на двух половинах по времени")
    report([r for r in rows if r["ts"] < cut], a.money, "  (старая половина)")
    report([r for r in rows if r["ts"] >= cut], a.money, "  (СВЕЖАЯ половина)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
