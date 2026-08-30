"""Whitelist успешных трейдеров — с уровнями и персональными порогами.

Читает файл со списком адресов (по умолчанию data/whitelist.txt). Hot-reload:
проверяет mtime файла — если изменился, перечитывает. Можно править файл
не останавливая трекер.

Формат строки (метаданные пишет tools/analyze_whitelist.py):

    0xADDRESS  # ник | tier=pass | pnl=$123,456 roi=8.4% | big=$1,200 | ...

  tier=pass  — зарабатывает и торгует с разумной частотой: даёт свой сигнал;
  tier=watch — под наблюдением (мало зарабатывает либо торгует потоком):
               своего сигнала НЕ даёт, только добавляет баллы скорингу;
  big=$N     — 90-й процентиль ЕГО покупок. Сигналим от этой суммы, а не от
               общего порога: $200 от того, кто обычно ставит $5000, — шум,
               а $2000 от того, кто обычно ставит $200, — редкая уверенность.

Строки без метаданных (старый формат) читаются как tier=pass без личного
порога — обратная совместимость.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

log = logging.getLogger(__name__)

TIER_PASS = "pass"
TIER_WATCH = "watch"

_TIER_RE = re.compile(r"tier\s*=\s*(\w+)", re.I)
# big=$1,234 или big=1234
_BIG_RE = re.compile(r"big\s*=\s*\$?([\d,.]+)", re.I)


@dataclass(frozen=True)
class WhitelistEntry:
    """Адрес из whitelist со своими настройками."""

    address: str
    tier: str = TIER_PASS
    big_usdc: float = 0.0  # 0 — личного порога нет, работает общий
    nickname: str = ""

    @property
    def signals_on_its_own(self) -> bool:
        """Даёт ли собственный сигнал Ветки B (watch — только баллы)."""
        return self.tier == TIER_PASS


class Watchlist:
    def __init__(self, path: str):
        self.path = Path(path)
        self._entries: Dict[str, WhitelistEntry] = {}
        self._mtime: float = 0.0
        self.reload()

    def reload(self) -> None:
        """Перечитать файл если он изменился."""
        if not self.path.exists():
            if self._entries:
                log.warning("Whitelist %s пропал — сбрасываю", self.path)
                self._entries = {}
            return

        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return

        if mtime == self._mtime:
            return  # не менялся

        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError as e:
            log.warning("Не смог прочитать whitelist %s: %s", self.path, e)
            return

        entries: Dict[str, WhitelistEntry] = {}
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            addr = line.split("#")[0].split()[0].strip().lower()
            if not (addr.startswith("0x") and len(addr) == 42):
                if addr:
                    log.warning("Пропускаю невалидный адрес в whitelist: %s", addr)
                continue
            entries[addr] = self._parse_entry(addr, line)

        if set(entries) != set(self._entries):
            added = set(entries) - set(self._entries)
            removed = set(self._entries) - set(entries)
            if added:
                log.info("Whitelist: добавлено %d адресов", len(added))
            if removed:
                log.info("Whitelist: убрано %d адресов", len(removed))

        self._entries = entries
        self._mtime = mtime

    @staticmethod
    def _parse_entry(addr: str, line: str) -> WhitelistEntry:
        """Разобрать метаданные из комментария после адреса."""
        comment = line.split("#", 1)[1] if "#" in line else ""
        nickname = comment.split("|")[0].strip() if comment else ""

        tier = TIER_PASS
        m = _TIER_RE.search(comment)
        if m:
            value = m.group(1).lower()
            if value in (TIER_PASS, TIER_WATCH):
                tier = value
            else:
                log.warning("Неизвестный tier=%s у %s — считаю как pass", value, addr[:12])

        big = 0.0
        m = _BIG_RE.search(comment)
        if m:
            try:
                big = float(m.group(1).replace(",", ""))
            except ValueError:
                log.warning("Не разобрал big= у %s: %s", addr[:12], m.group(1))

        return WhitelistEntry(address=addr, tier=tier, big_usdc=big, nickname=nickname)

    def get(self, address: str) -> Optional[WhitelistEntry]:
        """Запись по адресу или None. Дёргает hot-reload."""
        self.reload()
        return self._entries.get(address.lower())

    def is_whitelisted(self, address: str) -> bool:
        """Есть ли адрес в списке (любого уровня)."""
        return self.get(address) is not None

    def count_by_tier(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for e in self._entries.values():
            counts[e.tier] = counts.get(e.tier, 0) + 1
        return counts

    def __len__(self) -> int:
        return len(self._entries)
