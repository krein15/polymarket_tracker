"""Подтверждение сигнала тем, что рынок пошёл за трейдером.

Идея
----
Инсайдер заходит первым. Если он прав, за ним в тот же исход заходят
другие — и заходят ПО БОЛЕЕ ВЫСОКОЙ ЦЕНЕ, потому что информация расходится
и предложение сжимается. Значит, погоня последователей — это подтверждение
задним числом, но очень сильное.

Замер на 4362 теневых сделках (окно 20 минут, деньги последователей от $2000):

    последователи платили      n     дрейф через час   перевес
    ниже него на 5%+          177         -39.3%       -24.6 пп
    примерно как он          2683          -1.0%        -0.4 пп
    выше на 5-15%             163          +3.4%        +3.3 пп
    выше на 15%+               91         +83.3%       +35.7 пп

Перевес +35.7 пп — самый сильный из всего, что удалось намерить.

Честная оговорка про запаздывание
---------------------------------
Признак смотрит на то, что произошло ПОСЛЕ сделки, поэтому в момент самой
сделки он недоступен: сигнал приходит через ~20 минут, когда цена уже
ушла вверх на те самые 15%+. Это не предсказание входа по его цене, а
подтверждение, что вход был информированным.

Смысл всё равно есть: средний дрейф в этой группе +83% от его цены, то
есть даже войдя на 15% выше, участник забирает основную часть движения.
Но входить придётся по новой цене, и в сообщении это сказано прямо.

Почему отдельная задача, а не часть основного цикла
--------------------------------------------------
Основной цикл идёт по сделкам в хронологическом порядке и в момент
обработки сделки просто не знает будущего. Поэтому подтверждение —
отдельный проход по уже записанным кандидатам, отстающий на окно
наблюдения.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Optional

log = logging.getLogger(__name__)

# Как часто просыпаемся. Чаще нет смысла: сделки приходят пачками раз в 5 минут.
CHECK_INTERVAL_SEC = 120

# Сколько кандидатов разбираем за проход. Ограничение бережёт цикл: каждая
# строка — это запрос к БД по окну сделок.
BATCH_LIMIT = 200


class ChaseConfirmer:
    """Фоновый проход: ищет кандидатов, за которыми пошёл рынок."""

    def __init__(self, storage, notifier, config, market_ctx=None):
        self.storage = storage
        self.notifier = notifier
        self.config = config
        self.market_ctx = market_ctx
        self._recent_alerts: deque = deque()   # отметки времени отправок
        self._recent_retracts: deque = deque()
        self.stats = {"checked": 0, "confirmed": 0, "skipped_rate": 0,
                      "backfilled": 0, "retracted": 0}

    def _rate_ok(self, now: float, queue: Optional[deque] = None,
                 limit: Optional[int] = None) -> bool:
        """Не больше N отправок в час.

        Без этого предела любая ошибка в калибровке порога превращается в
        поток сообщений: на теневой выборке порог обещал 14 сигналов в
        сутки, а на живом потоке дал около 580. Предел ограничивает ущерб
        независимо от того, насколько порог угадан.
        """
        queue = self._recent_alerts if queue is None else queue
        limit = self.config.chase_max_per_hour if limit is None else limit
        while queue and now - queue[0] > 3600:
            queue.popleft()
        return len(queue) < limit

    async def run(self) -> None:
        cfg = self.config
        log.info(
            "Подтверждение по погоне: окно %d мин, порог +%.0f%%, деньги от $%.0f",
            cfg.chase_window_minutes, cfg.chase_min_ratio * 100, cfg.chase_min_money_usdc,
        )
        while True:
            try:
                await asyncio.sleep(CHECK_INTERVAL_SEC)
                await self._pass()
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001 — фон не должен ронять трекер
                log.warning("Подтверждение: ошибка прохода: %s", e)

    async def _pass(self) -> None:
        cfg = self.config
        window = int(cfg.chase_window_minutes * 60)

        # Опираемся на голову СВОИХ данных, а не на часы: Data API отдаёт
        # сделки с задержкой, и по настенному времени окно ещё не набралось бы.
        head = self.storage.last_trade_ts()
        if not head:
            return
        ready_before = head - window
        oldest = ready_before - int(cfg.chase_max_age_minutes * 60)

        rows = self.storage.candidates_awaiting_chase(oldest, ready_before, BATCH_LIMIT)
        if not rows:
            return

        # Кандидаты старше "свежего" окна разбираем молча: при перезапуске
        # в очереди оказываются сотни накопленных строк, и без этого правила
        # трекер вываливал бы их разом. Рынок по ним всё равно уже ушёл.
        fresh_after = ready_before - int(cfg.chase_fresh_minutes * 60)

        for row in rows:
            money, vwap = self.storage.follower_flow(
                token_id=row["token_id"],
                exclude_maker=row["maker"],
                since_ts=row["ts"],
                until_ts=row["ts"] + window,
            )
            price = float(row["price"] or 0)
            chase = ((vwap - price) / price) if (vwap and price > 0) else None
            self.storage.save_chase(row["id"], chase, money, int(time.time()))
            self.stats["checked"] += 1

            if chase is None:
                continue
            if chase <= cfg.chase_retract_ratio:
                await self._maybe_retract(row, chase, money, vwap, fresh_after)
                continue
            if chase < cfg.chase_min_ratio:
                continue
            if money < cfg.chase_min_money_usdc:
                continue
            if row["ts"] < fresh_after:
                # Разобрали задним числом: в базу записали, но не шумим.
                self.stats["backfilled"] += 1
                continue
            now = time.time()
            if not self._rate_ok(now):
                self.stats["skipped_rate"] += 1
                log.info("Подтверждение пропущено: исчерпан лимит %d в час",
                         cfg.chase_max_per_hour)
                continue
            self._recent_alerts.append(now)
            await self._emit(row, chase, money, vwap)

    async def _maybe_retract(self, row, chase: float, money: float,
                             vwap: float, fresh_after: int) -> None:
        """Отбой по сигналу, который мы уже отправили.

        Зеркало подтверждения: если следом за трейдером деньги пошли по
        цене НИЖЕ его, он с большой вероятностью не прав. На 4985 сделках
        с посчитанной погоней:

            рынок пошёл за ним (>= +15%)   n=394   перевес +25.0 пп, ROI +50.4%
            рынок пошёл против (<= -15%)   n=306   перевес -29.0 пп, ROI -54.0%

        А среди сделок, по которым мы РЕАЛЬНО отправили сигнал, отбойная
        группа ещё хуже: винрейт 21.4% при безубытке 53.2%, ROI -60.4%.

        Почему только по отправленным. Про остальные мы молчали, и отбой по
        ним был бы сообщением о том, чего пользователь не видел, — а это
        два десятка сообщений в сутки вместо одного.
        """
        sent = self.storage.sent_signal_for_trade(row["tx_hash"])
        if sent is None:
            return
        if row["ts"] < fresh_after:
            return  # накопленное после простоя разбираем молча, как и всё прочее
        now = time.time()
        if not self._rate_ok(now, self._recent_retracts,
                             self.config.chase_retract_max_per_hour):
            self.stats["skipped_rate"] += 1
            return
        self._recent_retracts.append(now)

        self.stats["retracted"] += 1
        price = float(row["price"] or 0)
        slug = row["market_slug"] or sent["market_slug"] or ""
        url = f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com"
        lines = [
            "🔴 <b>ОТБОЙ · рынок пошёл против него</b>",
            slug or '?',
            "",
            f"Мы дали сигнал: вход по <b>{price:.3f}</b> "
            f"на ${float(row['usdc_amount'] or 0):,.0f}",
            f"За {self.config.chase_window_minutes:.0f} мин следом зашло "
            f"<b>${money:,.0f}</b> по средней <b>{vwap:.3f}</b> "
            f"(<b>{chase*100:.0f}%</b> к его цене)",
            "",
            "Рынок закладывает исход дешевле, чем он купил. На выборке такие "
            "сделки давали винрейт 21% при безубытке 53% и ROI -60%.",
            f'<a href="{url}">Открыть рынок</a>',
        ]
        text = chr(10).join(lines)
        await self.notifier.send_alert(text)
        log.info("Отбой: %s %.0f%% на $%.0f", slug, chase * 100, money)

    async def _emit(self, row, chase: float, money: float, vwap: float) -> None:
        """Записать подтверждение и отправить сообщение."""
        self.stats["confirmed"] += 1
        price = float(row["price"])
        reason = (
            f"Рынок пошёл следом: за {self.config.chase_window_minutes:.0f} мин "
            f"${money:,.0f} зашло по средней {vwap:.3f} против его {price:.3f} "
            f"(+{chase*100:.0f}%)"
        )
        signal_id = self.storage.save_signal(
            ts=row["ts"],
            signal_type="chase",
            maker=row["maker"],
            token_id=row["token_id"],
            market_slug=row["market_slug"],
            usdc_amount=float(row["usdc_amount"] or 0),
            price=price,
            reason=reason,
            tx_hash=row["tx_hash"],
            side="buy",
        )
        self.storage.init_outcome_record(signal_id, int(time.time()))

        url = (
            f"https://polymarket.com/event/{row['market_slug']}"
            if row["market_slug"] else "https://polymarket.com"
        )
        text = (
            f"🟢 <b>ПОГОНЯ · рынок пошёл следом</b>\n"
            f"{row['market_slug'] or '?'}\n\n"
            f"Он взял по <b>{price:.3f}</b> на ${float(row['usdc_amount'] or 0):,.0f}\n"
            f"За {self.config.chase_window_minutes:.0f} мин следом зашло "
            f"<b>${money:,.0f}</b> по средней <b>{vwap:.3f}</b> "
            f"(<b>+{chase*100:.0f}%</b> к его цене)\n\n"
            f"Вход сейчас — уже по новой цене, не по его. "
            f"На выборке такие случаи давали перевес +35 пп.\n"
            f'<a href="{url}">Открыть рынок</a>'
        )
        await self.notifier.send_html(text)
        log.info("Подтверждение: %s +%.0f%% на $%.0f", row["market_slug"], chase * 100, money)
