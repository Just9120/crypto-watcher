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
- продуктовым поведением;
- техническим state-контрактом;
- README;
- деталями реализации,

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
- порогов сигнала
- дефолтной частоты polling
- deployment-specific параметров
- legacy/default seed полей

### persistent chat state
Используется для:
- реального поведения конкретного чата
- активного режима радара
- пользовательского списка монет
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

Правила миграции:

1. Если raw persisted `alert_universe_mode` существует, он считается authoritative.
2. Если raw persisted `alert_universe_mode` отсутствует, режим должен выводиться из legacy-поведения.
3. Если raw persisted `custom_pairs` существует, он считается authoritative, включая случай явного пустого списка.
4. Если `custom_pairs` отсутствует, он должен выводиться из legacy raw fields в таком порядке:
   - `bybit_pairs`
   - `watchlist`
   - иначе пустой список
5. Символы должны нормализоваться через `_normalize_bybit_pair`.
6. Дедупликация должна сохранять порядок элементов.

Критичное правило:
решения по миграции должны приниматься на основе raw persisted settings, а не уже default-merged settings.

## 7. Правило правдивости состояния

Все пользовательские поверхности должны согласованно отражать один и тот же активный state:

- polling behavior
- `/status`
- settings text
- watchlist/list view
- help text там, где это релевантно

Пример:
если алерты реально сканируют Top 100, `/status` не должен вести себя так, как будто существует только custom list.

## 8. Правило backward compatibility

PR, который меняет структуру state, считается неполным, если он не покрывает миграционное поведение явно.

Минимум нужно проверять:
- legacy payload со старым `bybit_pairs`
- legacy payload со старым `watchlist`
- случай с явным пустым `custom_pairs`
- случай с явным `alert_universe_mode`

## 9. Политика изменений

Любой PR, который добавляет или меняет persistent state fields, должен обновлять этот документ.
