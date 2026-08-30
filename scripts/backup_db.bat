@echo off
REM Снимает бэкап data\tracker.db в data\backups\ и чистит старые (пункт 0.2 роадмапа).
REM Безопасно даже на работающем трекере - SQLite Online Backup API.
REM Запускать: вручную, через Планировщик задач, либо вызовом из start_tracker.bat.
cd /d "%~dp0.."
REM Python: печатать в кодировке консоли, непечатаемые символы (эмодзи)
REM заменять на "?" вместо падения с UnicodeEncodeError при перенаправлении вывода.
set "PYTHONIOENCODING=cp866:replace"
REM Аргумент 1: не делать бэкап, если свежий моложе N часов (0 - делать всегда).
set "MIN_AGE=%~1"
if "%MIN_AGE%"=="" set "MIN_AGE=0"
py -3 tools\db_maintenance.py backup --min-interval-hours %MIN_AGE%