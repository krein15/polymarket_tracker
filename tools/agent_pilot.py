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

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from polymarket_tracker.forecaster import DEFAULT_EFFORT, forecast  # noqa: E402
from polymarket_tracker.market_scanner import pick_candidates  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

OUT_JSON = ROOT / "data" / "agent_pilot.json"

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass


def load_key() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("ANTHROPIC_API_KEY="):
            return line.split("=", 1)[1].split("#")[0].strip()
    return ""


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

    markets = pick_candidates(args.markets)
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
