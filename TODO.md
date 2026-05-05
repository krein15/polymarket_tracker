# TODO: восстановление проекта на новом ПК

> Этот файл — пошаговая инструкция «с нуля до работающего трекера» после потери жёсткого диска. Иди по порядку, каждый шаг проверяй прежде чем переходить к следующему. Все команды для Windows (cmd).

---

## Часть 1: Сохранение в GitHub (после распаковки архива, ОДИН РАЗ)

Это первое что делаешь — чтобы такая ситуация больше не повторилась. Если жёсткий диск снова умрёт, исходники будут на серверах GitHub.

### 1.1 Создать аккаунт на GitHub (если нет)

- Зарегистрируйся на <https://github.com/signup> (бесплатно)
- Подтверди email

### 1.2 Установить Git для Windows

- Скачай: <https://git-scm.com/download/win>
- Установка с дефолтными настройками (просто жми Next везде)
- Проверь:
  ```cmd
  git --version
  ```
  Должно вывести `git version 2.x.x`. Если "не найдена команда" — перезапусти cmd (PATH обновляется только в новых окнах).

### 1.3 Настроить Git (один раз)

```cmd
git config --global user.name "Твоё имя"
git config --global user.email "твой@email.com"
```

Email используй тот же, что в GitHub-аккаунте.

### 1.4 Создать пустой репозиторий на GitHub

1. Открой <https://github.com/new>
2. **Repository name:** `polymarket_tracker`
3. **Description:** (опционально) `Polymarket insider activity tracker — Telegram alerts on suspicious trades`
4. **Private** — выбери (это твой код, не светим публично)
5. **НЕ ставь** галочки "Add README", "Add .gitignore", "Add license" — они уже есть в архиве, иначе будет конфликт
6. Жми **Create repository**

После создания GitHub покажет страницу с инструкциями. Ниже — ровно те же команды, адаптированные.

### 1.5 Распаковать архив и перейти в папку

Распакуй архив в любое место, например `C:\polymarket_tracker\`. После распаковки структура должна быть:

```
C:\polymarket_tracker\
├── tracker_main.py
├── README.md
├── TODO.md          ← этот файл
├── .env.example
├── .gitignore
├── requirements.txt
├── whitelist.txt
└── polymarket_tracker\
    ├── __init__.py
    ├── config.py
    └── ... остальные модули
