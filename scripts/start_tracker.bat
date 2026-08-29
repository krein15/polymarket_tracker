@echo off
REM ═══════════════════════════════════════════════════════════════
REM  Запуск трекера. Пути относительные - папку проекта можно
REM  переносить куда угодно, править .bat не нужно.
REM ═══════════════════════════════════════════════════════════════
cd /d "%~dp0.."
REM Python: печатать в кодировке консоли, непечатаемые символы (эмодзи)
REM заменять на "?" вместо падения с UnicodeEncodeError при перенаправлении вывода.
set "PYTHONIOENCODING=cp866:replace"

REM Бэкап БД перед стартом (безопасно, SQLite Online Backup API)
call "%~dp0backup_db.bat"

if not exist ".venv\Scripts\activate.bat" (
    echo [ОШИБКА] venv не найден: %CD%\.venv
    echo Создай его:  py -3.12 -m venv .venv ^&^& .venv\Scripts\activate ^&^& pip install -r requirements.txt
    pause
    exit /b 1
)

if not exist ".env" (
    echo [ОШИБКА] Нет файла .env - скопируй .env.example в .env и заполни токен/chat_id
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat
python tracker_main.py
pause