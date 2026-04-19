# CryptoWatcher — Bybit Spot Volume Radar

Telegram-бот для ручной spot-торговли: ловит ускорение объёма/движения цены и отправляет алерты по рынку Bybit.

## Продуктовая модель (текущий scope)

- **Режим алертов по умолчанию:** `Top 100 Bybit`.
- **Альтернативный режим:** `Мой список` (кастомный список пользователя).
- Кастомный список хранится **в persistent state по каждому чату**, а не управляется в `.env` как основной механизм.
- `/status` по умолчанию сфокусирован на рабочем пользовательском списке трейдера.

## Композитный сигнал

Алерт отправляется только если одновременно выполнены все условия:

1. `abs(price_change_5m) >= PRICE_MOVE_MIN`
2. `turnover_spike_ratio >= TURNOVER_SPIKE_MIN`
3. `turnover24h >= LIQUIDITY_FLOOR_24H`

Где `turnover_spike_ratio = current_5m_turnover / sma_5m_turnover`, а расчёт идёт по закрытой 5m свече.

## Telegram UX

- Основной UX — **persistent bottom keyboard**.
- Slash-команды остаются как вторичный интерфейс.
- Есть глоссарий через `/terms` (и кнопку `Термины`).
- Пользователь может переключать режим радара:
  - `Радар: Top 100`
  - `Радар: Мой список`
- Пользователь может управлять кастомным списком из Telegram:
  - `Список`
  - `Добавить монету`
  - `Удалить монету`
  - `Очистить список`

## Команды

- `/start` — активировать бота в чате
- `/status` — текущий статус радара
- `/settings` — настройки радара
- `/terms` — краткий глоссарий сигналов
- `/watchlist` — показать режим и пользовательский список
- `/setlist BTC,ETH,SOL` — перезаписать пользовательский список
- `/addcoin` — добавить монеты в пользовательский список
- `/removecoin` — удалить монеты из пользовательского списка
- `/clearlist` — очистить пользовательский список
- `/radar_top` — включить `Top 100 Bybit`
- `/radar_custom` — включить `Мой список`
- `/mute BTC 60`, `/unmute BTC`, `/unmute all`
- `/help`

## Что в `.env`, а что в chat state

### `.env` (дефолты и техпараметры)

- токены/ключи (`TELEGRAM_BOT_TOKEN`, и т.д.)
- дефолтные интервалы/тайминги (`RADAR_POLL_SEC`, `ENGINE_INTERVAL_SEC`, ...)
- пороги сигнала (`PRICE_MOVE_MIN`, `TURNOVER_SPIKE_MIN`, `LIQUIDITY_FLOOR_24H`)
- прочие технические параметры (`STATE_FILE`, whitelist, retry/TTL настройки)

### Chat state (активное поведение пользователя)

- `alert_universe_mode` (`top` / `custom`)
- `custom_pairs` (активный кастомный список)
- пользовательский workflow в чате (например, шаг add/remove)
- mute/baseline/runtime для конкретного чата

## Запуск

```bash
docker compose up -d --build
```
