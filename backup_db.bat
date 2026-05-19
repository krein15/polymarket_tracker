@echo off
REM Снимает бэкап tracker.db в backups\ и чистит старые (пункт 0.2 TODO).
REM Безопасно даже на работающем трекере — SQLite Online Backup API.
REM Запускать: вручную, через Планировщик задач, либо `call backup_db.bat`
REM первой строкой в start_tracker.bat (перед запуском трекера).
cd /d "%~dp0"
py -3 db_maintenance.py backup
