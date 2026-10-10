# CryptoWatcher — runtime architecture

## Foundation и текущие boundaries

Один Python 3.11 процесс, один Compose service `crypto_watcher`, persistent SQLite schema1 в WAL mode. Владелец один; settings/domain state не создаются для входящих чужих chats. Trading credentials и LLM отсутствуют.

| Boundary | Модуль | Ответственность |
| --- | --- | --- |
| Infrastructure / domain configuration | `cryptowatcher/config.py` | Fail-closed owner identity, token/bot binding, validated immutable owner settings |
| Persistence | `cryptowatcher/storage.py`, `lease.py` | Owner revisions/CAS, delivery preference, mutes, runtime, preserved legacy import; один application writer |
| Market adapter / computation | `cryptowatcher/market.py` | Общий async HTTP client, bounded REST requests; closed-candle composite, gaps/invalid data diagnostics |
| Detectors / profiles | `detector_config.py`, `detectors.py` | Validated explicit presets/manual values; five pure versioned formulas без I/O, previous-only baselines |
| History / episode producer | `events.py` | Atomic events/inputs/revisions, episode lifecycle/re-arm, notification decisions и independent pending work |
| Telegram consumer | `telegram_delivery.py` | Bounded durable attempts, retry/expiry/config checks; network вне DB transaction, successful state после ack |
| Views | `cryptowatcher/render.py` | Plain-text status/settings/alert, market links и keyboards без state mutation |
| Telegram application | `cryptowatcher/bot.py` | Один guard до каждого entrypoint, revision snapshot вокруг await, owner control deck, send result |
| Entrypoint / operations | `cryptowatcher/__main__.py` | CLI run/config/diagnostics/health/state-check/migrate/backup; safe error output |
| Compatibility | `telegram_crypto_watcher.py` | Сохраняет прежний startup entrypoint; private legacy Python helpers не являются объявленным API |

Network I/O выполняется вне DB transactions. Snapshot до market fetch имеет revision; смена settings во время fetch отменяет применение результата. Alerts маркируют использованную revision. SQLite update использует compare-and-swap; отправка сообщения не удерживает DB/state lock.

G1 compatibility adapter сохраняет прежнюю composite formula; production BybitRadar использует shared two-market/4TF ingestion и G2B episode producer. Selected market/TF относятся к карточке. Legacy cooldown хранится для compatibility, но production delivery определяется episodes. Persisted event identity подавляет duplicate candle/settings/version; durable pending/retry и lifecycle переживают restart. Диагностика отличает detector/source failure и неполноту от отсутствия сигнала.

CMC и reset-baseline сценарии устарели по согласованным решениям. Старые callbacks отвечают объяснением; baselines, custom settings и другие chats остаются в migration archive. External consumers прежних private helpers не подтверждены; F24 сохраняет это ограничение.

## Следующие boundaries

G3 добавляет outcomes/cohorts/export/retention; G4 — независимый signed consumer/outbox. G2A/G2B используют versioned logical records schema1 без physical DDL. Новая stateful несовместимость требует отдельного delivery gate. Полный контракт и численные policy находятся в [spec](product_scope.md), состояние реализации — в [плане](open_issues.md).

## Runtime и эксплуатация

Image: pinned Python base digest, hash-locked dependencies, UID/GID10001, read-only root, no capabilities. `/data/sqlite` и его backups принадлежат приложению; исходный `/data/state_crypto_watcher.json` сохраняется. Health проверяет recent application heartbeat, а не внешний Bybit uptime. Market/data availability и Telegram delivery отображаются отдельно. Resource limits не назначены без измерений actual workload.

Operational target, trust boundaries, artifact identity, queue и recovery описаны в [delivery runbook](runbooks/delivery.md); ownership/migration — в [state contract](state_contract.md).

## G2A shared ingestion

`universe.py` владеет metadata eligibility/classification; `sources.py` — closed observations/provenance и bounded windows; `ingestion.py` — shared BybitRadar, subscriptions/clock/refresh/recovery. HTTP client общий, network вне DB transactions. Source records используют schema1 runtime namespaces с явной record version; физической DDL migration нет. Canonical REST recovery может исправить ordinary source observation; event snapshots остаются immutable. Detectors/notifications читают только persisted sources. Selected owner category/TF выбирают карточку, ingestion получает оба рынка и четыре native TF.

