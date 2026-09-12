"""Карточка трейдера в сообщении: кто он и как отработал раньше.

Зачем отдельный модуль
----------------------
Эти строки нужны каждому виду сигнала, а собирались только в быстрой
полосе. В подтверждении по погоне их не было вовсе: сообщение говорило
"рынок пошёл следом", но не говорило, ЗА КЕМ и на какой исход. По такому
сигналу нечего проверить и не за кем следить.

Ссылка ведёт на профиль Polymarket, а не на адрес в обозревателе цепочки:
сам по себе кошелёк ничего не говорит, а в профиле видны позиции и история.

История берётся по НАШИМ наблюдениям (wallet_track_record), а не по словам
профиля: нас интересует, как кошелёк отработал по закрытым сделкам, которые
мы сами записали.
"""
from __future__ import annotations

import html
import logging
from typing import Optional

import aiohttp

from .telegram_notifier import clean_nickname

log = logging.getLogger(__name__)

# Ник трейдера у Data API. В логе цепочки его нет — там только адрес.
# Запрос делается ТОЛЬКО на отправку сигнала (единицы раз в час), поэтому на
# скорость обработки потока не влияет.
NICKNAME_URL = "https://data-api.polymarket.com/trades"
NICKNAME_TIMEOUT_SEC = 6


async def fetch_nickname(maker: str) -> str:
    """Ник трейдера или пустая строка.

    Ошибку глушим намеренно: без ника сигнал остаётся полезным, а
    задерживать из-за неё отправку незачем.
    """
    try:
        timeout = aiohttp.ClientTimeout(total=NICKNAME_TIMEOUT_SEC)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(NICKNAME_URL, params={"user": maker, "limit": "1"}) as r:
                if r.status != 200:
                    return ""
                data = await r.json()
    except Exception:  # noqa: BLE001 — ник не стоит того, чтобы ронять сигнал
        return ""
    if not isinstance(data, list) or not data:
        return ""
    return clean_nickname(data[0].get("name") or "", data[0].get("pseudonym") or "")


def track_record(storage, maker: str) -> str:
    """Как этот кошелёк отработал по нашим закрытым сделкам."""
    record = storage.wallet_track_record(maker)
    if not record:
        return "истории по нему у нас пока нет"
    return (f"{record['winrate'] * 100:.0f}% побед, "
            f"ROI {record['roi'] * 100:+.0f}% "
            f"на {record['resolved']} закрытых сделках")


def trader_lines(storage, maker: str, nickname: str = "") -> list:
    """Две строки сообщения: ссылка на профиль и послужной список."""
    short = f"{maker[:8]}..{maker[-4:]}"
    url = f"https://polymarket.com/profile/{maker}"
    nick = f" ({html.escape(nickname)})" if nickname else ""
    return [
        f'<b>Трейдер:</b> <a href="{url}">{short}</a>{nick}',
        f"<i>{html.escape(track_record(storage, maker))}</i>",
    ]


async def market_of(market_ctx, token_id: str):
    """Метаданные рынка или None. Без них сигнал всё равно отправляем —
    лучше сообщение без исхода, чем молчание."""
    if market_ctx is None:
        return None
    try:
        return await market_ctx.get_by_token_id(token_id)
    except Exception as e:  # noqa: BLE001
        log.debug("Не удалось получить рынок по токену: %s", e)
        return None


def outcome_line(market, price: float) -> Optional[str]:
    """Строка с исходом, на который сделана ставка."""
    outcome = getattr(market, "outcome", "") if market is not None else ""
    if not outcome:
        return None
    return f"<b>Outcome:</b> {html.escape(str(outcome))} @ {price:.3f}"
