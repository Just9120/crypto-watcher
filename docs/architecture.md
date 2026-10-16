# CryptoWatcher — runtime architecture

## Foundation и текущие boundaries

Один Python 3.11 процесс, один Compose service `crypto_watcher`, persistent SQLite physical schema2 в WAL mode; legacy schema1 поддерживается compatibility reader. Владелец один; settings/domain state не создаются для входящих чужих chats. Trading credentials и LLM отсутствуют.

Advisory consumer boundary: `agent_contract.py` проецирует immutable history в typed v2;
`agent_delivery.py` владеет отдельными preference/intents/frozen bytes/attempts/ack baseline.
Event/episode/outcome admission выполняется атомарно с source history; отдельный job
использует shared HTTPx client вне transactions. Telegram/digest/agent не делят successful
notification state. Optional config/error не останавливает deterministic radar.
Binding/activation/recovery — в [agent procedure](runbooks/agent-delivery.md).

| Boundary | Модуль | Ответственность |
| --- | --- | --- |
| Infrastructure / domain configuration | `cryptowatcher/config.py` | Fail-closed owner identity, token/bot binding, validated immutable owner settings |
| Persistence | `cryptowatcher/storage.py`, `lease.py` | Owner revisions/CAS, delivery preference, mutes, runtime, preserved legacy import; один application writer |
| Universe / realtime sources | `universe.py`, `ingestion.py`, `sources.py` | Metadata eligibility, market identity, WS ACK/recovery и shared bounded windows |
| Compact market storage | `market_storage.py`, `history_codec.py`, `record_codec.py`, `compact_json.py` | Normalized candle/context revisions, content-addressed shared data и strict public readback |
| Analytics / recovery | `outcomes.py`, `analytics.py`, `analytics_worker.py`, `analytics_recovery.py` | Frozen horizons/comparator versions, readonly calculations, CAS apply и referenced recovery |
| Retention / summaries | `storage_lifecycle.py`, `quality_summary.py`, `expiry_index.py`, `quality_layout.py` | Bounded expiry, all-method numeric quality перед удалением details, finite service dependencies |
| Storage control | `storage_meter.py`, `storage_model.py`, `storage_repack.py` | Cheap counters/samples, separated occupancy/retained-flow forecast, isolated quality scenarios и durable bounded lossless repack |
| Capacity evidence | `capacity.py`, `scripts/observe_capacity.py`, `scripts/rehearse_retention.py` | Readonly boundary ledger/cardinality/pages, finite host observation после stability и offline full lifecycle; tests/CD отдельно от20GB proof |
| Agent consumer | `agent_contract.py`, `agent_delivery.py` | Полный public snapshot/hash, independent immutable outbox/retries/ack и owner diagnostics |
| Digest / optional context | `digest.py`, `enrichment.py`, `enrichment_runtime.py` | Independent period delivery и qualified derivatives mapping/source freshness |
| Market adapter / computation | `cryptowatcher/market.py` | Общий async HTTP client, bounded REST requests; closed-candle composite, gaps/invalid data diagnostics |
| Detectors / profiles | `detector_config.py`, `detectors.py` | Validated explicit presets/manual values; five pure versioned formulas без I/O, previous-only baselines |
| History / episode producer | `events.py` | Atomic events/inputs/revisions, episode lifecycle/re-arm, notification decisions и independent pending work |
| Telegram consumer | `telegram_delivery.py` | Bounded durable attempts, retry/expiry/config checks; network вне DB transaction, successful state после ack |
| Views | `cryptowatcher/render.py` | Plain-text status/settings/alert, market links и keyboards без state mutation |
| Telegram application | `cryptowatcher/bot.py` | Один guard до каждого entrypoint, revision snapshot вокруг await, owner control deck, send result |
| Entrypoint / operations | `cryptowatcher/__main__.py` | CLI run/config/diagnostics/health/state-check/migrate/backup; safe error output |
| Compatibility | `telegram_crypto_watcher.py` | Сохраняет прежний startup entrypoint; private legacy Python helpers не являются объявленным API |

