"""По какой цене мы РЕАЛЬНО могли бы войти в момент сигнала.

Зачем это вообще понадобилось
-----------------------------
Все замеры прибыли считались от цены ТРЕЙДЕРА. Но сигнал приходит после
того, как рынок за ним пошёл, — и уходит вверх ровно потому, что пошёл.
Пересчёт на цену последователей показал, чего стоит эта разница:

    от его цены                     ROI +44.4%
    по цене последователей          ROI  +4.2%   [-1.7; +10.1]
    плюс проскальзывание 2%         ROI  +2.2%   [-3.7;  +8.1]

То есть перевес живёт в его цене входа, которой у нас нет. Пока мы не
записываем достижимую цену, любой подсчёт прибыли — самообман.

Что записываем
--------------
Через ENTRY_DELAY_SEC после отправки сигнала берём стакан и считаем, почём
налился бы ордер на несколько размеров. Не цену последней сделки: она
говорит, где торговали, а не где нальют.

Ловушка в API, стоившая бы целого спреда
----------------------------------------
У Polymarket `/price?side=buy` возвращает лучший БИД, а не аск. Проверено
по стакану на живом рынке:

    лучший бид 0.58,  лучший аск 0.59
    price?side=buy  -> 0.58        price?side=sell -> 0.59

Покупая, вы платите АСК. Ошибка в одну букву дала бы систематически
заниженную цену входа и завышенную прибыль — молча, без единого падения.
Поэтому берём `/book` целиком и разбираем сами: заодно видно глубину.

Красный вердикт (21.09.2026)
---------------------------
Замер перестал быть только наблюдением. На 541 сигнале с исходом убыток
оказался сосредоточен в дешёвых входах:

    наша цена     сделок   ROI у нас   ROI у трейдера   доля убытка
    < 0.35            54      -83.5%          -87.5%           55%
    0.35-0.50         81      -29.4%          -38.2%           29%
    0.50-0.60         87       -3.6%           +0.7%            4%

Четверть сделок даёт 84% убытка, и там в минусе сам трейдер — значит
отсекается не наше опоздание, а его плохие сделки. Поэтому вход ниже
ENTRY_MIN_PRICE помечается отдельным сообщением в ту же ветку.

Почему обход стакана, а не верх книги
-------------------------------------
Верх книги — это цена первой сотни долларов. Сигнальные сделки — тысячи
долларов, и они съедают несколько уровней. На живом рынке аски шли
0.59 на $3634, затем 0.60 на $9856: ордер в $5000 уже не поместился бы
в лучшую цену.
"""
from __future__ import annotations

import asyncio
import html
import logging
import time
from typing import Optional

import aiohttp

log = logging.getLogger(__name__)

BOOK_URL = "https://clob.polymarket.com/book"
BOOK_TIMEOUT_SEC = 8

# Сколько ждём после отправки сигнала, прежде чем снять цену. Это оценка
# времени реакции: увидеть сообщение, открыть рынок, нажать. Раньше снимать
# нечестно — так быстро человек не успеет.
ENTRY_DELAY_SEC = 120

# На какие размеры считаем цену налива. Разные суммы — разные цены, и это
# само по себе результат: если $5000 стоят заметно дороже $500, значит
# стратегия не масштабируется.
FILL_SIZES_USDC = (500.0, 2000.0, 5000.0)

# На какой размер считаем вердикт. Именно на нём мерились пороги:
# все цифры про "84% убытка ниже 0.50" получены по fill_2000.
VERDICT_SIZE_USDC = 2000.0

# Как часто просыпаемся и сколько сигналов разбираем за проход.
#
# Замерено на 514 замерах: при интервале 60 с и пачке 20 фактическая
# задержка от отправки сигнала до снимка составляла 205 с медианы вместо
# задуманных 120, а хвост доходил до 30 минут. Сигналы приходят пачками,
# и очередь не рассасывалась.
#
# Это не мелочь: группа, где замер опоздал больше чем на 7 минут, даёт
# ROI -27.1% против -7.4% там, где он был вовремя. Часть этой разницы —
# наша погрешность, а не свойство сигнала.
CHECK_INTERVAL_SEC = 20
BATCH_LIMIT = 50

# Позже этого срока снимать бессмысленно: цена уже не та, которую человек
# увидел бы по сигналу. Такие записи честно остаются пустыми.
ENTRY_MAX_AGE_SEC = 1800


def fill_price(asks: list, usdc_amount: float) -> Optional[float]:
    """Средняя цена, по которой нальётся ордер на usdc_amount долларов.

    asks — список (цена, размер в долях), порядок любой.
    None — если глубины не хватает: делать вид, что налилось по последней
    доступной цене, значило бы приукрасить результат.
    """
    if usdc_amount <= 0:
        return None
    remaining = usdc_amount
    spent = 0.0
    shares = 0.0
    for price, size in sorted(asks):
        if price <= 0 or size <= 0:
            continue
        level_usdc = price * size
        take = min(level_usdc, remaining)
        spent += take
        shares += take / price
        remaining -= take
        if remaining <= 1e-9:
            break
    if remaining > 1e-9 or shares <= 0:
        return None
    return spent / shares


