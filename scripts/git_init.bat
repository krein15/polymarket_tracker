@echo off
REM ═══════════════════════════════════════════════════════════════
REM  Разовая инициализация git-репозитория для проекта.
REM
REM  Использование:
REM      git_init.bat                                      - только локальный репозиторий
REM      git_init.bat https://github.com/USER/repo.git      - + привязка к GitHub
REM
REM  Репозиторий на GitHub создай заранее (https://github.com/new),
REM  ПУСТОЙ - без README, .gitignore и лицензии, иначе будет конфликт.
REM ═══════════════════════════════════════════════════════════════
cd /d "%~dp0.."
echo Проект: %CD%
echo.

where git >nul 2>&1
if errorlevel 1 goto :no_git

git rev-parse --is-inside-work-tree >nul 2>&1
if not errorlevel 1 goto :already

echo [1/4] git init
git init
if errorlevel 1 goto :fail
git branch -M main

echo [2/4] git add -A
git add -A
if errorlevel 1 goto :fail

echo.
echo Что попадёт в первый коммит (проверь, что тут НЕТ .env и *.db):
git status --short
echo.
pause

echo [3/4] Первый коммит
git commit -m "Restructure: пакет, tools/, scripts/, data/, docs/"
if errorlevel 1 goto :fail

if "%~1"=="" goto :no_remote_given
echo [4/4] git remote add origin %~1
git remote add origin %~1
if errorlevel 1 goto :fail
git push -u origin main
if errorlevel 1 goto :fail
echo.
echo [OK] Готово. Дальше просто запускай scripts\git_push.bat
goto :end

:no_remote_given
echo [4/4] Remote не указан - репозиторий пока только локальный.
echo Чтобы привязать GitHub:
echo     git remote add origin https://github.com/USER/polymarket_tracker.git
echo     git push -u origin main
goto :end

:already
echo Репозиторий уже инициализирован - ничего делать не нужно.
echo Для коммитов используй scripts\git_push.bat
goto :end

:no_git
echo [ОШИБКА] git не установлен или не в PATH.
echo https://git-scm.com/download/win  (после установки открой новое окно cmd)
goto :end

:fail
echo.
echo [ОШИБКА] Команда git завершилась с ошибкой (см. вывод выше).
goto :end

:end
echo.
pause