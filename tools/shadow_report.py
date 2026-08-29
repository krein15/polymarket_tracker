#!/usr/bin/env python3
"""Shadow tracker report (TODO 0.3) — измерение false negatives фильтров.

Таблица shadow_trades содержит ВСЕ покупки >= MIN_TRADE_USDC на неликвидных
рынках: и те, что породили боевой сигнал Ветки A (passed_filters=1), и те,
что фильтры Ветки A отбросили (passed_filters=0). У обеих групп
outcome_tracker одинаково подтягивает резолв.

Вопрос отчёта: какой winrate у ОТБРОШЕННЫХ сделок? Если он не ниже, чем у
пропущенных — фильтры Ветки A (размер / категория / объём / новизна / цена)
режут выборку наугад, а не отбирают alpha.

Запуск из корня проекта (зависит только от stdlib, read-only — можно гонять
параллельно с работающим трекером):

    python tools/shadow_report.py
    python tools/shadow_report.py --db путь --by-zone --by-category

Whitelist (Ветка B) в passed_filters НЕ учитывается — отчёт про Ветку A.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

# Пути по умолчанию считаем от КОРНЯ проекта (скрипт лежит в tools/),
# поэтому запускать можно из любой папки.
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = str(ROOT / "data" / "tracker.db")

# Минимум resolved в группе, ниже которого выводы делать рано
# (сквозной принцип TODO — размер выборки решает всё).
MIN_RESOLVED_FOR_VERDICT = 30

# Корзины балла скоринга. Границы совпадают со ступенями весов в scoring.py,
# чтобы по отчёту было видно, где проходит осмысленная граница сигнала.
SCORE_BUCKETS = [(-100.0, 20.0), (20.0, 35.0), (35.0, 50.0),
                 (50.0, 65.0), (65.0, 80.0), (80.0, 1e9)]

# Зоны цены входа — те же границы, что в TODO.
PRICE_ZONES = [
    ("<0.20", 0.0, 0.20),
    ("0.20-0.35", 0.20, 0.35),
    ("0.35-0.50", 0.35, 0.50),
    ("0.50-0.65", 0.50, 0.65),
    ("0.65-0.80", 0.65, 0.80),
    ("0.80-0.95", 0.80, 0.95),
    (">=0.95", 0.95, 1.0001),
]


def wilson_ci(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Доверительный интервал Уилсона для доли (см. TODO 2.2)."""
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z**2 / n
    center = (p + z**2 / (2 * n)) / denom
    margin = z * ((p * (1 - p) / n + z**2 / (4 * n**2)) ** 0.5) / denom
    return (center - margin, center + margin)


def median(values: list) -> "float | None":
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def bucket_stats(rows: list) -> dict:
    """Свести список shadow-строк в метрики.

    Каждая строка — sqlite3.Row с market_resolved / trader_was_right /
    roi_if_followed.
    """
    resolved = [r for r in rows if r["market_resolved"]]
    nres = len(resolved)
    wins = sum(1 for r in resolved if r["trader_was_right"])
    rois = [r["roi_if_followed"] for r in resolved if r["roi_if_followed"] is not None]
    lo, hi = wilson_ci(wins, nres) if nres else (None, None)
    return {
        "n": len(rows),
        "resolved": nres,
        "wins": wins,
        "winrate": (wins / nres) if nres else None,
        "ci_lo": lo,
        "ci_hi": hi,
        "mean_roi": (sum(rois) / len(rois)) if rois else None,
        "median_roi": median(rois),
    }


def fmt_stats(label: str, s: dict) -> str:
    if s["resolved"] == 0:
        return f"  {label:<24} n={s['n']:<6} resolved=0 — нет данных"
    wr = f"{s['winrate'] * 100:.1f}%"
    ci = f"[{s['ci_lo'] * 100:.0f}%, {s['ci_hi'] * 100:.0f}%]"
    mean = f"{s['mean_roi'] * 100:+.1f}%" if s["mean_roi"] is not None else "—"
    med = f"{s['median_roi'] * 100:+.1f}%" if s["median_roi"] is not None else "—"
    return (
        f"  {label:<24} n={s['n']:<6} res={s['resolved']:<5} "
        f"wr={wr:<7} CI {ci:<15} mean ROI {mean:<9} median ROI {med}"
    )


def load_rows(conn: sqlite3.Connection) -> list:
    return conn.execute(
        "SELECT price, passed_filters, category, market_resolved, "
        "trader_was_right, roi_if_followed, score, score_parts FROM shadow_trades"
    ).fetchall()


