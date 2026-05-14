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

    # По типу
    where, params = signal_filter_clause(args.signal_type)
    header("По типу сигнала (только resolved)")
    print(f"  {'тип':<20} {'n':>4} {'wins':>5} {'wr':>7} {'ROI':>7} {'avg_hrs':>8}")
    rows = c.execute(f"""
        SELECT s.signal_type,
               COUNT(*) AS n,
               SUM(o.trader_was_right) AS wins,
               AVG(o.roi_if_followed) AS roi,
               AVG(o.hours_to_resolve) AS hrs
        FROM signals s
        JOIN signal_outcomes o ON o.signal_id = s.id
        WHERE o.market_resolved = 1 {where}
        GROUP BY s.signal_type
        ORDER BY n DESC
    """, params).fetchall()
    for r in rows:
        wr = fmt_pct(r["wins"] or 0, r["n"])
        print(f"  {r['signal_type']:<20} {r['n']:>4} {r['wins'] or 0:>5} {wr:>7} {fmt_roi(r['roi']):>7} {r['hrs']:>7.1f}h")

    # По side
    header("По side (только resolved)")
    print(f"  {'side':<6} {'n':>4} {'wins':>5} {'wr':>7} {'ROI':>7}")
    rows = c.execute(f"""
        SELECT s.side, COUNT(*) AS n, SUM(o.trader_was_right) AS wins, AVG(o.roi_if_followed) AS roi
        FROM signals s
        JOIN signal_outcomes o ON o.signal_id = s.id
        WHERE o.market_resolved = 1 {where}
        GROUP BY s.side
    """, params).fetchall()
    for r in rows:
        wr = fmt_pct(r["wins"] or 0, r["n"])
        print(f"  {r['side']:<6} {r['n']:>4} {r['wins'] or 0:>5} {wr:>7} {fmt_roi(r['roi']):>7}")

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

    header(title)
    print(f"  {'address':<14} {'n':>4} {'res':>4} {'wins':>5} {'wr':>7} {'ROI':>8} {'volume':>10}")
    rows = c.execute(f"""
        SELECT s.maker,
               COUNT(*) AS n,
               SUM(CASE WHEN o.market_resolved=1 THEN 1 ELSE 0 END) AS resolved,
               SUM(CASE WHEN o.trader_was_right=1 THEN 1 ELSE 0 END) AS wins,
               AVG(CASE WHEN o.market_resolved=1 THEN o.roi_if_followed END) AS roi,
               SUM(s.usdc_amount) AS vol
        FROM signals s
        LEFT JOIN signal_outcomes o ON o.signal_id = s.id
        WHERE 1=1 {where}
        GROUP BY s.maker
        ORDER BY n DESC
    """, params).fetchall()

    if not rows:
        print("  (нет сигналов)")
        return

    for r in rows:
        addr = r["maker"]
        addr_short = f"{addr[:8]}..{addr[-4:]}"
        resolved = r["resolved"] or 0
        wins = r["wins"] or 0
        wr = fmt_pct(wins, resolved) if resolved else "  -  "
        roi = fmt_roi(r["roi"])
        vol = f"${r['vol']:>8,.0f}"
        print(f"  {addr_short:<14} {r['n']:>4} {resolved:>4} {wins:>5} {wr:>7} {roi:>8} {vol:>10}")

    print()
    print("  Подсказка: для расширенной выборки по конкретному адресу:")
    print("  SELECT * FROM signals WHERE maker='<address>' ORDER BY ts DESC;")


def cmd_by_day(c: sqlite3.Connection, args) -> None:
    where, params = signal_filter_clause(args.signal_type)

    header("По дням")
    print(f"  {'date':<10} {'n':>4} {'res':>4} {'wins':>5} {'wr':>7} {'ROI':>8}")
    rows = c.execute(f"""
        SELECT date(s.ts, 'unixepoch') AS day,
               COUNT(*) AS n,
               SUM(CASE WHEN o.market_resolved=1 THEN 1 ELSE 0 END) AS resolved,
               SUM(CASE WHEN o.trader_was_right=1 THEN 1 ELSE 0 END) AS wins,
               AVG(CASE WHEN o.market_resolved=1 THEN o.roi_if_followed END) AS roi
        FROM signals s
        LEFT JOIN signal_outcomes o ON o.signal_id = s.id
        WHERE 1=1 {where}
        GROUP BY day
        ORDER BY day DESC
    """, params).fetchall()
    for r in rows:
        resolved = r["resolved"] or 0
        wins = r["wins"] or 0
        wr = fmt_pct(wins, resolved) if resolved else "  -  "
        print(f"  {r['day']:<10} {r['n']:>4} {resolved:>4} {wins:>5} {wr:>7} {fmt_roi(r['roi']):>8}")


def cmd_by_size(c: sqlite3.Connection, args) -> None:
    where, params = signal_filter_clause(args.signal_type)

    header("По корзинам размера сделки")
    # Корзины: <500, 500-1k, 1k-5k, 5k-25k, 25k+
    print(f"  {'bucket':<14} {'n':>4} {'res':>4} {'wins':>5} {'wr':>7} {'ROI':>8} {'avg':>10}")
    rows = c.execute(f"""
        SELECT
            CASE
                WHEN s.usdc_amount < 500 THEN '1) <$500'
                WHEN s.usdc_amount < 1000 THEN '2) $500-1k'
                WHEN s.usdc_amount < 5000 THEN '3) $1k-5k'
                WHEN s.usdc_amount < 25000 THEN '4) $5k-25k'
                ELSE '5) $25k+'
            END AS bucket,
            COUNT(*) AS n,
            SUM(CASE WHEN o.market_resolved=1 THEN 1 ELSE 0 END) AS resolved,
            SUM(CASE WHEN o.trader_was_right=1 THEN 1 ELSE 0 END) AS wins,
            AVG(CASE WHEN o.market_resolved=1 THEN o.roi_if_followed END) AS roi,
            AVG(s.usdc_amount) AS avg_size
        FROM signals s
        LEFT JOIN signal_outcomes o ON o.signal_id = s.id
        WHERE 1=1 {where}
        GROUP BY bucket
        ORDER BY bucket
    """, params).fetchall()
    for r in rows:
        resolved = r["resolved"] or 0
        wins = r["wins"] or 0
        wr = fmt_pct(wins, resolved) if resolved else "  -  "
        print(f"  {r['bucket']:<14} {r['n']:>4} {resolved:>4} {wins:>5} {wr:>7} "
              f"{fmt_roi(r['roi']):>8} ${r['avg_size']:>7,.0f}")


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