Network I/O выполняется вне DB transactions. Snapshot до market fetch имеет revision; смена settings во время fetch отменяет применение результата. Alerts маркируют использованную revision. SQLite update использует compare-and-swap; отправка сообщения не удерживает DB/state lock.

Analytics calculations выполняются через `analytics_worker.py` в отдельном thread с собственным read-only WAL snapshot connection и `query_only`. Application event loop остаётся единственным writer: применяет результат только при совпадении сохранённой job с прочитанной, атомарно сохраняет revision/protected sources/intents и фиксирует фактический completion time как availability. Отмена reader не меняет pending work. Outcome/comparator batches ограничены256/64 jobs и2s между завершёнными calculations; отдельный долгий calculation не обрывается с fabricated metrics. Между writer-side results происходит yield. Telegram analytics/diagnostics также читаются в отдельном snapshot; дорогие diagnostic counts имеют общий3s budget и показывают PENDING вместо выдуманного0. WS/recovery snapshot публикуется отдельным5s task независимо от REST discovery/backoff; возраст snapshot виден явно.

G1 compatibility adapter сохраняет прежнюю composite formula; production BybitRadar использует shared two-market/4TF ingestion и G2B episode producer. Selected market/TF относятся к карточке. Legacy cooldown хранится для compatibility, но production delivery определяется episodes. Persisted event identity подавляет duplicate candle/settings/version; durable pending/retry и lifecycle переживают restart. Диагностика отличает detector/source failure и неполноту от отсутствия сигнала.

CMC и reset-baseline сценарии устарели по согласованным решениям. Старые callbacks отвечают объяснением; baselines, custom settings и другие chats остаются в migration archive. External consumers прежних private helpers не подтверждены; F24 сохраняет это ограничение.

## Runtime и эксплуатация

Outcomes/cohorts/export/digest и независимый signed agent outbox реализованы.
Наличие receiver/producer не означает активацию: owner credential/binding и signed
runtime smoke остаются отдельной зависимостью в [плане](open_issues.md).

Текущий market layout — physical schema2: normalized candle/context revisions,
shared settings и compact immutable revisions/identity-checked heads.
`market_storage.py`, `record_codec.py`, `quality_summary.py`, `storage_lifecycle.py`
и `storage_meter.py` владеют этим storage. Public history/export/analytics/agent
получают прежние полные values, formulas/reference/cutoff не меняются. Numeric quality
summaries сохраняют recorded methodology и settings после истечения подробностей;
ссылки на raw windows не продлевают подробную историю бессрочно.

Image: pinned Python base digest, hash-locked dependencies, UID/GID10001, read-only
root, no capabilities. `/data/sqlite` и backups принадлежат приложению; исходный
JSON migration archive сохраняется. Health проверяет recent heartbeat, а market
availability и обе delivery — отдельные diagnostics. Runtime hard resource limits
не назначены без измеренного основания.

Routine schema2 delivery выполняет full source integrity и SQLite online backup
на диске, full backup integrity и streaming SHA256. Вся DB в RAM/tmpfs не копируется.
Resource/deadline/space failure блокирует switch; downgrade, restore поверх live
и снижение checks не выполняются. Канонические limits, disk reserve, exact-target
reset и recovery — в [текущей процедуре](runbooks/bounded-storage-reset.md),
artifact/SSH/queue/trust boundaries — в [delivery runbook](runbooks/delivery.md).