```

В cmd:
```cmd
cd C:\polymarket_tracker
```

### 1.6 Залить в GitHub

```cmd
git init
git add .
git status
```

Команда `git status` покажет, что будет закоммичено. **Убедись что в списке НЕТ `.env`, `tracker.db`, `.venv`** — это секреты и временные файлы. Если они там есть — что-то с `.gitignore` не так, спроси меня.

Если всё ок, продолжай:

```cmd
git commit -m "Initial commit: v0.2 Data API edition"
git branch -M main
git remote add origin https://github.com/ТВОЙ_USERNAME/polymarket_tracker.git
git push -u origin main
```

GitHub попросит логин — введи username и **Personal Access Token** вместо пароля (обычные пароли больше не принимают). Как получить токен:
- <https://github.com/settings/tokens> → Generate new token (classic)
- **Note:** `polymarket_tracker push`
- **Expiration:** 90 days (или больше)
- **Scopes:** ✅ `repo` (вся секция)
- Жми Generate, **скопируй токен сразу** (показывается ОДИН раз!), сохрани в надёжное место (например в менеджер паролей)
- Используй этот токен вместо пароля при `git push`

После успешного `git push` — открой <https://github.com/ТВОЙ_USERNAME/polymarket_tracker> и убедись что файлы там.

### 1.7 Дальнейшие коммиты

После любых правок кода:
```cmd
git add .
git commit -m "Описание что поменял"
git push
```

Делай это **минимум раз в день** когда работаешь над кодом.

---

## Часть 2: Установка трекера на новом ПК (по шагам)

После того как архив распакован и (желательно) залит в GitHub — настраиваем рабочую среду. **Это повтор того что мы делали в прошлом чате**, но с учётом всего что выяснили (Python 3.12, Data API вместо RPC, и т.д.).

### Шаг 1. Установить Python 3.12

- Скачай: <https://www.python.org/downloads/release/python-31210/>
- Файл: **Windows installer (64-bit)**
- При установке:
  - ❌ галочка "Add python.exe to PATH" — НЕ ставь (может конфликтовать с другим питоном)
  - ✅ галочка "Use admin privileges when installing py.exe" — поставь
  - Жми **Install Now**

Проверь в новом cmd:
```cmd
py -3.12 --version
```
Должно показать `Python 3.12.x`.

### Шаг 2. Создать venv и поставить зависимости

```cmd
cd C:\polymarket_tracker
py -3.12 -m venv .venv
.venv\Scripts\activate
```

После активации в строке появится `(.venv)`. Дальше:

```cmd
python -m pip install --upgrade pip
pip install -r requirements.txt
```

В отличие от v0.1 — **никаких ошибок про lru-dict / Visual C++**. Все зависимости pure-Python либо имеют готовые wheels.

Проверь:
```cmd
python -c "import aiohttp, dotenv, cachetools; print('OK')"
```

### Шаг 3. Создать Telegram-бота (если нет)

Если у тебя сохранился токен от старого бота — **используй его**, бот по-прежнему живой на серверах Telegram, твой жёсткий диск его не затрагивал. Просто вспомни/посмотри chat_id (открой в браузере `https://api.telegram.org/bot<ТОКЕН>/getUpdates` — нужно сначала написать боту что-то).

Если токен потерян:
1. Telegram → @BotFather → `/mybots` → выбери своего бота → API Token (там же можно перегенерить)
2. ИЛИ создай нового через `/newbot` (см. README → Настройка Telegram бота)

### Шаг 4. Заполнить .env

```cmd
copy .env.example .env
notepad .env
```

Минимум — два поля:
```env
TELEGRAM_BOT_TOKEN=твой_токен
TELEGRAM_CHAT_ID=твой_chat_id
```

Сохрани (Ctrl+S), закрой.

Проверка:
```cmd
python -c "from polymarket_tracker.config import Config; c = Config.from_env('.env'); errs = c.validate(); print('OK' if not errs else errs); print(f'token_len={len(c.telegram_bot_token)}, chat_id={c.telegram_chat_id!r}')"
```

Ждём `OK` и `token_len=46`.

### Шаг 5. Проверить Data API

```cmd
python -c "import urllib.request, json; r = urllib.request.urlopen('https://data-api.polymarket.com/trades?limit=3', timeout=10); data = json.loads(r.read()); print(f'got {len(data)} trades'); print(json.dumps(data[0], indent=2, ensure_ascii=False)[:1500] if data else 'empty')"
```

Должен вывести 3 сделки в JSON. Если упало с ошибкой — Data API недоступен (редко) или у тебя нет интернета.

### Шаг 6. Первый запуск

```cmd
python tracker_main.py
```

В консоли:
```
2026-XX-XX HH:MM:SS [INFO] polymarket_tracker.core: === Tracker started ===
2026-XX-XX HH:MM:SS [INFO] polymarket_tracker.data_api_listener: DataApiListener стартует без чекпоинта — берём свежие сделки
```

В Telegram через 5-10 секунд:
> ℹ️ *Трекер запущен (Data API). Whitelist: 0 адресов. ...*

### Шаг 7. Проверить что сделки идут

**Не закрывая первое окно**, открой второе cmd:
```cmd
cd C:\polymarket_tracker
.venv\Scripts\activate
python -c "import sqlite3; c = sqlite3.connect('tracker.db'); print('trades:', c.execute('SELECT COUNT(*) FROM trades').fetchone()[0]); print('wallets:', c.execute('SELECT COUNT(*) FROM wallets').fetchone()[0]); print('signals:', c.execute('SELECT COUNT(*) FROM signals').fetchone()[0])"
```

