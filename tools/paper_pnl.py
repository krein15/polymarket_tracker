#!/usr/bin/env python3
"""Бумажная прибыль по НАШЕЙ цене входа, а не по цене трейдера.

Зачем отдельный отчёт
---------------------
`roi_if_followed` в базе считается от цены трейдера. Но сигнал приходит
после того, как рынок за ним пошёл, и войти по его цене нельзя. Пересчёт на
цену последователей показал, чего стоит разница (507 сделок с исходом):

    от его цены                 ROI +44.4%
    по цене последователей      ROI  +4.2%   [-1.7; +10.1]

Этот отчёт считает по замеру `signal_entries` — цене, по которой ордер
реально налился бы через ENTRY_DELAY_SEC после отправки сигнала. Никаких
допущений: чего в стакане не было, того не покупаем.

Когда числам можно верить
-------------------------
Смотреть надо на нижнюю границу доверительного интервала, а не на среднее.
При наблюдаемом разбросе, чтобы отличить +5% от нуля, нужно порядка 700
сделок. На сотне интервал шире самого эффекта, и любое среднее там —
случайность.

Запуск:
    python tools/paper_pnl.py
    python tools/paper_pnl.py --size 2000 --type chase
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

SIZE_COLUMNS = {500: "fill_500", 2000: "fill_2000", 5000: "fill_5000"}


def mean_ci(values: list) -> tuple:
    """Среднее и его 95% интервал. Для денег важно именно среднее: медиана
    красива, но не она складывается в итог по счёту."""
    n = len(values)
    mean = sum(values) / n
    if n < 2:
        return mean, mean, mean
    var = sum((x - mean) ** 2 for x in values) / (n - 1)
    se = math.sqrt(var / n)
    return mean, mean - 1.96 * se, mean + 1.96 * se


def report(db: Path, size: int, signal_type: str) -> int:
    col = SIZE_COLUMNS[size]
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    try:
        rows = c.execute(
            f"""
            SELECT s.signal_type, s.price AS his_price, e.{col} AS entry,
                   e.best_ask, e.delay_sec, o.settled_price, o.market_resolved
            FROM signals s
            JOIN signal_entries e ON e.signal_id = s.id
            LEFT JOIN signal_outcomes o ON o.signal_id = s.id
            WHERE (? = 'all' OR s.signal_type = ?)
            """,
            (signal_type, signal_type),
        ).fetchall()
    except sqlite3.OperationalError:
        print("Таблицы signal_entries ещё нет — трекер с этим замером не работал.")
        return 1
    c.close()

    if not rows:
        print("Замеров цены входа пока нет. Нужен перезапуск и время на накопление.")
        return 0

    measured = len(rows)
    thin = sum(1 for r in rows if r["entry"] is None)
    resolved = [r for r in rows
                if r["market_resolved"] and r["settled_price"] is not None
                and r["entry"] is not None]

    print(f"Замеров цены входа: {measured}")
    print(f"  стакан не тянул ${size}: {thin} "
          f"({thin / measured * 100:.0f}%) — такие сделки не открылись бы")
    print(f"  дождались резолва: {len(resolved)}")
    if not resolved:
        print("\nПока нечего считать: ни один замеренный сигнал не закрылся.")
        return 0

    ours = [(r["settled_price"] - r["entry"]) / r["entry"] for r in resolved]
    theirs = [(r["settled_price"] - r["his_price"]) / r["his_price"] for r in resolved]
    slip = [(r["entry"] - r["his_price"]) / r["his_price"] for r in resolved]

    m, lo, hi = mean_ci(ours)
    tm, _, _ = mean_ci(theirs)
    sm, _, _ = mean_ci(slip)
    wins = sum(1 for r in resolved if r["settled_price"] > r["entry"])
    n = len(resolved)
    med = sorted(ours)[n // 2]

    print()
    print(f"=== Ордер ${size}, тип сигнала: {signal_type} ===")
    print(f"  прибыльных: {wins}/{n} ({wins / n * 100:.1f}%)")
    print(f"  ROI по НАШЕЙ цене:   {m * 100:+.1f}%   "
          f"[{lo * 100:+.1f}; {hi * 100:+.1f}]   медиана {med * 100:+.1f}%")
    print(f"  ROI по цене трейдера:{tm * 100:+.1f}%   <- так считалось раньше")
    print(f"  переплата к нему:    {sm * 100:+.1f}%")
    print()
    if lo > 0:
        print(f"  Нижняя граница выше нуля при n={n}.")
    else:
        need = 0
        if n >= 2:
            _, l2, h2 = mean_ci(ours)
            se = (h2 - l2) / (2 * 1.96)
            if m > 0 and se > 0:
                need = int((1.96 * se * math.sqrt(n) / m) ** 2) + 1
        print(f"  Нижняя граница НЕ выше нуля: перевес от нуля неотличим.")
        if need and need > n:
            print(f"  При таком же разбросе для уверенности нужно около {need} сделок.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Бумажная прибыль по нашей цене входа.")
    p.add_argument("--db", default=str(ROOT / "data" / "tracker.db"))
    p.add_argument("--size", type=int, choices=sorted(SIZE_COLUMNS), default=2000)
    p.add_argument("--type", default="all",
                   help="score | chase | onchain_early | whitelist | all")
    a = p.parse_args()
    return report(Path(a.db), a.size, a.type)


if __name__ == "__main__":
    sys.exit(main())