def verdict(passed: dict, rejected: dict) -> str:
    """Сформулировать аккуратный вердикт по сравнению групп."""
    if passed["resolved"] < MIN_RESOLVED_FOR_VERDICT or \
       rejected["resolved"] < MIN_RESOLVED_FOR_VERDICT:
        return ("Выборка мала (<%d resolved хотя бы в одной группе) — выводы "
                "делать рано. Просто продолжаем копить." % MIN_RESOLVED_FOR_VERDICT)
    p_wr, r_wr = passed["winrate"], rejected["winrate"]
    if rejected["ci_lo"] > p_wr:
        return ("⚠ Отброшенные сделки статистически НЕ ХУЖЕ пропущенных "
                "(нижняя граница CI отброшенных выше точечного winrate "
                "пропущенных). Фильтры Ветки A, похоже, режут выборку наугад — "
                "кандидат на пересмотр.")
    if passed["ci_lo"] > rejected["ci_hi"]:
        return ("Фильтры Ветки A отбирают сделки с более высоким winrate — "
                "интервалы Уилсона не пересекаются. Похоже, фильтры работают.")
    return ("Интервалы пропущенных и отброшенных пересекаются — преимущество "
            "фильтров статистически не доказано. Нужно больше resolved-данных.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Shadow tracker report (TODO 0.3).")
    parser.add_argument("--db", default=DEFAULT_DB,
                        help="путь к БД (default: data/tracker.db)")
    parser.add_argument("--by-zone", action="store_true",
                        help="разбивка отброшенных сделок по зонам цены входа")
    parser.add_argument("--by-category", action="store_true",
                        help="разбивка отброшенных сделок по категориям")
    parser.add_argument("--by-score", action="store_true",
                        help="разбивка по баллу скоринга — по ней калибруется SCORE_THRESHOLD")
    parser.add_argument("--by-feature", action="store_true",
                        help="winrate по наличию каждого признака: кто несёт alpha, кто шумит")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"✘ БД не найдена: {db_path}")
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        has_table = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='shadow_trades'"
        ).fetchone()
        if not has_table:
            print("✘ Таблицы shadow_trades нет — обнови storage.py и перезапусти трекер.")
            return 1

        rows = load_rows(conn)
    finally:
        conn.close()

    print("═══ Shadow tracker report (TODO 0.3) ═══")
    print(f"БД: {db_path}")

    total = len(rows)
    nres = sum(1 for r in rows if r["market_resolved"])
    print(f"Всего shadow-сделок: {total}  (resolved: {nres})")

    if total == 0:
        print()
        print("Пусто — shadow tracker ещё не накопил данные. Это нормально:")
        print("строки появляются по мере работы трекера. Загляни через пару дней.")
        return 0

    passed_rows = [r for r in rows if r["passed_filters"]]
    rejected_rows = [r for r in rows if not r["passed_filters"]]
    print(f"  прошли фильтры Ветки A (passed_filters=1): {len(passed_rows)}")
    print(f"  отброшены фильтрами        (passed_filters=0): {len(rejected_rows)}")

    passed = bucket_stats(passed_rows)
    rejected = bucket_stats(rejected_rows)

    print()
    print("─── Сравнение: пропущенные vs отброшенные ───")
    print(fmt_stats("✅ ПРОПУЩЕНЫ (signal)", passed))
    print(fmt_stats("✗ ОТБРОШЕНЫ (rejected)", rejected))

    print()
    print("─── Вердикт ───")
    print("  " + verdict(passed, rejected))

    if args.by_zone:
        print()
        print("─── Отброшенные по зонам цены входа ───")
        resolved_rejected = [r for r in rejected_rows if r["market_resolved"]]
        for label, lo, hi in PRICE_ZONES:
            zone_rows = [r for r in rejected_rows if lo <= r["price"] < hi]
            print(fmt_stats(label, bucket_stats(zone_rows)))
        if not resolved_rejected:
            print("  (resolved-данных по отброшенным пока нет)")

    if args.by_category:
        print()
        print("─── Отброшенные по категориям ───")
        cats: dict = {}
        for r in rejected_rows:
            cats.setdefault(r["category"] or "(пусто)", []).append(r)
        for cat, crows in sorted(cats.items(), key=lambda kv: -len(kv[1])):
            print(fmt_stats(cat, bucket_stats(crows)))

    if args.by_score:
        print()
        print("─── По баллу скоринга ───")
        scored = [r for r in rows if r["score"] is not None]
        if not scored:
            print("  Балла ни у одной строки нет — скоринг ещё не работал"
                  " (SCORING_ENABLED=0 или строки старше него).")
        else:
            print(f"  строк с баллом: {len(scored)} из {total}")
            for lo, hi in SCORE_BUCKETS:
                bucket = [r for r in scored if lo <= r["score"] < hi]
                label = f"{lo:.0f}-{hi:.0f}" if hi < 1e9 else f"{lo:.0f}+"
                print(fmt_stats(label, bucket_stats(bucket)))
            print()
            print("  Порог SCORE_THRESHOLD ставят там, где winrate по корзинам")
            print("  перестаёт расти: выше него сигналы, ниже — шум. Пока в")
            print("  корзине меньше 30 resolved, её цифра ни о чём не говорит.")

    if args.by_feature:
        print()
        print("─── По признакам скоринга ───")
        scored = [r for r in rows if r["score_parts"]]
        if not scored:
            print("  Разбивки по признакам ещё нет.")
        else:
            names = sorted({n for r in scored for n in json.loads(r["score_parts"])})
            print(f"  строк с разбивкой: {len(scored)}")
            for name in names:
                with_f = [r for r in scored if name in json.loads(r["score_parts"])]
                without = [r for r in scored if name not in json.loads(r["score_parts"])]
                a, b = bucket_stats(with_f), bucket_stats(without)
                print(fmt_stats(f"{name} есть", a))
                print(fmt_stats(f"{name} нет ", b))
            print()
            print("  Признак полезен, если winrate 'есть' устойчиво выше 'нет'.")
            print("  Иначе он только добавляет баллов шуму — вес пора менять.")

    print()
    print("Напоминание: ни один фильтр не пересматривается, пока подгруппа не")
    print("накопит ≥100 resolved и вывод не подтверждён out-of-sample (TODO).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
