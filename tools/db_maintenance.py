#!/usr/bin/env python3
"""CLI обслуживания БД трекера: онлайн-бэкап + retention старых trades.

Реализует пункт 0.2 роадмапа (docs/ROADMAP.md). Пути по умолчанию берутся
от корня проекта, поэтому запускать можно из любой папки.

Команды:
    python tools/db_maintenance.py backup   — снять бэкап + ротация старых
    python tools/db_maintenance.py prune    — удалить старые trades + VACUUM
    python tools/db_maintenance.py all      — backup, затем prune

Опции:
    --db PATH           путь к БД (default: data/tracker.db)
    --backup-dir PATH   куда складывать бэкапы (default: data/backups)
    --keep N            сколько бэкапов хранить (default: 14)
    --days N            возраст trades для удаления, дней (default: 7)
    --yes               не спрашивать подтверждение для prune (для планировщика)

Особенности:
  * backup использует SQLite Online Backup API — безопасно ДАЖЕ на работающем
    трекере (в отличие от обычного copy, который ловит файл посреди транзакции
    → 'database disk image is malformed'). Источник открывается read-only.
  * prune/VACUUM требуют ОСТАНОВЛЕННОГО трекера (нужен эксклюзивный доступ).
    Скрипт это проверяет и внятно сообщает, если БД залочена.
  * Свежий бэкап проверяется через PRAGMA quick_check.
  * Зависит только от stdlib (sqlite3). venv активировать не нужно.

Типовое использование:
  * перед каждым стартом трекера — `backup` (см. backup_db.bat);
  * раз в неделю при остановленном трекере — `all --yes`.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

# Пути по умолчанию — от КОРНЯ проекта (скрипт лежит в tools/).
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = str(ROOT / "data" / "tracker.db")
DEFAULT_BACKUP_DIR = str(ROOT / "data" / "backups")
DEFAULT_KEEP = 14
DEFAULT_RETENTION_DAYS = 7


def _human_size(num_bytes: int) -> str:
    val = float(num_bytes)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if val < 1024:
            return f"{val:.1f} {unit}"
        val /= 1024
    return f"{val:.1f} ТБ"


def cmd_backup(db_path: Path, backup_dir: Path, keep: int) -> int:
    """Снять онлайн-бэкап БД и проредить старые. Возвращает 0 при успехе."""
    if not db_path.exists():
        print(f"⚠ БД не найдена: {db_path} — бэкапить нечего "
              f"(трекер ещё ни разу не запускался?).")
        return 0

    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    final_path = backup_dir / f"tracker_{stamp}.db"
    tmp_path = backup_dir / f"tracker_{stamp}.db.tmp"

    print(f"Бэкап {db_path} → {final_path}")
    # Источник — строго read-only: бэкап не может ничего испортить в боевой БД.
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30.0)
    try:
        if tmp_path.exists():
            tmp_path.unlink()
        dst = sqlite3.connect(str(tmp_path), timeout=30.0)
        try:
            # Online Backup API: страницы копируются согласованно даже под
            # параллельной записью трекера.
            src.backup(dst)
        finally:
            dst.close()
    except sqlite3.Error as e:
        print(f"✘ Ошибка бэкапа: {e}")
        if tmp_path.exists():
            tmp_path.unlink()
        return 1
    finally:
        src.close()

    # Финальный файл появляется атомарно — прерванный бэкап не оставит
    # битый tracker_*.db, только .tmp.
    os.replace(tmp_path, final_path)
    size = final_path.stat().st_size

    # Проверка целостности свежего бэкапа.
    chk = sqlite3.connect(str(final_path))
    try:
        result = chk.execute("PRAGMA quick_check").fetchone()
    finally:
        chk.close()
    ok = bool(result) and result[0] == "ok"
    print(f"  ✔ {_human_size(size)}, целостность: "
          f"{'ok' if ok else f'⚠ ПРОБЛЕМА: {result}'}")

    # Ротация: имена tracker_YYYY-MM-DD.db сортируются лексикографически =
    # хронологически, поэтому старейшие — в начале списка.
    backups = sorted(backup_dir.glob("tracker_*.db"))
    excess = len(backups) - keep
    removed = 0
    for old in backups[:max(0, excess)]:
        try:
            old.unlink()
            removed += 1
        except OSError as e:
            print(f"  ⚠ не смог удалить старый бэкап {old.name}: {e}")
    if removed:
        print(f"  ротация: удалено {removed} старых (храним последние {keep})")
    print(f"  всего бэкапов: {len(list(backup_dir.glob('tracker_*.db')))}")
    return 0 if ok else 1


def cmd_prune(db_path: Path, days: int, assume_yes: bool) -> int:
    """Удалить старые trades + VACUUM. Возвращает 0 при успехе."""
    if not db_path.exists():
        print(f"⚠ БД не найдена: {db_path} — нечего чистить.")
        return 1

    # prune_old_trades / vacuum живут в storage.py — импортируем пакет.
    try:
        from polymarket_tracker.storage import Storage
    except ImportError as e:
        print(f"✘ Не смог импортировать polymarket_tracker.storage: {e}")
        print("  Запускай скрипт из КОРНЯ проекта (где лежит папка "
              "polymarket_tracker/).")
        return 1

    size_before = db_path.stat().st_size
    try:
        storage = Storage(str(db_path))
        total_before = storage.count_trades()
    except sqlite3.OperationalError as e:
        print(f"✘ БД недоступна или залочена ({e}).")
        print("  Останови трекер перед prune — нужен эксклюзивный доступ.")
        return 1

    cutoff = int(time.time()) - days * 86400
    cutoff_str = datetime.fromtimestamp(cutoff).strftime("%Y-%m-%d %H:%M")
    print(f"БД: {db_path}  ({_human_size(size_before)}, trades: {total_before})")
    print(f"Удаляю trades старше {days} дн. (раньше {cutoff_str}), затем VACUUM.")

    if not assume_yes:
        ans = input("Трекер остановлен? Продолжить? [y/N] ").strip().lower()
        if ans not in ("y", "yes", "д", "да"):
            print("Отменено.")
            return 0

    try:
        deleted = storage.prune_old_trades(older_than_days=days)
    except sqlite3.OperationalError as e:
        print(f"✘ Не смог удалить строки ({e}). Похоже, трекер запущен.")
        return 1
    print(f"  удалено строк trades: {deleted}")

    print("  VACUUM …")
    try:
        storage.vacuum()
    except sqlite3.OperationalError as e:
        print(f"  ⚠ VACUUM не выполнен ({e}). Строки удалены, но место "
              f"на диске не возвращено.")
        print("    Повтори 'prune' при полностью остановленном трекере.")
        return 1

    size_after = db_path.stat().st_size
    freed = max(0, size_before - size_after)
    print(f"  ✔ готово. Размер: {_human_size(size_before)} → "
          f"{_human_size(size_after)} (освобождено {_human_size(freed)})")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Обслуживание БД трекера: онлайн-бэкап + retention trades.",
    )
    parser.add_argument("command", choices=["backup", "prune", "all"],
                        help="что делать")
    parser.add_argument("--db", default=DEFAULT_DB,
                        help=f"путь к БД (default: {DEFAULT_DB})")
    parser.add_argument("--backup-dir", default=DEFAULT_BACKUP_DIR,
                        help=f"папка бэкапов (default: {DEFAULT_BACKUP_DIR})")
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP,
                        help=f"сколько бэкапов хранить (default: {DEFAULT_KEEP})")
    parser.add_argument("--days", type=int, default=DEFAULT_RETENTION_DAYS,
                        help=f"возраст trades для удаления, дней "
                             f"(default: {DEFAULT_RETENTION_DAYS})")
    parser.add_argument("--yes", action="store_true",
                        help="не спрашивать подтверждение для prune")
    args = parser.parse_args(argv)

    if args.keep < 1:
        parser.error("--keep должно быть ≥ 1")
    if args.days < 0:
        parser.error("--days должно быть ≥ 0")

    db_path = Path(args.db)
    backup_dir = Path(args.backup_dir)

    rc = 0
    if args.command in ("backup", "all"):
        rc = cmd_backup(db_path, backup_dir, args.keep)
        # В режиме 'all' не чистим, если бэкап не удался — это страховка.
        if rc != 0 and args.command == "all":
            print("✘ Бэкап не удался — prune пропущен (защита данных).")
            return rc
    if args.command in ("prune", "all"):
        rc = cmd_prune(db_path, args.days, args.yes)
    return rc


if __name__ == "__main__":
    sys.exit(main())
