#!/usr/bin/env python3
"""Кто финансировал кошелёк и связаны ли между собой те, кто зарабатывает.

Гипотеза
--------
Классическая подпись инсайдера: несколько свежих кошельков, залитых с
ОДНОГО адреса, синхронно заходят в один исход. Именно так инсайдеров
находили публично.

Почему наивный подход не работает
---------------------------------
У кошелька Polymarket в переводах виден только приход pUSD — либо чеканка
с нулевого адреса, либо перевод с контрактов биржи. Настоящего спонсора
там нет.

Реальная цепочка (разобрана на живой транзакции, 213 событий в одной
пакетной транзакции релеера):

    спонсор --USDC.e--> шлюз --> pUSD чеканится кошельку

Поэтому ищем так: берём транзакцию первой чеканки, запрашиваем её чек и
находим внутри перевод USDC на ту же сумму. Отправитель этого перевода и
есть спонсор.

Результат полного прогона (07.09.2026, 1022 кошелька с исходами)
----------------------------------------------------------------
Общих источников не нашлось: 801 уникальный спонсор на 933 распутанных
кошелька, и лишь ДВА адреса залили больше одного — оба шлюзы, прогоняющие
тысячу переводов за считанные часы.

    группа                кошел.  сделок  винрейт  безубыток  перевес
    общий узкий источник       0       0        —          —        —
    общий источник-шлюз      134    1265    72.6%      75.4%   -2.7 пп
    свой источник            799    9778    68.9%      67.9%   +1.0 пп

Гипотеза о связках кошельков в наших данных не подтвердилась. Инструмент
оставлен как повторяемый замер: выборка растёт, и проверить снова — это
одна команда.

Про соблазн голого винрейта
---------------------------
Кошельки одного из шлюзов дают 89.8% побед — против 68.9% у остальных.
Разница обманчива: они берут фаворитов по средней 0.860, а безубыточный
винрейт для покупки равен цене входа. Перевес над безубытком выходит
+3.8 пп при нижней границе +0.4 пп — то есть почти ничего, и объясняется
он ценовой зоной, а не осведомлённостью. Поэтому в отчёте сравнивается
перевес над безубытком, а не винрейт.

Запуск (нужен ETHERSCAN_API_KEY в .env):
    python tools/analyze_funders.py
    python tools/analyze_funders.py --limit 300 --workers 4
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUT_JSON = DATA_DIR / "funders.json"

ETHERSCAN = "https://api.etherscan.io/v2/api"
CHAIN_ID = 137
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
EXCHANGES = {
    "0xe111180000d2663c0091e4f400237545b87b996b",
    "0xe2222d279d744050d28e00520010520000310f59",
}
ZERO = "0x" + "0" * 40

# Больше стольких получателей — это шлюз или биржа, а не связка кошельков.
NARROW_MAX_RECIPIENTS = 10

# Сколько переводов запрашиваем у источника, чтобы судить о его природе.
SAMPLE_SIZE = 1000

# Если столько переводов уложились в меньшее время — это поток, а не кошелёк
# человека. Живой шлюз прогоняет тысячу переводов быстрее чем за час.
BUSY_SPAN_HOURS = 24.0

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

_local = threading.local()
_key = ""


def _session() -> requests.Session:
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers["User-Agent"] = "polymarket-tracker/0.5 (funder analysis)"
        _local.session = s
    return s


def es(params: dict, tries: int = 3):
    params = dict(params, chainid=CHAIN_ID, apikey=_key)
    for attempt in range(tries):
        try:
            r = _session().get(ETHERSCAN, params=params, timeout=30)
            if r.status_code == 200:
                data = r.json()
                result = data.get("result")
                if result is not None and not isinstance(result, str):
                    return result
                # "Max rate limit reached" приходит именно строкой
                if isinstance(result, str) and "rate limit" in result.lower():
                    time.sleep(1.0 * (attempt + 1))
                    continue
                return result
        except requests.RequestException:
            pass
        time.sleep(1.0 * (attempt + 1))
    return None


def wilson(k: int, n: int, z: float = 1.96) -> tuple:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (centre - half, centre + half)


def funder_of(wallet: str):
    """Адрес, приславший первые деньги. None — если распутать не удалось."""
    txs = es({"module": "account", "action": "tokentx", "contractaddress": PUSD,
              "address": wallet, "page": 1, "offset": 8, "sort": "asc"})
    if not isinstance(txs, list):
        return None
    for t in txs:
        if str(t.get("to", "")).lower() != wallet.lower():
            continue
        try:
            amount = int(t["value"])
        except (KeyError, ValueError):
            continue
        receipt = es({"module": "proxy", "action": "eth_getTransactionReceipt",
                      "txhash": t["hash"]})
        if not isinstance(receipt, dict):
            continue
        for log in receipt.get("logs", []):
            topics = log.get("topics") or []
            if not topics or topics[0].lower() != TRANSFER or len(topics) < 3:
                continue
            if str(log.get("address", "")).lower() == PUSD.lower():
                continue  # это сама чеканка, а не источник
            try:
                value = int(log["data"], 16)
            except (KeyError, ValueError):
                continue
            # Сумма должна совпасть с чеканкой: в пакетной транзакции
            # десятки чужих переводов, и различить их можно только так.
            if amount == 0 or abs(value - amount) > amount * 0.01:
                continue
            src = ("0x" + topics[1][-40:]).lower()
            if src in EXCHANGES or src == ZERO:
                continue
            return src
    return None


def classify_funder(funder: str) -> dict:
    """Связка кошельков или шлюз? Считаем не получателей, а пропускную способность.

    Наивный счёт "сколько разных адресов в последних N переводах" врёт, и
    врёт опасно. Шлюз 0x4d97dcd9 профинансировал 64 наших кошелька, но в
    последних 200 его переводах получателей всего четыре — просто потому,
    что 1000 переводов у него укладываются в НОЛЬ часов. Окно наблюдения
    оказывается короче минуты, и высокочастотный шлюз выглядит узкой связкой.

    Поэтому смотрим на охват по времени: если выборка уперлась в предел и
    при этом покрывает считанные часы, перед нами инфраструктура, и
    настоящее число получателей нам просто не измерить.
    """
    txs = es({"module": "account", "action": "tokentx", "address": funder,
              "page": 1, "offset": SAMPLE_SIZE, "sort": "desc"})
    if not isinstance(txs, list) or not txs:
        return {"kind": "unknown", "recipients": -1, "span_hours": -1.0}

    stamps = [int(t["timeStamp"]) for t in txs if str(t.get("timeStamp", "")).isdigit()]
    span_hours = (max(stamps) - min(stamps)) / 3600 if stamps else 0.0
    recipients = len({str(t.get("to", "")).lower() for t in txs
                      if str(t.get("from", "")).lower() == funder.lower()})

    saturated = len(txs) >= SAMPLE_SIZE
    if saturated and span_hours < BUSY_SPAN_HOURS:
        return {"kind": "service", "recipients": recipients, "span_hours": span_hours}
    if recipients > NARROW_MAX_RECIPIENTS:
        return {"kind": "service", "recipients": recipients, "span_hours": span_hours}
    return {"kind": "narrow", "recipients": recipients, "span_hours": span_hours}


def load_wallets(db: Path, limit: int) -> list:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT maker, COUNT(*) AS n, "
        "       AVG(CAST(trader_was_right AS REAL)) AS wr, "
        "       AVG(roi_if_followed) AS roi "
        "FROM shadow_trades "
        "WHERE trader_was_right IS NOT NULL AND market_resolved = 1 "
        "GROUP BY maker HAVING n >= 2 "
        "ORDER BY n DESC LIMIT ?", (limit,)
    ).fetchall()
    c.close()
    return [dict(r) for r in rows]


def group_stats(db: Path, makers: set) -> Optional[dict]:
    """Сделки группы, взвешенные честно: винрейт против безубытка.

    Сравнивать группы по голому винрейту нельзя. Кто берёт фаворитов по
    0.86, выигрывает 9 раз из 10 и не зарабатывает ничего: безубыточный
    винрейт для покупки равен средней цене входа. Разница между винрейтом
    и ценой входа — единственное, что имеет смысл сравнивать.
    """
    if not makers:
        return None
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    rows = [r for r in c.execute(
        "SELECT maker, price, trader_was_right, roi_if_followed FROM shadow_trades "
        "WHERE trader_was_right IS NOT NULL AND market_resolved = 1"
    ) if r["maker"].lower() in makers]
    c.close()
    if not rows:
        return None
    n = len(rows)
    wins = sum(1 for r in rows if r["trader_was_right"])
    breakeven = sum(r["price"] for r in rows) / n
    lo, hi = wilson(wins, n)
    return {
        "wallets": len(makers), "trades": n, "winrate": wins / n,
        "breakeven": breakeven, "edge": wins / n - breakeven,
        "edge_lo": lo - breakeven, "roi": sum(r["roi_if_followed"] or 0 for r in rows) / n,
    }


def print_group(name: str, st: Optional[dict]) -> None:
    if not st or st["trades"] < 50:
        print(f"  {name:<24} мало данных")
        return
    print(f"  {name:<24}{st['wallets']:>6}{st['trades']:>8}"
          f"{st['winrate']*100:>9.1f}%{st['breakeven']*100:>11.1f}%"
          f"{st['edge']*100:>+9.1f}пп{st['edge_lo']*100:>+9.1f}пп{st['roi']*100:>+8.1f}%")


def main() -> int:
    global _key
    p = argparse.ArgumentParser(description="Анализ источников финансирования кошельков.")
    p.add_argument("--db", default=str(DATA_DIR / "tracker.db"))
    p.add_argument("--limit", type=int, default=1200, help="сколько кошельков взять")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--reuse", action="store_true",
                   help="не ходить в сеть за источниками, взять из funders.json")
    args = p.parse_args()
    db = Path(args.db)

    env_path = ROOT / ".env"
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("ETHERSCAN_API_KEY="):
            _key = line.split("=", 1)[1].split("#")[0].strip()
    if not _key:
        print("[ОШИБКА] ETHERSCAN_API_KEY не задан в .env")
        return 1

    from concurrent.futures import ThreadPoolExecutor

    if args.reuse and OUT_JSON.exists():
        results = json.loads(OUT_JSON.read_text(encoding="utf-8"))
        print(f"Источники взяты из {OUT_JSON.name}: {len(results)} кошельков")
    else:
        wallets = load_wallets(db, args.limit)
        print(f"Кошельков с исходами: {len(wallets)}")
        print("Ищу источник финансирования у каждого (две выборки Etherscan на кошелёк).")
        done = {"n": 0}
        lock = threading.Lock()
        t0 = time.time()

        def work(w):
            src = funder_of(w["maker"])
            with lock:
                done["n"] += 1
                if done["n"] % 100 == 0:
                    print(f"   {done['n']}/{len(wallets)} за {time.time()-t0:.0f} с", flush=True)
            return dict(w, funder=src)

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            results = list(pool.map(work, wallets))

    found = [r for r in results if r["funder"]]
    print()
    print(f"Источник найден: {len(found)} из {len(results)}")

    counts = {}
    for r in found:
        counts[r["funder"]] = counts.get(r["funder"], 0) + 1
    shared = sorted({s for s, k in counts.items() if k > 1})
    print(f"Источников, заливавших больше одного кошелька: {len(shared)} "
          f"(уникальных источников всего {len(counts)})")

    narrow = set()
    if shared:
        print("Разбираю их: связка кошельков или шлюз?")
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for src, info in zip(shared, pool.map(classify_funder, shared)):
                mark = "УЗКИЙ" if info["kind"] == "narrow" else info["kind"]
                print(f"   {src[:12]}..  наших кошельков {counts[src]:>4}, "
                      f"получателей {info['recipients']:>4} за {info['span_hours']:.1f} ч"
                      f"   -> {mark}")
                if info["kind"] == "narrow":
                    narrow.add(src)

    for r in results:
        r["funder_kind"] = (
            "narrow" if r.get("funder") in narrow else
            "service" if r.get("funder") in set(shared) else
            "solo" if r.get("funder") else "unknown"
        )
    OUT_JSON.write_text(json.dumps(results, indent=1), encoding="utf-8")

    def makers(kind):
        return {r["maker"].lower() for r in results if r["funder_kind"] == kind}

    print()
    print("=== Исходы по типу источника ===")
    print(f"  {'группа':<24}{'кошел.':>6}{'сделок':>8}{'винрейт':>10}"
          f"{'безубыток':>11}{'перевес':>11}{'нижняя гр.':>11}{'ROI':>8}")
    print_group("общий УЗКИЙ источник", group_stats(db, makers("narrow")))
    print_group("общий источник-шлюз", group_stats(db, makers("service")))
    print_group("свой источник", group_stats(db, makers("solo")))
    print_group("источник не распутан", group_stats(db, makers("unknown")))

    # Отдельно по каждому общему источнику: усреднять их вместе бессмысленно,
    # если это разные шлюзы с разной публикой.
    if shared:
        print()
        print("=== По каждому общему источнику отдельно ===")
        for src in sorted(shared, key=lambda s: -counts[s]):
            group = {r["maker"].lower() for r in results if r.get("funder") == src}
            print_group(f"{src[:10]}.. ({counts[src]} кош.)", group_stats(db, group))

    print()
    print(f"Сырые данные -> {OUT_JSON}")
    print("Признак имеет смысл, только если перевес НАД БЕЗУБЫТКОМ держится "
          "и его нижняя граница выше нуля.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