Через 5-10 минут после старта должно быть **trades > 0**, **wallets > 0**. Сигналов на старте может быть 0 — это нормально, фильтры жёсткие.

### Шаг 8. Собрать whitelist

Открой <https://polymarket.com/leaderboard>, скопируй адреса топ-10 трейдеров за месяц/год. Добавь в `whitelist.txt` (один адрес на строку, после `#` можно комментарий с никнеймом).

Hot-reload — править файл можно не останавливая трекер.

### Шаг 9. Автозапуск

Чтобы трекер работал когда закрыто окно:

**Вариант 1 — Windows Task Scheduler** (рекомендую для постоянной работы):
- Win+R → `taskschd.msc`
- Create Task → название "Polymarket Tracker"
- Triggers: At startup
- Actions: Start a program → `C:\polymarket_tracker\.venv\Scripts\python.exe`, аргументы `tracker_main.py`, Start in `C:\polymarket_tracker`
- Settings → ✅ "If the task fails, restart every 1 minute, attempt up to 999 times"

**Вариант 2 — оставить cmd открытым** (просто свернуть). Меньше возни, но при перезагрузке надо запускать вручную.

### Шаг 10. Зафиксировать бейзлайн

Через 1 час после старта запиши в заметку:
- Время старта
- `MIN_TRADE_USDC`, `CLUSTER_MIN_WALLETS`, `MAX_MARKET_VOLUME_24H` из .env
- Сколько trades в БД через 1 час (запрос из шага 7)
- Сколько signals пришло в Telegram

Это база для калибровки порогов через 1-2 недели.

---

## Часть 3: Регулярные действия (после установки)

### Раз в день
- Проверять что трекер живой (последние сообщения в Telegram свежие)
- Если что-то правил в коде — `git add . && git commit -m "..." && git push`

### Раз в неделю
- Анализировать сигналы (запросы из README → "Анализ сигналов")
- Решать понизить ли пороги (см. README → "Рекомендованные настройки по этапам")
- Дополнять whitelist по итогам анализа

### Раз в месяц
- Бэкап `tracker.db` куда-нибудь вне ПК (Google Drive / Dropbox):
  ```cmd
  copy tracker.db C:\Users\reink\Dropbox\polymarket_backup_2026-MM-DD.db
  ```
  В .gitignore БД исключена (правильно — она большая и содержит локальную историю).

---

## Шпаргалка по типичным командам

```cmd
# Активировать venv
cd C:\polymarket_tracker
.venv\Scripts\activate

# Запустить трекер
python tracker_main.py

# Остановить трекер
Ctrl+C  (в окне где он запущен)

# Проверить БД
python -c "import sqlite3; c = sqlite3.connect('tracker.db'); print(c.execute('SELECT COUNT(*) FROM trades').fetchone())"

# Закоммитить изменения в GitHub
git add .
git commit -m "что поменял"
git push

# Скачать обновления из GitHub (если ставишь на ещё один ПК)
git pull

# Посмотреть последние сигналы
python -c "import sqlite3; c = sqlite3.connect('tracker.db'); [print(row) for row in c.execute('SELECT ts, signal_type, market_slug, usdc_amount, reason FROM signals ORDER BY ts DESC LIMIT 10')]"
```

---

## Если что-то сломалось

1. **Сначала прочитай ошибку** в консоли. 90% проблем — опечатка в .env, нет интернета, или забыл `activate` venv.
2. **Если непонятно** — открой новый чат с Claude и приложи:
   - Что делал (последняя команда)
   - Что увидел (полный вывод ошибки, скриншот окей)
   - Содержимое `.env` (БЕЗ токена!)
3. **Telegram молчит** — проверь токен через `https://api.telegram.org/bot<ТОКЕН>/getMe`
4. **Сделки не идут** — проверь Data API: `curl https://data-api.polymarket.com/trades?limit=3`
