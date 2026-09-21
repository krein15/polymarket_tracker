#!/usr/bin/env python3
"""Ошибается ли рынок систематически — по снимкам цены, а не по сделкам.

Вопрос
------
У предсказательных рынков известно смещение: фавориты недооценены,
аутсайдеры переоценены. На 38 669 теневых ПОКУПОК с исходом оно видно:

    цена 0.10-0.20   винрейт  8.4%   перевес -6.7 пп   ROI -44.4%
    цена 0.60-0.70   винрейт 68.3%   перевес +3.9 пп   ROI  +5.9%

Но там выборка условна на том, что кто-то совершил сделку, и часть этих
+5.9% может быть правотой информированных покупателей, а не ошибкой
рынка. Здесь отбора нет: `price_sampler` берёт рынки из списка подряд.

Три цены, три разных вопроса
----------------------------
    mid        ошибается ли рынок               (есть ли смещение вообще)
    best_ask   переживёт ли перевес спред       (первая сотня долларов)
    fill_2000  переживёт ли он обход стакана    (реальный ордер)

Смешивать их в одном числе нельзя. Смещение может быть настоящим и при
этом неторгуемым — это разные выводы, и решение принимается по третьему.

Почему смотреть надо на нижнюю границу
--------------------------------------
Ожидаемый эффект здесь единицы процентов, а разброс исходов бинарный.
Среднее по сотне наблюдений ничего не значит: интервал шире эффекта.
Решение принимается, когда нижняя граница выше нуля.

Запуск:
    python tools\\calibration.py
    python tools\\calibration.py --price fill_2000 --category sports
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
PRICE_COLUMNS = ("mid", "best_ask", "fill_2000")
BANDS = (0.00, 0.05, 0.10, 0.20, 0.30, 0.40, 0.50,
         0.60, 0.70, 0.80, 0.90, 0.95, 1.00)
MIN_BAND = 40


def wilson(k, n, z=1.96):
    """Интервал для доли. На малых группах обычная формула даёт нули и
    единицы, из-за которых полоса кажется идеальной."""
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    s = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (c - s) / d), min(1.0, (c + s) / d)


def mean_ci(values):
    n = len(values)
    m = sum(values) / n
    if n < 2:
        return m, m, m
    se = math.sqrt(sum((x - m) ** 2 for x in values) / (n - 1) / n)
    return m, m - 1.96 * se, m + 1.96 * se


def load(db, price_col, category=None, max_days=None):
    c = sqlite3.connect("file:" + db + "?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    try:
        rows = c.execute(
            "SELECT ts, market_slug, outcome, mid, best_bid, best_ask, "
            "       fill_2000, depth_usdc, volume_24h, liquidity, "
            "       end_date_ts, category, settled_price, won, hours_to_resolve "
            "FROM price_samples WHERE market_resolved = 1 "
            "  AND settled_price IS NOT NULL"
        ).fetchall()
        total = c.execute("SELECT COUNT(*) FROM price_samples").fetchone()[0]
    except sqlite3.OperationalError:
        print("Таблицы price_samples ещё нет — трекер с этим замером не работал.")
        return None, 0
    finally:
        c.close()

    out = []
    for r in rows:
        price = r[price_col]
        if price is None or not (0.0 < price < 1.0):
            continue
        if category and (r["category"] or "") != category:
            continue
        if max_days and r["hours_to_resolve"] and r["hours_to_resolve"] > max_days * 24:
            continue
        d = dict(r)
        d["price"] = price
        d["win"] = 1 if r["settled_price"] >= 0.5 else 0
        d["roi"] = (r["settled_price"] - price) / price
        out.append(d)
    return out, total


def table(rows, title):
    print()
    print("=== " + title + " ===")
    print("  {:<12}{:>6}{:>9}{:>9}{:>17}{:>10}{:>18}".format(
        "цена", "n", "цена", "винрейт", "интервал винрейта", "ROI",
        "интервал ROI"))
    for lo, hi in zip(BANDS, BANDS[1:]):
        sub = [d for d in rows if lo <= d["price"] < hi]
        n = len(sub)
        label = "{:.2f}-{:.2f}".format(lo, hi)
        if n < MIN_BAND:
            print("  {:<12}{:>6}   мало".format(label, n))
            continue
        k = sum(d["win"] for d in sub)
        p = sum(d["price"] for d in sub) / n
        wlo, whi = wilson(k, n)
        m, rlo, rhi = mean_ci([d["roi"] for d in sub])
        mark = "  <-" if rlo > 0 or rhi < 0 else ""
        print("  {:<12}{:>6}{:>8.1f}%{:>8.1f}%   [{:>5.1f};{:>5.1f}]"
              "{:>+9.1f}%   [{:>+6.1f};{:>+6.1f}]{}"
              .format(label, n, p * 100, k / n * 100, wlo * 100, whi * 100,
                      m * 100, rlo * 100, rhi * 100, mark))


def main():
    ap = argparse.ArgumentParser(description="Калибровка цен рынка.")
    ap.add_argument("--db", default=DB)
    ap.add_argument("--price", choices=PRICE_COLUMNS, default=None,
                    help="по какой цене считать; без него — все три")
    ap.add_argument("--category", default=None)
    ap.add_argument("--max-days", type=float, default=None,
                    help="только рынки, закрывшиеся быстрее N дней")
    a = ap.parse_args()

    cols = [a.price] if a.price else list(PRICE_COLUMNS)
    first = True
    for col in cols:
        rows, total = load(a.db, col, a.category, a.max_days)
        if rows is None:
            return 1
        if first:
            print("снимков всего: {}".format(total))
            if not rows:
                print("Закрывшихся снимков пока нет. Замер идёт примерно "
                      "700 рынков в сутки, первые исходы — в тот же день, "
                      "основная масса — по мере закрытия рынков.")
                return 0
            print("закрывшихся и пригодных: {}".format(len(rows)))
            print()
            print("Стрелка справа = интервал ROI не накрывает ноль.")
            first = False
        name = {"mid": "по середине стакана (есть ли смещение)",
                "best_ask": "по лучшему аску (первая сотня долларов)",
                "fill_2000": "по цене ордера $2000 (реальный вход)"}[col]
        table(rows, name)

    rows, _ = load(a.db, cols[-1], a.category, a.max_days)
    if rows:
        print()
        print("=== по категориям (цена {}) ===".format(cols[-1]))
        by = {}
        for d in rows:
            by.setdefault(d["category"] or "(нет)", []).append(d)
        for k in sorted(by, key=lambda k: -len(by[k]))[:8]:
            sub = by[k]
            if len(sub) < MIN_BAND:
                print("  {:<16}{:>6}   мало".format(k, len(sub)))
                continue
            m, lo, hi = mean_ci([d["roi"] for d in sub])
            print("  {:<16}{:>6}  ROI {:>+7.1f}%  [{:>+6.1f};{:>+6.1f}]"
                  .format(k, len(sub), m * 100, lo * 100, hi * 100))
    return 0


if __name__ == "__main__":
    sys.exit(main())
