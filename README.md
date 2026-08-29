# Polymarket Insider Activity Tracker v0.2 (Data API edition)

> Отдельный от торгового бота проект. Слушает сделки Polymarket через официальный Data API, детектит подозрительную активность, шлёт сигналы в Telegram.

**Куда смотреть:**

| Вопрос | Файл |
|---|---|
| На чём остановились, что работает, что делать первым | [docs/STATE.md](docs/STATE.md) |
| Что сделано и какой план дальше | [docs/ROADMAP.md](docs/ROADMAP.md) |
| Установка с нуля на новом ПК + git | [docs/SETUP.md](docs/SETUP.md) |
| Сколько трекер пишет на диск и что с этим делать | [docs/PERFORMANCE.md](docs/PERFORMANCE.md) |
| Как всё устроено и какие есть параметры | этот файл |

**Быстрый старт** (venv уже создан, `.env` заполнен):

```cmd
scripts\start_tracker.bat
```

## Что изменилось в v0.2

В v0.1 трекер читал блокчейн напрямую (eth_getLogs на CTF Exchange V1 контрактах). **28 апреля 2026 Polymarket мигрировал на CTFv2** — старые контракты прекратили активную работу, новые имеют другую структуру (PMCT collateral, переписанный Order struct).

v0.2 переписан на **Polymarket Data API** (`https://data-api.polymarket.com/trades`):
- Не зависим от blockchain-апгрейдов (Polymarket сам адаптирует API)
- Не нужен RPC, web3, eth-utils — установка на Windows стала тривиальной
- Сделки приходят уже декодированными, с `title`, `outcome`, `pseudonym` трейдера
- Меньше кода, проще диагностика

Старый `onchain.py` оставлен для истории, но больше не используется.

## Что детектим (две независимые ветки)

**Ветка S — "Score"** (взвешенная оценка, [`scoring.py`](polymarket_tracker/scoring.py)).
Жёсткие ворота: только покупки, рынок открыт, категория не в `IGNORED_CATEGORIES`,
размер ≥ `SCORING_MIN_TRADE_USDC`. Дальше признаки складываются в балл, сигнал
уходит при `score ≥ SCORE_THRESHOLD`:

| Признак | Баллы | Что означает |
|---|---|---|
| `market_relative` | 0–35 | во сколько раз позиция больше обычного часового оборота ЭТОГО рынка |
| `cluster` | 0–20 | сколько новых кошельков зашло в тот же исход за окно |
| `accumulation` | 0–15 | позицию набирали частями — порог на одну сделку такого не видит |
| `wallet_new` | 0–15 | кошелёк новый по критериям конфига |
| `dormant_wake` | 0–15 | кошелёк молчал месяцами и вдруг взял крупно |
| `cheap_tail` | 0–10 | вход в дешёвый хвост (≤0.25) |
| `illiquid_market` | 0–10 | абсолютная неликвидность рынка |
| `market_maker` | −40 | торгует обе стороны рынка — не инсайдер |
| `near_resolved` | −25 | рынок почти решён, торговой ценности нет |

Разбивка по баллам приходит прямо в сообщение и пишется в БД: по ней потом
видно, какой признак несёт alpha, а какой шумит.

**Ветка B — "Whitelist"** (copy-trading):
- `maker ∈ data/whitelist.txt`
- размер ≥ `WHITELIST_MIN_USDC`

Ветки независимы — одна сделка может вызвать оба сигнала.

**Прежняя Ветка A** (жёсткая цепочка И) сигналов больше не шлёт, но её вердикт
продолжает считаться и писаться в `shadow_trades.passed_filters` — чтобы на
одних и тех же сделках сравнить старую методику с новой.

### Холодный старт

Признаки `wallet_new` и `cluster` опираются на локальную историю. Пока её
меньше трёх дней, новыми выглядят почти все (замер: при истории 1.4 часа —
95% кошельков), поэтому оба признака **не начисляются вовсе**, а балл держится
на признаках потока. Они включатся сами, и тогда порог надо будет поднять:

```cmd
python tools\calibrate_score.py
```

---

## ⚠️ Важное ограничение первых 2-4 недель

Анализатор кошельков использует **только локальную БД**: кошелёк считается "новым", если мы видим его впервые или сделали < N записей про него.

На старте все кошельки выглядят новыми. Решения:

1. **На старте `MIN_TRADE_USDC=2000` и `CLUSTER_MIN_WALLETS=3`** — это срежет ~95% шума, пока БД наполнится.
2. **Используй в основном Ветку B (whitelist)** для реальных сигналов первые недели.
3. Через 2-4 недели понижай пороги — БД будет знать ~80% активных трейдеров.

