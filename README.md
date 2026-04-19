# CryptoWatcher — Bybit Spot Volume Radar

Telegram-бот для ручной spot-торговли на Bybit: ищет краткосрочные объёмно-ценовые сигналы, отправляет алерты в Telegram и даёт быстрый переход в терминал Bybit.

## Documentation

Подробная рабочая документация лежит в папке `docs/`:

- `docs/product_scope.md` — текущий продуктовый scope и правила изменения scope
- `docs/state_contract.md` — контракт state, правила владения `.env` vs chat state и миграции
- `docs/open_issues.md` — текущие открытые вопросы и pre-deploy риски

`README.md` intentionally остаётся коротким: это обзор проекта, quickstart и карта документации.

### Source of truth order

При конфликте трактовок используйте следующий приоритет:

1. `docs/product_scope.md`
2. `docs/state_contract.md`
3. `README.md`
4. code comments / implementation details

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
- persistent bottom keyboard в Telegram как основной UX
- управление пользовательским списком прямо из Telegram
- per-chat persistent state
- быстрые действия по монете из алерта
- глоссарий терминов через `/terms`

## Что сейчас не является основным продуктовым путём

Следующее не считается main product path:

- CoinMarketCap как основной источник
- старый percent/baseline-driven monitor как основная модель алертов
- управление рабочим пользовательским списком через `.env`
- futures mode
- portfolio/account features
- order execution из Telegram

Если scope меняется, сначала нужно обновить `docs/product_scope.md`.

## Контракт сигнала

Алерт отправляется только если одновременно выполнены все условия:

1. `abs(price_change_5m) >= PRICE_MOVE_MIN`
2. `turnover_spike_ratio >= TURNOVER_SPIKE_MIN`
3. `turnover24h >= LIQUIDITY_FLOOR_24H`

Где:

- `price_change_5m` — изменение цены на закрытой 5m свече
- `turnover_spike_ratio = current_5m_turnover / sma_5m_turnover`
- `sma_5m_turnover` — средний оборот по предыдущим 5m свечам
- `turnover24h` — 24h оборот пары на Bybit

Важное правило:
основная логика сигнала работает только по **закрытым 5m свечам**, а не по формирующейся свече.

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
- не должен считаться основным механизмом через `.env`

## Telegram UX

Основной интерфейс — **постоянная нижняя клавиатура**.

Ожидаемые основные кнопки:

- `📊 Статус`
- `⚙️ Настройки`
- `Радар: Top 100`
- `Радар: Мой список`
- `Список`
- `Добавить монету`
- `Удалить монету`
- `Очистить список`
- `Термины`

Slash-команды остаются как вторичный интерфейс.

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
- `PRICE_MOVE_MIN`
- `TURNOVER_SPIKE_MIN`
- `LIQUIDITY_FLOOR_24H`
- `STATE_FILE`

Важно: `.env.example` поля `BYBIT_PAIRS` и `WATCHLIST` — это legacy/default seed-поля для bootstrap и обратной совместимости, а не основной путь управления рабочим списком пользователя. Основной путь управления списком — per-chat state (`alert_universe_mode` + `custom_pairs`) через Telegram UX.

### Persistent chat state
Используется для:
- `alert_universe_mode`
- `custom_pairs`
- mute-состояния
- runtime workflow конкретного чата
- per-chat timestamps и operational state

`baselines` в state — это operational/runtime storage и legacy-compatible данные. Наличие поля не означает возврат к старой baseline-driven продуктовой модели как main path.

Подробные правила — в `docs/state_contract.md`.

## Структура проекта

Ключевые файлы:

- `telegram_crypto_watcher.py` — основная логика Telegram-бота, state и radar engine
- `.env.example` — шаблон конфигурации
- `docker-compose.yml` — локальный/серверный запуск через Docker Compose
- `docs/product_scope.md` — продуктовый source of truth
- `docs/state_contract.md` — контракт state и миграции
- `docs/open_issues.md` — текущие риски и pending fixes

## Запуск

### Через Docker Compose

```bash
docker compose up -d --build
