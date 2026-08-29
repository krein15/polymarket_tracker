@echo off
REM ═══════════════════════════════════════════════════════════════
REM  Коммит + пуш всех изменений проекта.
REM
REM  Использование:
REM      git_push.bat                       - сообщение "Auto update: <дата>"
REM      git_push.bat Починил детектор       - своё сообщение (кавычки не нужны)
REM
REM  Пути относительные (%~dp0..) - папку проекта можно переносить.
REM ═══════════════════════════════════════════════════════════════
cd /d "%~dp0.."
echo Проект: %CD%
echo.

REM ── 1. Есть ли git ──
where git >nul 2>&1
if errorlevel 1 goto :no_git

REM ── 2. Это git-репозиторий- ──
git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 goto :no_repo

REM ── 3. Защита от утечки секретов ──
git ls-files --error-unmatch .env >nul 2>&1
if not errorlevel 1 goto :env_tracked

REM ── 4. Сообщение коммита ──
set "MSG=%*"
if "%MSG%"=="" set "MSG=Auto update: %date% %time%"

REM ── 5. Индексируем всё ──
echo [1/3] git add -A
git add -A
if errorlevel 1 goto :fail

REM ── 6. Коммит (если есть что коммитить) ──
git diff --cached --quiet
if not errorlevel 1 goto :nothing_to_commit
echo [2/3] git commit -m "%MSG%"
git commit -m "%MSG%"
if errorlevel 1 goto :fail
goto :push

:nothing_to_commit
echo [2/3] Изменений нет - новый коммит не создаём.
echo       Всё равно пробуем запушить то, что ещё не улетело.

REM ── 7. Пуш ──
:push
git remote get-url origin >nul 2>&1
if errorlevel 1 goto :no_remote

git rev-parse --abbrev-ref --symbolic-full-name @{u} >nul 2>&1
if errorlevel 1 goto :push_new_branch

echo [3/3] git push
git push
if errorlevel 1 goto :fail
goto :done

:push_new_branch
echo [3/3] Ветка ещё не связана с origin - пушим с -u
git push -u origin HEAD
if errorlevel 1 goto :fail
goto :done

:done
echo.
echo [OK] Готово.
git log -1 --oneline
goto :end

REM ═══════════════════ ошибки ═══════════════════

:no_git
echo [ОШИБКА] git не установлен или не в PATH.
echo Поставь Git for Windows: https://git-scm.com/download/win
echo После установки открой НОВОЕ окно cmd (PATH обновляется только в новых).
goto :end

:no_remote
echo.
echo [ВНИМАНИЕ] Коммит создан локально, но пушить некуда: нет remote origin.
echo Создай приватный репозиторий на https://github.com/new (ПУСТОЙ, без README),
echo затем один раз выполни:
echo     git remote add origin https://github.com/USER/polymarket_tracker.git
echo     git push -u origin main
echo Дальше git_push.bat будет пушить сам.
goto :end

:no_repo
echo [ОШИБКА] Здесь ещё нет git-репозитория.
echo Запусти один раз:  scripts\git_init.bat
goto :end

:env_tracked
echo [СТОП] Файл .env попал под контроль версий - это твои токены!
echo Убери его из индекса и только потом пушь:
echo     git rm --cached .env
echo     git commit -m "Remove .env from repo"
echo И проверь, что в .gitignore есть строка  .env
goto :end

:fail
echo.
echo [ОШИБКА] Команда git завершилась с ошибкой (см. вывод выше).
echo Частые причины:
echo   * нужен логин - вводи username и Personal Access Token вместо пароля
echo   * кто-то пушил в ту же ветку - сделай:  git pull --rebase
goto :end

:end
echo.
pause