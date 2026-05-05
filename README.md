# Polymarket Insider Activity Tracker v0.2 (Data API edition)

> Отдельный от торгового бота проект. Слушает сделки Polymarket через официальный Data API, детектит подозрительную активность, шлёт сигналы в Telegram.

## Что изменилось в v0.2

В v0.1 трекер читал блокчейн напрямую (eth_getLogs на CTF Exchange V1 контрактах). **28 апреля 2026 Polymarket мигрировал на CTFv2** — старые контракты прекратили активную работу, новые имеют другую структуру (PMCT collateral, переписанный Order struct).

v0.2 переписан на **Polymarket Data API** (`https://data-api.polymarket.com/trades`):
- Не зависим от blockchain-апгрейдов (Polymarket сам адаптирует API)
- Не нужен RPC, web3, eth-utils — установка на Windows стала тривиальной
- Сделки приходят уже декодированными, с `title`, `outcome`, `pseudonym` трейдера
- Меньше кода, проще диагностика

Старый `onchain.py` оставлен для истории, но больше не используется.

## Что детектим (две независимые ветки)

**Ветка A — "Suspicious Entry"** (составной сигнал, все фильтры должны совпасть):
- размер покупки ≥ `MIN_TRADE_USDC` (по умолчанию $2000 на старте)
- категория рынка НЕ `crypto` и НЕ `sports`
- `volume_24h < MAX_MARKET_VOLUME_24H` (малоликвидный рынок)
- **maker "новый"** (< N сделок И < M дней) **ИЛИ кластер** (≥ K новых кошельков на этом рынке за час)

**Ветка B — "Whitelist"** (copy-trading):
- `maker ∈ whitelist.txt`
- размер ≥ `WHITELIST_MIN_USDC`

Обе ветки работают параллельно — одна сделка может вызвать оба сигнала.

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

```bash
cd polymarket_tracker
python -m venv .venv
source .venv/bin/activate    # Linux/Mac
# .venv\Scripts\activate     # Windows
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

```bash
cp .env.example .env
```

Минимум, что нужно заполнить:

```env
TELEGRAM_BOT_TOKEN=<токен из BotFather>
TELEGRAM_CHAT_ID=<chat_id из getUpdates>
```

Остальные параметры по умолчанию уже консервативные (хороший старт).

### 4. Запуск

```bash
python tracker_main.py
```

В TG должно прийти сообщение "Трекер запущен (Data API)". Если не пришло — проверь токен и chat_id (ошибки будут в консоли).

---

## Архитектура

```
polymarket_tracker/
├── tracker_main.py           ← entry point (asyncio main loop)
├── requirements.txt
├── .env                      ← твой конфиг (не коммитить!)
├── .env.example
├── .gitignore
├── whitelist.txt             ← адреса доверенных трейдеров, hot-reload
├── tracker.db                ← SQLite с историей (создаётся автоматически)
├── README.md
├── TODO.md                   ← следующие шаги, миграция на новый ПК
│
└── polymarket_tracker/
    ├── __init__.py
    ├── config.py             ← загрузка .env + константы Polymarket
    ├── storage.py            ← SQLite (wallets, trades, signals)
    ├── data_api_listener.py  ← НОВОЕ: polling Data API, замена onchain.py
    ├── market_context.py     ← Gamma API wrapper с кэшем
    ├── wallet_analyzer.py    ← классификация кошельков
    ├── watchlist.py          ← hot-reload whitelist.txt
    ├── anomaly_detector.py   ← две ветки детекции
    ├── telegram_notifier.py  ← отправка сигналов
    └── core.py               ← оркестратор
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
DATA_API_POLL_INTERVAL=3.0                # сек между запросами; меньше 1.0 не имеет смысла
DATA_API_BATCH_LIMIT=200                  # сделок за один запрос (max 10000)

# ── Signal filters ──
MIN_TRADE_USDC=2000.0                     # стартовый порог; через 2-4 недели можно снизить до 500
MAX_MARKET_VOLUME_24H=50000.0             # отсекаем ликвидные рынки
NEW_WALLET_MAX_TRADES=20
NEW_WALLET_MAX_AGE_DAYS=30
CLUSTER_MIN_WALLETS=3                     # на старте 3, потом 2
CLUSTER_WINDOW_SECONDS=3600

# ── Category filter ──
IGNORED_CATEGORIES=crypto,sports

# ── Whitelist ──
WHITELIST_FILE=whitelist.txt
WHITELIST_MIN_USDC=200.0

# ── Storage ──
DB_PATH=tracker.db
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

Файл `whitelist.txt`, по одному адресу в строке:

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

## Отладка и траблшутинг

### Сигналов нет совсем

Проверь в консоли логи уровня INFO — должно быть `=== Tracker started ===` и периодически статистика. Если за 30 минут ноль — проверь БД:
```bash
sqlite3 tracker.db "SELECT COUNT(*) FROM trades;"
sqlite3 tracker.db "SELECT * FROM trades ORDER BY ts DESC LIMIT 5;"
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

### v0.2 (сейчас) ✅
- Polymarket Data API вместо ончейн-листенера
- Совместимость с CTFv2 (28.04.2026 миграция)
- Telegram notifications с pseudonym трейдера
- SQLite storage + checkpoint

### v0.3 — следующие шаги
- Polymarket `/activity` API для точного возраста кошелька (не ждать накопления локальной БД)
- Feedback через реакции Telegram (👍/👎 → user_feedback в БД)
- Dashboard winrate по типам сигналов
- Backtest на исторических данных

### v0.4 — интеграция с торговым ботом
- Cross-process сигнал в `polymarket_bot/` через Redis/SQLite
- Ограниченное автоисполнение Ветки B
- Reuse Kelly Criterion для размера позиции

---

## Лицензия

Private / internal use.
