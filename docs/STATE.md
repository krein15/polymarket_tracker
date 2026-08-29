# Состояние проекта на 29.08.2026

> Читать первым при возвращении в проект. Здесь — что работает, где остановились,
> что делать в первый час после возврата. План на будущее — в [ROADMAP.md](ROADMAP.md),
> установка с нуля — в [SETUP.md](SETUP.md).

---

## Одной строкой

Трекер **дописан и работоспособен** (v0.2 + весь блок аналитики исходов).
Он не «недоделан» — он **не докручен по данным**: не хватает накопленной
статистики, чтобы решить, какие фильтры оставить. Последняя активность —
**05.05.2026** (дата в `data/whitelist_filtered.txt`), пауза ~3.5 месяца.

## Что было последним ходом

1. Собран и отфильтрован whitelist: 56 кандидатов прогнали через
   `/activity` (REDEEM vs BUY), оставили 16 адресов с winrate ≥ 80%
   и ≥ 5 закрытыми рынками → `data/whitelist_filtered.txt`.
   В боевой `data/whitelist.txt` перенесли 15 из них с пометками PASS/WATCH.
2. Дописан слой измерения качества сигналов: `outcome_tracker.py`,
   таблицы `signal_outcomes` и `shadow_trades`, отчёты `tools/stats.py`
   и `tools/shadow_report.py`, команды Telegram-бота.
3. Дописано обслуживание БД: `tools/db_maintenance.py` (онлайн-бэкап,
   retention, VACUUM) + `scripts/backup_db.bat`.

Дальше работа встала — не на баге, а на паузе.

## Что работает (проверено по коду)

| Блок | Файл | Статус |
|---|---|---|
| Поток сделок | `data_api_listener.py` | ✅ polling Data API, дедуп по (tx, wallet, asset), чекпоинт |
| Метаданные рынков | `market_context.py` | ✅ Gamma API + кэш, фолбэк на поля из Trade |
| Классификация кошельков | `wallet_analyzer.py` | ✅ но только по локальной БД (см. «Ограничения») |
| Детекция | `anomaly_detector.py` | ✅ Ветка A (suspicious_entry / cluster) + Ветка B (whitelist) |
| Хранилище | `storage.py` | ✅ wallets, trades, signals, signal_outcomes, shadow_trades |
| Резолв исходов | `outcome_tracker.py` | ✅ price_1h/24h/7d, min/max, settled, ROI, два прохода (боевой + shadow) |
| Уведомления | `telegram_notifier.py` | ✅ HTML-сигналы |
| Бот-команды | `telegram_commands.py` | ✅ `/today /yesterday /open /stats /signal <id>` |
| Аналитика CLI | `tools/stats.py`, `tools/shadow_report.py` | ✅ winrate vs breakeven, Wilson CI, разбивка по зонам цены |
| Обслуживание БД | `tools/db_maintenance.py` | ✅ backup / prune / vacuum |
| Скоринг whitelist | `tools/analyze_whitelist.py` | ✅ отдельный офлайн-прогон |

## Чего нет

- **`data/tracker.db` отсутствует** — в архиве её не было (она в `.gitignore`).
  Если БД сохранилась на старой машине или в бэкапах — положи её в `data/`.
  Если нет — трекер создаст пустую и статистика копится заново с нуля.
- **`.env` отсутствует** — восстанавливается из `.env.example` (нужны токен
  бота и chat_id; бот на серверах Telegram жив, токен можно перевыпустить
  через @BotFather → `/mybots`).
- **venv отсутствует** — создаётся заново, зависимости pure-Python.
- **Тестов нет вообще.** Проверка кода — только запуском.
- **Remote на GitHub не привязан.** Локальный репозиторий инициализирован
  29.08.2026 (ветка `main`, история начинается с реструктуризации), коммиты
  делаются через `scripts\git_push.bat`. Но пока всё лежит только на этом
  диске — создай приватный репозиторий на <https://github.com/new> (ПУСТОЙ,
  без README) и один раз выполни:
  ```cmd
  git remote add origin https://github.com/USER/polymarket_tracker.git
  git push -u origin main
  ```
  Дальше `git_push.bat` пушит сам.

## Ограничения, о которых легко забыть

1. **«Новизна» кошелька считается только по локальной БД.** С пустой БД ВСЕ
   кошельки выглядят новыми → Ветка A первые 2–4 недели шумит. Поэтому
   стартовые пороги задраны (`MIN_TRADE_USDC=2000`, `CLUSTER_MIN_WALLETS=3`),
   а реальные сигналы первое время даёт Ветка B (whitelist).
2. **Whitelist собран 05.05.2026** — за 3.5 месяца winrate адресов мог
   поехать. Перед тем как доверять Ветке B, прогони скоринг заново.
3. **Winrate 100% у 11 из 16 адресов — красный флаг методики.** Метрика
   «REDEEM по conditionId = выигрыш, BUY без REDEEM = проигрыш» не видит
   позиций, проданных до резолва, и завышает winrate. Считай эти цифры
   ранжированием, а не вероятностью.
4. **Схема БД от v0.1 несовместима** — старые `tracker.db` с ончейн-листенера
   не подходят (сменился PRIMARY KEY у `trades`).

## Первый час после возврата

```cmd
cd C:\путь\до\Polymarket_tracker
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
notepad .env                      :: вписать TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID
scripts\start_tracker.bat
```

Через 10 минут проверить, что сделки идут:

```cmd
python -c "import sqlite3; c=sqlite3.connect('data/tracker.db'); print('trades', c.execute('SELECT COUNT(*) FROM trades').fetchone()[0])"
```

Подробный разбор по шагам — в [SETUP.md](SETUP.md).

## Что решать дальше

Единственный содержательный вопрос проекта сейчас:
**фильтры Ветки A отбирают alpha или режут выборку наугад?**
Ответ даёт `tools/shadow_report.py`, но только после ≥100 закрытых сделок
в `shadow_trades`. До этого любые правки порогов — угадывание.
Что делать по шагам — в [ROADMAP.md](ROADMAP.md).
