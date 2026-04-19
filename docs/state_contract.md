# CryptoWatcher — Контракт state и миграции

## 1. Назначение

Этот документ определяет:
- что должно жить в persistent chat state
- что должно жить в `.env`
- как должна работать миграция state

Документ нужен для того, чтобы снижать риск случайных регрессий при развитии продукта.

## 2. Связь с другими документами

Этот документ дополняет `docs/product_scope.md`.

Если возникает конфликт между:
- продуктовым поведением
- техническим state-контрактом
- README
- деталями реализации

то порядок приоритета такой:

1. `docs/product_scope.md`
2. `docs/state_contract.md`
3. `README.md`
4. детали реализации и комментарии в коде

## 3. Модель владения

### `.env`
Используется для:
- секретов
- дефолтных значений
- инфраструктурной и runtime-конфигурации
- дефолтной частоты polling
- default timeframe сигнала
- deployment-specific параметров
- legacy/default seed полей

### persistent chat state
Используется для:
- реального поведения конкретного чата
- активного режима радара
- пользовательского списка монет
- активного timeframe сигнала
- mute-данных
- временного chat workflow состояния
- per-chat runtime timestamps

## 4. Важные поля state

### Поля продуктового поведения
- `settings.alert_universe_mode`
  - допустимые значения: `top`, `custom`
  - определяет, какой alert universe активен

- `settings.custom_pairs`
  - нормализованные Bybit symbols
  - активный пользовательский список текущего чата

- `settings.signal_timeframe`
  - допустимые значения: `1m`, `3m`, `5m`, `15m`
  - определяет, по каким закрытым свечам считаются price move, turnover и spike ratio

- `settings.price_move_min`
  - порог `Движение цены` для конкретного чата
  - дефолт берётся из `.env` (`PRICE_MOVE_MIN`)

- `settings.turnover_spike_min`
  - порог `Спайк объёма` для конкретного чата
  - дефолт берётся из `.env` (`TURNOVER_SPIKE_MIN`)

- `settings.liquidity_floor_24h`
  - порог `Ликвидность 24ч` для конкретного чата
  - дефолт берётся из `.env` (`LIQUIDITY_FLOOR_24H`)

### Runtime
- `runtime.last_poll_ts`
- `runtime.pending_action`

### Операционные данные
- `mutes`
- `baselines`

Важно:
`baselines` в текущем коде — это operational/runtime storage и legacy-compatible state.
Это не означает возврат к старой baseline-driven продуктовой модели как main path.

## 5. Legacy/default seed поля в `.env`

Поля вроде:
- `BYBIT_PAIRS`
- `WATCHLIST`

могут оставаться в `.env.example` и в коде как:
- legacy-compatible defaults
- seed-значения для старых сценариев и миграции
- технический мост для backward compatibility

Но они не являются основным пользовательским способом управления рабочим списком.
Основной пользовательский механизм — это per-chat `custom_pairs` в persistent state.

## 6. Правила миграции legacy state

Старые persisted chats могут не содержать:
- `alert_universe_mode`
- `custom_pairs`
- `signal_timeframe`
- `price_move_min`
- `turnover_spike_min`
- `liquidity_floor_24h`

Правила миграции:

1. Если raw persisted `alert_universe_mode` существует, он считается authoritative.
2. Если raw persisted `alert_universe_mode` отсутствует, режим должен выводиться из legacy-поведения.
3. Если raw persisted `custom_pairs` существует, он считается authoritative, включая случай явного пустого списка.
4. Если `custom_pairs` отсутствует, он должен выводиться из legacy raw fields в таком порядке:
   - `bybit_pairs`
   - `watchlist`
   - иначе пустой список
5. Если `signal_timeframe` отсутствует, должен использоваться безопасный дефолт `5m`.
6. Символы должны нормализоваться через `_normalize_bybit_pair`.
7. Дедупликация должна сохранять порядок элементов.
8. Если нет threshold-полей (`price_move_min`, `turnover_spike_min`, `liquidity_floor_24h`), они должны безопасно заполняться дефолтами из `.env`.

Критичное правило:
решения по миграции должны приниматься на основе raw persisted settings, а не уже default-merged settings.

## 7. Правило правдивости состояния

Все пользовательские поверхности должны согласованно отражать один и тот же активный state:

- polling behavior
- `/status`
- settings text
- watchlist/list view
- terms/help text там, где это релевантно

Пример:
если алерты реально сканируют Top 100 и считают сигнал по `3m`, UI не должен вести себя так, как будто режим `custom` или timeframe `5m`.

## 8. Правило timeframe

`signal_timeframe` и `radar_poll_sec` — разные настройки.

- `signal_timeframe` определяет, по каким закрытым свечам считаются метрики сигнала
- `radar_poll_sec` определяет, как часто бот перепроверяет рынок

`SMA periods` трактуется как количество предыдущих свечей выбранного timeframe.

Примеры:
- `signal_timeframe = 5m`, `SMA periods = 12` → средний оборот считается примерно за предыдущий час
- `signal_timeframe = 15m`, `SMA periods = 12` → средний оборот считается примерно за предыдущие три часа

## 9. Правило backward compatibility

PR, который меняет структуру state, считается неполным, если он не покрывает миграционное поведение явно.

Минимум нужно проверять:
- legacy payload со старым `bybit_pairs`
- legacy payload со старым `watchlist`
- отсутствие `signal_timeframe`
- случай с явным пустым `custom_pairs`
- случай с явным `alert_universe_mode`

## 10. Политика изменений

Любой PR, который добавляет или меняет persistent state fields, должен обновлять этот документ.
