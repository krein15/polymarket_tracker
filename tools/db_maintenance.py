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
from typing import Optional

# Windows-консоль работает в cp866/cp1251 и не знает части символов (стрелки,
# галочки). Пока вывод идёт в консоль, Python печатает их через WriteConsoleW,
# но при ПЕРЕНАПРАВЛЕНИИ (> log.txt, Планировщик задач) переключается на
# кодировку локали и падает с UnicodeEncodeError. Заменяем непечатаемое на "?".
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):  # не TextIOWrapper — не наша забота
        pass


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


def _drop_trades(path: Path) -> None:
    """Выбросить из копии таблицу trades и сжать файл.

    trades — 95% размера базы и при этом сырьё: её всё равно чистит retention,
    восстанавливать неоткуда и незачем. Ценное (signals, signal_outcomes,
    shadow_trades, wallets, checkpoint) весит единицы мегабайт и помещается
    в бесплатный тариф облака.
    """
    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
    try:
        conn.execute("DELETE FROM trades")
        conn.execute("VACUUM")
    finally:
        conn.close()


def _fresh_backup_exists(backup_dir: Path, prefix: str, max_age_hours: float) -> Optional[Path]:
    """Свежий бэкап моложе max_age_hours, если он есть."""
    if max_age_hours <= 0:
        return None
    cutoff = time.time() - max_age_hours * 3600
    for path in sorted(backup_dir.glob(f"{prefix}*.db"), reverse=True):
        try:
            if path.stat().st_mtime >= cutoff:
                return path
        except OSError:
            continue
    return None


def cmd_backup(
    db_path: Path,
    backup_dir: Path,
    keep: int,
    light: bool = False,
    min_interval_hours: float = 0.0,
) -> int:
    """Снять онлайн-бэкап БД и проредить старые. Возвращает 0 при успехе."""
    if not db_path.exists():
        print(f"⚠ БД не найдена: {db_path} — бэкапить нечего "
              f"(трекер ещё ни разу не запускался?).")
        return 0

    backup_dir.mkdir(parents=True, exist_ok=True)
    prefix = "light_" if light else "tracker_"

    fresh = _fresh_backup_exists(backup_dir, prefix, min_interval_hours)
    if fresh is not None:
        age_h = (time.time() - fresh.stat().st_mtime) / 3600
        print(f"Свежий бэкап уже есть ({fresh.name}, {age_h:.1f} ч назад) — пропускаю.")
        return 0

    stamp = datetime.now().strftime("%Y-%m-%d")
    final_path = backup_dir / f"{prefix}{stamp}.db"
    tmp_path = backup_dir / f"{prefix}{stamp}.db.tmp"

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

    if light:
        full_size = tmp_path.stat().st_size
        try:
            _drop_trades(tmp_path)
        except sqlite3.Error as e:
            print(f"✘ Не смог облегчить копию: {e}")
            tmp_path.unlink(missing_ok=True)
            return 1
        print(f"  облегчено: {_human_size(full_size)} → "
              f"{_human_size(tmp_path.stat().st_size)} (без таблицы trades)")

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
    backups = sorted(backup_dir.glob(f"{prefix}*.db"))
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
    print(f"  всего бэкапов: {len(list(backup_dir.glob(f'{prefix}*.db')))}")
    return 0 if ok else 1


def cmd_prune(db_path: Path, days: int, assume_yes: bool) -> int:
    """Удалить старые trades + VACUUM. Возвращает 0 при успехе."""
    if not db_path.exists():
        print(f"⚠ БД не найдена: {db_path} — нечего чистить.")
        return 1

    # prune_old_trades / vacuum живут в storage.py — импортируем пакет.
    #
    # Корень проекта добавляем в путь явно. При запуске "python
    # tools/db_maintenance.py" интерпретатор кладёт в sys.path папку
    # СКРИПТА, то есть tools/, а не текущий каталог, — и совет "запускай
    # из корня" не помогал: импорт падал именно из корня.
    root = str(Path(__file__).resolve().parent.parent)
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from polymarket_tracker.storage import Storage
    except ImportError as e:
        print(f"✘ Не смог импортировать polymarket_tracker.storage: {e}")
        print(f"  Ожидал найти пакет в {root}")
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
        last = [0]

        def show(done: int) -> None:
            # Печатаем не каждую порцию, а раз в полмиллиона строк: иначе
            # вывод сам становится помехой.
            if done - last[0] >= 500_000:
                last[0] = done
                print(f"    удалено {done:,}…", flush=True)

        deleted = storage.prune_old_trades(older_than_days=days, progress=show)
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
    parser.add_argument("--light", action="store_true",
                        help="копия без таблицы trades: единицы МБ вместо гигабайтов, "
                             "для выгрузки в облако")
    parser.add_argument("--min-interval-hours", type=float, default=0.0,
                        help="не делать бэкап, если свежий моложе N часов "
                             "(default: 0 — делать всегда)")
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
        rc = cmd_backup(db_path, backup_dir, args.keep, args.light, args.min_interval_hours)
        # В режиме 'all' не чистим, если бэкап не удался — это страховка.
        if rc != 0 and args.command == "all":
            print("✘ Бэкап не удался — prune пропущен (защита данных).")
            return rc
    if args.command in ("prune", "all"):
        rc = cmd_prune(db_path, args.days, args.yes)
    return rc


if __name__ == "__main__":
    sys.exit(main())
