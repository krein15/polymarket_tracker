"""Скоринг сделок — взвешенная оценка вместо жёсткой цепочки И.

Зачем. Ветка A требовала совпадения ВСЕХ условий сразу (размер, категория,
объём рынка, новизна кошелька, цена). Это ловит один сценарий — новичок сразу
заходит крупно — и пропускает остальные. Хуже другое: при И-фильтрации нельзя
измерить вклад отдельного условия, потому что до исхода доживают только сделки,
прошедшие все фильтры разом. Скоринг это чинит: балл и его разбивка пишутся для
ВСЕЙ теневой выборки, поэтому потом видно, какой признак реально несёт alpha,
а какой шумит.

Признаки (веса подобраны на глаз — это стартовая гипотеза, а не истина;
калибровать по накопленной статистике, см. docs/ROADMAP.md):

    market_relative  0..35  во сколько раз набранная позиция больше обычного
                            часового оборота этого рынка. Главный признак:
                            $5000 на рынке с оборотом $300/день и на рынке с
                            $40000/день — разные события.
    accumulation     0..15  позиция набиралась частями (дробление ордера) —
                            порог на одну сделку такого трейдера не видит.
    wallet_new       0..15  кошелёк новый по текущим критериям конфига.
    dormant_wake     0..15  кошелёк молчал месяцами и вдруг берёт крупно.
    cluster          0..20  сколько НОВЫХ кошельков зашло в этот же исход за
                            окно. Именно новых: просто «много участников» —
                            это активность рынка, её уже меряет
                            market_relative.
    cheap_tail       0..10  вход в дешёвый хвост (<=0.25) — классический
                            профиль «знал заранее».
    illiquid_market  0..10  абсолютная неликвидность рынка. Раньше была
                            жёстким фильтром, теперь просто слагаемое.

Штрафы:

    hedge           -60  кошелёк купил ОБА исхода рынка (YES и NO): ставка
                         на оба результата мнения не выражает
    market_maker    -40  кошелёк торговал обе стороны этого рынка: это
                         маркет-мейкер или арбитражник, а не инсайдер.
                         Фактически дисквалификация.
    near_resolved   -25  цена выше max_trade_price: рынок почти решён,
                         торговой ценности в сигнале нет.

Максимум без штрафов — 120 баллов.

Холодный старт. Признаки wallet_new и cluster опираются на локальную историю:
кошелёк «новый», если мы мало его видели. Пока история короче HISTORY_MIN_DAYS,
новыми выглядят почти все (замер на живой БД: возраст истории 1.4 часа → 95%
кошельков «новые»), и оба признака давали 35 баллов практически даром. Поэтому
на молодой БД они не начисляются вовсе, а балл держится на признаках потока —
market_relative, accumulation, cheap_tail, illiquid_market, — которые считаются
от объёмов Gamma и надёжны с первого дня. По мере накопления истории признаки
кошелька включаются сами.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .config import Config
    from .data_api_listener import Trade
    from .market_context import MarketInfo
    from .storage import Storage
    from .wallet_analyzer import WalletAssessment


# ═══════════════════════════════════════════════════════════════════
# Пороги и баллы. Вынесены константами, чтобы менять их осознанно и
# одним местом: любое изменение обнуляет сопоставимость накопленной
# статистики, поэтому правки — только после замера по shadow-выборке.
# ═══════════════════════════════════════════════════════════════════

# market_relative: (во сколько раз больше базового часового оборота, баллы)
MARKET_RELATIVE_STEPS = ((30.0, 35), (10.0, 26), (3.0, 18), (1.0, 10))

# accumulation: (число покупок в окне, баллы)
ACCUMULATION_STEPS = ((6, 15), (3, 10))

WALLET_NEW_POINTS = 15

# dormant_wake: (сколько дней молчал, баллы)
DORMANT_STEPS = ((90.0, 15), (30.0, 10))

# cluster: (сколько разных кошельков за окно, баллы)
CLUSTER_STEPS = ((8, 20), (5, 15), (3, 10))

# cheap_tail: (цена входа не выше, баллы)
CHEAP_TAIL_STEPS = ((0.15, 10), (0.25, 5))

ILLIQUID_POINTS = 10

# Пока локальной истории меньше — не начисляем баллы за признаки кошелька
# (новизна, кластер новых): в свежей БД «новый» значит лишь «мы его не видели».
HISTORY_MIN_DAYS = 3.0

# Баллы за присутствие в whitelist. Уровень watch своего сигнала не даёт
# (см. watchlist.py), но как признак он ценен: покупка от того, кто на
# площадке в плюсе, — довод в пользу сделки.
WHITELIST_PASS_POINTS = 30
WHITELIST_WATCH_POINTS = 15

MARKET_MAKER_PENALTY = -40
# Покупка обоих исходов — не ставка на результат, а хедж или арбитраж.
# Штраф жёстче, чем у скальпинга: тут сделка вообще не несёт мнения.
HEDGE_PENALTY = -60
NEAR_RESOLVED_PENALTY = -25

# Базовый часовой оборот считаем по локальной истории за неделю, но только
# если её накопилось хотя бы на несколько часов — иначе цифра случайная.
BASELINE_WINDOW_HOURS = 168
BASELINE_MIN_HOURS = 6


@dataclass
class Features:
    """Сырые измерения по сделке. Отделены от баллов, чтобы их можно было
    записать, посмотреть глазами и пересчитать баллы задним числом."""

    accumulated_usdc: float  # набрано кошельком по токену за окно (с текущей)
    accumulation_trades: int  # сколько сделок в этом наборе
    baseline_hourly: Optional[float]  # обычный часовой оборот рынка
    market_relative: Optional[float]  # accumulated / baseline_hourly
    dormant_days: Optional[float]  # сколько молчал до этой сделки
    cluster_new_wallets: int  # НОВЫХ кошельков в исходе за окно
    cluster_all_wallets: int  # всего участников за окно — для контекста в пояснении
    history_days: float  # сколько дней локальной истории накоплено
    is_new_wallet: bool
    is_market_maker: bool
    price: float
    volume_24h: float
    whitelist_tier: str = ""
    # Признаки кошелька посчитаны по истории из API, а не по локальной базе.
    # Если True — правило холодного старта не применяется: цифры честные
    # с первой же сделки.
    wallet_from_api: bool = False
    is_hedged: bool = False  # купил ОБА исхода рынка — мнения о результате нет  # "pass" | "watch" | "" — уровень в whitelist


@dataclass
class Score:
    """Итоговый балл и его разбивка."""

    total: float
    parts: dict = field(default_factory=dict)  # признак -> баллы
    # Максимум, достижимый на ЭТОЙ сделке: признаки кошелька считаются, только
    # если их есть чем посчитать, whitelist — только если адрес в списке.
    # Нужен, чтобы порог не зависел от того, какие источники сейчас доступны:
    # при недоступном API максимум падает со 120 до 70, и фиксированный порог
    # превращается в «нужен идеальный максимум».
    available_max: float = 0.0
    notes: list = field(default_factory=list)  # человекочитаемые пояснения

    def parts_json(self) -> str:
        return json.dumps(self.parts, ensure_ascii=False, sort_keys=True)

    def summary(self) -> str:
        """Короткая строка вида "market_relative +26, cluster +15, ...".

        Только ненулевые слагаемые, по убыванию вклада.
        """
        items = sorted(
            (kv for kv in self.parts.items() if kv[1]),
            key=lambda kv: -abs(kv[1]),
        )
        return ", ".join(f"{name} {pts:+.0f}" for name, pts in items)


def _steps(value: float, steps) -> int:
    """Первый порог, который value перешагнул. steps — по убыванию порога."""
    for threshold, points in steps:
        if value >= threshold:
            return points
    return 0


def _steps_desc(value: float, steps) -> int:
    """То же, но для порогов "не больше" (цена)."""
    for threshold, points in steps:
        if value <= threshold:
            return points
    return 0


class FeatureExtractor:
    """Собирает признаки по сделке из локальной БД.

    Вызывается только для сделок, прошедших дешёвый предфильтр (покупка не
    меньше min_trade_usdc), — каждый признак это отдельный запрос к SQLite.
    """

    # Возраст истории меняется медленно, а MIN(ts) — скан таблицы: кэшируем.
    _HISTORY_TTL_SEC = 300

    def __init__(self, storage: "Storage", config: "Config"):
        self.storage = storage
        self.config = config
        self._history_cache: tuple = (0, 0.0)  # (когда посчитали, значение)

    def _history_days(self, now_ts: int) -> float:
        cached_at, value = self._history_cache
        if now_ts - cached_at > self._HISTORY_TTL_SEC:
            value = self.storage.history_days(now_ts)
            self._history_cache = (now_ts, value)
        return value

    def extract(
        self,
        trade: "Trade",
        market: "MarketInfo",
        wallet: "WalletAssessment",
        whitelist_tier: str = "",
        history=None,
    ) -> Features:
        cfg = self.config
        acc_since = trade.timestamp - cfg.accumulation_window_seconds

        # ВАЖНО: текущая сделка уже сохранена в trades к моменту вызова,
        # поэтому она входит и в накопление, и в кластер.
        accumulated, acc_trades = self.storage.sum_wallet_buys_for_token(
            trade.maker, trade.token_id, acc_since
        )
        if accumulated <= 0:  # страховка, если сделку почему-то не сохранили
            accumulated, acc_trades = trade.usdc_amount, 1

        baseline = self.storage.market_hourly_baseline(
            trade.token_id, trade.timestamp, BASELINE_WINDOW_HOURS, BASELINE_MIN_HOURS
        )
        if not baseline or baseline <= 0:
            # Локальной истории мало (нормально на свежей БД) — падаем на
            # суточный объём из Gamma, размазанный по часам.
            baseline = (market.volume_24h / 24.0) if market.volume_24h > 0 else None

        relative = (accumulated / baseline) if baseline and baseline > 0 else None

        # История из API точнее локальной: она знает кошелёк с его первой
        # сделки, а не с момента, когда мы его впервые увидели.
        if history is not None:
            prev_ts = history.prev_trade_ts(trade.timestamp)
            if prev_ts is None:
                prev_ts = self.storage.wallet_prev_trade_ts(trade.maker, trade.timestamp)
            is_new_wallet = history.is_new(
                cfg.new_wallet_max_trades, cfg.new_wallet_max_age_days, trade.timestamp
            )
        else:
            prev_ts = self.storage.wallet_prev_trade_ts(trade.maker, trade.timestamp)
            is_new_wallet = wallet.is_new
        dormant_days = (
            (trade.timestamp - prev_ts) / 86400.0 if prev_ts is not None else None
        )

        cluster_since = trade.timestamp - cfg.cluster_window_seconds
        cluster_new = self.storage.count_recent_new_wallets_for_token(
            token_id=trade.token_id,
            since_ts=cluster_since,
            max_trades=cfg.new_wallet_max_trades,
        )
        cluster_all = self.storage.count_distinct_wallets_for_token(
            trade.token_id, cluster_since
        )

        return Features(
            accumulated_usdc=accumulated,
            accumulation_trades=acc_trades,
            baseline_hourly=baseline,
            market_relative=relative,
            dormant_days=dormant_days,
            cluster_new_wallets=cluster_new,
            cluster_all_wallets=cluster_all,
            history_days=self._history_days(trade.timestamp),
            whitelist_tier=whitelist_tier,
            is_new_wallet=is_new_wallet,
            wallet_from_api=history is not None,
            is_market_maker=self.storage.wallet_traded_both_sides(
                trade.maker, trade.token_id
            ),
            is_hedged=self.storage.wallet_bought_both_outcomes(
                trade.maker, trade.condition_id or "", acc_since
            ),
            price=trade.price,
            volume_24h=market.volume_24h,
        )


def compute_score(features: Features, config: "Config") -> Score:
    """Свернуть признаки в балл. Чистая функция — тестируется без БД."""
    parts: dict = {}
    notes: list = []
    f = features

    if f.market_relative is not None:
        pts = _steps(f.market_relative, MARKET_RELATIVE_STEPS)
        if pts:
            parts["market_relative"] = pts
            notes.append(
                f"×{f.market_relative:.1f} к обычному часовому обороту рынка "
                f"(${f.baseline_hourly:,.0f}/ч)"
            )

    if f.accumulation_trades >= ACCUMULATION_STEPS[-1][0]:
        pts = _steps(f.accumulation_trades, ACCUMULATION_STEPS)
        if pts:
            parts["accumulation"] = pts
            notes.append(
                f"набирал частями: {f.accumulation_trades} покупок "
                f"на ${f.accumulated_usdc:,.0f}"
            )

    # Признаки кошелька работают только на достаточной истории (см. модульный
    # докстринг): иначе они шумят и перевешивают всё остальное.
    # История из API снимает холодный старт: там возраст настоящий, а не
    # «сколько мы успели посмотреть».
    wallet_features_ready = f.wallet_from_api or f.history_days >= HISTORY_MIN_DAYS
    if not wallet_features_ready:
        notes.append(
            f"новизна и кластер не учтены: локальной истории всего "
            f"{f.history_days:.1f}д из {HISTORY_MIN_DAYS:.0f} нужных"
        )

    if wallet_features_ready and f.is_new_wallet:
        parts["wallet_new"] = WALLET_NEW_POINTS
        notes.append("новый кошелёк")

    if f.dormant_days is not None:
        pts = _steps(f.dormant_days, DORMANT_STEPS)
        if pts:
            parts["dormant_wake"] = pts
            notes.append(f"молчал {f.dormant_days:.0f} дней и вернулся")

    if wallet_features_ready:
        pts = _steps(f.cluster_new_wallets, CLUSTER_STEPS)
        if pts:
            parts["cluster"] = pts
            notes.append(
                f"кластер: {f.cluster_new_wallets} новых кошельков "
                f"из {f.cluster_all_wallets} участников за окно"
            )

    if f.whitelist_tier == "pass":
        parts["whitelist"] = WHITELIST_PASS_POINTS
        notes.append("адрес из whitelist (tier=pass)")
    elif f.whitelist_tier == "watch":
        parts["whitelist"] = WHITELIST_WATCH_POINTS
        notes.append("адрес из whitelist (tier=watch)")

    pts = _steps_desc(f.price, CHEAP_TAIL_STEPS)
    if pts:
        parts["cheap_tail"] = pts
        notes.append(f"дешёвый вход @ {f.price:.3f}")

    if 0 < f.volume_24h < config.max_market_volume_24h:
        parts["illiquid_market"] = ILLIQUID_POINTS
        notes.append(f"неликвидный рынок (vol24h ${f.volume_24h:,.0f})")

    if f.is_hedged:
        parts["hedge"] = HEDGE_PENALTY
        notes.append("купил оба исхода рынка — это хедж, а не мнение")

    if f.is_market_maker:
        parts["market_maker"] = MARKET_MAKER_PENALTY
        notes.append("торгует обе стороны рынка — похоже на маркет-мейкера")

    if f.price >= config.max_trade_price:
        parts["near_resolved"] = NEAR_RESOLVED_PENALTY
        notes.append(f"рынок почти решён @ {f.price:.3f}")

    available = (
        MARKET_RELATIVE_STEPS[0][1]   # оборот относительно обычного
        + ACCUMULATION_STEPS[0][1]    # набор позиции частями
        + CHEAP_TAIL_STEPS[0][1]      # дешёвый хвост
        + ILLIQUID_POINTS
    )
    if wallet_features_ready:
        available += WALLET_NEW_POINTS + DORMANT_STEPS[0][1] + CLUSTER_STEPS[0][1]
    if f.whitelist_tier == "pass":
        available += WHITELIST_PASS_POINTS
    elif f.whitelist_tier == "watch":
        available += WHITELIST_WATCH_POINTS

    return Score(
        total=float(sum(parts.values())),
        parts=parts,
        notes=notes,
        available_max=float(available),
    )
