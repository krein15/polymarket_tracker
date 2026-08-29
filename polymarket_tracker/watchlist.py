"""Whitelist известных инсайдеров/успешных трейдеров.

Читает файл со списком адресов (по умолчанию data/whitelist.txt). Hot-reload:
проверяет mtime файла — если изменился, перечитывает. Можно править файл
не останавливая трекер.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Set

log = logging.getLogger(__name__)


class Watchlist:
    def __init__(self, path: str):
        self.path = Path(path)
        self._addresses: Set[str] = set()
        self._mtime: float = 0.0
        self.reload()

    def reload(self) -> None:
        """Перечитать файл если он изменился."""
        if not self.path.exists():
            if self._addresses:
                log.warning("Whitelist %s пропал — сбрасываю", self.path)
                self._addresses = set()
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

        addresses = set()
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Может быть с комментарием после адреса
            addr = line.split("#")[0].split()[0].strip().lower()
            if addr.startswith("0x") and len(addr) == 42:
                addresses.add(addr)
            elif addr:
                log.warning("Пропускаю невалидный адрес в whitelist: %s", addr)

        if addresses != self._addresses:
            added = addresses - self._addresses
            removed = self._addresses - addresses
            if added:
                log.info("Whitelist: добавлено %d адресов", len(added))
            if removed:
                log.info("Whitelist: убрано %d адресов", len(removed))

        self._addresses = addresses
        self._mtime = mtime

    def is_whitelisted(self, address: str) -> bool:
        self.reload()
        return address.lower() in self._addresses

    def __len__(self) -> int:
        return len(self._addresses)
