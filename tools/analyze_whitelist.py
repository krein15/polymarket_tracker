#!/usr/bin/env python3
"""Скоринг кандидатов в whitelist по ДЕНЬГАМ, а не по числу погашений.

Почему переписано (30.08.2026)
------------------------------
Версия v3 считала winrate как «REDEEM по conditionId = рынок выигран,
BUY без REDEEM = проигран». Метрика оказалась негодной: тот, кто скупает
много исходов в NegRisk-рынке, гасит выигравшую ногу почти всегда и
показывает 100% побед, теряя при этом деньги.

Проверка на живых данных:
  0xC41D736b       v3: «100% winrate, ROI +$257k»  -> реально -$400k за 56 дн
  LaBradfordSmith22 v3: «100% winrate, ROI +$685k» -> реально -$152k за 37 дн

Что считаем теперь
------------------
Денежный поток по всей доступной истории:

    PnL = (SELL + REDEEM + текущая стоимость открытых позиций) - BUY

Это ровно «сколько человек заработал», без предположений о том, что значит
погашение. Плюс профиль торговли — он решает, годится ли адрес для
копирования вообще:

  * давность последней сделки — мёртвые адреса копировать не с чего;
  * сделок за 30 дней — у кого их сотни, тот маркет-мейкер или скальпер:
    его edge не в информации, а в потоке, и копировать его бессмысленно;
  * типичный и крупный размер сделки — чтобы Ветка B ловила не любую
    сделку от $200, а нетипично крупную ДЛЯ ЭТОГО кошелька.

Ограничение: /activity отдаёт историю страницами по 500 событий. У активных
адресов --max-pages упирается в несколько месяцев, а не в весь срок жизни.
Обрезка играет В ПОЛЬЗУ трейдера ((погашения старых позиций попадают в окно,
а покупки под них — нет), так что отрицательный PnL на таком окне — вывод
надёжный, положительный — требует осторожности.

Запуск:
    python tools/analyze_whitelist.py                     # адреса из data/whitelist.txt
    python tools/analyze_whitelist.py --file кандидаты.txt
    python tools/analyze_whitelist.py --max-pages 20

Результат:
    data/whitelist_scored.json     — полный разбор по каждому адресу
    data/whitelist_proposed.txt    — готовый список с метаданными
Боевой data/whitelist.txt не трогается: сравни и переименуй сам.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from statistics import median

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUT_JSON = DATA_DIR / "whitelist_scored.json"
OUT_TXT = DATA_DIR / "whitelist_proposed.txt"

DATA_API = "https://data-api.polymarket.com"
# Публичный лидерборд Polymarket. Окна: 1d / 7d / 30d / all, максимум 50 строк
# за запрос. Это единственный официальный способ получить список тех, кто
# реально зарабатывает на площадке — вручную такой список не собрать.
LB_API = "https://lb-api.polymarket.com"
LB_WINDOWS = ("30d", "7d", "all")
LB_LIMIT = 50
TIMEOUT = 30
REQUEST_DELAY = 0.35
PAGE = 500

# Windows-консоль работает в cp866/cp1251 и не знает части символов (стрелки,
# галочки). Пока вывод идёт в консоль, Python печатает их через WriteConsoleW,
# но при ПЕРЕНАПРАВЛЕНИИ (> log.txt, Планировщик задач) переключается на
# кодировку локали и падает с UnicodeEncodeError. Заменяем непечатаемое на "?".
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass


# ───────── пороги классификации ─────────

MAX_SILENT_DAYS = 45.0      # дольше молчит — копировать нечего
MIN_PNL_USDC = 5_000.0      # ниже — не отличить от шума
MIN_ROI = 0.03              # 3% на вложенный доллар
MAX_TRADES_30D = 150        # выше — поток, а не решения; копировать бессмысленно
MIN_TRADES_30D = 3          # ниже — сигналов от него всё равно не будет


def get(url: str, params: dict, tries: int = 3):
    for attempt in range(tries):
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 200:
                return r.json()
        except requests.RequestException:
            pass
        time.sleep(2 * (attempt + 1))
    return None


def fetch_activity(address: str, max_pages: int) -> list:
    """Вся доступная активность адреса, страницами по 500."""
    out = []
    for page in range(max_pages):
        chunk = get(f"{DATA_API}/activity",
                    {"user": address, "limit": PAGE, "offset": page * PAGE})
        if not chunk:
            break
        out.extend(chunk)
        if len(chunk) < PAGE:
            break
        time.sleep(REQUEST_DELAY)
    return out


def fetch_open_value(address: str) -> float:
    """Текущая стоимость открытых позиций — незакрытая часть капитала."""
    pos = get(f"{DATA_API}/positions", {"user": address, "limit": 500})
    if not pos:
        return 0.0
    return sum(float(p.get("currentValue") or 0) for p in pos)


def analyze(address: str, nickname: str, max_pages: int, now: float) -> dict:
    acts = fetch_activity(address, max_pages)
    if not acts:
        return {"address": address, "nickname": nickname, "verdict": "НЕТ ДАННЫХ",
                "reason": "activity пуст или API не ответил"}

    trades = [a for a in acts if a.get("type") == "TRADE"]
    if not trades:
        return {"address": address, "nickname": nickname, "verdict": "DROP",
                "reason": "ни одной сделки в истории"}

    def usdc(a) -> float:
        return float(a.get("usdcSize") or 0)

    buys = [a for a in trades if a.get("side") == "BUY"]
    money_in = sum(usdc(a) for a in buys)
    money_out = sum(usdc(a) for a in trades if a.get("side") == "SELL")
    money_out += sum(usdc(a) for a in acts if a.get("type") == "REDEEM")
    open_value = fetch_open_value(address)

    pnl = money_out + open_value - money_in
    roi = (pnl / money_in) if money_in > 0 else 0.0

    last_ts = max(a["timestamp"] for a in trades)
    silent_days = (now - last_ts) / 86400.0
    trades_30d = sum(1 for a in trades if now - a["timestamp"] < 30 * 86400)
    span_days = (last_ts - min(a["timestamp"] for a in trades)) / 86400.0

    buy_sizes = sorted(usdc(a) for a in buys if usdc(a) > 0)
    typical = median(buy_sizes) if buy_sizes else 0.0
    # Порог «нетипично крупно для него»: 90-й процентиль его же покупок.
    big = buy_sizes[int(len(buy_sizes) * 0.9)] if buy_sizes else 0.0

    res = {
        "address": address, "nickname": nickname,
        "pnl_usdc": round(pnl), "roi": round(roi, 4),
        "money_in_usdc": round(money_in), "money_out_usdc": round(money_out),
        "open_value_usdc": round(open_value),
        "silent_days": round(silent_days, 1), "trades_30d": trades_30d,
        "trades_total": len(trades), "history_span_days": round(span_days, 1),
        "typical_buy_usdc": round(typical), "big_buy_usdc": round(big),
        "history_truncated": len(acts) >= max_pages * PAGE,
    }

    # ───────── вердикт ─────────
    if silent_days > MAX_SILENT_DAYS:
        res.update(verdict="DROP", reason=f"молчит {silent_days:.0f} дн")
    elif pnl <= 0:
        res.update(verdict="DROP", reason=f"PnL ${pnl:,.0f} — теряет деньги")
    elif trades_30d > MAX_TRADES_30D:
        res.update(verdict="WATCH",
                   reason=f"{trades_30d} сделок/30д — поток, а не решения")
    elif trades_30d < MIN_TRADES_30D:
        res.update(verdict="WATCH", reason=f"почти не торгует ({trades_30d}/30д)")
    elif pnl < MIN_PNL_USDC or roi < MIN_ROI:
        res.update(verdict="WATCH",
                   reason=f"слабо: ${pnl:,.0f} при ROI {roi*100:.1f}%")
    else:
        res.update(verdict="PASS",
                   reason=f"${pnl:,.0f} при ROI {roi*100:.1f}%, {trades_30d} сделок/30д")
    return res


def fetch_leaderboard(windows=LB_WINDOWS, limit: int = LB_LIMIT) -> list:
    """Кандидаты с лидерборда по прибыли, объединённые по окнам.

    Окна берём разные не случайно: "all" даёт заслуженных ветеранов, которые
    могли давно остыть, "30d" — тех, кто в форме прямо сейчас. Дубликаты
    схлопываем, дальше каждого всё равно проверяет полный скоринг.
    """
    seen = {}
    for window in windows:
        data = get(f"{LB_API}/profit", {"window": window, "limit": limit})
        if not data:
            print(f"  лидерборд {window}: не ответил")
            continue
        print(f"  лидерборд {window}: {len(data)} адресов")
        for row in data:
            addr = str(row.get("proxyWallet") or "").lower()
            if not addr.startswith("0x"):
                continue
            nick = row.get("pseudonym") or row.get("name") or addr[:10]
            # Ник первого попадания сохраняем, окно дописываем к нему.
            if addr in seen:
                seen[addr] = (seen[addr][0], seen[addr][1] + f",{window}")
            else:
                seen[addr] = (str(nick), window)
        time.sleep(REQUEST_DELAY)
    return [(a, f"{n} [{w}]") for a, (n, w) in seen.items()]


def load_candidates(path: Path) -> list:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        addr = line.split("#")[0].strip().split()[0].lower()
        nick = "?"
        if "#" in line:
            nick = line.split("#", 1)[1].split("|")[0].strip() or "?"
        if addr.startswith("0x") and len(addr) == 42:
            out.append((addr, nick))
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="Скоринг кандидатов в whitelist по деньгам.")
    p.add_argument("--file", default=str(DATA_DIR / "whitelist.txt"),
                   help="файл с адресами (default: data/whitelist.txt)")
    p.add_argument("--max-pages", type=int, default=12,
                   help="страниц истории по 500 событий (default: 12)")
    p.add_argument("--from-leaderboard", action="store_true",
                   help="взять кандидатов с лидерборда Polymarket вместо файла")
    p.add_argument("--plus-current", action="store_true",
                   help="с --from-leaderboard: добавить текущий whitelist к кандидатам")
    args = p.parse_args()

    if args.from_leaderboard:
        print("Собираю кандидатов с лидерборда Polymarket:")
        cands = fetch_leaderboard()
        if args.plus_current and Path(args.file).exists():
            current = load_candidates(Path(args.file))
            known = {a for a, _ in cands}
            extra = [(a, n) for a, n in current if a not in known]
            cands += extra
            print(f"  плюс текущий whitelist: +{len(extra)} адресов")
        if not cands:
            print("[ОШИБКА] Лидерборд не ответил — попробуй позже")
            return 1
    else:
        src = Path(args.file)
        if not src.exists():
            print(f"[ОШИБКА] Нет файла {src}")
            return 1
        cands = load_candidates(src)
    print(f"Кандидатов: {len(cands)}. История: до {args.max_pages * PAGE} событий на адрес.")
    print("Это займёт время — API отдаёт страницами по 500.\n")

    now = time.time()
    results = []
    for i, (addr, nick) in enumerate(cands, 1):
        print(f"[{i}/{len(cands)}] {nick[:24]:24} {addr[:12]}...", flush=True)
        results.append(analyze(addr, nick, args.max_pages, now))
        time.sleep(REQUEST_DELAY)

    order = {"PASS": 0, "WATCH": 1, "DROP": 2, "НЕТ ДАННЫХ": 3}
    results.sort(key=lambda r: (order.get(r.get("verdict"), 9), -r.get("pnl_usdc", 0)))

    print("\n" + "=" * 108)
    print(f"{'вердикт':8} {'ник':22} {'PnL':>12} {'ROI':>8} {'молчит':>8} "
          f"{'сд/30д':>7} {'типичная':>9} {'крупная':>9}")
    print("-" * 108)
    for r in results:
        if "pnl_usdc" not in r:
            print(f"{r['verdict']:8} {r['nickname'][:22]:22} {r.get('reason','')}")
            continue
        print(f"{r['verdict']:8} {r['nickname'][:22]:22} ${r['pnl_usdc']:>11,} "
              f"{r['roi']*100:>7.1f}% {r['silent_days']:>7.0f}д {r['trades_30d']:>7} "
              f"${r['typical_buy_usdc']:>8,} ${r['big_buy_usdc']:>8,}")
    print("=" * 108)

    counts = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    print("Итог:", ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))

    OUT_JSON.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nПолный разбор -> {OUT_JSON}")

    keep = [r for r in results if r["verdict"] in ("PASS", "WATCH")]
    lines = [
        "# Whitelist Polymarket — отобран по денежному потоку, не по winrate",
        f"# Сгенерировано: {time.strftime('%Y-%m-%d %H:%M')} (tools/analyze_whitelist.py)",
        "#",
        "# PnL = (SELL + REDEEM + открытые позиции) - BUY по доступной истории.",
        "# tier=pass  — зарабатывает и торгует с разумной частотой;",
        "# tier=watch — держим под наблюдением, сигналить осторожнее;",
        "# big=$N     — 90-й процентиль его покупок: порог «крупно ДЛЯ НЕГО».",
        "#",
        f"# Отброшено: {counts.get('DROP', 0)} адресов (мертвы или теряют деньги).",
        "",
    ]
    for r in keep:
        lines.append(
            f"{r['address']}  # {r['nickname']} | tier={r['verdict'].lower()} | "
            f"pnl=${r['pnl_usdc']:,} roi={r['roi']*100:.1f}% | "
            f"big=${r['big_buy_usdc']:,} | {r['reason']}"
        )
    OUT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Предлагаемый список -> {OUT_TXT} ({len(keep)} адресов)")
    print("Боевой data/whitelist.txt НЕ тронут — сравни и переименуй сам.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
