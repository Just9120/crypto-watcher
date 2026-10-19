# CryptoWatcher — state contract

Текущий runtime использует **physical SQLite schema2**. Версии public event/history
и logical `radar:v1`/`source:v1` namespaces независимы от physical user_version.
Сначала приведен действующий market layout; последующий compact schema1 раздел
сохраняет historical compatibility и не задает routine schema2 backup/retention.

## Новая policy хранения и разовый reset (D22)

Updated S01/owner request2026-10-16 отменяют raw90d и бессрочное хранение event/outcome/comparator inputs. Актуальные сроки/AC: [E13](product_scope.md#e13--компактное-хранение-сроки-и-новый-период-истории). Implementation state и gates: [Current Goal](open_issues.md). Полная конвертация прежней тестовой market history больше не задача. Описания прежних converter/qualification tools относятся к историческому tooling и не являются precondition нового reset.

Bounded Telegram decision head содержит только `_cw_decision_revision`; неизменная
revision хранит полный результат. Reader проверяет ключ/event identity/revision и
возвращает прежний public JSON. Старые полные heads читаются; bounded repack имеет
отдельный durable decision cursor и не сбрасывает прогресс прежних проходов.
После новых pointer writes rollback требует совместимого reader/forward-fix.
Cleanup telemetry и накопительные `market_retention_totals` фиксируются в одной
transaction с удалениями; это counters, не отдельная копия market history.

Reset — отдельная разовая процедура с exact market-data allowlist/target и private preservation/restore/equality proof; не startup migration. Owner settings/watchlist/thresholds/notifications/mutes сохраняются; credentials/infra не меняются. Старые market queues/cursors не импортируются; новая generation/start явно показывает нехватку истории и исключает old live replay. Backups/experiments исключены. Обычные backup/CD<=900s и RAM4GiB сохраняются. Формат и проверенная reset процедура определены ниже и в runbook; факт поставки/reset подтверждается только primary Evidence плана, не самим описанием.

### Storage extension3: совместимое уплотнение

Physical schema остается2; public JSON schemas/IDs не меняются. Writer atomically
устанавливает additive extension после ordinary CD backup. `market_lifetimes` и
`market_quality` становятся compatibility views над сохраненными legacy rows и
новыми normalized tables. Expiry использует explicit stable IDs и shared event IDs;
quality — shared immutable cohort dimensions/provenance. Legacy data не копируется
при startup. Перенос выполняет [storage_repack.py](../cryptowatcher/storage_repack.py)
короткими транзакциями с durable cursors/identity readback и incremental vacuum.
Новый writer/reader читает CW2 и typed CW3; dictionary3 и float representations
frozen. Canonical JSON bytes, SHA256, signed outbox body и source revision links
не меняются. Progress rollback атомарен с измененными rows; skipped large records
диагностируются, а не удаляются. Metadata references собираются bounded GC и после
окончания repack. Большие quality blocks используют rowid table, чтобы не создавать
почти пустую overflow page для каждого промежуточного1–2KiB блока.
Подробные правила и ограничения — [D24](product_scope.md#d24--lossless-storage-growth-correction-grow-2026-10-16).

### Market layout v2: новый период

#### CAPACITY25: совместимые readers и конечное уплотнение

Source windows разделяют immutable coordinate bodies с exact runs/order/revisions/SHA.
Числовые outcome metrics используют26/34byte frames без изменения float bits и
int types; неподдерживаемые формы остаются inline. Numeric2 refs используют
bodyID*4+view для exact mirrored/neutral представлений. Общий body допускается
только при полном bit/type round trip; невыводимый range сохраняется целиком.
Numeric IDs никогда не
переиспользуются после удаления. Runtime refs и expiry leases обновляются в той
же transaction, что immutable revision; отсутствующая зависимость отклоняется.

Repack11 добавляет NB1/NR1 numeric blocks:64 slots адресуют прежние IDs,
26/34byte frames остаются точными, без outcome arithmetic или зависимости от
исходных свечей. SHA256 связывает actual compressed bytes/counts с block ID;
bounded decoder проверяет header/window/content size до materialization.
FP8 lookup сравнивает полный frame при совпадении hash. Per-slot detail/quality
leases и GC атомарны с владельцами runtime/quality/catalogue; потеря counts,
lookup или totals прекращает изменение. Удаление старой retained строки
разрешено только после readback exact frame+leases в той же transaction.
Передается исходный AUTOINCREMENT high water, включая уже удалённые IDs;
частичная очистка не уплотняет IDs и не переиспользует слоты. Finite PK ceiling
не растет от новых admissions и переживает restart/DDL/VACUUM. Installation и
writer activation выполняет explicit repack; Store open регистрирует только
чистые UDF и проверяет известный marker. Это также поддерживает reader,
открытый до upgrade, при переходе к новой read transaction.
Чистые frame/count transforms используют тот же общий1MiB cache; actual bytes,
lookup presence и актуальные leases проверяются при чтении. Encoded bytes
block/counts/lookup относятся к detail/quality proportional frame lengths,
quality lease имеет прежний приоритет над detail. Это logical attribution;
SQLite tables/indexes/free pages/WAL/SHM/journal измеряются отдельно целиком.
Старые numeric rows остаются читаемыми до finite transfer. После новых writes
требуется compatible reader/forward-fix; native и mature capacity gates прежние.

Repack12 допускает NB2/PR1 exact self-contained price operands после explicit
numeric layout1→2 и отдельного writer marker, атомарного с новым checkpoint.
Producers/retained normalizer используют candidate только при этом marker и
проверенном frozen method/window context. NB1 bytes/длины frozen; NB2 имеет тот же
64-slot bound/full SHA256 и хранит raw frames совместно с operands. ID/FP и
owner leases относятся к восстановленному прежнему26/34byte canonical frame.
Private offer допускает только byte equality всех metric values/types и
уменьшение целого compressed block; rollback/readback/quality GC используют
прежние transaction boundaries. Layout marker/actual bytes проверяются и при
cache hit. Обычный open не повышает marker, не запускает migration и не включает
writer. Record pass ждёт finite source+numeric transfer; signed numeric savings
учитываются и без изменения owner reference. Legacy byte counters проверяются
без cold доли; неизвестные/потерянные counters не исправляются обнулением.
Неизвестный checkpoint отклоняется до DDL; committed12 без writer marker
отклоняется без его восстановления. Clean baseline требует COMPLETE именно
текущей версии repack. Native/mature qualification обязательны до поставки.

Архивный CQ4 packet сохраняет прежний public fact JSON. Общие templates,
strength/noise columns и точные ordered comparator lists не меняют методику,
N, ограничения и population_size. Delta имеет не более8ancestor links; numeric
bodies удерживаются фактическими leaf blocks, а не удалёнными элементами ancestor.
Старые J/Z/CW2/CW3/CQ1/CQ2 inputs и numeric refs читаются. Provenance body живёт до последнего
зависимого block. GC всех зависимостей конечен и учитывается как выполненная
retention work даже после удаления последнего event.

`market_body_links` сохраняет owner/body через compatibility view; новые пары
хранят integer coordinates существующего owner registry и shared body dictionary.
Legacy rows остаются видны, пока bounded repack не перенесёт их атомарно.
Repack12 сохраняет durable finite phases records/decisions/expiry/quality/namespaces/
references/quality-tally/key-registry/prospective-cohorts/source-tiles/numeric-blocks. Secondary registry CK1 сохраняет исходные
runtime keys и stable integer IDs; mixed legacy text/tokens видимы через expiry4/
reference2 views. Point mutations используют индексы, не полный scan view.
Словарь prefixes/UUID/hex/padded integers frozen; malformed token отклоняется.
Namespace и archived fact counters квалифицируются
конечным сканированием; concurrent writes и rollback учитываются triggers.
Prospective cohort/strength counters также имеют finite initialization и
поколения после schema change; старые служебные поколения удаляются bounded GC.
Полные ablations разделяются по UTC days, чтобы probe не держал месяц events
в Python sets. Missing/unknown projection и unqualified generation не дают
полного boundary. Это auxiliary accounting, не замена history/analytics.
Schema change требует повторной квалификации transient cursors. Ни startup,
ни capacity probe не объявляет частичный scan полным. После новых writes
восстановление требует compatible reader/forward-fix; старый binary и blind
restore поверх новых данных запрещены.

Contexts2 сохраняет полный SHA256 один раз у referenced `market_context` row,
а ссылки содержат точные kind/stream/time coordinates. Старый combined hash
проверяется до преобразования. При каждом чтении parts проверяется их frozen
SHA256; body/timing/coordinates и установленный digest неизменяемы. Unreferenced
rows не конвертируются; сроки/leases прежние, public context bytes неизменны.
Digest counters и detail ref conversion атомарны с writer transaction.
Byte-flow samples после завершения новой версии repack имеют отдельный epoch:
backfill pools/digests не выдаётся за последующий ежедневный прирост. Старые
samples и event-frequency observations сохраняются.

CH1 — frozen template для inline projected heads с полным SHA256. Field positions
и typed defaults неизменны; `schema`/`revision_key` восстанавливаются ровно из
сохранённых ID/revision. Все query fields остаются в head; новой ссылки на detail
для SQL нет. Unknown fields/неподходящие shapes остаются в общем codec. CW3 reader
не меняет семантику. Чистый byte-to-JSON cache разделяет прежний1MiB budget с
reference extraction; Python Unicode sizes учитываются через фактический размер
objects, cache не заменяет проверку наличия внешних bodies.

DB1/DB2 catalogue primitives пока не включены в runtime/repack. Это frozen blocks
с отдельно сжатыми projection/detail frames, полным SHA256 actual frames,
проверкой UUID/revision/slot и allocation bounds до decompression. Каталог
хранит immutable coordinates/parent deadlines и не дублирует integer rowid
для primary domain key. Oversized отдельная запись требует прежнего layout.
Append-only SQLite arenas публикуют packets атомарно; refs/verified relocation
и GC не меняют logical packet identity. Numeric/source-window leases считаются
по logical revision, shared JSON bodies — по packet до последнего владельца.
Missing dependency/counter отклоняет удаление. Install/partial primitives
не являются разрешением migration: полные mixed readers, analytics/export,
retention/accounting adapters, finite restart и resource qualification нужны
до выбора этого layout приложением. Existing Store open регистрирует UDF для
уже установленного известного catalogue; DDL/migration при open не добавлены.
DB2 использует IF1: неизменяемый typed dictionary, byte offsets и общий Zstd
frame, с полным SHA256 actual header/offsets/compressed bytes и bounded content
size/window до decompression. Чтение materialize только выбранный typed slice;
чистая binary decompression разделяет общий1MiB cache с text/reference transforms.
По IDs/existence данные не кэшируются. DB1 остаётся читаемым; формат IF1 не
меняет canonical JSON, float bits или numeric types. Linux/resource gates для
runtime activation остаются обязательными.

Candidate catalogue service expiry отделён от immutable body. Event parent
deadline неизменяем; episode service row может только продлить lease до MAX
действительно retained members. Hot duplicate и cold lease обновляются одним
SAVEPOINT; missing/conflicting member/owner/checksum не даёт продлить или удалить
запись. Episode extensions с внешними source/context dependencies до отдельного
lease adapter остаются hot и учитываются в budget. Mixed expiry сначала сохраняет
все method results/comparators/provenance, затем в той же transaction удаляет
hot/cold revisions, membership и parent; refs освобождаются до bounded GC.
Frozen future leases не теряются из-за нового age cutoff; bounded history cursor
позволяет дойти до остальных due records и сохраняется только вместе с обработкой.
Это candidate adapter, не runtime activation или подтверждение mature25GB.

SB1 source tiles введены явным finite repack10 и сохраняются в repack11; обычный Store open не
создает их и не включает writer. Замороженный блок
содержит32 native временных слота, до256 immutable revisions суммарно,
точные typed values/needed_until/purge_after и SHA256 actual header/IF1 frame.
Mixed point/range adapters сохраняют общий revision space, порядок, пропуски
и frozen window SHA. Пакетная запись переносит legacy tile в том же savepoint;
непоместившаяся история коррекций остается в прежней таблице с явным fallback
и scalar counters. SQL guards запрещают изменение старых значений и сокращение
leases. GC перепроверяет каждую revision по исходной retention/hold policy;
общая first_due блока — только индекс кандидатов, не срок удаления всех свечей.
Чистые binary transforms используют прежний общий1MiB cache, actual bytes
проверяются при чтении. DDL выполняется только explicit install; Store open
регистрирует UDF существующего известного layout. Source transfer/checkpoint
и writer marker фиксируются в одной транзакции; исходная logical верхняя
граница не растет от новых поступлений и не зависит от rowid/VACUUM.
Только после переноса sources выполняется record pass: source-cells заменяет
descriptor после проверки полного исходного window SHA и точного равенства
всех свечей; fallback сохраняет прежний descriptor/hash. Cell coordinates
защищены full-checksum owner record, который сохраняется даже без выигрыша
длины; каждый source block проверяется по actual bytes. Descriptor leases
снимаются атомарно с заменой owner, source leases/policy не сокращаются.
Новый формат требует compatible reader/forward-fix. Native mature/resource
qualification и общий backlog SLO остаются обязательными release gates.
Проверка старого SHA и доказательство cold-only разрешения выполняются одним
проходом; при duplicate legacy revision дополнительно требуется не меньшая
needed_until/purge_after у cold копии. Иначе сохраняется прежний hash/descriptor.
Один синхронный window удерживает один проверенный tile reader; доказательство
существования объектов не переживает этот вызов. Чистый typed-to-canonical-JSON
transform source slots делит прежний1MiB Store cache. Actual header/bytes
проверяются до его использования, JSON round trip сохраняет IEEE bits/int types;
возвращаемые mutable values не разделяются. Полная admission/SQL проверка новых
frames сохраняется; уже проверенные incoming values не парсятся повторно.

#### Market layout v2: устройство

Format2 включается только для нового reset candidate; schema1 не переписывается обычным open. Private tables сохраняются побайтно по значениям SQL rows; `reset_history.prepare` читает только private state, сохраняет private JSON recovery, проверяет restore на отдельном файле и integrity нового candidate. Он не удаляет и не переключает production. Host cutover/CI/artifact/unit/locks остаются отдельной обязательной процедурой.

`market_candles` хранит один closed value/revision с compact numeric payload и source/provenance. Source correction создает новую revision, не меняет used inputs. Coordinate windows с checksum ссылаются на точные revisions. `needed_until` в этом layout — anchor observation time последнего использующего detail; effective expiry вычисляется по актуальной detail policy. `purge_after` — indexed scheduling hint, перепроверяемый перед удалением. Pending holds имеют конечную owner dependency. `market_context` отделяет availability timestamps от shared stable bodies; used context также имеет anchor lease. Старые permanent protected markers в v2 не пишутся.

`runtime` остается rowid table для достаточно крупных JSON envelopes: WITHOUT ROWID вызывал лишние overflow pages. Narrow normalized tables используют WITHOUT ROWID. Frozen `CW2` dictionary + Zstd frame/checksum и SHA256 сохраняют точные исходные JSON bytes; public readers/SQL filters декодируют format. Все изменяемые state/index/counter updates происходят в той же writer transaction. Удаление detail и admission numeric summary атомарны; повтор cleanup не создает duplicate summary. Summary сохраняет все записанные outcome/comparator versions и полный settings/methodology provenance исходной event revision, а не последнего head/default. Численные metrics/comparator outcomes/strength/N/completeness сохраняются без старой event identity, candle/source/context arrays; cohorts/NDJSON/CSV обозначают отсутствие старых details, calendar12mo и UTC-day resolution.

Completed outcome/comparison jobs не дублируют результаты. Bootstrap проверяет immutable heads, explicit recompare восстанавливает job из исходной event revision. Job сохраняет frozen method; недоступная старая формула дает явный INVALID_SOURCE, а не расчет новой формулой под прежним номером. Retention читает metadata без материализации уже истекших raw windows; обычные public readers по-прежнему проверяют полные references. Retry/IN_FLIGHT payload остается immutable; terminal delivery receipt сокращается, успешные attempts/receipts диагностируются1day, event/episode/transport sequence сохраняют dedup в подробном сроке. Pending recovery проверяет существование активного owner hold; отсутствие владельца дает EXPIRED_RETENTION_INCOMPLETE, а не бесконечный backfill.

Category counters обновляются triggers и rollback вместе с данными. Initial small DB index walk bounded; дальше runtime использует cheap counters/files/pages/samples и явно помеченную модель до появления годовых summaries. Exact later index snapshot — отдельная bounded read-only команда. Physical target25GB включает free pages и sidecars; его соблюдение не следует из успешного codec/CI test, backups/experiments отдельно. После expiry incremental vacuum освобождает pages небольшими порциями; полный VACUUM живой истории не запускается.


## Compact history representation

Этот раздел описывает **legacy physical schema1** и retained archive tooling.
Текущий normalized schema2 contract приведен выше; full-history conversion старой
тестовой истории отменена и не запускается для нового накопления.

Mutable runtime обновляется на месте по existing key; byte-identical canonical
payload не вызывает повторной записи. Source recovery сохраняет ключ свечи и
обновляет её значения/provenance в прежней transaction. Rowid — внутренний
physical identifier, не observation/update time и не public event identity.
Изменение write strategy не меняет schema, encoding, retained history или
reader/rollback compatibility с существующим compact-aware runtime.

Во время первоначальной WebSocket subscription phase closed observations временно
буферизуются отдельно от ACK: максимум128 сообщений/2MiB raw bytes на session.
WS shards ограничены100topics и20,000bytes topic list, без сокращения universe.
Deadline ACK15s абсолютный, включая continuously ready receive. При restart worker
по одному восстанавливает complete/current закрытое локальное окно с yield;
неполное, устаревшее или конфликтующее окно требует REST recovery. Local restore
не публикует observation и не превращает сохранённые candles в новые live alerts.
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
Legacy schema1 writer создаётV1 и использует UTC-aligned16-candle blocks; schema2 использует normalized revisions, а не эти полные blocks.
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

Logical `radar:v1:agent:*` namespaces сохраняют logical preference v1 (default OFF),
episode/outcome intents и due indexes, monotonic per-event transport sequence,
immutable canonical serialized bodies/hash, attempts, pending due, target diagnostics
и successful episode notification baseline. Source event revision отделён от transport
revision; original settings/reference/source snapshots не переписываются доставкой.
Secret/HMAC headers/Access credentials в DB не сохраняются. Unknown preference fail-closed.
Source event/episode/outcome и intent записываются одной транзакцией; frozen body появляется
до request. Crash IN_FLIGHT повторяет same bytes/identity с диагностируемой ambiguity.
Logical agent state включен в полный SQLite backup. Это не меняет physical schema2; legacy schema1 reader сохранен отдельно. Routine backup — disk path действующей bounded-storage процедуры.
Compatibility и operational flow — в [agent procedure](runbooks/agent-delivery.md).

## Владение и precedence

Product requirements задаёт [spec](product_scope.md); текущие задачи/Evidence — [план](open_issues.md). Этот контракт уточняет persistence и compatibility, operational gate — [runbook](runbooks/delivery.md).

Один владелец и его private delivery chat. Infrastructure secrets, owner/bot binding и пути — environment/host `.env`. Env процесса выше `.env` (`load_dotenv(override=False)`). Owner settings уже в DB выше initial defaults; Telegram изменение создаёт новую revision без restart. Invalid values не заменяют сохранённые данные и не выводятся в error text. Invalid infrastructure или corrupt owner record блокирует startup с field/class diagnostic.

Retired `.env` keys: ALLOWED_CHAT_IDS, PRICER/CMC, BYBIT_PAIRS/WATCHLIST seeds. Они не включают второй chat и не перезаписывают существующий список. Infrastructure defaults отсутствующих обычных settings описаны `.env.example`; raw legacy payload определяет migration behavior.

### Owner UI state

`ui:v1:input` — один bounded input context владельца (kind/field/market, expected owner revision, время); навигация/cancel очищает его, ввод старше15min или другой revision не применяется. `ui:v1:custom_parameters` — один сохраненный набор собственных параметров перед явным применением preset. Он не является active domain config: только owner restore валидирует и переносит его в owner_settings через CAS. Эти private rows не содержат secrets, не ссылаются на market inputs и не удерживают retention. Обычный all-table backup сохраняет их; migration/schema/default rewrites при добавлении UI не нужны. Единственный in-memory statistics snapshot ограничен20группами и становится недоступен через5min; он не создает persistence jobs.

## SQLite schema1

Историческая physical schema первой JSON→SQLite migration. Ordinary open не
конвертирует ее в schema2; приведенная таблица не заменяет current market tables
выше. Path, lease и WAL integrity правила общие, physical layouts различны.

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

D22 заменяет прежнюю indefinite policy: details30d, small-TF35d плюс ограниченные used dependencies сохраняемых расчетов, quality summaries12mo. Сначала агрегаты, затем automatic bounded cleanup. Settings/methodology provenance summaries сохраняется без raw arrays. Legacy private source/archived chats и backups не входят в market retention/reset; их удаление требует отдельного точного перечня. Backup names уникальны.

До new-runtime write можно восстановить прежний application с сохранённым JSON по approved sequence. После write legacy JSON отстаёт от SQLite: blind rollback приложения теряет новые settings/runtime. Для legacy schema1 нужен verified compatible reader либо forward-fix; для current schema2 schema1 binary запрещен и допустим только verified schema2 reader/forward-fix; DB restore поверх live данных требует отдельного scoped approval и writer quiescence. Наличие файла `.bak` само по себе не подтверждает пригодность восстановления.

## G2A source record contract

`Sources` сохраняет logical source:v1 identity в обоих layouts. В legacy schema1 candle rows находятся в runtime с padded timestamp/prefix index; в current schema2 они хранятся в market_candles с stream/start/revision indexes. Metadata/quarantine/recovery и private owner tables сохраняют отдельное владение. Таблица ниже показывает logical addresses, не обязанность писать raw candles в runtime. Detector memory window ограничен256 закрытыми candles.

| Namespace | Значение |
| --- | --- |
| source:v1:metadata:market/symbol | Validated instrument metadata, classification version, server timestamp |
| source:v1:quarantine:market/symbol | Exclusion reason/version/time; quarantine не становится eligible universe |
| source:v1:candle:market/symbol/TF:start | Closed OHLCV/turnover, interval/start, source, exchange/receipt times; quote units USDT |
| source:v1:gap:market/symbol/TF | Durable start/end historical recovery cursor и PENDING/COMPLETE |
| ingestion | Actual source/market errors, subscription acknowledgements, TF, pending/buffer diagnostics |

Native свечи закрыты по confirm=true либо REST exchange time. Historical/bootstrap/recovered записи сохраняются, но live eligibility — process-local и после restart пуста. Cached window≤256; остальные rows остаются в DB. Online backup schema1 включает все source namespaces. Raw retention35d и используемые snapshots до окончания срока подробных расчетов определены D22; owner/import/backup data не входят в market cleanup.

Settings дополняются typed `watchlist` и `watchlist_multiplier` (default0.75). Отсутствующие fields старой schema1 принимают defaults без переписывания сохранённого owner record; legacy custom_pairs остаются исходным migration evidence и fallback выбранного рынка. Новый список содержит spot/symbol либо linear/symbol; explicit clear очищает оба representations, empty list не получает seed. Legacy JSON watchlist остаётся untyped alias при import, не интерпретируется как новый field. Saved numeric thresholds не заменяются preset. Typed mutes имеют market/symbol; сохранённые legacy symbol mutes действуют только на selected legacy category.

После записи новых owner fields приложение G1 не поддерживает typed payload: автоматического rollback на G1 нет, recovery — forward-fix либо проверенная compatible G2A revision. DB schema downgrade/restore поверх live data не выполняется. Новая versioned physical schema, если понадобится следующим Goals, требует своей явной delivery classification, NONE не подменяет migration gate.

## G2B radar logical schema v1

Physical schema2 использует indexed runtime и normalized source tables; legacy schema1 reader сохранен. Logical record schema `cryptowatcher.radar.v1`, event `cryptowatcher.event.v1`. Online backup включает все namespaces и проверяется all-row readback на isolated copy.

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

Event + episode + decision + initial outbox сохраняются одной transaction до отправки. Consumer замораживает exact event revisions и text; receipt/retry не меняют observation time или TTL. Failed/expired/obsolete attempts остаются history; successful-notification baseline меняется только после ack. Unknown ack отмечается AMBIGUOUS_AFTER_RESTART/UNKNOWN, повтор сохраняет identity. Сохранение attempts/history ограничено D22: terminal receipt сокращается, event/revision/detail retention и dedup сохраняются в пределах соответствующих сроков. Новое подтверждение во время I/O может создать отдельный follow-up после ack; future event revisions не включаются в прежний payload.

Отсутствующие profile/parameters/alert_detectors читаются compatible defaults: custom legacy thresholds и Telegram composite only. Upgrade не записывает новый preset автоматически. Явная owner операция создаёт revision; preset definition полностью validated, manual edit переводит profile в custom. После записи этих fields G1/G2A images не считаются compatible rollback; recovery только forward-fix либо verified compatible G2B application. JSON import не повторяется, эта операция не меняет physical schema и не разрешает DB downgrade.

## G3 outcomes, comparisons и digest — logical schemas v1

Один application writer; logical v1 records работают в current physical schema2 и legacy schema1. `outcome-method:1` / `comparison-method:1` immutable; изменение методики без новой version блокирует работу. `outcome-job:UUID` / timestamp-length-ordered `outcome-pending:` фиксируют event revision, horizon, immutable reference и retries; `comparison-job:event` / `comparison-pending:` — отдельный bounded comparator worker. `outcome-admission` idempotently проходит indexed history; в schema2 admission ограничен текущим подробным сроком и не удерживает всю историю бессрочно. Event transaction включает initial jobs/holds до consumers. Старые snapshots без новых fields читаются agreed defaults5/15/30/60, не будущими текущими owner settings.

`outcome:UUID:revision` / `outcome-head:UUID` и `comparison:UUID:revision` / head сохраняют прежние versions/results и availability timestamps. Неполные outcomes могут получить новую COMPLETE revision после recovery; завершённые results не пересчитываются автоматически. Complete result и references на used inputs записываются атомарно. D22 требует bounded holds только нужных inputs еще сохраняемых details; pending при истечении допустимого срока получает terminal incomplete/expired status и освобождает hold. Aggregate-before-delete retention удаляет expired details/services/references; legacy private JSON/backups не входят в market cleanup.

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
source/outcome snapshots и history хранятся по D22; прежний запрет автоматического удаления отменен. Legacy schema1 и current schema2 различаются; старый binary после compact head writes не является совместимым reader: recovery требует forward-fix либо проверенную версию с этим reader.

`source-context:v1:ticker|instrument:market/symbol:availableTime` содержит public values/metadata, units/source и actual observation/availability timestamps. Event и выбранные comparator windows включают полный использованный context snapshot. Historical composite без prior ticker≤5min не считается no-signal; future liquidity/noise исходного event не используются. `analytics-gap:` / due index персистентно восстанавливает referenced closed ranges без изменения live subscriptions/notification state. Conflicting existing source сохраняется и диагностируется; recovery не перезаписывает его и не создаёт historical live alerts.

Owner horizons/digest interval — validated owner settings; enabled preference/cursor `digest-preferences` отдельно от realtime delivery preference и mutes. `digest-outbox:UUID` содержит stable UTC interval identity, frozen aggregate/text и durable attempts; один `digest-pending` на последний interval, `digest-skipped` обозначает диапазон пропущенных после restart. Enabled change во время network не воскрешает canceled pending; acknowledged prior-preference attempt явно помечается. Shared Telegram target permanent failure виден независимо от каждой queue; explicit alerts/digest enable возобновляет target, не меняя другой toggle.

Read-only export pinning BEGIN/WAL snapshot потоково читает records100/page, только declared public/history/settings namespaces. Events/revisions/episodes/outcomes/settings/provenance и completion counts сохраняются; infrastructure/secrets/outbox/transport attempts не экспортируются. Causal context использует revision availability≤cutoff и не включает позднюю analytics. Методологические ограничения и small N остаются частью результата.

После записи новых outcome_horizons/digest_interval_min G2B payload reader не считается rollback-compatible. Recovery — forward-fix или проверенная compatible G3 application; downgrade/restore поверх live data запрещены. Upgrade/backup fixtures подтверждают исходные bytes прежних event records и all-row restore readback без нового import.

## G5 context и explicit comparator recalculation

Owner `enrichment_enabled` — строгий bool, default OFF при отсутствии старого field; отдельно от alerts/digest/chat state. Mapping revisions `radar:v1:mapping:UUID:revision` фиксируют qualified Spot/Perpetual metadata, candidates и reason/availability. `source-context:v1:oi:linear/symbol:availableTime` хранит public latest5min points/units/server time. `oi-attempt` сохраняет next attempt/error; restart не повторяет весь batch. `enrichment-method:1` immutable; изменение definition без новой version блокируется.

Event `enrichment` — frozen optional context, выбранный по availability≤observation cutoff, с method/mapping revision, instrument identities и per-metric values/units/source/observation/availability/cadence. Historical observation не получает future mapping/OI/ticker; malformed/missing/stale metric остаётся маркированным. Current owner view рассчитывает latest freshness отдельно. Использованные snapshots доступны в event/export в пределах подробного срока; после expiry остаются quality summaries/settings/methodology, без бессрочных source arrays.

Explicit `recompare` сохраняет comparison job original event_revision, повторно удерживает prior input ranges до completion и добавляет recalculation_requested_ms. Предыдущие result revisions доступны по as-of cutoff и export; event/settings/reference не изменяются. Автоматической recalibration/threshold mutation нет. При записи owner payload с enrichment field G3 reader несовместим: разрешён forward-fix/verified G5, без DB downgrade или restore поверх живых данных. Enrichment/recompare не меняют текущую physical schema2; logical v1 не разрешает запуск schema1 binary на schema2.