В v0.3 планируется обогащение через `/positions` и `/activity` — даст точный возраст кошелька с первой встречи.

---

## Установка

### 1. Зависимости

```cmd
cd C:\путь\до\Polymarket_tracker
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Нужен **Python 3.11 или 3.12** (3.14 пока несовместим с некоторыми зависимостями).

В отличие от v0.1 — НЕ нужны Visual C++ Build Tools, web3, RPC. Просто `pip install` и работает.

### 2. Настройка Telegram бота

1. Открой Telegram, найди **@BotFather** (с галочкой ✓).
2. `/newbot` → имя → username (заканчивается на `bot`).
3. Скопируй токен из ответа BotFather.
4. **Напиши боту любое сообщение** (без этого он не сможет тебе ответить).
5. Открой `https://api.telegram.org/bot<ТВОЙ_ТОКЕН>/getUpdates` в браузере.
6. Найди в JSON `"chat":{"id": 123456789}` — это твой `chat_id`.

### 3. Настройка .env

```cmd
copy .env.example .env
notepad .env
```

Минимум, что нужно заполнить:

```env
TELEGRAM_BOT_TOKEN=<токен из BotFather>
TELEGRAM_CHAT_ID=<chat_id из getUpdates>
```

Остальные параметры по умолчанию уже консервативные (хороший старт).

### 4. Запуск

```cmd
python tracker_main.py
```

Или просто двойным кликом: `scripts\start_tracker.bat` (сам снимет бэкап БД,
активирует venv и проверит, что `.env` на месте).

В TG должно прийти сообщение "Трекер запущен (Data API)". Если не пришло — проверь токен и chat_id (ошибки будут в консоли).

---

## Архитектура

```
Polymarket_tracker/                 ← корень проекта (запускать всё отсюда)
├── tracker_main.py                 ← entry point (asyncio main loop)
├── requirements.txt
├── .env                            ← твой конфиг (в git НЕ попадает)
├── .env.example                    ← шаблон конфига
├── .gitignore
├── README.md                       ← этот файл: что это и как запустить
│
├── polymarket_tracker/             ← ПАКЕТ: вся боевая логика
│   ├── __init__.py
│   ├── config.py                   ← загрузка .env + константы Polymarket
│   ├── storage.py                  ← SQLite (wallets, trades, signals,
│   │                                  signal_outcomes, shadow_trades)
│   ├── data_api_listener.py        ← polling Data API (замена onchain.py)
│   ├── market_context.py           ← Gamma API wrapper с кэшем
│   ├── wallet_analyzer.py          ← классификация кошельков
│   ├── watchlist.py                ← hot-reload whitelist
│   ├── anomaly_detector.py         ← ветки детекции (score + whitelist)
│   ├── scoring.py                  ← признаки и веса, свёртка в балл
│   ├── outcome_tracker.py          ← фоновый резолв исходов сигналов
│   ├── telegram_notifier.py        ← отправка сигналов
│   ├── telegram_commands.py        ← бот отвечает на /today, /stats, ...
│   └── core.py                     ← оркестратор
│
├── tools/                          ← CLI-утилиты (запускать из корня)
│   ├── setup_telegram.py           ← проверка токена + автопоиск chat_id
│   ├── calibrate_score.py          ← подбор SCORE_THRESHOLD по данным
│   ├── stats.py                    ← аналитика сигналов и winrate
│   ├── shadow_report.py            ← отчёт по теневой выборке
│   ├── db_maintenance.py           ← бэкап / retention / VACUUM
│   └── analyze_whitelist.py        ← скоринг кандидатов в whitelist
│
├── scripts/                        ← .bat для Windows, пути относительные
│   ├── start_tracker.bat           ← бэкап БД + запуск трекера
│   ├── backup_db.bat               ← только бэкап
│   ├── git_push.bat                ← add + commit + push
│   └── git_init.bat                ← разовая инициализация репозитория
│
├── data/                           ← ВСЕ данные проекта
│   ├── whitelist.txt               ← боевой список, hot-reload
│   ├── whitelist_filtered.txt      ← результат analyze_whitelist.py
│   ├── whitelist_analysis.json     ← полный разбор кандидатов
│   ├── tracker.db                  ← SQLite (создаётся автоматически)
│   └── backups/                    ← ротация бэкапов БД
│
├── tests/                          ← pytest: скоринг, детектор, запросы БД
│   ├── conftest.py
│   ├── test_scoring.py
│   ├── test_detector.py
│   └── test_storage_features.py
│
├── docs/
│   ├── STATE.md                    ← ГДЕ ОСТАНОВИЛИСЬ (читать первым)
│   ├── ROADMAP.md                  ← что сделано и что дальше
│   └── SETUP.md                    ← установка с нуля на новом ПК
│
└── archive/                        ← сюда сваливаем всё отжившее
    ├── debug/                      ← одноразовые скрипты разведки API
    └── polymarket_tracker-main.zip ← исходный архив до реструктуризации
```

