# CryptoWatcher — Bybit Spot Volume Radar

Telegram-бот для ручной spot-торговли на Bybit: ищет краткосрочные объёмно-ценовые сигналы, отправляет алерты в Telegram и даёт быстрый переход в терминал Bybit.

## Документация

Рабочая документация лежит в папке `docs/`:

- `docs/product_scope.md` — текущий продуктовый scope и правила его изменения
- `docs/state_contract.md` — контракт state, правила владения `.env` vs chat state и правила миграции
- `docs/open_issues.md` — текущие follow-up задачи, операционные риски и темы на следующий спринт

### Иерархия источников истины

Для этого репозитория порядок такой:

1. `docs/product_scope.md`
2. `docs/state_contract.md`
3. `README.md`
4. комментарии в коде и детали реализации

Если между README и `docs/` появляется расхождение, приоритет у файлов из `docs/`.

## Что это за продукт

CryptoWatcher в текущем виде — это **Bybit Spot Volume Radar**, а не общий монитор цены и не трекер портфеля.

Главная задача:
дать трейдеру быстрый Telegram-инструмент, который показывает, куда зашёл объём на Bybit Spot, и помогает быстро перейти в торговый терминал.

## Текущий MVP scope

В текущий MVP входят:

- Bybit Spot как основной продуктовый контур
- композитный radar-сигнал
- режим алертов по умолчанию: **Top 100 Bybit**
- альтернативный режим: **Мой список**
- настраиваемый timeframe сигнала: `1m`, `3m`, `5m`, `15m`
- default timeframe сигнала: `5m`
- постоянная нижняя клавиатура в Telegram как основной UX
- управление пользовательским списком прямо из Telegram
- per-chat persistent state
- быстрые действия по монете из алерта
- глоссарий терминов через `/terms`

## Что не является основным продуктовым путём

Следующее не считается main product path:

- CoinMarketCap как основной источник
- старый percent/baseline-driven monitor как основная модель алертов
- управление активным пользовательским списком через `.env`
- futures mode
- portfolio/account features
- order execution из Telegram

Если scope меняется, сначала нужно обновить `docs/product_scope.md`.

## Контракт сигнала

Алерт отправляется только если одновременно выполнены все условия:

1. `abs(price_change_tf) >= PRICE_MOVE_MIN`
2. `turnover_spike_ratio >= TURNOVER_SPIKE_MIN`
3. `turnover24h >= LIQUIDITY_FLOOR_24H`

Где:

- `price_change_tf` — изменение цены на закрытой свече выбранного timeframe
- `current_tf_turnover` — оборот текущей закрытой свечи выбранного timeframe
- `sma_tf_turnover` — средний оборот по предыдущим свечам выбранного timeframe
- `turnover_spike_ratio = current_tf_turnover / sma_tf_turnover`
- `turnover24h` — 24h оборот пары на Bybit

Поддерживаемые timeframe сигнала:
- `1m`
- `3m`
- `5m`
- `15m`

Важные правила:
- основная логика сигнала работает только по **закрытым свечам** выбранного timeframe
- `Radar poll` не равен timeframe сигнала
- `Radar poll` отвечает за то, как часто бот перепроверяет рынок
- timeframe сигнала отвечает за то, по каким свечам считаются метрики и условия алерта

## Режимы радара

### Top 100 Bybit
Режим по умолчанию.

Бот сканирует Top 100 Bybit spot пар по `turnover24h`.

### Мой список
Альтернативный режим.

Бот сканирует только пользовательский список текущего чата.

Пользовательский список:
- хранится в persistent chat state
- управляется из Telegram
- не считается основным пользовательским механизмом через `.env`

## Telegram UX

Основной интерфейс — **постоянная нижняя клавиатура**.

Основные кнопки:

- `📊 Статус`
- `⚙️ Настройки`
- `Радар: Top 100`
- `Радар: Мой список`
- `Список`
- `Добавить монету`
- `Удалить монету`
- `Очистить список`
- `Термины`

Slash-команды остаются поддерживаемым, но вторичным интерфейсом.

## Основные команды

- `/start` — активировать бота в чате
- `/status` — показать текущий статус радара
- `/settings` — открыть настройки
- `/terms` — показать глоссарий терминов
- `/watchlist` — показать текущий режим и пользовательский список
- `/setlist BTC,ETH,SOL` — перезаписать пользовательский список
- `/addcoin` — добавить монеты в пользовательский список
- `/removecoin` — удалить монеты из пользовательского списка
- `/clearlist` — очистить пользовательский список
- `/radar_top` — включить режим `Top 100 Bybit`
- `/radar_custom` — включить режим `Мой список`
- `/mute BTC 60` — отключить алерты по монете на время
- `/unmute BTC`
- `/unmute all`
- `/help`

## Что лежит в `.env`, а что в chat state

### `.env`
Используется для:
- токенов и секретов
- технических интервалов и runtime-параметров
- порогов сигнала
- дефолтных значений
- deployment-конфига

Примеры:
- `TELEGRAM_BOT_TOKEN`
- `RADAR_POLL_SEC`
- `ENGINE_INTERVAL_SEC`
- `SIGNAL_TIMEFRAME`
- `PRICE_MOVE_MIN`
- `TURNOVER_SPIKE_MIN`
- `LIQUIDITY_FLOOR_24H`
- `SMA_PERIODS`
- `STATE_FILE`

Важно:
поля вроде `BYBIT_PAIRS` и `WATCHLIST` в `.env.example` сохраняются как legacy/default seed поля и для backward compatibility, но не являются основным пользовательским способом управления рабочим списком.

### Persistent chat state
Используется для:
- `alert_universe_mode`
- `custom_pairs`
- `signal_timeframe`
- mute-состояния
- runtime workflow конкретного чата
- per-chat timestamps и operational state

Подробные правила — в `docs/state_contract.md`.

## Структура проекта

Ключевые файлы:

- `telegram_crypto_watcher.py` — основная логика Telegram-бота, state и radar engine
- `.env.example` — шаблон конфигурации
- `docker-compose.yml` — запуск через Docker Compose
- `docs/product_scope.md` — продуктовый source of truth
- `docs/state_contract.md` — контракт state и миграции
- `docs/open_issues.md` — текущие follow-up риски и следующие темы

## Запуск

### Через Docker Compose

```bash
docker compose up -d --build
