# CryptoWatcher

Telegram-бот для мониторинга криптовалютных цен с алертами.

## Что умеет

CryptoWatcher отслеживает цены и присылает алерты в Telegram, когда цена уходит от сохраненной базовой точки на заданный процент. Поддерживается:

- **Bybit**
- **CoinMarketCap (CMC)**

Для каждого источника доступны два режима:

- **Top-N** — автоматически отслеживать N самых ликвидных монет
- **List** — свой список монет / пар

Также бот умеет:

- хранить настройки **отдельно для каждого чата**
- менять настройки прямо в Telegram через **inline-кнопки** (`/settings`)
- показывать текущий статус (`/status`)
- временно отключать алерты по монете (`/mute`)
- сбрасывать baseline (`/resetbase`)
- работать в Docker как фоновый сервис
- сохранять состояние между перезапусками
- ограничивать доступ через whitelist (`ALLOWED_CHAT_IDS`)

---

## Как устроена логика алертов

### Основной триггер

`THRESHOLD_PERCENT` — порог изменения от сохраненной базовой цены.

Важно:
- это изменение **от базы**, а не за какой-то период
- это **не** изменение за `POLL_INTERVAL`
- это **не** изменение за `CHANGE_TF`

### Частота проверки

`POLL_INTERVAL` — как часто бот проверяет рынок для конкретного чата.

### Дополнительная TF-метрика

`CHANGE_TF` — дополнительная информационная метрика по свечам. Она показывается в `/status` и алертах, но **не является триггером**.

> ⚠️ При `SHOW_TF_CHANGE=1` и большом `TOP_LIMIT` количество API-запросов значительно возрастает.

---

## Команды

| Команда | Описание |
|---------|----------|
| `/start` | Активировать бот для этого чата |
| `/status` | Текущее состояние цен и baselines |
| `/settings` | Настройки через inline-кнопки |
| `/watchlist` | Показать текущий список / top-режим |
| `/setlist BTC,ETH,SOL` | Обновить список для list-режима |
| `/mute BTC 60` | Отключить алерты по монете на 60 минут |
| `/unmute BTC` | Снять mute с монеты |
| `/unmute all` | Снять все mute |
| `/resetbase BTC` | Сбросить baseline по монете |
| `/resetbase all` | Сбросить все baseline для текущего источника |
| `/help` | Справка |

---

## Структура проекта

```
crypto_watcher/
├── telegram_crypto_watcher.py   # основной код бота
├── requirements.txt             # зависимости Python
├── Dockerfile                   # сборка образа
├── docker-compose.yml           # запуск контейнера
├── .env                         # секреты и настройки (не в git)
├── .env.example                 # пример конфигурации
├── data/                        # runtime state (не в git)
│   └── state_crypto_watcher.json
├── .gitignore
└── .dockerignore
```

---

## Быстрый старт

### 1. Клонировать и настроить

```bash
git clone git@github.com:Just9120/crypto-watcher.git
cd crypto-watcher
cp .env.example .env
# Заполнить .env реальными значениями
```

### 2. Запустить

```bash
mkdir -p data
docker compose up -d --build
docker compose logs --tail=30
```

### 3. Проверить

В логах должно быть:
```
Booting CryptoWatcher (Sprint 1.1): ...
```

В Telegram: отправить `/start` боту.

---

## Deploy (обновление на сервере)

GitHub — source of truth. Сервер — deployment target.

```bash
cd /opt/crypto_watcher
git fetch origin
git reset --hard origin/main
docker compose up -d --build
docker compose logs --tail=50
```

---

## Конфигурация

Все настройки задаются через `.env`. Подробное описание каждой переменной — в `.env.example`.

### Ключевые переменные

| Переменная | Описание | Пример |
|------------|----------|--------|
| `TELEGRAM_BOT_TOKEN` | Токен бота из BotFather | `123456:ABC...` |
| `PRICER` | Источник котировок | `BYBIT` или `CMC` |
| `THRESHOLD_PERCENT` | Порог алерта (%) | `20` |
| `POLL_INTERVAL` | Частота проверки | `5m` |
| `BYBIT_TOP_LIMIT` | Сколько монет в top-режиме Bybit | `500` |
| `SHOW_TF_CHANGE` | Показывать TF-метрику | `0` или `1` |
| `STATE_FILE` | Путь к файлу состояния | `/data/state_crypto_watcher.json` |
| `ALLOWED_CHAT_IDS` | Whitelist чатов (опционально) | `123456789,987654321` |

---

## Архитектура

- **Runtime:** Python 3.11 + python-telegram-bot 20.8 в Docker
- **State:** JSON-файл в `/data/` (Docker volume `./data:/data`)
- **Polling:** `poll_engine_job` тикает каждые `ENGINE_INTERVAL_SEC` секунд, реальный fetch — по `POLL_INTERVAL` каждого чата
- **Concurrency:** HTTP-запросы выполняются в `ThreadPoolExecutor`, event loop не блокируется
- **Thread safety:** per-thread `requests.Session`, отдельные пулы для fetch и kline
- **State safety:** `asyncio.Lock` для всех мутаций state

---

## Changelog

### Sprint 1.1
- Thread-safe HTTP sessions (per-thread via `threading.local`)
- Два отдельных executor pool (fetch + kline) — устранён риск deadlock
- Whitelist применяется и в poll loop, не только в handlers
- `status_text()` больше не мутирует state
- Логирование длительности fetch-запросов
- Обновлены `.env.example` и `README.md`

### Sprint 1
- Async-safe fetch через `ThreadPoolExecutor`
- `asyncio.Lock` для state mutations
- `fetch_quotes_bybit`: один bulk-запрос вместо N
- `_attach_bybit_tf_change`: параллельные запросы
- `ALLOWED_CHAT_IDS` whitelist
- `save_state()` один раз за poll cycle
- `requests.Session` с retry
- Замена deprecated `datetime.utcnow()`
