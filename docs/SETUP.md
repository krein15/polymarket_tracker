# SETUP: установка с нуля на новом ПК

> Пошагово, от пустой машины до работающего трекера. Каждый шаг проверяй,
> прежде чем переходить к следующему. Все команды — Windows (cmd).
> Что вообще происходит в проекте — в [STATE.md](STATE.md).

---

## Часть 1. Git — сделать один раз, до всего остального

Чтобы потеря диска больше не стоила проекта.

### 1.1 Аккаунт GitHub

Зарегистрируйся на <https://github.com/signup>, подтверди email. Бесплатно.

### 1.2 Git для Windows

Скачай <https://git-scm.com/download/win>, ставь с дефолтными настройками.
Проверка (в **новом** окне cmd — PATH обновляется только в новых):

```cmd
git --version
```

### 1.3 Представиться (один раз)

```cmd
git config --global user.name "Твоё имя"
git config --global user.email "твой@email.com"
```

Email — тот же, что в аккаунте GitHub.

### 1.4 Создать ПУСТОЙ репозиторий

1. <https://github.com/new>
2. **Repository name:** `polymarket_tracker`
3. **Private** — обязательно, это твой код.
4. **НЕ ставь** галочки «Add README», «Add .gitignore», «Add license» — они уже
   есть в проекте, иначе получишь конфликт при первом пуше.
5. Create repository → скопируй URL вида
   `https://github.com/USER/polymarket_tracker.git`.

### 1.5 Залить проект

Проект уже лежит в приватном репозитории
<https://github.com/krein15/polymarket_tracker>, remote привязан — шаги 1.1–1.4
нужны только при развёртывании на чистой машине. Там же вместо них:

```cmd
git clone https://github.com/krein15/polymarket_tracker.git
```

Если делаешь новый репозиторий с нуля —
за это отвечает `scripts\git_init.bat <URL>`: он сделает `git init`,
`git add -A`, покажет список файлов и **остановится на паузе** — проверь
глазами, что в списке НЕТ `.env`, `data/tracker.db`, `.venv`. Если они там
есть — закрывай окно и разбирайся с `.gitignore`: это секреты и мусор.

GitHub спросит логин: вводи username и **Personal Access Token** вместо
пароля (обычные пароли не принимают). Токен:
<https://github.com/settings/tokens> → Generate new token (classic) →
scope `repo` → Generate. **Копируй сразу, показывается один раз**, сохрани
в менеджер паролей.

### 1.6 Дальнейшие коммиты

```cmd
scripts\git_push.bat
scripts\git_push.bat Поднял порог кластера
```

Без аргумента сообщение будет `Auto update: <дата> <время>`, с аргументом —
твой текст (кавычки не нужны). Скрипт сам делает add + commit + push,
проверяет, что `.env` не утёк, и внятно ругается, если git не установлен,
репозитория нет или нужен `git pull`.

Делай это **минимум раз в день**, когда трогаешь код.

---

## Часть 2. Рабочая среда

### Шаг 1. Python 3.12

Скачай <https://www.python.org/downloads/release/python-31210/> →
**Windows installer (64-bit)**.

- ❌ «Add python.exe to PATH» — НЕ ставь (конфликтует с другими питонами)
- ✅ «Use admin privileges when installing py.exe» — поставь
- Install Now

Проверка в новом cmd:

```cmd
py -3.12 --version
```

Нужен **3.11 или 3.12**. На 3.14 часть зависимостей ещё не собирается.

### Шаг 2. venv и зависимости

```cmd
cd C:\путь\до\Polymarket_tracker
py -3.12 -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

После активации в строке появится `(.venv)`. Никаких ошибок про lru-dict и
Visual C++ быть не должно — с переезда на Data API всё pure-Python.

Проверка:

```cmd
python -c "import aiohttp, dotenv, cachetools; print('OK')"
```

### Шаг 3. Telegram-бот

Если токен от старого бота сохранился — **используй его**: бот живёт на
серверах Telegram, диск на это не влияет. Забыл chat_id — напиши боту любое
сообщение и открой `https://api.telegram.org/bot<ТОКЕН>/getUpdates`, ищи
`"chat":{"id": 123456789}`.

Токен потерян: Telegram → @BotFather → `/mybots` → бот → API Token (там же
можно перевыпустить). Или `/newbot` — новый бот с нуля.

`chat_id` руками искать не нужно — см. шаг 4.

### Шаг 4. .env

```cmd
copy .env.example .env
notepad .env
```

Вписать нужно **только `TELEGRAM_BOT_TOKEN`**. Остальное уже выставлено
консервативно, а `chat_id` определится сам:

```cmd
python tools\setup_telegram.py
```

Скрипт проверит токен, найдёт chat_id по истории сообщений бота и запишет
его в `.env`. Токен при этом нигде не печатается. Если список чатов пуст —
напиши боту в Telegram любое сообщение и запусти скрипт снова: без этого
Telegram не разрешает боту писать тебе первым.

Проверить, что связь есть:

