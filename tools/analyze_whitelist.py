"""
analyze_whitelist.py v3 — winrate через /activity (REDEEM vs BUY).

Логика:
    - Грузим всю активность трейдера (/activity, до 500 записей)
    - REDEEM по conditionId = выиграл этот рынок
    - BUY по conditionId без REDEEM = проиграл (позиция закрылась в 0)
    - winrate = кол-во выигранных conditionId / все завершённые conditionId

Запуск:
    python tools/analyze_whitelist.py

Результат (в data/):
    - data/whitelist_analysis.json
    - data/whitelist_filtered.txt
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Optional

import requests

# Результаты кладём в data/ рядом с боевым whitelist.txt.
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUT_JSON = DATA_DIR / "whitelist_analysis.json"
OUT_TXT = DATA_DIR / "whitelist_filtered.txt"

# Windows-консоль работает в cp866/cp1251 и не знает части символов (стрелки,
# галочки). Пока вывод идёт в консоль, Python печатает их через WriteConsoleW,
# но при ПЕРЕНАПРАВЛЕНИИ (> log.txt, Планировщик задач) переключается на
# кодировку локали и падает с UnicodeEncodeError. Заменяем непечатаемое на "?".
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):  # не TextIOWrapper — не наша забота
        pass

# ── Настройки ────────────────────────────────────────────────────
MIN_WINRATE   = 0.80   # минимальный winrate
MIN_RESOLVED  = 5      # минимум завершённых рынков для надёжной статистики
REQUEST_DELAY = 0.7    # пауза между запросами (сек)
TIMEOUT       = 15
DATA_API      = "https://data-api.polymarket.com"

CANDIDATES = {
    # === Старый whitelist (12) ===
    "0x9495425feeb0c250accb89275c97587011b19a27": "LaBradfordSmith22",
    "0x6ac5bb06a9eb05641fd5e82640268b92f3ab4b6e": "Lakersfan111",
    "0xb652e5dabc3fccd3c939acab0108f99866842db4": "0x2a2C53b (анон)",
    "0xe26cacfaa3f695a2a239e5918936b10d56f188cf": "Dvitaminbets",
    "0x5257aa84944804bbb0c718814ebebeeafaca3e2a": "NO-GOD-PLEASE-NO",
    "0x9ac2536ed93f8fe8ce91d9662b03bcbb19ccbe3d": "ChloeT1",
    "0xfbf3d501e88815464642d0e913f15379c3eeb218": "VPenguin",
    "0xe72bb501df5306c75c89383d48a1e81073fbb0a0": "norrisfan",
    "0x37e4728b3c4607fb2b3b205386bb1d1fb1a8c991": "SemyonMarmeladov",
    "0x5c3a1a602848565bb16165fcd460b00c3d43020b": "embarrassment",
    "0xc41d736bded9ed1accd6a44235039266219774fd": "0xC41D736b (#29)",
    "0x45c9c799e0e6ddf19c50e9dac5ab5a925f9b414b": "Uzim000",
    # === Новые (44) ===
    "0x160771e1041ea85cb780f6f9216de8e56259121e": "orangexyz",
    "0x384fdc42f4f9cdee6b311c5d0dba6f81eb4658ad": "Political-Predator",
    "0x03bac23a9d3285eb748b3fabd4c9653d11fda1ef": "KairosHunter",
    "0xcddfe6ceef57bbb2afcb68db611e4ffe26b81b3f": "krimut",
    "0xecaa8806a9a05049d7d5260a33dc924220e377a9": "Hisokaaa",
    "0x87650b9f63563f7c456d9bbcceee5f9faf06ed81": "BobBiswas",
    "0xfd22b8843ae03a33a8a4c5e39ef1e5ff33ebad91": "2B9S",
    "0x88c4919de76e526d55a32c1f8afb439dd1f1129a": "8934394839",
    "0x9236b31fe717c7cda64ad753a3ad3eb4e304e368": "LelouchVilndia",
    "0x613c89dbddbb5c7726eda68911f57fb2cbdee423": "george6688",
    "0x1681006e2a5c3ca35767f215947326591180b1c7": "shuanyang",
    "0x9ba9dec33838f4ec9f032f7247d7481987e63ce9": "TeamA",
    "0x2974bd0059e48f215c391882976e0f1b4c8c9c23": "65765757",
    "0x3de4543d599ffb09386aac2eab198a295511b032": "248188374",
    "0x57cd939930fd119067ca9dc42b22b3e15708a0fb": "Supah9ga",
    "0x53757615de1c42b83f893b79d4241a009dc2aeea": "0x53757615 (#13)",
    "0xc4f8b9cfffec674b34ba2679a920ef5498ec96a4": "feiqiu",
    "0x033f0346c007323030eb420305ffede19a95618e": "TheVeryGoodCow",
    "0xe8dd7741ccb12350957ec71e9ee332e0d1e6ec86": "influenz.eth",
    "0xefddbb135e2cc2648e3ca6a6b3d4fa4994d5017f": "maxgreen",
    "0xc96e5287ab294ac0388c2ddb00180fc464cff1f9": "wigglew",
    "0x398dafc40ced1757f33a263d809f18666ba5c7c3": "Shaktigulya",
    "0x706ccabb8023add7fe4e773aaaab812eb2d6a94b": "horiz0n",
    "0xa6d9b55a6a3a54a9d50ca94c64f00af69c50fd2f": "Amrosein",
    "0xa8c63f775ddbbe66b56614191747def3021444e8": "kinderSman",
    "0xcb25c43d98019b6acf4d6912a231bbb689a45ab5": "Tenebrus7",
    "0x612b36dc9ab6d1371103557ec8ad9ed0d2d16fdd": "aaron107",
    "0x0c0e270cf879583d6a0142fc817e05b768d0434e": "The Spirit of Ukraine>UMA",
    "0xf1bf47707b8e4cec6292eaa6bc47dc25871411d7": "mikemoneywire-20419",
    "0xbeb9d19f10274da29f21625a2c91fa5a00fd3870": "AnonymousElephant69",
    "0x7523cafcee7bcf2db9a79d80e0d79b88a9a54c4c": "DonaldinhoTrumpito",
    "0xf66166919d7d7afc3406d2dd36dd954a2f822259": "suvorov",
    "0x8e77537e059837d3c2ca5b4efe75e74e9498c4f3": "dwpoker",
    "0x1a3fb05e94caef23e28905767cd603eb574a6dea": "alextalley",
    "0x7bb244d0c70293e66dee84f3d0623fbbbf7d682c": "WongKimArk",
    "0x8de5e02553b6afba268e0ccf91f50d881d1f2b04": "adribici",
    "0x16cbe223607a6513ae76d1e3751c78e4eabc2704": "MRF",
    "0xc7a968ac87984729453ba1776d9567f2c8144081": "Rehman010",
    "0x75d4c19708ad084c1cf6a10cfdf528c76fd94027": "maduroisq",
    "0xc4d1a863e9cc45d02ba22d3a1ae9ba7822018ce8": "rdba",
    "0x45b0efd6a5bdbd114d9ed30c505cfbaea1eb4857": "Valued",
    "0x40672269263fe09685e07fd99042fd58956f6ffa": "wenwenwenwenwenwen",
    "0x63b10df4bfa6d03b909ae728ee79964a594c1676": "mostobesegoldfish",
    "0xe74d4976e5e034182d708a3b9df602e72d4722fd": "Pump",
}


def fetch_activity(address: str, limit: int = 500) -> Optional[list]:
    """GET /activity?user=ADDRESS&limit=N"""
    try:
        r = requests.get(
            f"{DATA_API}/activity",
            params={"user": address, "limit": limit},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        return data if isinstance(data, list) else None
    except Exception:
        return None


def analyze_address(address: str, nickname: str) -> dict:
    result = {
        "address":        address,
        "nickname":       nickname,
        "winrate":        None,
        "won":            0,
        "lost":           0,
        "resolved":       0,
        "open":           0,
        "total_redeemed": 0.0,
        "total_invested": 0.0,
        "error":          None,
        "pass":           False,
        "note":           "",
    }

    activity = fetch_activity(address)
    time.sleep(REQUEST_DELAY)

    if activity is None:
        result["error"] = "API failed"
        return result

    if not activity:
        result["note"] = "нет активности"
        return result

    # Группируем по conditionId
    # bought[cid]  = общая сумма вложений (usdcSize BUY)
    # redeemed[cid] = сумма забранного (usdcSize REDEEM)
    bought   = {}   # cid -> total usdcSize invested
    redeemed = {}   # cid -> total usdcSize redeemed

    for event in activity:
        cid  = event.get("conditionId", "")
        typ  = (event.get("type") or "").upper()
        side = (event.get("side") or "").upper()
        usdc = float(event.get("usdcSize") or 0)

        if not cid:
            continue

        if typ == "TRADE" and side == "BUY":
            bought[cid] = bought.get(cid, 0) + usdc

        elif typ == "REDEEM":
            redeemed[cid] = redeemed.get(cid, 0) + usdc

    # Определяем завершённые рынки:
    # Завершённый = был BUY и либо есть REDEEM (выиграл) либо нет (проиграл)
    # Только-REDEEM без BUY пропускаем (мог купить раньше лимита выборки)
    all_bought = set(bought.keys())

    won_cids  = {cid for cid in all_bought if cid in redeemed}
    lost_cids = {cid for cid in all_bought if cid not in redeemed}

    # Открытые позиции — где ещё нет результата (рынок не закрылся).
    # Мы не можем их точно определить без Gamma API, поэтому считаем
    # консервативно: все bought без redeem = проигрыш или открытая позиция.
    # Для минимизации ошибки — считаем только рынки где есть REDEEM как won,
    # а lost — только те где купил но нет redeem И активность старше 7 дней.
    # (свежие могут быть просто открытыми)

    now_ts = int(time.time())
    WEEK = 7 * 86400

    # Находим timestamp последней активности по каждому conditionId
    cid_last_ts = {}
    for event in activity:
        cid = event.get("conditionId", "")
        ts  = int(event.get("timestamp") or 0)
        if cid and ts:
            cid_last_ts[cid] = max(cid_last_ts.get(cid, 0), ts)

    # lost = bought без redeem, где последняя активность > 7 дней назад
    confirmed_lost = {
        cid for cid in lost_cids
        if (now_ts - cid_last_ts.get(cid, now_ts)) > WEEK
    }
    open_cids = lost_cids - confirmed_lost

    won   = len(won_cids)
    lost  = len(confirmed_lost)
    resolved = won + lost

    result["won"]            = won
    result["lost"]           = lost
    result["resolved"]       = resolved
    result["open"]           = len(open_cids)
    result["total_redeemed"] = round(sum(redeemed.values()), 2)
    result["total_invested"] = round(sum(bought.values()), 2)

    if resolved >= MIN_RESOLVED:
        result["winrate"] = round(won / resolved, 4)
        result["pass"]    = result["winrate"] >= MIN_WINRATE
    else:
        result["note"] = f"мало данных ({resolved} завершённых рынков)"

    return result


def main():
    candidates = list(CANDIDATES.items())
    total = len(candidates)
    print(f"Анализируем {total} адресов через /activity...\n")

    results = []
    for i, (address, nickname) in enumerate(candidates, 1):
        pct = int(i / total * 100)
        bar = "█" * (pct // 5) + "░" * (20 - pct // 5)
        print(f"\r[{bar}] {pct}% ({i}/{total}) {nickname[:25]:<25}", end="", flush=True)
        result = analyze_address(address, nickname)
        results.append(result)

    print("\n\nГотово!\n")

    results.sort(key=lambda x: x["winrate"] or 0, reverse=True)

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"Полная статистика → {OUT_JSON}")

    passed  = [r for r in results if r["pass"]]
    failed  = [r for r in results if r["winrate"] is not None and not r["pass"]]
    no_data = [r for r in results if r["winrate"] is None and not r["error"]]
    errors  = [r for r in results if r["error"]]

    print(f"\n{'='*65}")
    print(f"ПРОШЛИ (winrate >= {MIN_WINRATE*100:.0f}%, >= {MIN_RESOLVED} рынков): {len(passed)}")
    print(f"{'='*65}")
    for r in passed:
        wr  = f"{r['winrate']*100:.1f}%"
        roi = f"${r['total_redeemed'] - r['total_invested']:+,.0f}"
        print(f"  {wr:>6}  {r['won']:>3}W/{r['lost']:>3}L  ROI {roi:>10}  {r['nickname']}")

    print(f"\n{'='*65}")
    print(f"НЕ прошли: {len(failed)}")
    print(f"{'='*65}")
    for r in failed:
        wr = f"{r['winrate']*100:.1f}%"
        print(f"  {wr:>6}  {r['won']:>3}W/{r['lost']:>3}L  {r['nickname']}")

    print(f"\n{'='*65}")
    print(f"Нет данных: {len(no_data)}  |  Ошибки: {len(errors)}")
    print(f"{'='*65}")
    for r in no_data:
        print(f"  {r['nickname']:35} — {r['note']}")
    for r in errors:
        print(f"  ERROR: {r['nickname']} — {r['error']}")

    with open(OUT_TXT, "w", encoding="utf-8") as f:
        f.write("# Whitelist Polymarket трейдеров\n")
        f.write(f"# Фильтр: winrate >= {MIN_WINRATE*100:.0f}%, min {MIN_RESOLVED} завершённых рынков\n")
        f.write(f"# Сгенерировано: {time.strftime('%Y-%m-%d %H:%M')}\n")
        f.write(f"# Всего: {len(passed)} адресов\n\n")
        for r in passed:
            wr = f"{r['winrate']*100:.1f}%"
            f.write(f"{r['address']}  # {r['nickname']} (winrate {wr}, {r['won']}W/{r['lost']}L)\n")

    print(f"\nГотовый whitelist → {OUT_TXT} ({len(passed)} адресов)")
    print(f"Полный разбор по каждому адресу — в {OUT_JSON}")


if __name__ == "__main__":
    main()
