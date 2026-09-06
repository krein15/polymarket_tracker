"""Сигнал жизни: ежедневная сводка в Telegram и тревога при остановке потока.

Зачем
-----
Трекер задуман работать неделями без присмотра. Если процесс упадёт, ноутбук
уснёт или пропадёт сеть, узнать об этом можно будет только заглянув в консоль
— то есть с задержкой в сутки и больше, потеряв данные, ради которых всё и
затевалось.

Два разных отказа требуют двух разных сигналов:

  * процесс умер целиком — тогда никакого сообщения не придёт вовсе, и
    отсутствие ежедневной сводки само по себе служит тревогой;
  * процесс жив, но поток сделок иссяк (сеть отвалилась, Data API молчит,
    листенер застрял) — здесь молчание не поможет, нужно активное
    предупреждение.

Первое закрывает ежедневная сводка, второе — сторож потока.

О пороге тревоги
----------------
Data API публикует сделки пачками раз в ~5 минут, поэтому пауза в несколько
минут — норма, а не поломка. Порог по умолчанию 20 минут: это четыре
пропущенные пачки подряд, случайностью уже не объяснить.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

# Как часто просыпаемся проверить поток. Мельче смысла нет: пачки раз в 5 минут.
CHECK_INTERVAL_SEC = 120


@dataclass
class TrackerStats:
    """Снимок состояния для сводки. Заполняет core."""

    uptime_hours: float
    trades_processed: int
    signals_sent: int
    last_trade_age_min: Optional[float]
    trades_total: int
    signals_total: int
    resolved_total: int
    wallets_total: int


def _num(value: int) -> str:
    """Разряды неразрывным пробелом. Точечно, а не сплошной заменой по
    строке: та съедала и запятые-разделители в самом тексте."""
    return f"{value:,}".replace(",", " ")


def format_digest(s: TrackerStats) -> str:
    """Ежедневная сводка. Коротко: её читают мельком, с телефона."""
    lines = [
        "📊 <b>Сводка за сутки</b>",
        f"Аптайм: {s.uptime_hours:.1f} ч",
        f"Обработано сделок: {_num(s.trades_processed)}",
        f"Отправлено сигналов: {s.signals_sent}",
        "",
        f"Всего в базе: {_num(s.trades_total)} сделок, {_num(s.wallets_total)} кошельков",
        f"Сигналов всего: {s.signals_total}, исходов закрыто: {s.resolved_total}",
    ]
    if s.last_trade_age_min is not None:
        lines.append(f"Последняя сделка: {s.last_trade_age_min:.0f} мин назад")
    return chr(10).join(lines)


def format_stall(age_minutes: float) -> str:
    return (
        f"<b>Поток сделок остановился</b>\n"
        f"Новых сделок нет {age_minutes:.0f} мин.\n"
        f"Обычная пауза — 5 минут (Data API отдаёт пачками). "
        f"Проверь сеть и консоль трекера."
    )


def format_recovery(gap_minutes: float) -> str:
    return f"✅ Поток восстановился, простой составил {gap_minutes:.0f} мин."


class Heartbeat:
    """Фоновая задача: сводка по расписанию + сторож потока."""

    def __init__(self, notifier, stats_provider, config):
        self.notifier = notifier
        self.stats_provider = stats_provider  # callable -> TrackerStats
        self.config = config
        self._last_digest = time.time()
        self._stalled_since: Optional[float] = None

    async def run(self) -> None:
        interval = self.config.heartbeat_interval_hours * 3600
        stall_limit = self.config.stall_alert_minutes
        log.info(
            "Heartbeat: сводка раз в %.0f ч, тревога при простое > %.0f мин",
            self.config.heartbeat_interval_hours, stall_limit,
        )
        while True:
            try:
                await asyncio.sleep(CHECK_INTERVAL_SEC)
                now = time.time()
                stats = self.stats_provider()

                await self._check_stall(stats, stall_limit)

                if now - self._last_digest >= interval:
                    await self.notifier.send_html(format_digest(stats))
                    self._last_digest = now
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001 — сторож не должен ронять трекер
                log.warning("Heartbeat: ошибка цикла: %s", e)

    async def _check_stall(self, stats: TrackerStats, limit_minutes: float) -> None:
        age = stats.last_trade_age_min
        if age is None:
            return
        if age > limit_minutes:
            # Тревогу шлём один раз, а не каждые две минуты.
            if self._stalled_since is None:
                self._stalled_since = time.time()
                await self.notifier.send_alert(format_stall(age))
                log.warning("Поток сделок остановился: %.0f мин без новых", age)
        elif self._stalled_since is not None:
            gap = (time.time() - self._stalled_since) / 60.0
            self._stalled_since = None
            await self.notifier.send_html(format_recovery(gap))
            log.info("Поток восстановился после %.0f мин простоя", gap)
