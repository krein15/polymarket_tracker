@echo off
REM ═══════════════════════════════════════════════════════════════
REM  Лёгкий бэкап в облако (Яндекс.Диск).
REM
REM  Копия БЕЗ таблицы trades: единицы мегабайт вместо гигабайтов.
REM  trades - сырьё, её и так чистит retention; ценное (signals,
REM  signal_outcomes, shadow_trades, wallets) весит копейки и влезает
REM  в бесплатный тариф облака.
REM
REM  Использование:
REM      backup_cloud.bat                 - в %USERPROFILE%\Yandex.Disk\polymarket_tracker
REM      backup_cloud.bat "D:\другая\папка"
REM      backup_cloud.bat quiet           - без паузы в конце (для Планировщика)
REM
REM  Безопасно на работающем трекере: копия снимается SQLite Online
REM  Backup API, то есть согласованно. Синхронизировать саму папку
REM  data\ клиентом облака НЕЛЬЗЯ - он выгрузит базу посреди записи.
REM ═══════════════════════════════════════════════════════════════
cd /d "%~dp0.."
set "PYTHONIOENCODING=cp866:replace"

set "QUIET="
set "CLOUD_DIR="
for %%A in (%*) do (
    if /I "%%~A"=="quiet" (set "QUIET=1") else (if not defined CLOUD_DIR set "CLOUD_DIR=%%~A" & set "CLOUD_DIR_ARG=1")
)
if not defined CLOUD_DIR set "CLOUD_DIR=%USERPROFILE%\Yandex.Disk\polymarket_tracker"

if not exist "%USERPROFILE%\Yandex.Disk" (
    if not defined CLOUD_DIR_ARG (
        echo [ВНИМАНИЕ] Папка %USERPROFILE%\Yandex.Disk не найдена.
        echo Поставь клиент с https://disk.yandex.ru или укажи путь аргументом:
        echo     backup_cloud.bat "D:\куда\класть"
        if not defined QUIET pause
        exit /b 1
    )
)

echo Лёгкий бэкап в: %CLOUD_DIR%
py -3 tools\db_maintenance.py backup --light --backup-dir "%CLOUD_DIR%" --keep 7
echo.
echo Клиент облака выгрузит файл сам. Проверь, что он появился в веб-интерфейсе.

REM Аргумент "quiet" в любой позиции - не ждать клавишу: под Планировщиком
REM задач pause подвесил бы процесс навсегда.
if not defined QUIET pause
