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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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
# Data API отвечает ~8 секунд на страницу, поэтому последовательный обход
# 130 кандидатов по 8 страниц занимал бы больше двух часов. Лечится двумя
# приёмами: дешёвым отсевом (см. screen) и параллельными запросами.
DEFAULT_WORKERS = 5

# Соединение на поток: Session переиспользует TCP+TLS, это заметно быстрее
# нового подключения на каждый запрос.
_local = threading.local()


def _session() -> requests.Session:
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers["User-Agent"] = "polymarket-tracker/0.4 (whitelist scoring)"
        _local.session = s
    return s

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
            r = _session().get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:  # нас притормаживают — ждём дольше
                time.sleep(5 * (attempt + 1))
                continue
        except requests.RequestException:
            pass
        time.sleep(2 * (attempt + 1))
    return None


def fetch_activity(address: str, since_ts: float, max_pages: int) -> tuple:
    """Активность адреса до отсечки since_ts. Возвращает (события, дошли_ли_до_края).

    Качаем до отсечки ПО ВРЕМЕНИ, а не фиксированное число страниц: иначе у
    активного адреса окно — неделя, у редкого — год, и PnL несопоставим.
    Именно на этом ловилась нестабильность: SemyonMarmeladov на 8 страницах
    показывал +$125k, на 12 — минус $130k.
    """
    out = []
    reached = False
    for page in range(max_pages):
        chunk = get(f"{DATA_API}/activity",
                    {"user": address, "limit": PAGE, "offset": page * PAGE})
        if not chunk:
            break
        out.extend(chunk)
        if len(chunk) < PAGE:
            reached = True  # история кончилась раньше отсечки
            break
        if min(a["timestamp"] for a in chunk) <= since_ts:
            reached = True  # дошли до отсечки
            break
    return out, reached


def fetch_open_value(address: str, conditions: set) -> float:
    """Стоимость открытых позиций по рынкам из когорты.

    Считаем только те рынки, куда он заходил внутри окна: иначе в PnL попадёт
    стоимость позиций, купленных задолго до окна.
    """
    pos = get(f"{DATA_API}/positions", {"user": address, "limit": 500})
    if not pos:
        return 0.0
    return sum(
        float(p.get("currentValue") or 0)
        for p in pos
        if not conditions or p.get("conditionId") in conditions
    )


def screen(address: str, nickname: str, now: float) -> dict:
    """Этап 1: одна страница истории — жив ли адрес и не поток ли это.

    Полная история стоит 8 запросов по ~8 секунд. Тратить их на того, кто
    молчит полгода, незачем: последняя страница активности отвечает на это
    одним запросом. Возвращает вердикт DROP/WATCH — или None, если адрес
    заслуживает полного разбора.
    """
    acts = get(f"{DATA_API}/activity", {"user": address, "limit": PAGE, "offset": 0})
    if not acts:
        return {"address": address, "nickname": nickname, "verdict": "НЕТ ДАННЫХ",
                "reason": "activity пуст или API не ответил"}

    trades = [a for a in acts if a.get("type") == "TRADE"]
    if not trades:
        return {"address": address, "nickname": nickname, "verdict": "DROP",
                "reason": "ни одной сделки в истории"}

    last_ts = max(a["timestamp"] for a in trades)
    silent_days = (now - last_ts) / 86400.0
    if silent_days > MAX_SILENT_DAYS:
        return {"address": address, "nickname": nickname, "verdict": "DROP",
                "silent_days": round(silent_days, 1), "pnl_usdc": 0,
                "reason": f"молчит {silent_days:.0f} дн (отсев по первой странице)"}

    # Страница — это 500 последних событий. Если они уместились в пару дней,
    # у адреса сотни сделок в месяц: поток, копировать бессмысленно. Считаем
    # это WATCH сразу, без полной истории.
    span_days = (last_ts - min(a["timestamp"] for a in acts)) / 86400.0
    if len(acts) >= PAGE and span_days > 0:
        est_30d = len(trades) * (30.0 / max(span_days, 0.1))
        if est_30d > MAX_TRADES_30D * 3:
            return {"address": address, "nickname": nickname, "verdict": "WATCH",
                    "silent_days": round(silent_days, 1), "pnl_usdc": 0,
                    "trades_30d": int(est_30d),
                    "reason": f"~{est_30d:.0f} сделок/30д — поток (отсев по первой странице)"}
    return None