def verdict(fill: Optional[float], his_price: Optional[float],
            min_price: float, depth: Optional[float] = None,
            slug: Optional[str] = None) -> Optional[str]:
    """Сообщение "не входить", либо None, если вход проходит порог.

    Молчим, когда всё в порядке: вердикт по каждому сигналу удвоил бы
    поток сообщений и съел общий часовой лимит, а он один на все ветки —
    вердикт вытеснял бы сами сигналы.

    Важная оговорка: МОЛЧАНИЕ НЕ ЗНАЧИТ "можно". Замер мог не состояться
    (рынок закрылся, сигнал пролежал дольше ENTRY_MAX_AGE_SEC), и тогда
    вердикта не будет вовсе. Зелёного света этот механизм не даёт — он
    только гасит красный.

    Откуда порог: 541 замер с исходом. Ниже 0.50 — четверть сделок и 84%
    всего убытка, и там в минусе сам трейдер (перевес -36.8 пп ниже 0.35,
    -19.3 пп на 0.35-0.50). То есть мы отсекаем не своё опоздание, а его
    плохие сделки.
    """
    name = html.escape(slug or "?")
    if fill is None:
        got = f"{depth:,.0f}".replace(",", " ") if depth else "0"
        return (f"⚠️ <b>Стакан тоньше ${VERDICT_SIZE_USDC:.0f}</b>"
                f" · {name}"
                f"{chr(10)}Всего в асках ${got} — ордер целиком не нальётся")
    if fill >= min_price:
        return None
    lines = [f"⛔ <b>Не входить</b> · {name}",
             f"Вход ${VERDICT_SIZE_USDC:.0f} = <b>{fill:.2f}</b>"
             f" при пороге {min_price:.2f}"]
    if his_price and his_price > 0:
        lines.append(f"Трейдер вошёл по {his_price:.2f} "
                     f"({fill / his_price - 1:+.0%})")
    return chr(10).join(lines)


def parse_book(payload: dict) -> list:
    """Аски из ответа /book как список (цена, размер).

    Отсутствующие или битые уровни пропускаем молча: один кривой уровень не
    повод потерять весь стакан.
    """
    out = []
    for level in (payload or {}).get("asks") or []:
        try:
            out.append((float(level["price"]), float(level["size"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


class EntryPriceTracker:
    """Фоновая задача: записать достижимую цену входа по свежим сигналам."""

    def __init__(self, storage, config=None, notifier=None):
        self.storage = storage
        self.notifier = notifier
        self.delay = getattr(config, "entry_delay_sec", ENTRY_DELAY_SEC)
        self.min_price = getattr(config, "entry_min_price", 0.0)
        self.verdict_on = bool(
            notifier is not None
            and getattr(config, "entry_verdict_enabled", False)
        )
        self.stats = {"checked": 0, "saved": 0, "no_book": 0, "too_thin": 0,
                      "blocked": 0}

    async def run(self) -> None:
        log.info("Замер цены входа: снимок стакана через %d с после сигнала",
                 self.delay)
        while True:
            try:
                await asyncio.sleep(CHECK_INTERVAL_SEC)
                await self._pass()
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001 — фон не роняет трекер
                log.warning("Замер цены входа: ошибка прохода: %s", e)

    async def _pass(self) -> None:
        now = int(time.time())
        rows = self.storage.signals_awaiting_entry(
            ready_before=now - self.delay,
            oldest=now - ENTRY_MAX_AGE_SEC,
            limit=BATCH_LIMIT,
        )
        if not rows:
            return
        timeout = aiohttp.ClientTimeout(total=BOOK_TIMEOUT_SEC)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for row in rows:
                await self._one(session, row, now)

    async def _one(self, session, row, now: int) -> None:
        self.stats["checked"] += 1
        asks = await self._fetch_asks(session, row["token_id"])
        if asks is None:
            self.stats["no_book"] += 1
            return

        best_ask = min(a[0] for a in asks) if asks else None
        fills = {size: fill_price(asks, size) for size in FILL_SIZES_USDC}
        if best_ask is None or all(v is None for v in fills.values()):
            self.stats["too_thin"] += 1

        depth = sum(p * s for p, s in asks)
        self.storage.save_signal_entry(
            signal_id=row["id"],
            measured_ts=now,
            delay_sec=now - row["ts"],
            best_ask=best_ask,
            fill_500=fills.get(500.0),
            fill_2000=fills.get(2000.0),
            fill_5000=fills.get(5000.0),
            depth_usdc=depth,
        )
        self.stats["saved"] += 1
        await self._verdict(row, fills.get(VERDICT_SIZE_USDC), depth)

    async def _verdict(self, row, fill: Optional[float],
                       depth: Optional[float]) -> None:
        """Красный вердикт в ту же ветку сообщений, что и сигнал."""
        if not self.verdict_on:
            return
        text = verdict(fill, row["price"], self.min_price, depth,
                       row["market_slug"])
        if text is None:
            return
        self.stats["blocked"] += 1
        try:
            await self.notifier.send_html(
                text, reply_to=row["telegram_msg_id"])
        except Exception as e:  # noqa: BLE001 — вердикт не роняет замер
            log.warning("Вердикт по цене входа не отправлен: %s", e)

    async def _fetch_asks(self, session, token_id: str):
        try:
            async with session.get(BOOK_URL, params={"token_id": token_id}) as r:
                if r.status != 200:
                    return None
                return parse_book(await r.json())
        except Exception:  # noqa: BLE001 — рынок мог закрыться, это не сбой
            return None
