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
PRICE_COLUMNS = ("mid", "best_ask", "fill_200", "fill_500",
                 "fill_2000")
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


def load(db, price_col, category=None, max_days=None, two_sided=True):
    c = sqlite3.connect("file:" + db + "?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    try:
        rows = c.execute(
            "SELECT ts, market_slug, outcome, mid, best_bid, best_ask, "
            "       fill_200, fill_500, fill_2000, depth_usdc, volume_24h, "
            "       liquidity, "
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
        # Полоса — всегда по ОЦЕНКЕ рынка, а не по цене исполнения.
        # Иначе аутсайдер по 0.01, чей ордер на $2000 наливается по 0.44,
        # попадает в полосу "0.40-0.50" и портит её винрейт. Именно так и
        # вышло: таблица показывала 4% побед в полосе "0.60-0.70".
        # Только двусторонний стакан. Односторонний — это не цена, а
        # зависшая заявка: из 7 "фаворитов по 0.95+", которые проиграли,
        # все семь имели ликвидность $1-4 и одну сторону книги. Вместе
        # они давали отклонение z = -5.9 на 777 рынках, тогда как на
        # 1215 рынках с живой книгой отклонения нет вовсе (z = -1.0).
        #
        # Исключение не подгонка: у рынка с одной заявкой в книге просто
        # нет цены, о калибровке которой можно спрашивать. Строки эти
        # по-прежнему собираются, и ключ --one-sided их показывает.
        band = r["mid"] if two_sided else (
            r["mid"] if r["mid"] is not None else r["best_ask"])
        paid = r[price_col]
        if band is None or not (0.0 < band < 1.0):
            continue
        if two_sided and r["mid"] is None:
            continue
        if paid is None or not (0.0 < paid < 1.0):
            continue
        if category and (r["category"] or "") != category:
            continue
        if max_days and r["hours_to_resolve"] and r["hours_to_resolve"] > max_days * 24:
            continue
        d = dict(r)
        d["price"] = band
        d["paid"] = paid
        d["win"] = 1 if r["settled_price"] >= 0.5 else 0
        d["roi"] = (r["settled_price"] - paid) / paid
        out.append(d)
    return out, total


def table(rows, title):
    print()
    print("=== " + title + " ===")
    print("  {:<12}{:>6}{:>8}{:>8}{:>9}{:>17}{:>10}{:>18}".format(
        "оценка", "n", "оценка", "платим", "винрейт", "интервал винрейта",
        "ROI", "интервал ROI"))
    for lo, hi in zip(BANDS, BANDS[1:]):
        sub = [d for d in rows if lo <= d["price"] < hi]
        n = len(sub)
        label = "{:.2f}-{:.2f}".format(lo, hi)
        if n < MIN_BAND:
            print("  {:<12}{:>6}   мало".format(label, n))
            continue
        k = sum(d["win"] for d in sub)
        p = sum(d["price"] for d in sub) / n
        paid = sum(d["paid"] for d in sub) / n
        wlo, whi = wilson(k, n)
        m, rlo, rhi = mean_ci([d["roi"] for d in sub])
        # Интервал ROI на дешёвых полосах не заслуживает доверия: один
        # выигрыш по цене 0.01 даёт +9900%, и нормальное приближение
        # уезжает ниже -100%, чего не бывает. Там смотреть надо на
        # винрейт против оценки, а не на ROI.
        shaky = rlo < -1.0
        mark = "" if shaky else ("  <-" if rlo > 0 or rhi < 0 else "")
        ci = ("       разброс" if shaky
              else "[{:>+6.1f};{:>+6.1f}]".format(rlo * 100, rhi * 100))
        print("  {:<12}{:>6}{:>7.1f}%{:>7.1f}%{:>8.1f}%   [{:>5.1f};{:>5.1f}]"
              "{:>+9.1f}%   {}{}"
              .format(label, n, p * 100, paid * 100, k / n * 100,
                      wlo * 100, whi * 100, m * 100, ci, mark))


def main():
    ap = argparse.ArgumentParser(description="Калибровка цен рынка.")
    ap.add_argument("--db", default=DB)
    ap.add_argument("--price", choices=PRICE_COLUMNS, default=None,
                    help="по какой цене считать; без него — все три")
    ap.add_argument("--category", default=None)
    ap.add_argument("--max-days", type=float, default=None,
                    help="только рынки, закрывшиеся быстрее N дней")
    ap.add_argument("--one-sided", action="store_true",
                    help="включить рынки с односторонним стаканом "
                         "(у них нет цены, см. комментарий в коде)")
    a = ap.parse_args()

    cols = [a.price] if a.price else ["mid", "best_ask", "fill_2000"]
    first = True
    for col in cols:
        rows, total = load(a.db, col, a.category, a.max_days,
                           two_sided=not a.one_sided)
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
            print("Полоса — по ОЦЕНКЕ рынка (середина стакана), "
                  "колонка \"платим\" — цена исполнения.")
            print("Рынок точен, если винрейт равен оценке. "
                  "Стрелка справа = интервал ROI не накрывает ноль.")
            first = False
        name = {"mid": "по середине стакана (есть ли смещение)",
                "best_ask": "по лучшему аску (первая сотня долларов)",
                "fill_200": "по цене ордера $200",
                "fill_500": "по цене ордера $500",
                "fill_2000": "по цене ордера $2000"}[col]
        table(rows, name)

    rows, _ = load(a.db, cols[-1], a.category, a.max_days,
                   two_sided=not a.one_sided)
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