def analyze(address: str, nickname: str, max_pages: int, now: float,
            window_days: float = 90.0) -> dict:
    since_ts = now - window_days * 86400
    acts_all, reached = fetch_activity(address, since_ts, max_pages)
    # Режем строго по окну: события старше отсечки в расчёт не идут.
    acts = [a for a in acts_all if a.get("timestamp", 0) >= since_ts]
    if not acts:
        return {"address": address, "nickname": nickname, "verdict": "НЕТ ДАННЫХ",
                "reason": "activity пуст или API не ответил"}

    trades = [a for a in acts if a.get("type") == "TRADE"]
    if not trades:
        return {"address": address, "nickname": nickname, "verdict": "DROP",
                "reason": "ни одной сделки в истории"}

    def usdc(a) -> float:
        return float(a.get("usdcSize") or 0)

    # Когорта: рынки, куда он заходил ВНУТРИ окна. Выручку считаем только по
    # ним — иначе погашения позиций, купленных до окна, засчитались бы как
    # прибыль без соответствующих затрат, и метрика льстила бы трейдеру.
    buys = [a for a in trades if a.get("side") == "BUY"]
    cohort = {a.get("conditionId") for a in buys if a.get("conditionId")}
    in_cohort = lambda a: a.get("conditionId") in cohort

    money_in = sum(usdc(a) for a in buys)
    money_out = sum(usdc(a) for a in trades
                    if a.get("side") == "SELL" and in_cohort(a))
    money_out += sum(usdc(a) for a in acts
                     if a.get("type") == "REDEEM" and in_cohort(a))
    open_value = fetch_open_value(address, cohort)

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
        "window_days": window_days,
        "window_complete": reached,  # False — окно не выкачано целиком
        "markets_in_window": len(cohort),
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

    # Если окно не выкачано целиком, PnL занижен (часть выручки за отсечкой) и
    # несопоставим с другими адресами: на 6 страницах тот же трейдер давал
    # +$4k, на 14 — +$40k. Такой цифре нельзя доверять настолько, чтобы
    # копировать сделки, поэтому потолок — WATCH.
    if not reached and res["verdict"] == "PASS":
        res.update(verdict="WATCH",
                   reason=f"{res['reason']}, но окно {window_days:.0f}д не выкачано целиком")
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
    p.add_argument("--window-days", type=float, default=90.0,
                   help="окно расчёта PnL в днях (default: 90)")
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                   help=f"параллельных запросов (default: {DEFAULT_WORKERS})")
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
    done = {"n": 0, "screened": 0}
    lock = threading.Lock()

    def work(item):
        addr, nick = item
        # Этап 1 — одна страница, отсекает мёртвых и потоковых.
        res = screen(addr, nick, now)
        cheap = res is not None
        # Этап 2 — полная история только для выживших.
        if res is None:
            res = analyze(addr, nick, args.max_pages, now, args.window_days)
        with lock:
            done["n"] += 1
            if cheap:
                done["screened"] += 1
            print(f"[{done['n']}/{len(cands)}] {nick[:22]:22} "
                  f"{res.get('verdict', '?'):6} {'(быстрый отсев)' if cheap else ''}",
                  flush=True)
        return res

    print("Этап 1 — дешёвый отсев по одной странице, этап 2 — полная история "
          f"выживших. Потоков: {args.workers}.")
    print()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(work, cands))
    print()
    screened, total = done["screened"], len(cands)
    print(f"Быстрым отсевом закрыто {screened} из {total} — "
          f"полную историю качали только для остальных.")

    order = {"PASS": 0, "WATCH": 1, "DROP": 2, "НЕТ ДАННЫХ": 3}
    results.sort(key=lambda r: (order.get(r.get("verdict"), 9), -r.get("pnl_usdc", 0)))

    print("\n" + "=" * 108)
    print(f"{'вердикт':8} {'ник':22} {'PnL':>12} {'ROI':>8} {'молчит':>8} "
          f"{'сд/30д':>7} {'типичная':>9} {'крупная':>9}")
    print("-" * 108)
    for r in results:
        if "roi" not in r:  # закрыт дешёвым отсевом — полных цифр нет
            print(f"{r['verdict']:8} {r['nickname'][:22]:22} {'—':>12} "
                  f"{'—':>8} {r.get('silent_days', 0):>7.0f}д "
                  f"{r.get('trades_30d', 0):>7} {r.get('reason','')[:28]}")
            continue
        flag = "" if r.get("window_complete", True) else "  окно неполное"
        print(f"{r['verdict']:8} {r['nickname'][:22]:22} ${r['pnl_usdc']:>11,} "
              f"{r['roi']*100:>7.1f}% {r['silent_days']:>7.0f}д {r['trades_30d']:>7} "
              f"${r['typical_buy_usdc']:>8,} ${r['big_buy_usdc']:>8,}{flag}")
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
        "# PnL = (SELL + REDEEM + открытые позиции) - BUY за окно, причём выручка",
        "# считается только по рынкам, куда трейдер заходил ВНУТРИ окна.",
        "# tier=pass  — зарабатывает и торгует с разумной частотой;",
        "# tier=watch — держим под наблюдением, сигналить осторожнее;",
        "# big=$N     — 90-й процентиль его покупок: порог «крупно ДЛЯ НЕГО».",
        "#",
        f"# Отброшено: {counts.get('DROP', 0)} адресов (мертвы или теряют деньги).",
        "",
    ]
    for r in keep:
        if "roi" in r:
            lines.append(
                f"{r['address']}  # {r['nickname']} | tier={r['verdict'].lower()} | "
                f"pnl=${r['pnl_usdc']:,} roi={r['roi']*100:.1f}% | "
                f"big=${r['big_buy_usdc']:,} | {r['reason']}"
            )
        else:
            lines.append(
                f"{r['address']}  # {r['nickname']} | tier={r['verdict'].lower()} | "
                f"{r.get('reason', '')}"
            )
    OUT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Предлагаемый список -> {OUT_TXT} ({len(keep)} адресов)")
    print("Боевой data/whitelist.txt НЕ тронут — сравни и переименуй сам.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