```cmd
python tools\setup_telegram.py --test
```

Полная проверка конфига:

```cmd
python -c "from polymarket_tracker.config import Config; c=Config.from_env('.env'); e=c.validate(); print('OK' if not e else e); print('token_len', len(c.telegram_bot_token))"
```

Ждём `OK` и `token_len 46`.

### Шаг 5. Проверить, что Data API жив

```cmd
curl "https://data-api.polymarket.com/trades?limit=3"
```

Должен прийти JSON с тремя сделками.

⚠️ Не проверяй через `urllib` без заголовков — Data API отдаёт **403** на
дефолтный `User-Agent` питона (`Python-urllib/3.x`), и выглядит это как
«API лежит». Сам трекер ходит через `aiohttp` и отвечает `200`, так что
на его работу это не влияет.

### Шаг 6. Восстановить БД (если есть)

Если сохранилась `tracker.db` со старой машины или бэкап — положи её в
`data/tracker.db`. Тогда накопленная история кошельков не пропадёт.

⚠️ БД от **v0.1** (ончейн-листенер) не подойдёт — у `trades` сменился
PRIMARY KEY. Такую проще удалить и копить заново.

Нет БД — ничего не делай, создастся пустая при первом запуске.

### Шаг 7. Первый запуск

```cmd
scripts\start_tracker.bat
```

В консоли: `=== Tracker started ===`. В Telegram через 5–10 секунд:
«ℹ️ Трекер запущен (Data API). Whitelist: 15 адресов…».

### Шаг 8. Проверить, что сделки идут

**Не закрывая первое окно**, открой второе cmd:

```cmd
cd C:\путь\до\Polymarket_tracker
python -c "import sqlite3; c=sqlite3.connect('data/tracker.db'); [print(t, c.execute('SELECT COUNT(*) FROM '+t).fetchone()[0]) for t in ('trades','wallets','signals','shadow_trades')]"
```

Через 5–10 минут: `trades > 0`, `wallets > 0`. Сигналов может быть 0 —
это нормально, фильтры жёсткие.

### Шаг 9. Автозапуск

**Планировщик задач** (для постоянной работы):
Win+R → `taskschd.msc` → Create Task → имя «Polymarket Tracker» →
Triggers: At startup →
Actions: Start a program → `C:\путь\до\Polymarket_tracker\.venv\Scripts\python.exe`,
аргументы `tracker_main.py`, Start in `C:\путь\до\Polymarket_tracker` →
Settings → ✅ «If the task fails, restart every 1 minute, up to 999 times».

Проще: оставить окно `start_tracker.bat` свёрнутым, но после перезагрузки
запускать руками.

### Шаг 10. Зафиксировать бейзлайн

Через час после старта запиши: время старта, значения `MIN_TRADE_USDC`,
`CLUSTER_MIN_WALLETS`, `MAX_MARKET_VOLUME_24H` из `.env`, счётчики из шага 8,
сколько сигналов пришло в Telegram. Это база для калибровки порогов.

---

## Часть 3. Регулярные действия

**Раз в день**
- Убедиться, что трекер жив (свежие сообщения в Telegram, команда `/today` боту).
- Правил код — `scripts\git_push.bat`.

**Раз в неделю**

```cmd
python tools\stats.py
python tools\shadow_report.py
```

Решения по порогам — только когда набралась выборка (см. [ROADMAP.md](ROADMAP.md)).

**Раз в месяц**

Бэкап БД наружу (Google Drive / Dropbox):

```cmd
scripts\backup_db.bat
copy data\backups\tracker_*.db C:\Users\USER\Dropbox\
```

При **остановленном** трекере — чистка и сжатие БД:

```cmd
python tools\db_maintenance.py all --yes
```

---

## Шпаргалка

```cmd
:: активировать venv
cd C:\путь\до\Polymarket_tracker
.venv\Scripts\activate

:: запустить трекер (Ctrl+C — остановить)
scripts\start_tracker.bat

:: аналитика
python tools\stats.py
python tools\stats.py --open
python tools\shadow_report.py

:: последние сигналы прямо из БД
python -c "import sqlite3; c=sqlite3.connect('data/tracker.db'); [print(r) for r in c.execute('SELECT ts, signal_type, market_slug, usdc_amount, reason FROM signals ORDER BY ts DESC LIMIT 10')]"

:: сохранить код
scripts\git_push.bat
```

---

## Если что-то сломалось

1. **Прочитай ошибку.** 90% случаев — опечатка в `.env`, нет интернета,
   забыл `activate`.
2. **Telegram молчит** — `https://api.telegram.org/bot<ТОКЕН>/getMe` должен
   вернуть данные бота. И убедись, что ты хоть раз написал боту сам.
3. **Сделки не идут** — проверь Data API командой из шага 5.
4. **«database is locked»** — редко при параллельной записи; для `prune` и
   `vacuum` трекер надо останавливать.
5. **Не разобрался** — открой чат с Claude и приложи: последнюю команду,
   полный текст ошибки, содержимое `.env` **без токена**.