### Поток данных

```
Polymarket Data API
    ↓ GET /trades?limit=200
DataApiListener → Trade{maker, token_id, usdc_amount, price, side, title, outcome, pseudonym}
    ↓
Storage: save_trade + upsert_wallet_stats
    ↓
MarketContext.get_by_token_id (Gamma API enrichment) → MarketInfo{question, category, volume24h}
    ↓
WalletAnalyzer.assess → WalletAssessment{is_new, age_days, trade_count}
    ↓
AnomalyDetector.evaluate → [Signal, Signal, ...]
    ↓
TelegramNotifier.send_signal → HTML сообщение в Telegram
```

### Polymarket контракты (для справки)

С 28.04.2026 актуальная версия — V2:

| Контракт | Адрес |
|---|---|
| CTF Exchange V2 | `0xE111180000d2663C0091e4f400237545B87B996B` |
| NegRisk CTF Exchange V2 | `0xe2222d279d744050d28e00520010520000310F59` |
| pUSD (PolyMarket Collateral Token) | `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB` |

Адреса хранятся в `config.py` для будущего использования. Сам трекер их не дёргает — Data API абстрагирует контракты.

---

## Параметры .env — полное описание

```env
# ── Telegram (обязательно) ──
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...

# ── Data API ──
DATA_API_POLL_INTERVAL=3.0                # сек между запросами; меньше 1.0 запрещено
DATA_API_BATCH_LIMIT=200                  # сделок за один запрос (max 10000)

# ── Signal filters (Ветка A) ──
MIN_TRADE_USDC=2000.0                     # стартовый порог; через 2-4 недели можно снизить до 500
MAX_TRADE_PRICE=0.95                      # отсекаем "почти решённые" рынки: ROI < спреда
MAX_MARKET_VOLUME_24H=50000.0             # отсекаем ликвидные рынки
NEW_WALLET_MAX_TRADES=20
NEW_WALLET_MAX_AGE_DAYS=30
CLUSTER_MIN_WALLETS=3                     # на старте 3, потом 2
CLUSTER_WINDOW_SECONDS=3600

# ── Category filter ──
IGNORED_CATEGORIES=crypto,sports

# ── Whitelist (Ветка B) ──
WHITELIST_FILE=data/whitelist.txt
WHITELIST_MIN_USDC=200.0

# ── Shadow tracker (измерение false negatives Ветки A) ──
SHADOW_ENABLED=1
SHADOW_MAX_VOLUME_24H=500000.0            # должен быть >= MAX_MARKET_VOLUME_24H

# ── Storage ──
DB_PATH=data/tracker.db
LOG_LEVEL=INFO
```

### Рекомендованные настройки по этапам

**Этап 1: первая неделя (наполнение БД)**
```env
MIN_TRADE_USDC=2000
CLUSTER_MIN_WALLETS=3
MAX_MARKET_VOLUME_24H=50000
```

**Этап 2: 2-4 недели (калибровка)**
```env
MIN_TRADE_USDC=1000
CLUSTER_MIN_WALLETS=2
MAX_MARKET_VOLUME_24H=100000
```

**Этап 3: 1+ месяц (production)**
```env
MIN_TRADE_USDC=500
CLUSTER_MIN_WALLETS=2
MAX_MARKET_VOLUME_24H=150000
```

---

## Whitelist

Файл `data/whitelist.txt`, по одному адресу в строке:

```
0x1234567890abcdef1234567890abcdef12345678  # комментарий после #
```

Hot-reload: правки применяются сразу, не нужно перезапускать.

### Где брать whitelist

1. **Polymarket Leaderboard:** <https://polymarket.com/leaderboard>
2. **Twitter / X:** трейдеры, публикующие свои адреса
3. **Dune Analytics:** дашборды Polymarket Whales
4. **Собственный анализ:** через 1-2 месяца — адреса, чьи сигналы часто совпадают с публичными новостями

На старте достаточно 5-10 адресов. Качество важнее количества.

---

## Формат сигнала в Telegram