REST budget5req/s/concurrency4, один pending entry на stream; current recovery выше bootstrap и historical pages. Равный приоритет обслуживается с чередованием рынков. WS args шардируются по character budget, Spot subscribe batches≤10, ack обязателен. Отказ/timeout закрывает connection, снимает live eligibility и приводит к heartbeat/resubscribe/backfill с bounded backoff. Open candles в source store не попадают. Duplicate closed candle не расходует запись или live identity; конфликт блокирует affected window до canonical REST recovery. Out-of-order/history никогда не повышаются до нового live observation.

Текущий detector window восстанавливается первым. Длинный outage оставляет durable historical gap cursor; каждый worker берёт только одну страницу, уступая более важной recovery. Неполная historical page диагностируется, а missing outcome data не превращаются в zero return. Source retention/protection и outcome completeness реализует G3 worker с durable admission/holds.

Delivery readiness требует discovery обоих рынков, успешные subscription acks и хотя бы один fresh usable window каждого active market/TF. Полный initial bootstrap продолжает работу; `WARMING` не проходит existing receiver post-check. После bootstrap статус различает data errors и no-signal. Worker failure влияет на health; endpoint/clock/rate problems видны отдельно.

## G2B event → episode → consumer

Persisted closed observation запускает five detectors на одном settings snapshot. Ошибка family записывает reason/class без sensitive details; другие families продолжаются. Source inputs, immutable outcome reference и provenance сохраняются вместе с event/episode/decision/outbox в короткой transaction. Active episode prefix index ограничивает lifecycle работу текущим instrument; глобальный2s tick читает только active index. Recent history и pending consumers используют indexed prefix ranges; network не удерживает writer.

History всех families независима от Telegram selection/mute. Coalescing2s фиксирует episode snapshot и exact event revisions; text замораживается перед первой попыткой. Новые observations во время I/O остаются в history; после ack reconciliation создаёт meaningful follow-up. Settings revisions не смешиваются в pending payload/normalized-strength baseline. Исторический outcome update в будущем не может изменить уже frozen live context. Telegram batch≤20; retries переиндексируются по следующему attempt time и уступают более ранним ready deliveries.

Restart восстанавливает episodes, per-direction conditions и pending work; exchange clock устанавливается до expiry решения. Незавершённый IN_FLIGHT означает ambiguous ack, поэтому exactly-once не заявляется. Forced closure подавляет старое условие до re-arm; complete recovered non-signal bars могут подтвердить re-arm без historical live flood. Bootstrap/recovery сохраняют последнее проверенное наблюдение как historical; G2B не выполняет ретроспективный detector backtest всех raw candles.

## G3 analytics и independent digest

`outcomes.py` принимает jobs в event transaction, затем batch20 вычисляет полные future1m окна без смены reference; `analytics.py` frozen prior comparator обрабатывает по одному событию и отделяет causal readout от late analytics. Cohort distributions сортирует/агрегирует SQLite, exporter не собирает все raw returns в памяти. Telegram/CLI views имеют явные ограничения выдачи; streaming export через отдельный readonly connection не усечён и pinning snapshot проверен concurrent-writer fixture.

`source_context.py` архивирует public metadata/ticker availability; historical formula получает доступную тогда liquidity/tick context. Unavailable historical input означает limitation, не future substitution. `analytics_recovery.py` получает один referenced page через существующий HTTP client/budget; live gaps/bootstrap имеют приоритет. Он не создаёт memory windows/WS subscriptions inactive instruments и не переписывает conflicting used source. Outcome/retention holds переживают restart.

`digest.py` независимо от realtime alerts/mutes агрегирует общую event history обоих рынков, strongest period episodes и ongoing snapshot. Period identity, cursor/skip range, immutable text и attempts persist before send; network вне writer. Один pending latest period предотвращает historical burst после длительного outage. Disabled/reconfigured preference during I/O сохраняет фактический attempt с prior-preference/canceled result. Permanent Telegram target общий, enabled preferences раздельны.

Controller orchestration запускает bounded analytics job5s, episode/consumer delivery2s и heartbeat20s. Source errors, incomplete horizons, reference recovery и delivery target имеют отдельные diagnostics; analytics worker failure влияет на health. Отсутствие выгодной/большой live выборки не является failure расчёта. Physical schema остаётся1, versioned logical namespaces и recovery ограничения перечислены в [state contract](state_contract.md).
