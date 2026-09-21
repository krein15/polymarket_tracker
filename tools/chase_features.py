"""Признаки, доступные в МОМЕНТ сделки, против будущей погони.

Зачем
-----
Весь перевес проекта сидит в одной метке. На 38 137 закрывшихся покупок
по цене самого трейдера:

    все покупки                   +0.7 пп   ROI  +0.2%
    рынок пошёл за ним (>= +15%) +25.4 пп   ROI +51.6%
    рынок пошёл против           -31.0 пп   ROI -57.8%

То есть сама по себе сделка инсайдера не стоит ничего — стоит то, как на
неё отреагировали следующие 20 минут. Но метка приходит через 20 минут и
на 29 минут позже сделки, а цена к тому времени уходит на +59% медианы.

Отсюда задача: найти признаки, доступные В МОМЕНТ СДЕЛКИ, которые
предсказывают метку. Тогда входить можно сразу, по цене трейдера, и
переплата падает с 59% до 2%.

Метка
-----
Абсолютный сдвиг цены, а не процент: цены живут в (0,1], и относительный
порог недостижим на дорогих рынках.

    сдвиг >= +0.05   погоня
    сдвиг <= -0.05   разворот

Почему лифт считается внутри коридоров цен
------------------------------------------
Метка связана с ценой механически: при цене 0.90 сдвиг +0.05 упирается в
единицу. Любой признак, коррелирующий с ценой (размер, категория, объём),
покажет ложный лифт, если не разделить выборку по цене.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import statistics
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DB = os.path.join(ROOT, "data", "tracker.db")
WINDOW_SEC = 1800          # окно "недавнего" для опорной цены и активности
POS_SHIFT = 0.05
NEG_SHIFT = -0.05
PRICE_BANDS = ((0.20, 0.50), (0.50, 0.65), (0.65, 0.80), (0.80, 0.95))


def connect(path=DB):
    c = sqlite3.connect("file:" + path + "?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def build(conn, limit=None, progress=None):
    """Собрать признаки минуты 0 для теневых сделок с посчитанной погоней.

    Берём только те, что попадают в окно хранения trades: без локальной
    истории признаки не посчитать, а выдумывать их нечем.
    """
    floor = conn.execute("SELECT MIN(ts) FROM trades").fetchone()[0]
    sql = ("SELECT id, ts, maker, token_id, usdc_amount, price, market_slug, "
           "       category, volume_24h, chase, chase_money, score, "
           "       market_resolved, roi_if_followed, trader_was_right "
           "FROM shadow_trades "
           "WHERE chase IS NOT NULL AND side='buy' AND ts >= ? "
           "ORDER BY ts")
    if limit:
        sql += " LIMIT " + str(int(limit))
    rows = conn.execute(sql, (floor + WINDOW_SEC,)).fetchall()

    out = []
    for i, r in enumerate(rows):
        if progress and i and i % 1000 == 0:
            progress(i, len(rows))
        out.append(features(conn, r))
    return out


def features(conn, r) -> dict:
    ts, token, maker = r["ts"], r["token_id"], r["maker"]
    recent = conn.execute(
        "SELECT price, usdc_amount, maker, side FROM trades "
        "WHERE token_id = ? AND ts >= ? AND ts < ?",
        (token, ts - WINDOW_SEC, ts),
    ).fetchall()
    buys = [x for x in recent if x["side"] == "buy"]
    prices = [x["price"] for x in buys]
    base = statistics.median(prices) if prices else None

    hist = conn.execute(
        "SELECT COUNT(*) n, MIN(ts) first_ts, SUM(usdc_amount) vol "
        "FROM trades WHERE maker = ? AND ts < ?", (maker, ts)).fetchone()
    same_token = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE maker = ? AND token_id = ? AND ts < ?",
        (maker, token, ts)).fetchone()[0]

    flow = sum(x["usdc_amount"] for x in recent)
    d = {
        "id": r["id"], "ts": ts, "price": r["price"],
        # Кошелёк нужен для признаков "про человека", а не про рынок:
        # рыночные признаки предсказывают движение, но не направление.
        "maker": maker, "token_id": token,
        "usdc": r["usdc_amount"], "slug": r["market_slug"],
        "category": r["category"], "volume_24h": r["volume_24h"],
        "score": r["score"], "chase": r["chase"],
        "chase_money": r["chase_money"],
        "resolved": r["market_resolved"], "roi": r["roi_if_followed"],
        "right": r["trader_was_right"],
        # ── признаки минуты 0 ──
        "impact": (r["price"] / base - 1) if base else None,
        "recent_trades": len(recent),
        "recent_flow": flow,
        "recent_makers": len({x["maker"] for x in recent}),
        "spread30": (max(prices) - min(prices)) if prices else None,
        "size_vs_flow": (r["usdc_amount"] / flow) if flow > 0 else None,
        "wallet_trades": hist["n"] or 0,
        "wallet_vol": hist["vol"] or 0.0,
        "wallet_age_h": ((ts - hist["first_ts"]) / 3600.0)
                        if hist["first_ts"] else 0.0,
        "first_in_token": 1 if same_token == 0 else 0,
        "hour": time.gmtime(ts).tm_hour,
    }
    d["shift"] = r["chase"] * r["price"]
    d["pos"] = 1 if d["shift"] >= POS_SHIFT else 0
    d["neg"] = 1 if d["shift"] <= NEG_SHIFT else 0
    return d


# ───────────────────────── отчёт ─────────────────────────


def wilson(k, n, z=1.96):
    """Доверительный интервал для доли. На малых группах обычная формула
    даёт нули и единицы, из-за которых признак кажется идеальным.

    Границы подрезаны по [0, 1]: на k = n формула даёт 1.0000000000000002,
    и в отчёте это выглядело бы как доля больше ста процентов."""
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    s = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (c - s) / d), min(1.0, (c + s) / d)


CUTS = [
    ("удар по цене > +10%", lambda d: (d["impact"] or 0) > 0.10),
    ("удар по цене +3..10%", lambda d: 0.03 < (d["impact"] or 0) <= 0.10),
    ("удар по цене около 0", lambda d: abs(d["impact"] or 0) <= 0.03),
    ("удар по цене < -3%", lambda d: (d["impact"] or 0) < -0.03),
    ("сделка > 5% потока за 30 мин", lambda d: (d["size_vs_flow"] or 0) > 0.05),
    ("сделка < 1% потока", lambda d: (d["size_vs_flow"] or 1) < 0.01),
    ("в рынке < 20 сделок за 30 мин", lambda d: d["recent_trades"] < 20),
    ("в рынке > 200 сделок за 30 мин", lambda d: d["recent_trades"] > 200),
    ("меньше 5 участников за 30 мин", lambda d: d["recent_makers"] < 5),
    ("больше 30 участников за 30 мин", lambda d: d["recent_makers"] > 30),
    ("разброс цен > 0.10 за 30 мин", lambda d: (d["spread30"] or 0) > 0.10),
    ("разброс цен < 0.02 за 30 мин", lambda d: (d["spread30"] or 1) < 0.02),
    ("кошелёк впервые в этом рынке", lambda d: d["first_in_token"] == 1),
    ("кошелёк уже был в рынке", lambda d: d["first_in_token"] == 0),
    ("кошелёк новый (< 24 ч)", lambda d: d["wallet_age_h"] < 24),
    ("кошелёк старше 5 суток", lambda d: d["wallet_age_h"] > 120),
    ("у кошелька > 500 сделок", lambda d: d["wallet_trades"] > 500),
    ("у кошелька < 20 сделок", lambda d: d["wallet_trades"] < 20),
    ("сделка > $15k", lambda d: d["usdc"] > 15000),
    ("сделка $2-5k", lambda d: 2000 <= d["usdc"] < 5000),
]


def report_band(rows, lo, hi):
    sub = [d for d in rows if lo <= d["price"] < hi]
    if len(sub) < 200:
        print("  коридор {:.2f}-{:.2f}: {} сделок — мало".format(
            lo, hi, len(sub)))
        return
    base = sum(d["pos"] for d in sub) / len(sub)
    print()
    print("  ── цена {:.2f}-{:.2f}: {} сделок, погонь {:.1f}% ──".format(
        lo, hi, len(sub), base * 100))
    print("    {:<30}{:>6}{:>9}{:>8}{:>18}".format(
        "признак", "n", "погонь", "лифт", "95% интервал"))
    for label, pred in CUTS:
        g = [d for d in sub if pred(d)]
        if len(g) < 60:
            continue
        k = sum(d["pos"] for d in g)
        lo_ci, hi_ci = wilson(k, len(g))
        mark = "  <-" if lo_ci > base or hi_ci < base else ""
        print("    {:<30}{:>6}{:>8.1f}%{:>8.2f}   [{:>5.1f};{:>5.1f}]{}"
              .format(label, len(g), k / len(g) * 100,
                      (k / len(g)) / base if base else 0,
                      lo_ci * 100, hi_ci * 100, mark))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", default=None, help="сохранить выборку в JSON")
    ap.add_argument("--load", default=None, help="взять готовую выборку")
    args = ap.parse_args()

    if args.load:
        with open(args.load, encoding="utf-8") as f:
            rows = json.load(f)
    else:
        conn = connect()
        t0 = time.time()

        def prog(i, n):
            el = time.time() - t0
            print("    {}/{}, осталось ~{:.0f} с".format(
                i, n, el / i * (n - i)), flush=True)

        print("собираю признаки...", flush=True)
        rows = build(conn, limit=args.limit, progress=prog)
        print("собрано {} за {:.0f} с".format(len(rows), time.time() - t0))
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                json.dump(rows, f)

    n = len(rows)
    print()
    print("сделок с признаками и меткой: {}".format(n))
    print("погонь {:.1f}%, разворотов {:.1f}%".format(
        sum(d["pos"] for d in rows) / n * 100,
        sum(d["neg"] for d in rows) / n * 100))
    print()
    print("Стрелка справа = интервал доли не накрывает базовую: признак "
          "действительно сдвигает вероятность.")
    for lo, hi in PRICE_BANDS:
        report_band(rows, lo, hi)


if __name__ == "__main__":
    main()
