# CryptoWatcher — runtime architecture

## G1 implementation

Один Python 3.11 процесс, один Compose service `crypto_watcher`, persistent SQLite schema1 в WAL mode. Владелец один; settings/domain state не создаются для входящих чужих chats. Trading credentials и LLM отсутствуют.

| Boundary | Модуль | Ответственность |
| --- | --- | --- |
| Infrastructure / domain configuration | `cryptowatcher/config.py` | Fail-closed owner identity, token/bot binding, validated immutable owner settings |
| Persistence | `cryptowatcher/storage.py`, `lease.py` | Owner revisions/CAS, delivery preference, mutes, runtime, preserved legacy import; один application writer |
| Market adapter / computation | `cryptowatcher/market.py` | Общий async HTTP client, bounded REST requests; closed-candle composite, gaps/invalid data diagnostics |
| Views | `cryptowatcher/render.py` | Plain-text status/settings/alert, market links и keyboards без state mutation |
| Telegram application | `cryptowatcher/bot.py` | Один guard до каждого entrypoint, revision snapshot вокруг await, owner control deck, send result |
| Entrypoint / operations | `cryptowatcher/__main__.py` | CLI run/config/diagnostics/health/state-check/migrate/backup; safe error output |
| Compatibility | `telegram_crypto_watcher.py` | Сохраняет прежний startup entrypoint; private legacy Python helpers не являются объявленным API |

Network I/O выполняется вне DB transactions. Snapshot до market fetch имеет revision; смена settings во время fetch отменяет применение результата. Alerts маркируют использованную revision. SQLite update использует compare-and-swap; отправка сообщения не удерживает DB/state lock.

G1 сохраняет Top100/custom и один selected TF, inclusive composite thresholds, successful-send cooldown. Duplicate успешной candle подавляется. Неуспешная Telegram отправка не меняет successful-notification state; durable pending/retry приходит в G2B. Локальная candle gap validation уже блокирует некорректный расчёт; WS recovery и TTL ещё не реализованы. Диагностика отличает ошибки и неполноту от отсутствия сигнала.

CMC и reset-baseline сценарии устарели по согласованным решениям. Старые callbacks отвечают объяснением; baselines, custom settings и другие chats остаются в migration archive. External consumers прежних private helpers не подтверждены; F24 сохраняет это ограничение.

## Следующие boundaries

G2A заменяет scan dataset на shared metadata/universe/WS/recovery с обоими markets и четырьмя TF. G2B добавляет versioned detectors/events/episodes и transactional delivery decisions. G3 добавляет outcomes/cohorts/export/retention; G4 — независимый signed outbox. Store schema развивается versioned migrations; новая stateful несовместимость требует отдельного delivery gate. Полный контракт и численные policy находятся в [spec](product_scope.md), состояние реализации — в [плане](open_issues.md).

## Runtime и эксплуатация

Image: pinned Python base digest, hash-locked dependencies, UID/GID10001, read-only root, no capabilities. `/data/sqlite` и его backups принадлежат приложению; исходный `/data/state_crypto_watcher.json` сохраняется. Health проверяет recent application heartbeat, а не внешний Bybit uptime. Market/data availability и Telegram delivery отображаются отдельно. Resource limits не назначены без измерений actual workload.

Operational target, trust boundaries, artifact identity, queue и recovery описаны в [delivery runbook](runbooks/delivery.md); ownership/migration — в [state contract](state_contract.md).
