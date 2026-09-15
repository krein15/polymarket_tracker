#!/usr/bin/env python3
"""Пилот: сравнить две модели на одних и тех же рынках.

Отвечает на три вопроса, которые иначе решаются спором:

  1. Сколько это стоит на самом деле — по фактическим токенам из usage,
     а не по прикидке.
  2. Расходятся ли модели между собой. Если близки — платить втрое незачем,
     и бюджет растягивается на настоящий замер калибровки.
  3. Читает ли модель КРИТЕРИИ РАСЧЁТА, а не заголовок. Для этого в выводе
     печатается поле resolution_note — его проверяют глазами.

Чего пилот НЕ отвечает: прав ли агент. Это станет известно, только когда
рынки закроются. Здесь мы смотрим на стоимость, расхождение моделей и
вменяемость чтения правил.

Запуск (нужен ANTHROPIC_API_KEY в .env):
    python tools/agent_pilot.py
    python tools/agent_pilot.py --markets 8 --models claude-sonnet-5
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from polymarket_tracker.forecaster import DEFAULT_EFFORT, forecast  # noqa: E402
from polymarket_tracker.market_filter import is_ignored  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

GAMMA = "https://gamma-api.polymarket.com/markets"
OUT_JSON = ROOT / "data" / "agent_pilot.json"

# Отбор. Ликвидность нужна, но крупные рынки не нужны: там нас уже
# опередили. Крайние цены отбрасываем — на них нечего выигрывать, а
# неблагоприятный отбор максимален.
MIN_LIQUIDITY = 5_000
MAX_LIQUIDITY = 400_000
MIN_PRICE, MAX_PRICE = 0.10, 0.90
MIN_DAYS, MAX_DAYS = 1, 21

# Киберспорт внутри матча: замерено, что там перевес отрицательный даже у
# самого трейдера. Агенту там тоже нечего делать — исход решается на
# экране, а не в новостях.
IGNORED = {"sports", "crypto"}


def load_key() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("ANTHROPIC_API_KEY="):
            return line.split("=", 1)[1].split("#")[0].strip()
    return ""


def prices(market: dict):
    try:
        raw = json.loads(market.get("outcomePrices") or "[]")
        return [float(x) for x in raw]
    except (ValueError, TypeError):
        return []


def outcomes(market: dict):
    try:
        return json.loads(market.get("outcomes") or "[]")
    except (ValueError, TypeError):
        return []


def pick_markets(limit: int) -> list:
    """Кандидаты: ликвидные, закрываются скоро, цена не в крайностях."""
    now = datetime.datetime.now(datetime.timezone.utc)
    got = []
    for page in range(4):
        r = requests.get(GAMMA, params={
            "closed": "false", "limit": "100", "offset": str(page * 100),
            "order": "volume24hr", "ascending": "false",
            # Без include_tag Gamma не возвращает теги ВООБЩЕ: поле приходит
            # пустым, фильтру нечего проверять, и в пилот залетела крипта —
            # ровно те рынки, где у агента заведомо нет шансов.
            "include_tag": "true",
        }, timeout=30)
        if r.status_code != 200:
            break
        batch = r.json()
        if not batch:
            break
        got += batch

    out = []
    for m in got:
        if outcomes(m) != ["Yes", "No"]:
            continue          # многоисходные рынки требуют другой вопрос
        p = prices(m)
        if len(p) != 2 or not (MIN_PRICE <= p[0] <= MAX_PRICE):
            continue
        liq = float(m.get("liquidity") or 0)
        if not (MIN_LIQUIDITY <= liq <= MAX_LIQUIDITY):
            continue
        end = m.get("endDate")
        if not end:
            continue
        try:
            days = (datetime.datetime.fromisoformat(
                end.replace("Z", "+00:00")) - now).days
        except ValueError:
            continue
        if not (MIN_DAYS <= days <= MAX_DAYS):
            continue
        # Тот же чёрный список, что у остальных сигналов.
        tags = {str(t.get("slug") if isinstance(t, dict) else t)
                for t in (m.get("tags") or [])}
        if not tags:
            continue      # без тегов не отличить крипту от политики — пропускаем
        fake = type("M", (), {"category": "", "tags": tags})()
        if is_ignored(fake, set(), IGNORED):
            continue
        if len(str(m.get("description") or "")) < 80:
            continue          # без правил расчёта оценивать нечего
        m["_days"] = days
        m["_price_yes"] = p[0]
        out.append(m)
        if len(out) >= limit:
            break
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="Пилот агента-оценщика.")
    p.add_argument("--markets", type=int, default=10)
    p.add_argument("--models", default="claude-sonnet-5,claude-opus-5")
    p.add_argument("--effort", default=DEFAULT_EFFORT,
                   help="ниже high модель перестаёт искать — проверено")
    args = p.parse_args()

    key = load_key()
    if not key:
        print("[ОШИБКА] ANTHROPIC_API_KEY не найден в .env")
        return 1

    import anthropic
    client = anthropic.Anthropic(api_key=key, timeout=300.0)

    markets = pick_markets(args.markets)
    if not markets:
        print("Подходящих рынков не нашлось — ослабь фильтры в начале файла.")
        return 1
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    today = datetime.date.today().isoformat()

    print(f"Рынков: {len(markets)}, моделей: {len(models)}, дата: {today}")
    print("Модель НЕ видит цену рынка — цена подставляется после оценки.\n")

    results = []
    for i, m in enumerate(markets, 1):
        print(f"[{i}/{len(markets)}] {str(m.get('question'))[:78]}")
        print(f"     закрытие через {m['_days']} дн., "
              f"ликвидность ${float(m.get('liquidity') or 0):,.0f}, "
              f"рынок говорит {m['_price_yes']:.3f}")
        row = {"question": m.get("question"), "slug": m.get("slug"),
               "price_yes": m["_price_yes"], "days": m["_days"],
               "liquidity": float(m.get("liquidity") or 0), "models": {}}
        for model in models:
            f = forecast(client, m, today, model, effort=args.effort)
            if not f.ok:
                print(f"     {model:<18} ОШИБКА: {f.error[:70]}")
                row["models"][model] = {"error": f.error}
                continue
            diff = f.probability - m["_price_yes"]
            print(f"     {model:<18} {f.probability:.3f}  "
                  f"({diff:+.3f} к рынку)  {f.confidence:<6} "
                  f"поисков {f.web_searches}  ${f.cost_usd:.4f}")
            row["models"][model] = {
                "probability": f.probability, "confidence": f.confidence,
                "cost_usd": f.cost_usd, "input_tokens": f.input_tokens,
                "output_tokens": f.output_tokens, "searches": f.web_searches,
                "resolution_note": f.resolution_note, "reasoning": f.reasoning,
                "key_facts": f.key_facts,
            }
        results.append(row)
        print()

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(results, ensure_ascii=False, indent=1),
                        encoding="utf-8")

    print("=" * 70)
    print("ИТОГ\n")
    for model in models:
        got = [r["models"][model] for r in results
               if model in r["models"] and "probability" in r["models"][model]]
        if not got:
            print(f"  {model}: ни одной успешной оценки")
            continue
        total = sum(g["cost_usd"] for g in got)
        diffs = [abs(r["models"][model]["probability"] - r["price_yes"])
                 for r in results if "probability" in r["models"].get(model, {})]
        print(f"  {model}")
        print(f"    оценок {len(got)}, стоимость ${total:.3f} "
              f"(${total/len(got):.4f} за рынок)")
        print(f"    среднее расхождение с рынком {sum(diffs)/len(diffs):.3f}")
        print(f"    на $19 хватило бы примерно на {19/(total/len(got)):.0f} оценок")

    if len(models) == 2:
        pairs = [(r["models"][models[0]].get("probability"),
                  r["models"][models[1]].get("probability"))
                 for r in results
                 if all("probability" in r["models"].get(m, {}) for m in models)]
        if pairs:
            gap = sum(abs(a - b) for a, b in pairs) / len(pairs)
            print()
            print(f"  Модели расходятся между собой в среднем на {gap:.3f}")
            print("  Если это заметно меньше расхождения с рынком —")
            print("  платить за более дорогую модель нет смысла.")

    print(f"\nПодробности (включая чтение правил) -> {OUT_JSON}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
