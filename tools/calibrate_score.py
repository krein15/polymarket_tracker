#!/usr/bin/env python3
"""Подбор SCORE_THRESHOLD по накопленным данным вместо угадывания.

Прогоняет строки shadow_trades через тот же FeatureExtractor и compute_score,
что работают в бою, и показывает:

  * распределение баллов — где вообще лежит масса;
  * сколько сигналов в час дал бы каждый порог;
  * (когда появятся resolved) winrate по корзинам баллов.

Порог выбирают по двум критериям сразу: поток сигналов, который реально
успеваешь читать, и — как только накопятся исходы — точка, где winrate
перестаёт расти. До появления resolved-данных это подбор потока, не качества;
скрипт об этом честно предупреждает.

Запуск из корня проекта (read-only, трекер останавливать не нужно):

    python tools/calibrate_score.py
    python tools/calibrate_score.py --thresholds 20,30,40,50

Ограничение: shadow_trades содержит только покупки от MIN_TRADE_USDC, поэтому
оценка потока — нижняя граница: сигналы от набора позиции мелкими покупками
сюда не попадают.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from polymarket_tracker.config import Config  # noqa: E402
from polymarket_tracker.data_api_listener import Trade  # noqa: E402
from polymarket_tracker.market_context import MarketInfo  # noqa: E402
from polymarket_tracker.scoring import FeatureExtractor, compute_score  # noqa: E402
from polymarket_tracker.storage import Storage  # noqa: E402
from polymarket_tracker.wallet_analyzer import WalletAnalyzer  # noqa: E402

# Windows-консоль работает в cp866/cp1251 и не знает части символов (стрелки,
# галочки). Пока вывод идёт в консоль, Python печатает их через WriteConsoleW,
# но при ПЕРЕНАПРАВЛЕНИИ (> log.txt, Планировщик задач) переключается на
# кодировку локали и падает с UnicodeEncodeError. Заменяем непечатаемое на "?".
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):  # не TextIOWrapper — не наша забота
        pass


DEFAULT_DB = str(ROOT / "data" / "tracker.db")
DEFAULT_THRESHOLDS = (20, 30, 35, 40, 45, 50, 60, 70)


def build_trade(row: sqlite3.Row) -> Trade:
    return Trade(
        tx_hash=row["tx_hash"], log_index=0, block_number=0,
        timestamp=row["ts"], exchange="data_api", maker=row["maker"], taker="",
        side=row["side"], token_id=row["token_id"],
        usdc_amount=row["usdc_amount"], shares=0.0, price=row["price"],
    )


def build_market(row: sqlite3.Row) -> MarketInfo:
    """Рынок восстанавливаем из полей shadow-строки.

    Тегов там нет — сохранена только сводная категория, её и кладём в tags,
    чтобы фильтр категорий отработал так же, как в бою.
    """
    category = row["category"] or ""
    return MarketInfo(
        condition_id="", question="", slug=row["market_slug"] or "",
        category=category, volume_24h=row["volume_24h"] or 0.0,
        volume_total=0.0, liquidity=0.0, end_date_iso=None, outcome="",
        closed=False, tags=frozenset([category] if category else []),
        event_slug="",
    )


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Подбор SCORE_THRESHOLD по данным.")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--env", default=str(ROOT / ".env"))
    p.add_argument("--thresholds", default=",".join(map(str, DEFAULT_THRESHOLDS)))
    args = p.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"БД не найдена: {db_path}")
        return 1

    config = Config.from_env(args.env)
    storage = Storage(str(db_path))
    analyzer = WalletAnalyzer(storage, config)
    extractor = FeatureExtractor(storage, config)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT tx_hash, maker, token_id, ts, side, usdc_amount, price, "
        "market_slug, category, volume_24h, market_resolved, trader_was_right "
        "FROM shadow_trades ORDER BY ts"
    ).fetchall()
    conn.close()

    if not rows:
        print("shadow_trades пуст — калибровать нечего, дай трекеру поработать.")
        return 0

    span_hours = max(1e-9, (rows[-1]["ts"] - rows[0]["ts"]) / 3600.0)
    scored = []
    skipped_gates = 0

    for row in rows:
        trade = build_trade(row)
        market = build_market(row)
        if trade.side != "buy" or trade.usdc_amount < config.scoring_min_trade_usdc:
            skipped_gates += 1
            continue
        tags = {market.category} | set(market.tags)
        if not (config.allowed_tags & tags) and (config.ignored_categories & tags):
            skipped_gates += 1
            continue
        wallet_stats = storage.get_wallet(trade.maker)
        if wallet_stats is None:
            skipped_gates += 1
            continue
        assessment = analyzer.assess(wallet_stats)
        score = compute_score(extractor.extract(trade, market, assessment), config)
        scored.append((score.total, row))

    print("═══ Калибровка SCORE_THRESHOLD ═══")
    print(f"БД: {db_path}")
    print(f"shadow-строк: {len(rows)}, из них оценено: {len(scored)}, "
          f"отсеяно воротами: {skipped_gates}")
    print(f"окно данных: {span_hours:.1f} ч")
    if not scored:
        print("\nНи одна строка не прошла ворота — нечего калибровать.")
        return 0

    totals = sorted(t for t, _ in scored)
    print()
    print("─── Распределение баллов ───")
    for q, label in ((0.5, "медиана"), (0.75, "75-й перцентиль"),
                     (0.9, "90-й перцентиль"), (0.99, "99-й перцентиль")):
        idx = min(len(totals) - 1, int(q * len(totals)))
        print(f"  {label:18} {totals[idx]:6.0f}")
    print(f"  {'максимум':18} {totals[-1]:6.0f}")

    print()
    print("─── Сколько сигналов дал бы порог ───")
    print(f"  {'порог':>6} {'сигналов':>9} {'в час':>8}   winrate по resolved")
    for th in [float(x) for x in args.thresholds.split(",") if x.strip()]:
        hits = [r for t, r in scored if t >= th]
        resolved = [r for r in hits if r["market_resolved"]]
        wins = sum(1 for r in resolved if r["trader_was_right"])
        wr = f"{100*wins/len(resolved):.0f}% ({len(resolved)} resolved)" if resolved else "— нет данных"
        print(f"  {th:6.0f} {len(hits):9} {len(hits)/span_hours:8.1f}   {wr}")

    print()
    print("Как читать. Пока resolved пусто, выбирай порог по потоку, который")
    print("реально успеваешь смотреть. Когда исходы накопятся (≥30 в корзине),")
    print("двигай порог туда, где winrate перестаёт расти: выше — сигнал,")
    print("ниже — шум. Оценка потока занижена: сюда не попадают сигналы от")
    print(f"набора позиции покупками мельче ${config.min_trade_usdc:.0f}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
