# CryptoWatcher — Post-fix follow-up и операционные риски

Этот список отражает задачи после фиксов миграции/state truthfulness/no-op UX.
Это **не блокеры текущего деплоя**, а realistic follow-up backlog.

## 1) Операционная устойчивость top-mode

- Оценить фактическую нагрузку на Bybit API при росте числа чатов в режиме `Top 100 Bybit`.
- При необходимости ввести более явный rate-limit budgeting/telemetry для `fetch_radar_snapshot`.

Риск: при масштабировании возможны деградация latency и частичные пропуски snapshot.

## 2) Лёгкие regression checks в CI

- Добавить минимальные автопроверки для:
  - миграции legacy state (`bybit_pairs`, `watchlist`, empty `custom_pairs`);
  - truthfulness `/status` в top-mode;
  - no-op interaction (`Message is not modified`) без callback-failure.

Риск: ручная проверка может пропустить повторную регрессию в следующих PR.

## 3) Технический cleanup legacy/dead paths

- Пошагово изолировать/удалить неиспользуемые ветки старого поведения, не меняя main product path.
- Сохранять обратную совместимость state, но сокращать поверхность случайных side-effects.

Риск: чем больше legacy-кода остаётся рядом с основным путём, тем выше вероятность случайного отката scope.

## 4) Модульность кода без изменения архитектуры деплоя

- Разделить крупный `telegram_crypto_watcher.py` на небольшие модули (state, render, handlers, radar-engine).
- Делать это инкрементально, без смены runtime/deploy модели.

Риск: без декомпозиции скорость безопасных изменений со временем снижается.