```
🚨 CLUSTER

Рынок: Will Maduro resign before Feb 1?
Outcome: Yes @ 0.075
Размер: $4,250 (56666.7 shares)

Категория: politics | Volume 24h: $23,400

Трейдер: 0x1a2b3c..4d5e (CryptoWhale42)
новый (3 сделок, 1.2д)

Причина: Кластер: 4 новых кошельков за час

📊 Polymarket | 🔗 0xabc123...
```

Иконки:
- 🔍 `suspicious_entry` — одиночный новый кошелёк + базовые фильтры
- 🚨 `cluster` — несколько новых кошельков на одном рынке (сильный сигнал)
- ⭐ `whitelist` — активность доверенного адреса

---

## Тесты

```cmd
python -m pytest tests\ -q
```

Покрыты свёртка баллов, ворота детектора (включая исключение для киберспорта),
холодный старт и запросы-признаки в БД. Сети и боевой БД не трогают.

---

## Отладка и траблшутинг

### Сигналов нет совсем

Проверь в консоли логи уровня INFO — должно быть `=== Tracker started ===` и периодически статистика. Если за 30 минут ноль — проверь БД:
```bash
sqlite3 data/tracker.db "SELECT COUNT(*) FROM trades;"
sqlite3 data/tracker.db "SELECT * FROM trades ORDER BY ts DESC LIMIT 5;"
```

Если в trades есть записи, но в signals — пусто: фильтры жёсткие. Сними пороги:
```env
MIN_TRADE_USDC=100
IGNORED_CATEGORIES=
```

Если trades=0 — Data API не отвечает или проблема с сетью. Проверь:
```bash
curl https://data-api.polymarket.com/trades?limit=3
```

### Telegram не отвечает

- `curl https://api.telegram.org/bot<TOKEN>/getMe` — должно вернуть инфу о боте.
- Убедись что ты написал боту хоть одно сообщение.

### SQLite locked

Редко при параллельных записях. Перезапусти трекер.

---

## Анализ сигналов

Готовые отчёты (запускать из корня проекта, трекер останавливать не нужно —
всё read-only):

```cmd
python tools\stats.py                 :: общая сводка: winrate, ROI, breakeven
python tools\stats.py --addresses     :: разбивка по whitelist-адресам
python tools\stats.py --by-day        :: динамика по дням
python tools\stats.py --by-size       :: по корзинам размера сделки
python tools\stats.py --open          :: сейчас открытые позиции
python tools\stats.py --recent 20     :: последние 20 сигналов с исходом
python tools\shadow_report.py         :: false negatives фильтров Ветки A
```

То же самое, но прямо в Telegram: `/today`, `/yesterday`, `/open`, `/stats`,
`/signal <id>` — бот отвечает сам (`telegram_commands.py`).

Сырой SQL, если нужно что-то своё:

```sql
-- Сколько сигналов каждого типа
SELECT signal_type, COUNT(*) FROM signals GROUP BY signal_type;

-- Топ рынков по сигналам
SELECT market_slug, COUNT(*) FROM signals GROUP BY market_slug ORDER BY 2 DESC LIMIT 20;

-- Топ кошельков
SELECT maker, COUNT(*), SUM(usdc_amount) FROM signals GROUP BY maker ORDER BY 2 DESC LIMIT 20;

-- Ветераны
SELECT address, trade_count, total_volume_usdc FROM wallets
WHERE trade_count > 50 ORDER BY total_volume_usdc DESC LIMIT 20;
```

---

## Roadmap

Полная версия — в [docs/ROADMAP.md](docs/ROADMAP.md), текущее состояние —
в [docs/STATE.md](docs/STATE.md). Кратко:

### Сделано ✅
- v0.2: Data API вместо ончейн-листенера, совместимость с CTFv2 (28.04.2026)
- Telegram notifications + бот с командами `/today`, `/stats`, `/open`, `/signal`
- SQLite storage + checkpoint + бэкапы и retention (`tools/db_maintenance.py`)
- Outcome tracker: резолв исходов сигналов, winrate/ROI (`tools/stats.py`)
- Shadow tracker: измерение false negatives фильтров (`tools/shadow_report.py`)

### Дальше
- Накопить ≥100 resolved сигналов и решить по цифрам, какие фильтры оставить
- Polymarket `/activity` API для точного возраста кошелька
- Feedback через реакции Telegram (👍/👎 → `user_feedback` в БД)
- v0.4: интеграция с торговым ботом, ограниченное автоисполнение Ветки B

---

## Лицензия

Private / internal use.
