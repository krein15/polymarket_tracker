@echo off
REM ═══════════════════════════════════════════════════════════════
REM  Запуск трекера. Пути относительные - папку проекта можно
REM  переносить куда угодно, править .bat не нужно.
REM ═══════════════════════════════════════════════════════════════
cd /d "%~dp0.."
REM Аргумент "quiet" - не ждать клавишу в конце: под Планировщиком задач
REM pause подвесил бы задачу навсегда после падения трекера.
set "QUIET="
if /I "%~1"=="quiet" set "QUIET=1"
REM Python: печатать в кодировке консоли, непечатаемые символы (эмодзи)
REM заменять на "?" вместо падения с UnicodeEncodeError при перенаправлении вывода.
set "PYTHONIOENCODING=cp866:replace"

REM Бэкап БД перед стартом, но не чаще раза в 12 часов: при перезапусках
REM трекера полная копия базы каждый раз ни к чему.
call "%~dp0backup_db.bat" 12

if not exist ".venv\Scripts\activate.bat" (
    echo [ОШИБКА] venv не найден: %CD%\.venv
    echo Создай его:  py -3.12 -m venv .venv ^&^& .venv\Scripts\activate ^&^& pip install -r requirements.txt
    if not defined QUIET pause
    exit /b 1
)

if not exist ".env" (
    echo [ОШИБКА] Нет файла .env - скопируй .env.example в .env и заполни токен/chat_id
    if not defined QUIET pause
    exit /b 1
)

call .venv\Scripts\activate.bat
python tracker_main.py
if not defined QUIET pause