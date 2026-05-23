"""Stats CLI — анализ накопленных сигналов и их исходов.

Запускать из корня проекта:
    python stats.py                     # общая сводка
    python stats.py --addresses         # разбивка по whitelist-адресам
    python stats.py --by-day            # динамика по дням
    python stats.py --by-size           # по корзинам размера
    python stats.py --recent 20         # последние N сигналов с исходом
    python stats.py --open              # сейчас открытые позиции
    python stats.py --signal 65         # детально по одному сигналу

Подробности:
  --db PATH                     путь к tracker.db (default: tracker.db)
  --signal-type TYPE            фильтр: cluster|suspicious_entry|whitelist
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Optional


# ───────── Metrics (TODO 2.1) ─────────

def median(values: list) -> Optional[float]:
    """Медиана списка чисел. None если пусто."""
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def breakeven_wr_row(price: Optional[float], side: Optional[str]) -> Optional[float]:
    """Брейк-ивен winrate для одной сделки 1 share на бинарном рынке.

    BUY  по цене p: профит +(1-p) при win, -p при lose. На равных стейках
                    брейк-ивен на портфеле buys → wr ≈ mean(p).
    SELL по цене p: профит +p при win (settled<p), -(1-p) при lose.
                    Брейк-ивен → wr ≈ 1 - mean(p).
    Возвращает per-row значение; усредняем по группе → агрегатный need_wr,
    корректный и для смешанных buy/sell выборок.
    """
    if price is None:
        return None
    return 1.0 - price if side == "sell" else price


def aggregate_resolved(rows) -> dict:
    """Свести список resolved-строк в метрики.

    rows: sqlite3.Row с полями price, side, trader_was_right, roi_if_followed.
    Все строки уже должны быть resolved (вызывающий отфильтровал).
    """
    n = len(rows)
    wins = sum(1 for r in rows if r["trader_was_right"])
    rois = [r["roi_if_followed"] for r in rows if r["roi_if_followed"] is not None]
    bes = [
        b for b in (breakeven_wr_row(r["price"], r["side"]) for r in rows)
        if b is not None
    ]
    return {
        "n": n,
        "wins": wins,
        "winrate": wins / n if n else None,
        "breakeven_wr": sum(bes) / len(bes) if bes else None,
        "mean_roi": sum(rois) / len(rois) if rois else None,
        "median_roi": median(rois),
    }


def fmt_wr(wr: Optional[float]) -> str:
    return f"{wr * 100:5.1f}%" if wr is not None else "  -  "


# ───────── Helpers ─────────

def open_db(path: str) -> sqlite3.Connection:
    import os
    if not os.path.exists(path):
        print(f"БД '{path}' не найдена. Укажи путь через --db PATH.", file=sys.stderr)
        sys.exit(1)
    try:
        c = sqlite3.connect(path)
    except sqlite3.OperationalError as e:
        print(f"Не могу открыть БД '{path}': {e}", file=sys.stderr)
        sys.exit(1)
    c.row_factory = sqlite3.Row
    # Sanity-check: убедимся что это БД трекера
    tables = {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    required = {"trades", "wallets", "signals", "signal_outcomes"}
    if not required.issubset(tables):
        missing = required - tables
        print(f"В БД '{path}' нет таблиц трекера: {missing}.", file=sys.stderr)
        print("Возможно, трекер на этой БД ещё не запускался, или путь не тот.", file=sys.stderr)
        sys.exit(1)
    return c


def fmt_pct(n: int, total: int) -> str:
    if total == 0:
        return "  -  "
    return f"{n / total * 100:5.1f}%"


def fmt_roi(roi: Optional[float]) -> str:
    if roi is None:
        return "  -   "
    return f"{roi*100:+6.1f}%"


def fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M")


def fmt_age(seconds: float) -> str:
    if seconds < 3600:
        return f"{seconds/60:.0f}m"
    if seconds < 86400:
        return f"{seconds/3600:.1f}h"
    return f"{seconds/86400:.1f}d"


def header(text: str, char: str = "─") -> None:
    print(f"\n{text}")
    print(char * len(text))


def signal_filter_clause(signal_type: Optional[str]) -> tuple[str, list]:
    """Возвращает (WHERE-условие, параметры) для фильтра по типу."""
    if signal_type:
        return " AND s.signal_type = ?", [signal_type]
    return "", []


# ───────── Команды ─────────

def cmd_overview(c: sqlite3.Connection, args) -> None:
    now = int(time.time())

    # Общая статистика
    n_trades = c.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    n_wallets = c.execute("SELECT COUNT(*) FROM wallets").fetchone()[0]
    n_signals = c.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    n_outcomes = c.execute("SELECT COUNT(*) FROM signal_outcomes").fetchone()[0]

    span = c.execute("SELECT MIN(ts), MAX(ts) FROM trades").fetchone()
    first_ts, last_ts = span[0] or 0, span[1] or 0

    print("═══ ОБЗОР ═══")
    print(f"  trades:   {n_trades:>8,}")
    print(f"  wallets:  {n_wallets:>8,}")
    print(f"  signals:  {n_signals:>8,}")
    if first_ts:
        days = (last_ts - first_ts) / 86400
        print(f"  диапазон: {fmt_ts(first_ts)} — {fmt_ts(last_ts)} ({days:.1f}д)")
        print(f"  последняя сделка: {fmt_age(now - last_ts)} назад")

    # Outcome progress
    n_resolved = c.execute(
        "SELECT COUNT(*) FROM signal_outcomes WHERE market_resolved=1"
    ).fetchone()[0]
    n_open = n_outcomes - n_resolved
    n_with_price = c.execute(
        "SELECT COUNT(*) FROM signal_outcomes "
        "WHERE max_price_reached IS NOT NULL OR market_resolved=1"
    ).fetchone()[0]
    n_no_data = n_outcomes - n_with_price

    header("Outcome progress")
    print(f"  resolved:  {n_resolved:>5}  ({fmt_pct(n_resolved, n_outcomes)})")
    print(f"  open:      {n_open:>5}  ({fmt_pct(n_open, n_outcomes)})")
    print(f"  no data:   {n_no_data:>5}  ({fmt_pct(n_no_data, n_outcomes)})  ← нет ответа от Gamma")

    # По типу + По side: тянем raw rows один раз, агрегируем в Python —
    # SQL не умеет MEDIAN, а нам нужны mean/median ROI + breakeven_wr.
    where, params = signal_filter_clause(args.signal_type)
    raw = c.execute(f"""
        SELECT s.signal_type, s.price, s.side,
               o.trader_was_right, o.roi_if_followed, o.hours_to_resolve
        FROM signals s
        JOIN signal_outcomes o ON o.signal_id = s.id
        WHERE o.market_resolved = 1 {where}
    """, params).fetchall()

    by_type: dict = {}
    by_side: dict = {}
    for r in raw:
        by_type.setdefault(r["signal_type"], []).append(r)
        by_side.setdefault(r["side"] or "?", []).append(r)

    header("По типу сигнала (только resolved)")
    print(f"  {'тип':<20} {'n':>4} {'wins':>5} {'wr':>7} {'need':>7} "
          f"{'mean':>8} {'med':>8} {'avg_hrs':>8}")
    for stype in sorted(by_type, key=lambda k: -len(by_type[k])):
        group = by_type[stype]
        agg = aggregate_resolved(group)
        hrs_vals = [r["hours_to_resolve"] for r in group if r["hours_to_resolve"] is not None]
        avg_hrs = sum(hrs_vals) / len(hrs_vals) if hrs_vals else 0
        print(f"  {stype:<20} {agg['n']:>4} {agg['wins']:>5} "
              f"{fmt_wr(agg['winrate']):>7} {fmt_wr(agg['breakeven_wr']):>7} "
              f"{fmt_roi(agg['mean_roi']):>8} {fmt_roi(agg['median_roi']):>8} "
              f"{avg_hrs:>7.1f}h")

    header("По side (только resolved)")
    print(f"  {'side':<6} {'n':>4} {'wins':>5} {'wr':>7} {'need':>7} "
          f"{'mean':>8} {'med':>8}")
    for side in sorted(by_side, key=lambda k: -len(by_side[k])):
        group = by_side[side]
        agg = aggregate_resolved(group)
        print(f"  {side:<6} {agg['n']:>4} {agg['wins']:>5} "
              f"{fmt_wr(agg['winrate']):>7} {fmt_wr(agg['breakeven_wr']):>7} "
              f"{fmt_roi(agg['mean_roi']):>8} {fmt_roi(agg['median_roi']):>8}")

    # Активность по последним окнам
    header("Сигналы за последние окна")
    for label, sec in [("24h", 86400), ("7d", 7 * 86400), ("всё время", 999 * 86400)]:
        cutoff = now - sec
        n = c.execute(
            f"SELECT COUNT(*) FROM signals s WHERE s.ts >= ? {where}",
            [cutoff] + params,
        ).fetchone()[0]
        print(f"  {label:<10} {n:>4}")


def cmd_addresses(c: sqlite3.Connection, args) -> None:
    """Разбивка по конкретным адресам — по умолчанию только whitelist."""
    where, params = signal_filter_clause(args.signal_type)

    title = "По whitelist-адресам" if not args.signal_type else f"По адресам ({args.signal_type})"
    if not args.signal_type:
        # Если фильтр не задан, ограничиваем whitelist'ом — это самое полезное
        where = " AND s.signal_type = 'whitelist'"
        params = []

    rows = c.execute(f"""
        SELECT s.maker, s.price, s.side, s.usdc_amount,
               o.market_resolved, o.trader_was_right, o.roi_if_followed
        FROM signals s
        LEFT JOIN signal_outcomes o ON o.signal_id = s.id
        WHERE 1=1 {where}
    """, params).fetchall()

    by_addr: dict = {}
    for r in rows:
        by_addr.setdefault(r["maker"], []).append(r)

    header(title)
    print(f"  {'address':<14} {'n':>4} {'res':>4} {'wins':>5} {'wr':>7} {'need':>7} "
          f"{'mean':>8} {'med':>8} {'volume':>10}")

    if not by_addr:
        print("  (нет сигналов)")
        return

    for addr, group in sorted(by_addr.items(), key=lambda kv: -len(kv[1])):
        addr_short = f"{addr[:8]}..{addr[-4:]}"
        n_total = len(group)
        resolved_rows = [r for r in group if r["market_resolved"]]
        agg = aggregate_resolved(resolved_rows)
        vol = sum(r["usdc_amount"] for r in group)
        print(f"  {addr_short:<14} {n_total:>4} {agg['n']:>4} {agg['wins']:>5} "
              f"{fmt_wr(agg['winrate']):>7} {fmt_wr(agg['breakeven_wr']):>7} "
              f"{fmt_roi(agg['mean_roi']):>8} {fmt_roi(agg['median_roi']):>8} "
              f"${vol:>7,.0f}")

    print()
    print("  Подсказка: для расширенной выборки по конкретному адресу:")
    print("  SELECT * FROM signals WHERE maker='<address>' ORDER BY ts DESC;")


def cmd_by_day(c: sqlite3.Connection, args) -> None:
    where, params = signal_filter_clause(args.signal_type)
    rows = c.execute(f"""
        SELECT s.ts, s.price, s.side,
               o.market_resolved, o.trader_was_right, o.roi_if_followed
        FROM signals s
        LEFT JOIN signal_outcomes o ON o.signal_id = s.id
        WHERE 1=1 {where}
    """, params).fetchall()

    by_day: dict = {}
    for r in rows:
        day = datetime.fromtimestamp(r["ts"], timezone.utc).strftime("%Y-%m-%d")
        by_day.setdefault(day, []).append(r)

    header("По дням")
    print(f"  {'date':<10} {'n':>4} {'res':>4} {'wins':>5} {'wr':>7} {'need':>7} "
          f"{'mean':>8} {'med':>8}")
    for day in sorted(by_day.keys(), reverse=True):
        group = by_day[day]
        n_total = len(group)
        resolved_rows = [r for r in group if r["market_resolved"]]
        agg = aggregate_resolved(resolved_rows)
        print(f"  {day:<10} {n_total:>4} {agg['n']:>4} {agg['wins']:>5} "
              f"{fmt_wr(agg['winrate']):>7} {fmt_wr(agg['breakeven_wr']):>7} "
              f"{fmt_roi(agg['mean_roi']):>8} {fmt_roi(agg['median_roi']):>8}")


def cmd_by_size(c: sqlite3.Connection, args) -> None:
    where, params = signal_filter_clause(args.signal_type)
    rows = c.execute(f"""
        SELECT s.usdc_amount, s.price, s.side,
               o.market_resolved, o.trader_was_right, o.roi_if_followed
        FROM signals s
        LEFT JOIN signal_outcomes o ON o.signal_id = s.id
        WHERE 1=1 {where}
    """, params).fetchall()

    def bucket(usdc: float) -> str:
        if usdc < 500:
            return "1) <$500"
        if usdc < 1000:
            return "2) $500-1k"
        if usdc < 5000:
            return "3) $1k-5k"
        if usdc < 25000:
            return "4) $5k-25k"
        return "5) $25k+"

    by_bucket: dict = {}
    for r in rows:
        by_bucket.setdefault(bucket(r["usdc_amount"]), []).append(r)

    header("По корзинам размера сделки")
    print(f"  {'bucket':<14} {'n':>4} {'res':>4} {'wins':>5} {'wr':>7} {'need':>7} "
          f"{'mean':>8} {'med':>8} {'avg':>10}")
    for b in sorted(by_bucket.keys()):
        group = by_bucket[b]
        n_total = len(group)
        resolved_rows = [r for r in group if r["market_resolved"]]
        agg = aggregate_resolved(resolved_rows)
        avg_size = sum(r["usdc_amount"] for r in group) / n_total if n_total else 0
        print(f"  {b:<14} {n_total:>4} {agg['n']:>4} {agg['wins']:>5} "
              f"{fmt_wr(agg['winrate']):>7} {fmt_wr(agg['breakeven_wr']):>7} "
              f"{fmt_roi(agg['mean_roi']):>8} {fmt_roi(agg['median_roi']):>8} "
              f"${avg_size:>7,.0f}")


def cmd_recent(c: sqlite3.Connection, args) -> None:
    n = args.recent
    where, params = signal_filter_clause(args.signal_type)

    header(f"Последние {n} сигналов")
    rows = c.execute(f"""
        SELECT s.id, s.ts, s.signal_type, s.maker, s.market_slug,
               s.usdc_amount, s.price, s.side,
               o.market_resolved, o.settled_price, o.trader_was_right,
               o.roi_if_followed
        FROM signals s
        LEFT JOIN signal_outcomes o ON o.signal_id = s.id
        WHERE 1=1 {where}
        ORDER BY s.ts DESC
        LIMIT ?
    """, params + [n]).fetchall()

    for r in rows:
        time_str = fmt_ts(r["ts"])
        type_short = r["signal_type"][:8]
        addr = f"{r['maker'][:8]}..{r['maker'][-4:]}"
        slug = (r["market_slug"] or "?")[:35]
        size = f"${r['usdc_amount']:,.0f}"
        price = f"@{r['price']:.3f}"

        if r["market_resolved"]:
            outcome = ("✓ WIN " if r["trader_was_right"] else "✗ LOSE") + f" {fmt_roi(r['roi_if_followed'])}"
        else:
            outcome = "...open"

        print(f"  #{r['id']:<4} {time_str}  [{type_short:<8}] {r['side']:<4} "
              f"{addr}  {size:>9} {price}  {outcome:<18}  {slug}")


def cmd_open(c: sqlite3.Connection, args) -> None:
    where, params = signal_filter_clause(args.signal_type)
    now = int(time.time())

    header("Открытые позиции (не зарезолвленные)")
    rows = c.execute(f"""
        SELECT s.id, s.ts, s.signal_type, s.market_slug, s.usdc_amount,
               s.price AS entry_price, s.side,
               o.max_price_reached, o.min_price_reached,
               o.price_1h, o.price_24h, o.price_7d
        FROM signals s
        JOIN signal_outcomes o ON o.signal_id = s.id
        WHERE o.market_resolved = 0 {where}
        ORDER BY s.ts DESC
    """, params).fetchall()

    if not rows:
        print("  (все зарезолвлены)")
        return

    print(f"  {'#':<5} {'age':>6} {'type':<10} {'side':<4} {'entry':>6} {'max':>6} {'min':>6} {'1h':>6} {'24h':>6} {'slug':<30}")
    for r in rows:
        age = fmt_age(now - r["ts"])
        type_short = r["signal_type"][:9]
        slug = (r["market_slug"] or "?")[:30]
        entry = f"{r['entry_price']:.3f}"
        mx = f"{r['max_price_reached']:.3f}" if r["max_price_reached"] is not None else "  -  "
        mn = f"{r['min_price_reached']:.3f}" if r["min_price_reached"] is not None else "  -  "
        p1 = f"{r['price_1h']:.3f}" if r["price_1h"] is not None else "  -  "
        p24 = f"{r['price_24h']:.3f}" if r["price_24h"] is not None else "  -  "
        print(f"  #{r['id']:<4} {age:>6} {type_short:<10} {r['side']:<4} {entry:>6} "
              f"{mx:>6} {mn:>6} {p1:>6} {p24:>6} {slug:<30}")


def cmd_signal(c: sqlite3.Connection, args) -> None:
    """Полная информация по одному сигналу."""
    sid = args.signal
    row = c.execute("""
        SELECT s.*, o.price_1h, o.price_24h, o.price_7d,
               o.max_price_reached, o.min_price_reached,
               o.market_resolved, o.settled_price,
               o.trader_was_right, o.roi_if_followed,
               o.hours_to_resolve, o.last_checked_ts, o.created_ts
        FROM signals s
        LEFT JOIN signal_outcomes o ON o.signal_id = s.id
        WHERE s.id = ?
    """, (sid,)).fetchone()

    if not row:
        print(f"Сигнал #{sid} не найден.", file=sys.stderr)
        sys.exit(1)

    header(f"Сигнал #{sid}")
    print(f"  type:     {row['signal_type']}")
    print(f"  side:     {row['side']}")
    print(f"  ts:       {fmt_ts(row['ts'])}")
    print(f"  maker:    {row['maker']}")
    print(f"  slug:     {row['market_slug']}")
    print(f"  size:     ${row['usdc_amount']:,.2f}")
    print(f"  price:    {row['price']:.4f}")
    print(f"  reason:   {row['reason']}")
    print(f"  tx:       {row['tx_hash']}")

    if row["market_resolved"] is not None:
        header("Outcome")
        print(f"  resolved:        {'YES' if row['market_resolved'] else 'no'}")
        if row["market_resolved"]:
            print(f"  settled_price:   {row['settled_price']:.4f}")
            print(f"  trader_was_right: {'YES' if row['trader_was_right'] else 'NO'}")
            print(f"  roi_if_followed:  {fmt_roi(row['roi_if_followed'])}")
            print(f"  hours_to_resolve: {row['hours_to_resolve']:.1f}h")
        else:
            mp = row["max_price_reached"]
            mn = row["min_price_reached"]
            print(f"  max_reached:     {mp:.4f}" if mp is not None else "  max_reached:     -")
            print(f"  min_reached:     {mn:.4f}" if mn is not None else "  min_reached:     -")
            for label in ("price_1h", "price_24h", "price_7d"):
                v = row[label]
                print(f"  {label}: {v:.4f}" if v is not None else f"  {label}: -")
            if row["last_checked_ts"]:
                age = int(time.time()) - row["last_checked_ts"]
                print(f"  last_checked:    {fmt_age(age)} назад")


# ───────── Main ─────────

def main():
    p = argparse.ArgumentParser(description="Stats CLI для Polymarket трекера")
    p.add_argument("--db", default="tracker.db", help="Путь к tracker.db (default: tracker.db)")
    p.add_argument("--signal-type", choices=["cluster", "suspicious_entry", "whitelist"],
                   help="Фильтр по типу сигнала")

    g = p.add_mutually_exclusive_group()
    g.add_argument("--addresses", action="store_true", help="Разбивка по адресам")
    g.add_argument("--by-day", action="store_true", help="Динамика по дням")
    g.add_argument("--by-size", action="store_true", help="По корзинам размера сделки")
    g.add_argument("--recent", type=int, metavar="N", help="Последние N сигналов")
    g.add_argument("--open", action="store_true", dest="show_open", help="Открытые позиции")
    g.add_argument("--signal", type=int, metavar="ID", help="Детально по сигналу")

    args = p.parse_args()

    c = open_db(args.db)
    try:
        if args.addresses:
            cmd_addresses(c, args)
        elif args.by_day:
            cmd_by_day(c, args)
        elif args.by_size:
            cmd_by_size(c, args)
        elif args.recent is not None:
            cmd_recent(c, args)
        elif args.show_open:
            cmd_open(c, args)
        elif args.signal is not None:
            cmd_signal(c, args)
        else:
            cmd_overview(c, args)
    finally:
        c.close()


if __name__ == "__main__":
    main()
