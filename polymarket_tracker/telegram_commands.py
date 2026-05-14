"""Telegram-команды через long polling.

Принимает команды из чата (только от authorized chat_id) и отвечает сводками
по данным из storage. Работает параллельно с TelegramNotifier — у каждого
своя aiohttp-сессия, чтобы long polling (висит до 30 секунд) не блокировал
отправку обычных сигналов.

Команды:
    /today [type]        — сигналы за последние 24h
    /yesterday [type]    — окно 48h-24h назад
    /open [type]         — открытые позиции
    /stats [type]        — общая сводка
    /signal <id>         — детально по сигналу
    /help                — список команд

[type] = cluster | suspicious_entry | whitelist
"""
from __future__ import annotations

import asyncio
import html
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import aiohttp

from .config import Config
from .storage import Storage

log = logging.getLogger(__name__)

# Long polling timeout: 25 секунд держим открытым соединение, ждём updates.
POLL_TIMEOUT = 25
# Лимит длины ответа в Telegram — 4096; оставляем запас.
MAX_REPLY_LEN = 3900
# Сколько сигналов показывать в /today и /open максимум.
MAX_LIST_ITEMS = 15

VALID_SIGNAL_TYPES = {"cluster", "suspicious_entry", "whitelist"}


class TelegramCommandHandler:
    def __init__(self, config: Config, storage: Storage):
        self.config = config
        self.storage = storage
        self.bot_token = config.telegram_bot_token
        self.chat_id = str(config.telegram_chat_id)
        self.base_url = f"https://api.telegram.org/bot{config.telegram_bot_token}"
        self._session: Optional[aiohttp.ClientSession] = None

    def _make_session(self) -> aiohttp.ClientSession:
        # total=POLL_TIMEOUT+10 — даём с запасом на сетевые накладные
        return aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=POLL_TIMEOUT + 10, connect=15),
        )

    async def start(self) -> None:
        if self._session is None:
            self._session = self._make_session()

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def run(self) -> None:
        """Главный цикл long polling. Работает до отмены."""
        await self.start()

        # Восстанавливаем offset из checkpoint — чтобы при рестарте не получать
        # старые команды.
        offset = 0
        saved = self.storage.get_checkpoint("telegram_last_update_id")
        if saved:
            try:
                offset = int(saved) + 1
            except ValueError:
                pass

        log.info("TelegramCommandHandler стартует (offset=%d)", offset)

        consecutive_errors = 0
        while True:
            try:
                updates = await self._get_updates(offset)
                consecutive_errors = 0

                for u in updates:
                    update_id = u.get("update_id")
                    if update_id is None:
                        continue
                    offset = max(offset, update_id + 1)
                    self.storage.set_checkpoint("telegram_last_update_id", str(update_id))

                    try:
                        await self._handle_update(u)
                    except Exception as e:
                        log.exception("Ошибка обработки update %s: %s", update_id, e)

            except asyncio.CancelledError:
                log.info("TelegramCommandHandler остановлен")
                raise
            except aiohttp.ClientError as e:
                consecutive_errors += 1
                backoff = min(60.0, 3.0 * consecutive_errors)
                log.warning("Telegram polling error (#%d): %s — backoff %.0fс", consecutive_errors, e, backoff)
                await self.close()
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    raise
                self._session = self._make_session()
            except Exception as e:
                consecutive_errors += 1
                backoff = min(60.0, 3.0 * consecutive_errors)
                log.exception("Неожиданная ошибка в polling: %s — backoff %.0fс", e, backoff)
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    raise

    async def _get_updates(self, offset: int) -> list:
        """Long-poll за новыми updates. Возвращает массив (возможно пустой)."""
        if self._session is None:
            self._session = self._make_session()

        params = {
            "offset": str(offset),
            "timeout": str(POLL_TIMEOUT),
            "allowed_updates": '["message"]',  # нам нужны только сообщения
        }

        async with self._session.get(f"{self.base_url}/getUpdates", params=params) as resp:
            if resp.status != 200:
                body = await resp.text()
                log.warning("getUpdates status=%d: %s", resp.status, body[:200])
                return []
            data = await resp.json()

        if not data.get("ok"):
            log.warning("getUpdates not ok: %s", str(data)[:200])
            return []
        return data.get("result", []) or []

    async def _handle_update(self, update: dict) -> None:
        msg = update.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat = msg.get("chat") or {}
        from_chat_id = str(chat.get("id", ""))

        if not text or not text.startswith("/"):
            return  # игнорируем не-команды

        # Авторизация: отвечаем только в наш чат.
        if from_chat_id != self.chat_id:
            log.warning("Команда от неавторизованного chat_id=%s: %s", from_chat_id, text[:50])
            return

        # Парсим команду и аргументы. Удаляем @BotName если есть.
        parts = text.split()
        cmd = parts[0].split("@", 1)[0].lower()
        args = parts[1:]

        log.info("TG command: %s args=%s", cmd, args)
        try:
            reply = await self._dispatch(cmd, args)
        except Exception as e:
            log.exception("Ошибка в команде %s: %s", cmd, e)
            reply = f"⚠️ Ошибка обработки команды: <code>{html.escape(str(e))}</code>"

        if reply:
            await self._send_reply(reply)

    async def _dispatch(self, cmd: str, args: list) -> Optional[str]:
        if cmd in ("/start", "/help"):
            return self._render_help()
        if cmd == "/today":
            return self._render_window("Последние 24h", since_sec=86400, args=args)
        if cmd == "/yesterday":
            return self._render_window(
                "Окно 24h–48h назад",
                since_sec=2 * 86400,
                until_sec=86400,
                args=args,
            )
        if cmd == "/open":
            return self._render_open(args)
        if cmd == "/stats":
            return self._render_stats(args)
        if cmd == "/signal":
            if not args:
                return "Использование: <code>/signal &lt;id&gt;</code>"
            try:
                sid = int(args[0])
            except ValueError:
                return "ID сигнала должен быть числом."
            return self._render_signal(sid)
        return None  # неизвестная команда — молчим

    # ─────────── Renderers ───────────

    def _render_help(self) -> str:
        return (
            "<b>Команды:</b>\n"
            "/today [тип] — сигналы за последние 24 часа\n"
            "/yesterday [тип] — за окно 24-48 часов назад\n"
            "/open [тип] — открытые позиции\n"
            "/stats [тип] — общая сводка\n"
            "/signal &lt;id&gt; — детально по сигналу\n"
            "\n"
            "<b>Тип</b> (опционально): <code>cluster</code> / "
            "<code>suspicious_entry</code> / <code>whitelist</code>\n"
            "Пример: <code>/today cluster</code>"
        )

    def _parse_type_arg(self, args: list) -> Optional[str]:
        if not args:
            return None
        candidate = args[0].lower()
        if candidate in VALID_SIGNAL_TYPES:
            return candidate
        # Алиасы для удобства мобильного ввода
        aliases = {
            "susp": "suspicious_entry",
            "suspicious": "suspicious_entry",
            "wl": "whitelist",
            "white": "whitelist",
            "cl": "cluster",
        }
        return aliases.get(candidate)

    def _render_window(
        self,
        title: str,
        since_sec: int,
        until_sec: int = 0,
        args: list = None,
    ) -> str:
        """Сигналы в окне [now-since_sec; now-until_sec]."""
        now = int(time.time())
        ts_from = now - since_sec
        ts_to = now - until_sec
        type_filter = self._parse_type_arg(args or [])

        where = "s.ts >= ? AND s.ts < ?"
        params = [ts_from, ts_to]
        if type_filter:
            where += " AND s.signal_type = ?"
            params.append(type_filter)

        with self.storage._conn() as c:
            rows = c.execute(f"""
                SELECT s.id, s.ts, s.signal_type, s.maker, s.market_slug,
                       s.usdc_amount, s.price, s.side,
                       o.market_resolved, o.trader_was_right, o.roi_if_followed,
                       o.max_price_reached
                FROM signals s
                LEFT JOIN signal_outcomes o ON o.signal_id = s.id
                WHERE {where}
                ORDER BY s.ts DESC
            """, params).fetchall()

        if not rows:
            tail = f" ({type_filter})" if type_filter else ""
            return f"<b>{title}{tail}</b>\n\nСигналов нет."

        # Группировка: resolved-win / resolved-lose / open
        wins, loses, opens = [], [], []
        for r in rows:
            if r["market_resolved"]:
                (wins if r["trader_was_right"] else loses).append(r)
            else:
                opens.append(r)

        lines = []
        header = f"<b>{title}"
        if type_filter:
            header += f" · {type_filter}"
        header += f"</b>"
        lines.append(header)

        total = len(rows)
        by_type = {}
        for r in rows:
            by_type[r["signal_type"]] = by_type.get(r["signal_type"], 0) + 1
        type_summary = ", ".join(f"{k}: {v}" for k, v in sorted(by_type.items()))
        lines.append(f"Всего: {total}  ({type_summary})")
        lines.append(f"✅ wins: {len(wins)}  ✗ loses: {len(loses)}  ⏳ open: {len(opens)}")
        lines.append("")

        def fmt_signal(r, with_outcome: bool) -> str:
            icon = {"whitelist": "⭐", "suspicious_entry": "🔍", "cluster": "🚨"}.get(r["signal_type"], "📡")
            side = "📈" if r["side"] == "buy" else "📉"
            slug = (r["market_slug"] or "?")[:38]
            size = f"${r['usdc_amount']:,.0f}"
            line = f"{icon} <code>#{r['id']}</code> {side} {size} @{r['price']:.2f} "
            if with_outcome:
                if r["market_resolved"]:
                    if r["trader_was_right"]:
                        line += f"<b>+{r['roi_if_followed']*100:.0f}%</b>"
                    else:
                        line += f"−{abs(r['roi_if_followed']*100):.0f}%"
                else:
                    mp = r["max_price_reached"]
                    delta = ""
                    if mp is not None and r["price"]:
                        d = (mp - r["price"]) / r["price"] * 100
                        delta = f" Δmax {d:+.0f}%"
                    line += f"open{delta}"
            line += f"\n  <i>{html.escape(slug)}</i>"
            return line

        if wins:
            lines.append("<b>✅ Wins</b>")
            for r in wins[:MAX_LIST_ITEMS]:
                lines.append(fmt_signal(r, with_outcome=True))
            if len(wins) > MAX_LIST_ITEMS:
                lines.append(f"  …и ещё {len(wins) - MAX_LIST_ITEMS}")
            lines.append("")

        if loses:
            lines.append("<b>✗ Loses</b>")
            for r in loses[:MAX_LIST_ITEMS]:
                lines.append(fmt_signal(r, with_outcome=True))
            if len(loses) > MAX_LIST_ITEMS:
                lines.append(f"  …и ещё {len(loses) - MAX_LIST_ITEMS}")
            lines.append("")

        if opens:
            lines.append("<b>⏳ Open</b>")
            for r in opens[:MAX_LIST_ITEMS]:
                lines.append(fmt_signal(r, with_outcome=True))
            if len(opens) > MAX_LIST_ITEMS:
                lines.append(f"  …и ещё {len(opens) - MAX_LIST_ITEMS}")

        return self._truncate("\n".join(lines))

    def _render_open(self, args: list) -> str:
        type_filter = self._parse_type_arg(args or [])
        where = "o.market_resolved = 0"
        params = []
        if type_filter:
            where += " AND s.signal_type = ?"
            params.append(type_filter)

        with self.storage._conn() as c:
            rows = c.execute(f"""
                SELECT s.id, s.ts, s.signal_type, s.market_slug, s.usdc_amount,
                       s.price, s.side, o.max_price_reached, o.min_price_reached
                FROM signals s
                JOIN signal_outcomes o ON o.signal_id = s.id
                WHERE {where}
                ORDER BY s.ts DESC
            """, params).fetchall()

        if not rows:
            return "<b>Открытых позиций нет.</b>"

        now = int(time.time())
        lines = [f"<b>Открытые позиции: {len(rows)}</b>", ""]
        for r in rows[:MAX_LIST_ITEMS]:
            age_h = (now - r["ts"]) / 3600
            age_str = f"{age_h:.1f}h" if age_h < 48 else f"{age_h/24:.1f}d"
            icon = {"whitelist": "⭐", "suspicious_entry": "🔍", "cluster": "🚨"}.get(r["signal_type"], "📡")
            side = "📈" if r["side"] == "buy" else "📉"
            slug = (r["market_slug"] or "?")[:38]
            mp = r["max_price_reached"]
            delta = ""
            if mp is not None and r["price"]:
                d = (mp - r["price"]) / r["price"] * 100
                delta = f" Δmax {d:+.0f}%"
            lines.append(
                f"{icon} <code>#{r['id']}</code> {side} ${r['usdc_amount']:,.0f} "
                f"@{r['price']:.2f} ⏱{age_str}{delta}"
                f"\n  <i>{html.escape(slug)}</i>"
            )
        if len(rows) > MAX_LIST_ITEMS:
            lines.append(f"\n…и ещё {len(rows) - MAX_LIST_ITEMS}")
        return self._truncate("\n".join(lines))

    def _render_stats(self, args: list) -> str:
        type_filter = self._parse_type_arg(args or [])
        extra_where, params = "", []
        if type_filter:
            extra_where = " AND s.signal_type = ?"
            params = [type_filter]

        with self.storage._conn() as c:
            total = c.execute("SELECT COUNT(*) FROM signals s WHERE 1=1" + extra_where, params).fetchone()[0]
            resolved = c.execute(
                "SELECT COUNT(*) FROM signals s JOIN signal_outcomes o ON o.signal_id=s.id "
                "WHERE o.market_resolved=1" + extra_where, params).fetchone()[0]
            by_type = c.execute(f"""
                SELECT s.signal_type, COUNT(*) AS n,
                       SUM(CASE WHEN o.market_resolved=1 THEN 1 ELSE 0 END) AS res,
                       SUM(CASE WHEN o.trader_was_right=1 THEN 1 ELSE 0 END) AS wins,
                       AVG(CASE WHEN o.market_resolved=1 THEN o.roi_if_followed END) AS roi
                FROM signals s
                LEFT JOIN signal_outcomes o ON o.signal_id = s.id
                WHERE 1=1 {extra_where}
                GROUP BY s.signal_type ORDER BY n DESC
            """, params).fetchall()

        title = "<b>Общая сводка"
        if type_filter:
            title += f" · {type_filter}"
        title += "</b>"
        lines = [title, f"Всего сигналов: {total}", f"Резолвлено: {resolved}", ""]
        lines.append("<b>По типам:</b>")
        for r in by_type:
            wr = f"{r['wins']/r['res']*100:.1f}%" if r["res"] else "—"
            roi = f"{r['roi']*100:+.1f}%" if r["roi"] is not None else "—"
            lines.append(
                f"  {r['signal_type']}: {r['n']} (res {r['res']}, "
                f"wr {wr}, ROI {roi})"
            )
        return self._truncate("\n".join(lines))

    def _render_signal(self, sid: int) -> str:
        with self.storage._conn() as c:
            row = c.execute("""
                SELECT s.*, o.market_resolved, o.settled_price,
                       o.trader_was_right, o.roi_if_followed,
                       o.hours_to_resolve, o.max_price_reached,
                       o.min_price_reached, o.price_1h, o.price_24h
                FROM signals s
                LEFT JOIN signal_outcomes o ON o.signal_id = s.id
                WHERE s.id = ?
            """, (sid,)).fetchone()
        if not row:
            return f"Сигнал #{sid} не найден."

        ts_str = datetime.fromtimestamp(row["ts"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        icon = {"whitelist": "⭐", "suspicious_entry": "🔍", "cluster": "🚨"}.get(row["signal_type"], "📡")
        side = "📈 BUY" if row["side"] == "buy" else "📉 SELL"
        lines = [
            f"{icon} <b>Сигнал #{sid}</b>",
            f"Тип: {row['signal_type']} · {side}",
            f"Время: {ts_str}",
            f"Рынок: <i>{html.escape(row['market_slug'] or '?')}</i>",
            f"Размер: ${row['usdc_amount']:,.0f} @{row['price']:.3f}",
            f"Причина: {html.escape(row['reason'] or '?')}",
            "",
        ]
        if row["market_resolved"] is None:
            lines.append("<i>Outcome ещё не отслеживается.</i>")
        elif row["market_resolved"]:
            verdict = "✅ <b>WIN</b>" if row["trader_was_right"] else "✗ <b>LOSE</b>"
            roi_str = f"{row['roi_if_followed']*100:+.1f}%"
            hrs = f"{row['hours_to_resolve']:.1f}h"
            lines.append(
                f"Резолв: {verdict} (settled {row['settled_price']:.3f}, "
                f"ROI {roi_str}, {hrs})"
            )
        else:
            parts = ["⏳ Открыт"]
            if row["max_price_reached"] is not None:
                parts.append(f"max={row['max_price_reached']:.3f}")
            if row["min_price_reached"] is not None:
                parts.append(f"min={row['min_price_reached']:.3f}")
            if row["price_1h"] is not None:
                parts.append(f"1h={row['price_1h']:.3f}")
            if row["price_24h"] is not None:
                parts.append(f"24h={row['price_24h']:.3f}")
            lines.append(" · ".join(parts))
        return self._truncate("\n".join(lines))

    def _truncate(self, text: str) -> str:
        if len(text) <= MAX_REPLY_LEN:
            return text
        return text[:MAX_REPLY_LEN] + "\n\n<i>...(обрезано)</i>"

    async def _send_reply(self, text: str) -> None:
        if self._session is None:
            self._session = self._make_session()
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            async with self._session.post(
                f"{self.base_url}/sendMessage", json=payload
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    log.warning("sendMessage status=%d: %s", resp.status, body[:200])
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning("Не смог отправить ответ на команду: %s", e)