Legacy schema1 умеет читать compressed shared values и UTC-aligned blocks.
Исторические converter/compressed-VFS tooling сохраняются для retained archives
и tests; они не являются текущим live write layout или routine schema2 backup.
Полная конвертация discarded test history отменена. Подробный representation и
compatibility — в [state contract](state_contract.md#compact-history-representation).

## G2A shared ingestion

`universe.py` владеет metadata eligibility/classification; `sources.py` — closed observations/provenance и bounded windows; `ingestion.py` — shared BybitRadar, subscriptions/clock/refresh/recovery. HTTP client общий, network вне DB transactions. Sources сохраняет logical source:v1 identity: в schema2 свечи и context нормализованы, в legacy schema1 используется runtime namespace. Ordinary startup не переводит одну physical schema в другую. Canonical REST recovery может исправить ordinary source observation; event snapshots остаются immutable. Detectors/notifications читают только persisted sources. Selected owner category/TF выбирают карточку, ingestion получает оба рынка и четыре native TF.

REST budget5req/s/concurrency4, один pending entry на stream; current recovery выше bootstrap и historical pages. Равный приоритет обслуживается с чередованием рынков. WS args шардируются по character budget, Spot subscribe batches≤10, ack обязателен. Отказ/timeout закрывает connection, снимает live eligibility и приводит к heartbeat/resubscribe/backfill с bounded backoff. Open candles в source store не попадают. Duplicate closed candle не расходует запись или live identity; конфликт блокирует affected window до canonical REST recovery. Out-of-order/history никогда не повышаются до нового live observation.

Текущий detector window восстанавливается первым. Длинный outage оставляет durable historical gap cursor; каждый worker берёт только одну страницу, уступая более важной recovery. Неполная historical page диагностируется, а missing outcome data не превращаются в zero return. Source retention/protection и outcome completeness реализует G3 worker с durable admission/holds.

Delivery readiness требует discovery обоих рынков, успешные subscription acks и хотя бы один fresh usable window каждого active market/TF. Полный initial bootstrap продолжает работу; `WARMING` не проходит existing receiver post-check. После bootstrap статус различает data errors и no-signal. Worker failure влияет на health; endpoint/clock/rate problems видны отдельно.

## G2B event → episode → consumer

Persisted closed observation запускает five detectors на одном settings snapshot. Ошибка family записывает reason/class без sensitive details; другие families продолжаются. Source inputs, immutable outcome reference и provenance сохраняются вместе с event/episode/decision/outbox в короткой transaction. Active episode prefix index ограничивает lifecycle работу текущим instrument; глобальный2s tick читает только active index. Recent history и pending consumers используют indexed prefix ranges; network не удерживает writer.

History всех families независима от Telegram selection/mute. Coalescing2s фиксирует episode snapshot и exact event revisions; text замораживается перед первой попыткой. Новые observations во время I/O остаются в history; после ack reconciliation создаёт meaningful follow-up. Settings revisions не смешиваются в pending payload/normalized-strength baseline. Исторический outcome update в будущем не может изменить уже frozen live context. Telegram batch≤20; retries переиндексируются по следующему attempt time и уступают более ранним ready deliveries.

Restart восстанавливает episodes, per-direction conditions и pending work; exchange clock устанавливается до expiry решения. Незавершённый IN_FLIGHT означает ambiguous ack, поэтому exactly-once не заявляется. Forced closure подавляет старое условие до re-arm; complete recovered non-signal bars могут подтвердить re-arm без historical live flood. Bootstrap/recovery сохраняют последнее проверенное наблюдение как historical; G2B не выполняет ретроспективный detector backtest всех raw candles.

## G3 analytics и independent digest

`outcomes.py` принимает jobs в event transaction, затем bounded worker вычисляет полные future1m окна без смены reference; `analytics.py` вычисляет frozen prior comparator для каждого события в ограниченном batch и отделяет causal readout от late analytics. Cohort distributions сортирует/агрегирует SQLite, exporter не собирает все raw returns в памяти. Telegram/CLI views имеют явные ограничения выдачи; streaming export через отдельный readonly connection не усечён и pinning snapshot проверен concurrent-writer fixture.

`source_context.py` архивирует public metadata/ticker availability; historical formula получает доступную тогда liquidity/tick context. Unavailable historical input означает limitation, не future substitution. `analytics_recovery.py` получает один referenced page через существующий HTTP client/budget; live gaps/bootstrap имеют приоритет. Он не создаёт memory windows/WS subscriptions inactive instruments и не переписывает conflicting used source. Outcome/retention holds переживают restart.

`digest.py` независимо от realtime alerts/mutes агрегирует общую event history обоих рынков, strongest period episodes и ongoing snapshot. Period identity, cursor/skip range, immutable text и attempts persist before send; network вне writer. Один pending latest period предотвращает historical burst после длительного outage. Disabled/reconfigured preference during I/O сохраняет фактический attempt с prior-preference/canceled result. Permanent Telegram target общий, enabled preferences раздельны.

Controller orchestration запускает bounded analytics job5s, episode/consumer delivery2s и heartbeat20s. Source errors, incomplete horizons, reference recovery и delivery target имеют отдельные diagnostics; analytics worker failure влияет на health. Отсутствие выгодной/большой live выборки не является failure расчёта. Logical v1 namespaces не определяют physical schema; schema2 и compatibility/recovery ограничения перечислены в [state contract](state_contract.md).

## G5 optional enrichment и manual calibration

`enrichment.py` содержит pure exact-asset mapping и per-metric calculations/units/freshness; `enrichment_runtime.py` использует общий BybitRadar client/budget. Owner setting первоначально OFF. При включении ticker refresh обоих рынков coalesced lock/cadence1min, OI latest2 points batch≤4/worker2s, cadence5min. Optional work уступает current recovery; failures остаются локальными, не меняя core heartbeat/Spot formula. Immutable mapping revisions и public OI availability переживают restart; ambiguous aliases не угадываются. Диагностика явно показывает mapping и ошибки OI с ограничением выдачи.

Event callback синхронно выбирает mapping/ticker/OI, доступные до candle observation cutoff, и сохраняет snapshot в общей event transaction. Новые current metrics/outcomes/retries не заменяют прежний context. Funding%/next funding UTCms, OI BASE/asset и5min absolute/percent change, snapshot basis с individual times сохраняются независимо; stale/missing metric не скрывает другие. Perpetual event продолжает самостоятельный detector path. Current owner карточка отдельно показывает latest snapshot, alert не добавляет выключенный/N/A context.

Analytics grouping включает frozen profile, detector/version/settings/market/TF/horizon. Owner `/recompare` requeues только prior comparator, сохраняя исходный event revision/settings/reference и прежнюю analytics revision. Новые inputs/availability дают отдельный late result, строго prior observation; без owner действия completed comparison не меняется. Никакой threshold write не выполняется. NDJSON включает frozen event context и methodology definitions; CSV сохраняет profile grouping. Эта функция не требует новой physical migration; schema2 и forward recovery остаются действующими.

## Bounded market layout v2 (текущий storage этап)

Нормализованные candle/context revisions и shared settings питают прежние detector/outcome formulas без копирования полных windows. History/consumer APIs сохраняются; serialized signed bytes не пересобираются при retry. `market_storage.py`, `record_codec.py`, `quality_summary.py`, `storage_lifecycle.py` и `storage_meter.py` реализуют этот формат. Подробные ownership/expiry/reset contracts: [state contract](state_contract.md#market-layout-v2-новый-период). Schema1 остается legacy compatibility input; fresh schema2 создается только отдельной reset preparation, не ordinary startup. Текущий runtime уже использует schema2; actual reset/delivery Evidence находится в плане.

Automatic cleanup использует expiry/GC indexes и bounded batches вместо обхода всего архива. Record body refs и context body refs имеют transactional counters. Quality summaries сохраняются до удаления подробностей; pending service/recovery ownership имеет конечный срок. Контроль объема отделяет измеренные bytes/counts, extrapolation и данные которые еще не подтверждены. Наличие этих modules само по себе не подтверждает поставку/reset; их завершение проверяется по primary CI/CD и runtime Evidence плана. Повторный reset обычным restart/deploy запрещен.

Storage extension3 сохраняет physical schema2 и public contracts. Legacy expiry/quality
rows остаются видимыми через compatibility views до bounded in-place drain. Internal
row IDs/shared dimensions/provenance не заменяют внешние event IDs. Recovery убирает
уже заполненные local ranges без REST и coalesce overlapping work, сохраняя source
conflict checks/owner expiry. Детали upgrade/rollback boundaries — в [state contract](state_contract.md#storage-extension3-совместимое-уплотнение).
