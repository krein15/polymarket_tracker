@echo off
REM Снимает бэкап data\tracker.db в data\backups\ и чистит старые (пункт 0.2 роадмапа).
REM Безопасно даже на работающем трекере - SQLite Online Backup API.
REM Запускать: вручную, через Планировщик задач, либо вызовом из start_tracker.bat.
cd /d "%~dp0.."
REM Python: печатать в кодировке консоли, непечатаемые символы (эмодзи)
REM заменять на "?" вместо падения с UnicodeEncodeError при перенаправлении вывода.
set "PYTHONIOENCODING=cp866:replace"
py -3 tools\db_maintenance.py backup