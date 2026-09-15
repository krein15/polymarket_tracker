#!/usr/bin/env python3
"""Связность оценок: сумма вероятностей взаимоисключающих исходов.

Зачем это лучше, чем ждать закрытия рынков
------------------------------------------
Проверить, прав ли агент, можно только по факту — а это недели. Но одну
вещь можно проверить сегодня и бесплатно: если исходы события
взаимоисключающие и исчерпывающие, их вероятности обязаны давать в сумме
единицу. Агент оценивает каждый рынок ОТДЕЛЬНО и про соседние не знает,
поэтому сумма — честная проверка на вменяемость.

Нашлось в пилоте
----------------
Событие "какая партия получит больше мест в Госдуме":

    Sonnet:  Единая Россия 0.970 + Новые люди 0.020  = 0.99   связно
    Opus:    Единая Россия 0.940 + Новые люди 0.280  = 1.22   противоречие

Обе партии не могут получить больше всех мест. Модель, которая этого не
замечает, даёт числа, а не оценки — и в замер калибровки такие числа
попадать не должны.

Сравниваем с суммой РЫНКА, а не с единицей
------------------------------------------
Первая версия сравнивала с 1.0 и сразу выдала "противоречие" там, где его
не было: у события "кандидат в номинанты 2028" одиннадцать имён покрывают
лишь часть поля, и сам рынок даёт в сумме 0.28. Правильное сравнение —
отношение суммы агента к сумме рынка на том же наборе.

Второе, что нашлось на этом же прогоне
--------------------------------------
Обе модели упираются в собственный пол около 0.01-0.02 и завышают
аутсайдеров в 10-20 раз:

    рынок 0.001 -> Sonnet 0.020, Opus 0.010
    рынок 0.003 -> Sonnet 0.010, Opus 0.020
    рынок 0.011 -> Sonnet 0.040, Opus 0.050

Отсюда сумма 0.43 против рыночных 0.28 — не прозрение, а неумение назвать
число мельче сотой. Любой "перевес", посчитанный на дешёвом рынке, будет
артефактом этого пола, поэтому исходы дешевле MIN_PRICE в проверку не
берутся, а в отборе кандидатов стоит нижняя граница цены.

Запуск:
    python tools/agent_coherence.py --event fed-decision-in-september-762
    python tools/agent_coherence.py --top 1 --models claude-sonnet-5
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
from polymarket_tracker.market_scanner import (  # noqa: E402
    group_by_event,
    pick_candidates,
    yes_price,
)

OUT_JSON = ROOT / "data" / "agent_coherence.json"

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


def fetch_events(top: int, wanted: str = "") -> list:
    """События с несколькими взаимоисключающими исходами.

    Отбор общий с пилотом (market_scanner): свои правила здесь и были
    причиной того, что прогон начался с биткойна — крипту отсеивал только
    соседний файл.
    """
    candidates = pick_candidates(limit=10_000)
    groups = group_by_event(candidates)
    if wanted:
        return [(k, v) for k, v in groups if k == wanted]
    return groups[:top]


def main() -> int:
    p = argparse.ArgumentParser(description="Проверка связности оценок агента.")
    p.add_argument("--event", default="", help="слаг события")
    p.add_argument("--top", type=int, default=1, help="сколько событий взять")
    p.add_argument("--models", default="claude-sonnet-5")
    p.add_argument("--effort", default=DEFAULT_EFFORT)
    args = p.parse_args()

    key = load_key()
    if not key:
        print("[ОШИБКА] ANTHROPIC_API_KEY не найден в .env")
        return 1

    events = fetch_events(args.top, args.event)
    if not events:
        print("Событий с тремя и более взаимоисключающими рынками не нашлось.")
        return 1

    import anthropic
    client = anthropic.Anthropic(api_key=key, timeout=300.0)
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    today = datetime.date.today().isoformat()
    out = []

    for slug, markets in events:
        title = (markets[0].get("events") or [{}])[0].get("title") or slug
        print("=" * 72)
        print(f"СОБЫТИЕ: {title}")
        print(f"Взаимоисключающих исходов: {len(markets)}")
        print("Агент оценивает каждый ОТДЕЛЬНО и про соседние не знает.\n")

        market_sum = sum(yes_price(m) or 0 for m in markets)
        record = {"event": slug, "title": title, "market_sum": market_sum,
                  "outcomes": [], "models": {}}

        for model in models:
            print(f"--- {model} ---")
            total = 0.0
            cost = 0.0
            rows = []
            for m in markets:
                f = forecast(client, m, today, model, effort=args.effort)
                name = m.get("groupItemTitle") or str(m.get("question"))[:40]
                price = yes_price(m) or 0.0
                if not f.ok:
                    print(f"  {str(name)[:38]:<38} ОШИБКА {f.error[:40]}")
                    continue
                total += f.probability
                cost += f.cost_usd
                rows.append({"outcome": name, "price": price,
                             "probability": f.probability,
                             "searches": f.web_searches})
                print(f"  {str(name)[:38]:<38} рынок {price:.3f}   "
                      f"агент {f.probability:.3f}   поисков {f.web_searches}")
            # Сравниваем с суммой РЫНКА, а не с единицей: набор исходов
            # редко исчерпывающий. У "кандидата в номинанты 2028" одиннадцать
            # имён покрывают лишь часть поля, и рынок сам даёт 0.28.
            ratio = total / market_sum if market_sum > 0 else 0.0
            verdict = ("связно" if 0.8 <= ratio <= 1.25 else
                       f"ЗАВЫШАЕТ в {ratio:.1f}x" if ratio > 1.25 else
                       f"занижает в {1/ratio:.1f}x" if ratio > 0 else "нет данных")
            print(f"  {'СУММА':<38} рынок {market_sum:.3f}   "
                  f"агент {total:.3f}   -> {verdict}")
            print(f"  стоимость ${cost:.3f}\n")
            record["models"][model] = {"sum": total, "cost_usd": cost,
                                       "verdict": verdict, "rows": rows}
        out.append(record)

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    print(f"Подробности -> {OUT_JSON}")
    print()
    print("Сильное отклонение от суммы рынка означает, что модель отвечает")
    print("на каждый вопрос изолированно. Такие оценки в замер не годятся.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
