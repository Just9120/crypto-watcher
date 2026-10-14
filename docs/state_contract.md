# CryptoWatcher — state contract

Подготовка15Oct: reader дополнительно поддерживает exact-byte envelopes
`_history_record_storage` / `cryptowatcher.compressed-record.v1`. Original UTF-8
JSON сохраняется lossless; content size/window/checksum/strict single Zstd frame
и SHA256 проверяются до decode. Public values, timestamps и numeric types не
меняются; live writer продолжает существующий shared-fields format. Candidate
с такими historical records имеет SQLite user_version2 и exact format declaration
`historyrecord:format:v1`; version1 остаётся default. Unknown declaration/version
закрывает доступ, version2 сохраняется при restart. Старый version1-only reader
не является допустимым rollback после такой migration. Current production ещё
schema1: запись framed records/promotion не выполнялась.
SQLite history filters/analytics/export используют `cw_record_json`, registered
на каждом Store connection, включая read-only export. Scalar возвращает exact
original JSON для framed records; обычные/shared-field записи не меняет. Поэтому
period/version/market filters, causal cutoff, revisions, cohort/comparator samples
и settings export продолжают видеть прежние данные. Коррупция frame прекращает
query. Compatibility fixture сверяет complete filtered NDJSON и cohorts before/after
conversion, включая ранний и поздний cutoff; private transport fields исключены.

Disk-backed validation — explicit isolated option: owned private disk file,
encoded allocation bound,1.5GiB reserve и отказ для tmpfs/unknown filesystem.
Default compressed-RAM limit3GiB и общий runtime validation cap4GiB не меняются.
Все full integrity/CHECK и source/candidate/recovery proofs остаются обязательными.
Это подготовленные primitives; whole current conversion≤900s ещё не qualified.

## Compact history representation

Mutable runtime обновляется на месте по existing key; byte-identical canonical
payload не вызывает повторной записи. Source recovery сохраняет ключ свечи и
обновляет её значения/provenance в прежней transaction. Rowid — внутренний
physical identifier, не observation/update time и не public event identity.
Изменение write strategy не меняет schema, encoding, retained history или
reader/rollback compatibility с существующим compact-aware runtime.

Во время первоначальной WebSocket subscription phase closed observations временно
буферизуются отдельно от ACK: максимум128 сообщений/2MiB raw bytes на session.
После полного ACK они проходят прежние validation/persistence/detector paths
с исходным network receipt time; freshness остаётся от market observation time.
Replay уступает event loop между сообщениями, buffer освобождается после обработки.
Overflow блокирует affected stream и запускает priority REST recovery; historical
recovery не создаёт live alert. До ACK/rejection buffer не пишет candles/events.
Это bounded transient ingestion state, не durable consumer outbox и не history;
restart восстанавливает пропуски существующим source recovery.

Physical SQLite user_version остаётся1. Public immutable revisions могут содержать
`_history_storage` / `cryptowatcher.shared-fields.v1`: content-addressed ссылки на
полные lossless settings/context/windows; head `cryptowatcher.history-head.v1`
содержит projection и revision_key. Public history/export/analytics/agent readers
восстанавливают все исходные значения и numeric types, а не возвращают storage
markers. Формулы, reference/cutoff, event identity и signed private transport bytes
не меняются. Private namespaces не преобразуются public codec.

`historyblob:v1:SHA256` идентифицирует canonical decoded JSON bytes независимо
от compression encoding. Поддерживаются `cryptowatcher.shared-json.v1` (zlib) и
`cryptowatcher.shared-json.v2` (один checksummed dictionary-free Zstd frame).
V2 хранит ровно schema/bytes/zstd; bytes≤32MiB, encoded bound, frame content size,
window≤32MiB, complete checksum/trailer, отсутствие дополнительных frames/data,
decoded SHA256 и canonical JSON обязательны. Unknown schema и любое нарушение
блокируют чтение; пустой state вместо исходника не создаётся.

