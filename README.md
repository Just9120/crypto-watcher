# CryptoWatcher — Bybit Spot Volume Radar

Telegram-бот для ручной spot-торговли: показывает, где в рынок вошли деньги, и даёт one-click переход в терминал Bybit.

## Новый MVP (product pivot)

Основной path: **Bybit-only + watchlist/list**.

Сигнал формируется только если одновременно выполнены 3 условия:

1. `abs(price_change_5m) >= PRICE_MOVE_MIN`
2. `current_5m_turnover / sma_5m_turnover >= TURNOVER_SPIKE_MIN`
3. `turnover24h >= LIQUIDITY_FLOOR_24H`

`5m` сигнал считается по **закрытой** свече `v5/market/kline`.

## Alert формат

```
🚨 BTCUSDT
5m: +2.3%
Объём 5m: $1.2M (x4.8)
Оборот 24h: $38.4M
```

Кнопки:
- `⚡ Trade BTCUSDT` → deep-link `https://www.bybit.com/trade/spot/BTC/USDT`
- `🔕 1h`
- `🔕 24h`
- `Монета` (single-symbol summary)

## Команды

- `/start`
- `/status`
- `/settings`
- `/watchlist`
- `/setlist BTC,ETH,SOL`
- `/mute BTC 60`
- `/unmute BTC`
- `/unmute all`
- `/help`

## Конфиг (.env)

Ключевые переменные:

- `TURNOVER_SPIKE_MIN=4.0`
- `PRICE_MOVE_MIN=2.0`
- `LIQUIDITY_FLOOR_24H=5000000`
- `SMA_PERIODS=12`
- `RADAR_POLL_SEC=90` (частый polling, отдельно от 5m timeframe)

См. полный пример в `.env.example`.

## Совместимость state

Бот сохраняет backward compatibility со старым state-файлом:
- legacy ключи могут оставаться в JSON
- `watchlist` и `mutes` продолжают работать
- baseline/CMC поля не используются в новом main path

## Запуск (Docker)

```bash
docker compose up -d --build
```

## Структура

```text
crypto-watcher/
├── telegram_crypto_watcher.py
├── .env.example
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```
