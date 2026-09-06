"""Отправка сигналов в Telegram.

Используем прямые вызовы Bot API через aiohttp — python-telegram-bot
слишком тяжёлый для нашей узкой задачи.
"""
from __future__ import annotations

import asyncio
import html
import time
from collections import deque
import logging
from typing import Optional

import aiohttp

from .anomaly_detector import Signal

log = logging.getLogger(__name__)


# Цвет = срочность и природа сообщения, а не тип ветки:
#   🟢 обычный сигнал  🟡 ранний (из цепочки)  🔴 сбой
SIGNAL_ICONS = {
    "whitelist": "🟢",
    "score": "🟢",
    "onchain_early": "🟡",
    "chase": "🟢",
    # Legacy-типы: сигналов с ними больше не приходит, оставлены чтобы
    # старые записи в БД рендерились по-человечески.
    "suspicious_entry": "🔍",
    "cluster": "🚨",
}


class TelegramNotifier:
    """Простой Telegram Bot API клиент."""

    def __init__(self, bot_token: str, chat_id: str, max_per_hour: int = 15):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.max_per_hour = max_per_hour
        self._sent_times: deque = deque()
        self._suppressed = 0
        self._session: Optional[aiohttp.ClientSession] = None
        self.base_url = f"https://api.telegram.org/bot{bot_token}"

    def _make_session(self) -> aiohttp.ClientSession:
        return aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30, connect=15),
        )

    async def start(self) -> None:
        if self._session is None:
            self._session = self._make_session()

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def send_signal(self, signal: Signal) -> Optional[int]:
        """Отправить сигнал. Возвращает message_id или None при ошибке."""
        text = self._format_signal(signal)
        return await self._send_message(text)

    async def send_status(self, text: str) -> Optional[int]:
        """Служебные сообщения (старт, статистика). Текст экранируется."""
        return await self._send_message(f"ℹ️ <i>{html.escape(text)}</i>")

    async def send_alert(self, text: str) -> Optional[int]:
        """Сбой, требующий внимания."""
        return await self._send_message(f"🔴 {text}")

    async def send_html(self, text: str) -> Optional[int]:
        """Готовая HTML-разметка — БЕЗ экранирования.

        Нужен отдельным методом: send_status экранирует всё подряд, и
        сообщения, собранные с тегами, приходили с видимыми <b> в тексте.
        """
        return await self._send_message(text)

    def _budget_ok(self) -> bool:
        """Общий потолок сообщений в час — поверх всех веток.

        Пределы в самих ветках уже есть, но каждый охраняет только себя:
        когда одновременно расшумелись три источника, в чат прилетело
        64 сообщения за час. Этот предел — последний рубеж, он не зависит
        от того, какая ветка ошиблась в калибровке.
        """
        now = time.time()
        while self._sent_times and now - self._sent_times[0] > 3600:
            self._sent_times.popleft()
        if len(self._sent_times) >= self.max_per_hour:
            self._suppressed += 1
            if self._suppressed in (1, 10, 50) or self._suppressed % 100 == 0:
                log.warning(
                    "Достигнут потолок %d сообщений в час, подавлено %d",
                    self.max_per_hour, self._suppressed,
                )
            return False
        self._sent_times.append(now)
        return True

    async def _send_message(self, text: str) -> Optional[int]:
        if not self._budget_ok():
            return None
        if self._session is None:
            self._session = self._make_session()

        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        for attempt in range(3):
            try:
                async with self._session.post(
                    f"{self.base_url}/sendMessage", json=payload
                ) as resp:
                    data = await resp.json()
                    if not data.get("ok"):
                        log.warning("Telegram API ошибка: %s", data)
                        return None
                    return data["result"]["message_id"]
            except asyncio.TimeoutError:
                log.warning("Telegram таймаут (попытка %d/3) — пересоздаю сессию", attempt + 1)
                await self.close()
                self._session = self._make_session()
                await asyncio.sleep(5)
            except aiohttp.ClientError as e:
                log.warning("Telegram network ошибка (попытка %d/3): %s — пересоздаю сессию", attempt + 1, e)
                await self.close()
                self._session = self._make_session()
                await asyncio.sleep(5)

        log.error("Не удалось отправить сообщение в Telegram после 3 попыток")
        return None

    def _format_signal(self, s: Signal) -> str:
        """Формат сигнала в HTML для Telegram."""
        icon = SIGNAL_ICONS.get(s.signal_type, "📡")
        type_label = {
            "whitelist": "WHITELIST",
            "score": "SCORE",
            "suspicious_entry": "SUSPICIOUS",
            "cluster": "CLUSTER",
        }.get(s.signal_type, s.signal_type.upper())
        if s.score is not None:
            type_label = f"{type_label} {s.score.total:.0f}"

        # Маркировка стороны: для whitelist особенно важно отличать вход от
        # выхода — "whale exit" и "whale entry" это разные сигналы.
        side = (s.trade.side or "").lower()
        if side == "buy":
            side_label = "📈 BUY"
        elif side == "sell":
            side_label = "📉 SELL"
        else:
            side_label = side.upper() if side else "?"

        question = html.escape(s.market.question or "?")
        reason = html.escape(s.reason)
        maker_short = f"{s.trade.maker[:8]}..{s.trade.maker[-4:]}"
        tx_short = f"{s.trade.tx_hash[:10]}..."

        # Никнейм трейдера от Data API, если есть
        pseudonym = getattr(s.trade, "pseudonym", None)
        user_name = getattr(s.trade, "user_name", None)
        nickname_str = ""
        if user_name and user_name.strip():
            nickname_str = f" ({html.escape(user_name)})"
        elif pseudonym and pseudonym.strip():
            nickname_str = f" ({html.escape(pseudonym)})"

        market_url = s.market.url() if (s.market.event_slug or s.market.slug) else "https://polymarket.com"
        polygonscan_tx = f"https://polygonscan.com/tx/{s.trade.tx_hash}"
        polygonscan_addr = f"https://polygonscan.com/address/{s.trade.maker}"

        lines = [
            f"{icon} <b>{type_label}</b> · {side_label}",
            "",
            f"<b>Рынок:</b> {question}",
            f"<b>Outcome:</b> {html.escape(s.market.outcome)} @ {s.trade.price:.3f}",
            f"<b>Размер:</b> ${s.trade.usdc_amount:,.0f} ({s.trade.shares:.1f} shares)",
            "",
            f"<b>Категория:</b> {html.escape(s.market.category or 'unknown')} | "
            f"<b>Volume 24h:</b> ${s.market.volume_24h:,.0f}",
            "",
            f"<b>Трейдер:</b> <a href=\"{polygonscan_addr}\">{maker_short}</a>{nickname_str}",
            f"<i>{html.escape(s.wallet.reason)}</i>",
            "",
            f"<b>Причина:</b> {reason}",
        ]

        # Разбивка балла: без неё число ни о чём не говорит, а по ней сразу
        # видно, на чём именно сигнал держится.
        if s.score is not None and s.score.notes:
            lines.append("")
            lines.append("<b>Из чего сложился балл:</b>")
            for name, pts in sorted(s.score.parts.items(), key=lambda kv: -abs(kv[1])):
                lines.append(f"  {pts:+.0f} · {html.escape(name)}")
            for note in s.score.notes:
                lines.append(f"<i>{html.escape(note)}</i>")

        lines += [
            "",
            f'<a href="{market_url}">📊 Polymarket</a> | '
            f'<a href="{polygonscan_tx}">🔗 {tx_short}</a>',
        ]

        return "\n".join(lines)