Разовая history conversion создаёт новые bodies вV2 по измеренному cost/size
выигрышу. Existing V1/V2 bodies и остальные исходные private rows сохраняются;
live writer по-прежнему создаётV1 и использует UTC-aligned16-candle blocks.
Historical conversion хранит целые frozen windows как shared values, не удаляя
исходные candles/provenance. Одинаковые decoded bytes имеют тот же key; более
новый codec не перезаписывает existing body только ради compression encoding.

Full readback проверяет каждую body row, каждый reference и logical manifests всех
пяти таблиц. Только frozen, физически write-refusing candidate может использовать
cache проверенных body bytes; single-value field сравнивает reference SHA с тем
же уже проверенным body key. Window joins по-прежнему проверяют полный joined hash.
Mutable runtime reader повторно читает актуальный DB payload перед cache reuse.

Старая G5 application не является reader-compatible с compact representation.
NONE/physical schema1 не разрешает app downgrade или restore поверх live данных.
Поставка/promotion требует [изолированной qualification и exact-target review](runbooks/delivery.md#разовый-recovery-gate-по-идентичности-проверенных-bytes).

## Advisory agent state

Logical `radar:v1:agent:*` namespaces сохраняют schema1 preference (default OFF),
episode/outcome intents и due indexes, monotonic per-event transport sequence,
immutable canonical serialized bodies/hash, attempts, pending due, target diagnostics
и successful episode notification baseline. Source event revision отделён от transport
revision; original settings/reference/source snapshots не переписываются доставкой.
Secret/HMAC headers/Access credentials в DB не сохраняются. Unknown preference fail-closed.
Source event/episode/outcome и intent записываются одной транзакцией; frozen body появляется
до request. Crash IN_FLIGHT повторяет same bytes/identity с диагностируемой ambiguity.
Existing online all-row backup включает эти namespaces; physical schema остаётся1.
Compatibility и operational flow — в [agent procedure](runbooks/agent-delivery.md).

## Владение и precedence

Product requirements задаёт [spec](product_scope.md); текущие задачи/Evidence — [план](open_issues.md). Этот контракт уточняет persistence и compatibility, operational gate — [runbook](runbooks/delivery.md).

Один владелец и его private delivery chat. Infrastructure secrets, owner/bot binding и пути — environment/host `.env`. Env процесса выше `.env` (`load_dotenv(override=False)`). Owner settings уже в DB выше initial defaults; Telegram изменение создаёт новую revision без restart. Invalid values не заменяют сохранённые данные и не выводятся в error text. Invalid infrastructure или corrupt owner record блокирует startup с field/class diagnostic.

Retired `.env` keys: ALLOWED_CHAT_IDS, PRICER/CMC, BYBIT_PAIRS/WATCHLIST seeds. Они не включают второй chat и не перезаписывают существующий список. Infrastructure defaults отсутствующих обычных settings описаны `.env.example`; raw legacy payload определяет migration behavior.

## SQLite schema1

Persistent DB: container `/data/sqlite/cryptowatcher.sqlite3`, host `/opt/crypto_watcher/data/sqlite/cryptowatcher.sqlite3`. WAL + synchronous FULL + busy_timeout5000ms + foreign keys; приложение имеет process lease по sidecar `.lock`, один writer. Network I/O вне транзакций. Online backup использует SQLite backup API, включая согласованный WAL state; простое копирование основного `.sqlite3` активного writer недостаточно.

| Table / namespace | Owner / значение |
| --- | --- |
| owner_settings | Единственный owner_id, revision>0, validated domain settings JSON |
| delivery_preferences | Единственный разрешённый private chat; alerts enabled, отдельно от расчёта |
| mutes | Typed market/symbol и timestamp; legacy symbol применяется к selected legacy category |
| runtime | Heartbeat, poll/delivery diagnostics, successful notifications, owner input workflow, preserved legacy baselines |
| legacy_import | Source SHA256, полный original payload, имя exact-byte backup; другие chats архивируются, не становятся consumers |
| runtime:radar:v1 | Versioned event/episode history, immutable source snapshots, detector registry, notification decisions и Telegram outbox/attempts; outcomes/agent добавляются G3/G4 |

Owner settings: category spot/linear, top/custom, custom_pairs/typed watchlist, selected card TF, price/turnover/liquidity thresholds, poll/cooldown/SMA, sensitivity profile/manual detector parameters и Telegram family selection. Finite positive validation, ratios>1, integer cadence/cooldown/lookback, legacy SMA>1; invalid field остаётся без mutation. CAS expected_revision предотвращает lost update. Результат market fetch со старой revision отменяется. Pending delivery старой revision не отправляется как решение по новой; ACK уже in-flight сообщения помечается DELIVERED_PRIOR_SETTINGS. Failed send не расходует successful episode notification state. Legacy cooldown сохранён, production episode consumer его не использует.

Known owner/schema проверяются до writable open. Unknown/future DB version и чужой owner не изменяются. Diagnostics/health/state-check работают read-only. Schema change после1 требует versioned миграции и delivery classification.

## JSON legacy migration

Accepted inputs: version2 с chats object; legacy1/неверсионированный recognized payload только при explicit chat identity. Duplicate keys, non-finite JSON, corrupt/unknown version, отсутствие owner record либо invalid settings/mutes блокируют import; source остаётся неизменным. Незнакомые fields и other chats сохраняются полностью для owner review.

Правила импорта owner233382575:

1. Raw settings читаются до normalization/default shaping. `custom_pairs`, включая явный `[]`, имеет приоритет. При отсутствии — legacy bybit_pairs/watchlist из JSON. Env seeds не добавляются.
2. Явный alert_universe_mode сохраняется. Иначе legacy top_limit>0 → top, 0 → custom; без него infer по raw списку. Negative/non-integer limit отклоняется.
3. Сохранённый TF сохраняется; отсутствующий legacy TF →5m независимо от env seed. Все допустимые сохранённые thresholds/category/cadence/overrides имеют приоритет над defaults. Unknown retired fields остаются в archive, не включают CMC path.
4. Enabled bool и mutes сохраняются. Legacy CMC/BYBIT mute identity нормализуется к USDT symbol, для совпадающего symbol используется максимальный срок. Такой mapping отражает old single-market behavior, не создаёт второй market target.
5. Полные baselines сохраняются в original archive и runtime. Numeric last_alert нормализуется для безопасного cooldown; новый event/episode runtime не генерирует историю из старых alerts.
6. Other chats не объединяются с owner configuration. Owner absent/conflict требует отдельного решения, не выбора первого chat.
7. Перед записью создаётся exact-byte JSON backup с SHA256, fsync и source readback. Caller обязан остановить old writer для финальной production migration. Изменившийся source блокирует import.
8. One transaction сохраняет owner/settings/preferences/mutes/baselines/import record. Повтор same source SHA возвращает already_imported. Existing owner либо другой imported SHA не перезаписываются. Source JSON не удаляется.

Требуемые paths должны быть различны; production schema/name определены выше. Новые directories app-owned0700, DB/backup0600. Не делать recursive chown legacy data. Windows permission model проверяется отдельно от Linux fixture; production POSIX ownership требует host readback.

## Canonical CLI

Из repository root / установленного image, с validated defaults и explicit owner:

```bash
python -m cryptowatcher migrate --legacy PATH_TO_QUIESCED_JSON --database PATH_TO_NEW_DB --backup-dir PATH_TO_BACKUPS --owner-id 233382575
python -m cryptowatcher state-check --database PATH_TO_DB --owner-id 233382575
python -m cryptowatcher backup --database PATH_TO_DB --destination PATH_TO_NEW_BACKUP --owner-id 233382575
```

Это placeholders для isolated fixture либо reviewed paths; production sequence требует first-delivery gate. Startup при существующем legacy без imported settings отвечает explicit migration required, не создаёт пустого owner state. `state-check` выводит integrity/schema/owner/revision/settings hash/import hash/preference, без secrets. Backup destination обязан быть новым; overwrite/restore команда отсутствует намеренно. Restore проверяется открытием нового isolated backup и сравнениями owner/settings/mutes/runtime.

## Recovery и retention

Legacy source и archived other chats автоматически не удаляются. Backup names уникальны; housekeeping manual до measured policy. Event source snapshots автоматически не удаляются. Согласованные90-day ordinary candles и защиту иных outcome/comparator sources реализует G3; это не поручение удаления JSON/backups.

До new-runtime write можно восстановить прежний application с сохранённым JSON по approved sequence. После write legacy JSON отстаёт от SQLite: blind rollback приложения теряет новые settings/runtime. Нужен compatible schema1 rollback или forward-fix; DB restore поверх live данных требует отдельного scoped approval и writer quiescence. Наличие файла `.bak` само по себе не подтверждает пригодность восстановления.

## G2A source record contract

Physical SQLite user_version1 не меняется. Логически независимые record families существуют в key/value `runtime`, owner settings и delivery preferences остаются в своих таблицах. Index по primary key поддерживает prefix range + ORDER BY key DESC LIMIT256; source keys имеют padded start timestamp, поэтому не требуется читать всю историю для detector window.

| Namespace | Значение |
| --- | --- |
| source:v1:metadata:market/symbol | Validated instrument metadata, classification version, server timestamp |
| source:v1:quarantine:market/symbol | Exclusion reason/version/time; quarantine не становится eligible universe |
| source:v1:candle:market/symbol/TF:start | Closed OHLCV/turnover, interval/start, source, exchange/receipt times; quote units USDT |
| source:v1:gap:market/symbol/TF | Durable start/end historical recovery cursor и PENDING/COMPLETE |
| ingestion | Actual source/market errors, subscription acknowledgements, TF, pending/buffer diagnostics |

Native свечи закрыты по confirm=true либо REST exchange time. Historical/bootstrap/recovered записи сохраняются, но live eligibility — process-local и после restart пуста. Cached window≤256; остальные rows остаются в DB. Online backup schema1 включает все source namespaces. 90-day raw retention/protected event snapshots вводятся в G3; owner/import/backup data автоматически не удаляются.

Settings дополняются typed `watchlist` и `watchlist_multiplier` (default0.75). Отсутствующие fields старой schema1 принимают defaults без переписывания сохранённого owner record; legacy custom_pairs остаются исходным migration evidence и fallback выбранного рынка. Новый список содержит spot/symbol либо linear/symbol; explicit clear очищает оба representations, empty list не получает seed. Legacy JSON watchlist остаётся untyped alias при import, не интерпретируется как новый field. Saved numeric thresholds не заменяются preset. Typed mutes имеют market/symbol; сохранённые legacy symbol mutes действуют только на selected legacy category.

После записи новых owner fields приложение G1 не поддерживает typed payload: автоматического rollback на G1 нет, recovery — forward-fix либо проверенная compatible G2A revision. DB schema downgrade/restore поверх live data не выполняется. Новая versioned physical schema, если понадобится следующим Goals, требует своей явной delivery classification, NONE не подменяет migration gate.

## G2B radar logical schema v1

SQLite user_version остаётся1; existing runtime primary key обеспечивает prefix queries. Logical record schema `cryptowatcher.radar.v1`, event `cryptowatcher.event.v1`. Online backup включает все namespaces и проверяется all-row readback на isolated copy.

| `radar:v1:` namespace | Контракт |
| --- | --- |
| detector-metadata:family:version | Immutable formula, baseline/ATR/strength methodology; изменение без новой version блокирует startup |
| event:UUID:revision / event-head:UUID | Append-only revisions и latest head; immutable reference close/time, settings/effective parameters, market/detection/receipt/availability timestamps, metrics/units, direction/strength, source inputs/hash и producer revision |
| history:observationTime:UUID / episode-event:UUID:time:event | Indexed recent history и полная связь events с episode |
| episode:UUID:revision / episode-head:UUID | Lifecycle/confirmations/conditions и immutable causal snapshots; late revision не регрессирует head |
| active:market/symbol/direction / condition:stream:family:direction | Active index, last closed observation, two non-signal bars и forced-closure re-arm block |
| decision:telegram:event / decision:telegram:event:revision | Latest reason и сохранённые notification decision revisions, включая silent/muted/historical/suppressed |
| outbox:telegram:UUID / pending:telegram:dueTime:UUID | Durable identity, exact event revisions/episode snapshot/settings revision, frozen text, attempts/acks и retry index |
| target:telegram / diagnostic:stream:family | Permanent target failure/re-enable и localized calculation errors |

Event identity включает stream/family/version/settings revision/candle start; episode identity основана на первом event. Episode grouping имеет market identity и direction. История сохраняет все detected events, включая historical/suppressed и muted; updates событий/episodes append-only, current heads отдельно. Каждый используемый input заморожен в event, поэтому последующая REST correction обычного source row не меняет исходную методику. ATR availability для будущего outcome помечается отдельно; недостающий контекст не превращается в нулевое значение.

Event + episode + decision + initial outbox сохраняются одной transaction до отправки. Consumer замораживает exact event revisions и text; receipt/retry не меняют observation time или TTL. Failed/expired/obsolete attempts остаются history; successful-notification baseline меняется только после ack. Unknown ack отмечается AMBIGUOUS_AFTER_RESTART/UNKNOWN, повтор сохраняет identity. Новое подтверждение во время I/O может создать отдельный follow-up после ack; future event revisions не включаются в прежний payload.

Отсутствующие profile/parameters/alert_detectors читаются compatible defaults: custom legacy thresholds и Telegram composite only. Upgrade не записывает новый preset автоматически. Явная owner операция создаёт revision; preset definition полностью validated, manual edit переводит profile в custom. После записи этих fields G1/G2A images не считаются compatible rollback; recovery только forward-fix либо verified compatible G2B application. JSON import не повторяется, physical DDL/DB downgrade отсутствуют.

## G3 outcomes, comparisons и digest — logical schemas v1

Physical schema1/NONE, один writer. `outcome-method:1` / `comparison-method:1` immutable; изменение методики без новой version блокирует работу. `outcome-job:UUID` / timestamp-length-ordered `outcome-pending:` фиксируют event revision, horizon, immutable reference и retries; `comparison-job:event` / `comparison-pending:` — отдельный bounded comparator worker. `outcome-admission` idempotently проходит прежнюю indexed history и блокирует retention до завершения. Event transaction включает initial jobs/holds до consumers. Старые snapshots без новых fields читаются agreed defaults5/15/30/60, не будущими текущими owner settings.

`outcome:UUID:revision` / `outcome-head:UUID` и `comparison:UUID:revision` / head сохраняют прежние versions/results и availability timestamps. Неполные outcomes могут получить новую COMPLETE revision после recovery; завершённые results не пересчитываются автоматически. Complete result, full used inputs и `protected:source:v1:candle:` записываются атомарно. `hold:stream:job` удерживает весь pending диапазон, включая данные старше90days; bounded `retention` удаляет только unreferenced ordinary candles. Legacy JSON/backups/events/episode/outcome/comparison/settings records автоматически не удаляются.

Новые event/outcome heads имеют internal schema `cryptowatcher.history-head.v1`:
`revision_key` указывает на неизменную revision того же kind/ID, рядом сохранены
небольшие поля для indexed selection, digest и cohort SQL. Полные settings,
provenance и source inputs остаются в revision. `History.get/items/scan` и agent
replay разрешают reference и возвращают прежний полный record без internal head
fields. Missing/cross-kind/cross-ID revision либо несовпадение query projection
вызывает диагностируемую ошибку вместо частичного record. Immutable revisions,
causal cutoff и public event/outcome/export schema не меняются.

Legacy full heads читаются в той же базе; upgrade не запускает bulk rewrite.
Только новая revision получает compact head в той же transaction. Existing raw
source/outcome snapshots и history автоматически не удаляются. Physical schema
остаётся1, но старый binary после compact head writes не является совместимым
reader: recovery требует forward-fix либо проверенную версию с этим reader.

`source-context:v1:ticker|instrument:market/symbol:availableTime` содержит public values/metadata, units/source и actual observation/availability timestamps. Event и выбранные comparator windows включают полный использованный context snapshot. Historical composite без prior ticker≤5min не считается no-signal; future liquidity/noise исходного event не используются. `analytics-gap:` / due index персистентно восстанавливает referenced closed ranges без изменения live subscriptions/notification state. Conflicting existing source сохраняется и диагностируется; recovery не перезаписывает его и не создаёт historical live alerts.

Owner horizons/digest interval — validated owner settings; enabled preference/cursor `digest-preferences` отдельно от realtime delivery preference и mutes. `digest-outbox:UUID` содержит stable UTC interval identity, frozen aggregate/text и durable attempts; один `digest-pending` на последний interval, `digest-skipped` обозначает диапазон пропущенных после restart. Enabled change во время network не воскрешает canceled pending; acknowledged prior-preference attempt явно помечается. Shared Telegram target permanent failure виден независимо от каждой queue; explicit alerts/digest enable возобновляет target, не меняя другой toggle.

Read-only export pinning BEGIN/WAL snapshot потоково читает records100/page, только declared public/history/settings namespaces. Events/revisions/episodes/outcomes/settings/provenance и completion counts сохраняются; infrastructure/secrets/outbox/transport attempts не экспортируются. Causal context использует revision availability≤cutoff и не включает позднюю analytics. Методологические ограничения и small N остаются частью результата.

После записи новых outcome_horizons/digest_interval_min G2B payload reader не считается rollback-compatible. Recovery — forward-fix или проверенная compatible G3 application; downgrade/restore поверх live data запрещены. Upgrade/backup fixtures подтверждают исходные bytes прежних event records и all-row restore readback без нового import.

## G5 context и explicit comparator recalculation

Owner `enrichment_enabled` — строгий bool, default OFF при отсутствии старого field; отдельно от alerts/digest/chat state. Mapping revisions `radar:v1:mapping:UUID:revision` фиксируют qualified Spot/Perpetual metadata, candidates и reason/availability. `source-context:v1:oi:linear/symbol:availableTime` хранит public latest5min points/units/server time. `oi-attempt` сохраняет next attempt/error; restart не повторяет весь batch. `enrichment-method:1` immutable; изменение definition без новой version блокируется.

Event `enrichment` — frozen optional context, выбранный по availability≤observation cutoff, с method/mapping revision, instrument identities и per-metric values/units/source/observation/availability/cadence. Historical observation не получает future mapping/OI/ticker; malformed/missing/stale metric остаётся маркированным. Current owner view рассчитывает latest freshness отдельно. Все использованные snapshots входят в event/export/online all-row backup и автоматически не удаляются.

Explicit `recompare` сохраняет comparison job original event_revision, повторно удерживает prior input ranges до completion и добавляет recalculation_requested_ms. Предыдущие result revisions доступны по as-of cutoff и export; event/settings/reference не изменяются. Автоматической recalibration/threshold mutation нет. При записи owner payload с enrichment field G3 reader несовместим: разрешён forward-fix/verified G5, без DB downgrade или restore поверх живых данных. Physical user_version1/NONE не изменяется.
