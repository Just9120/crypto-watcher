# CryptoWatcher — runtime architecture

## Foundation и текущие boundaries

Один Python 3.11 процесс, один Compose service `crypto_watcher`, persistent SQLite physical schema2 в WAL mode; legacy schema1 поддерживается compatibility reader. Владелец один; settings/domain state не создаются для входящих чужих chats. Trading credentials и LLM отсутствуют.

Bounded BP1 transpose выполняется C99 helper через ctypes при наличии artifact.
ABI3 также строит тот же bounded BR1/BP1 flat layout и предоставляет read-only
SQLite scalar для structural NF/NR/NS2 proof:
Python сначала аутентифицирует BF1 bytes/Zstd bounds и OW mask; native flatten
сохраняет прежние BR1/BP1 shape/identity/length checks. Затем native вызов
читает до64 current parent/ref rows и сверяет неизменные pointer/NR/NS2 значения.
SHA256 использует opaque EVP API уже загруженного Python OpenSSL; NR2 header —
pinned Zstd API. Собственной crypto implementation нет. SQL scalar DIRECTONLY,
extension loading отключается сразу после установки exact trusted artifact;
один вызов проверяет до8frames (temporary derivative<=1153737B), повторно
использует statement/EVP context внутри вызова и освобождает их на любом выходе,
не оставляет DB pointers/cache
и не выполняет writes. Общий pure cache1MiB и50s boundary budget сохранены.
Canonical native build и Docker image собирают helper из той же source revision;
installed bad ABI/error не подменяется portable fallback.

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
| Storage control | `storage_meter.py`, `storage_model.py`, `storage_repack.py` | Cheap counters/samples, separated occupancy/retained-flow forecast, episode/meaningful-membership model и durable bounded lossless repack |
| Capacity evidence | `capacity.py`, `scripts/observe_capacity.py`, `scripts/rehearse_retention.py` | Readonly boundary ledger/cardinality/pages, finite host observation после stability и offline full lifecycle; tests/CD отдельно от35GB proof |
| Agent consumer | `agent_contract.py`, `agent_delivery.py` | Полный public snapshot/hash, independent immutable outbox/retries/ack и owner diagnostics |
| Digest / optional context | `digest.py`, `enrichment.py`, `enrichment_runtime.py` | Independent period delivery и qualified derivatives mapping/source freshness |
| Market adapter / computation | `cryptowatcher/market.py` | Общий async HTTP client, bounded REST requests; closed-candle composite, gaps/invalid data diagnostics |
| Detectors / profiles | `detector_config.py`, `detectors.py` | Validated explicit presets/manual values; five pure versioned formulas без I/O, previous-only baselines |
| History / episode producer | `events.py` | Atomic events/inputs/revisions, episode lifecycle/re-arm, notification decisions и independent pending work |
| Telegram consumer | `telegram_delivery.py` | Bounded durable attempts, retry/expiry/config checks; network вне DB transaction, successful state после ack |
| Views | `cryptowatcher/render.py`, `telegram_views.py` | Compact status/settings/alert, readable units/time, market links и keyboards без изменения domain data |
| Telegram control deck | `cryptowatcher/telegram_ui.py` | Read-only navigation, explicit actions, bounded history/statistics pages, owner input/CAS/cancel и full-report downloads |
| Telegram application | `cryptowatcher/bot.py` | Один guard до каждого entrypoint, revision snapshot вокруг await, owner control deck, send result |
| Entrypoint / operations | `cryptowatcher/__main__.py` | CLI run/config/diagnostics/health/state-check/migrate/backup; safe error output |
| Compatibility | `telegram_crypto_watcher.py` | Сохраняет прежний startup entrypoint; private legacy Python helpers не являются объявленным API |

Owner configuration loop применяет смену selected source scope независимо от REST discovery/backoff. Reconcile сериализован; завершившийся старый REST refresh перечитывает owner revision и не возвращает прежний scope. Диагностика отдельно показывает revision applied scope, metadata readiness, freshness и completeness.

Штатный WS observer имеет зарегистрированную форму, присоединяющуюся к source
transaction. Закрытая свеча, EventEngine и consumer intents фиксируются одним
commit; prospective source window используется только синхронно, без await.
Опубликованный RAM window/live/gap state меняется после успешного commit.
Ошибка consumer откатывает эту единицу целиком. Standalone/replaced observers
сохраняют прежний вызов после source commit. Нового durable queue или формата нет.

Readonly outcome/comparison preparation выполняется последовательно. Controller
сохраняет256/64result batches и four publication lanes; comparison preparation
получает4s вместо2s, чтобы использовать уже подготовленную frozen population
до открытия следующего snapshot. Standalone defaults не меняются. Это soft
межоперационный deadline: один начатый расчёт по-прежнему может его превысить.
Live catalogue inbox обслуживает тела revisions и deferred heads/aliases по
очереди: durable next_lane сохраняет первую очередь следующего40ms slice. Обе
части используют общий прежний deadline,256unique rows и прежний buffer bound.
Один начатый body/flush может превысить soft deadline; следующая очередь получает
первый ход после commit/restart. Head/alias, чей выбранный body ещё ожидает transfer,
сохраняется в inbox; CAS/private equality/retention и atomic rollback неизменны.
Диагностика freshness читает актуальную свечу по максимальному validated start
в source window одного TF; новый deadline не кешируется.

CPU/active-wall stage counters исключают вложенные measured stages из parent,
поэтому source и observation work не суммируются дважды. Фактическая устойчивость
и ресурсный бюджет подтверждаются отдельно по [плану](open_issues.md).

Retirement прежних WS tasks ограничен15s: при timeout ownership/groups сохраняются,
replacement не запускается до их завершения. Manager и configuration loop повторяют
незавершённый reconcile даже при уже применённом active stream set/revision.
Неожиданная ошибка refresh сохраняет безопасный class/code/count/time и не завершает
discovery task; cancellation по-прежнему распространяется. Независимая диагностика
показывает named tasks, STOPPING/FAILED, соответствие groups/sessions/connections и
возраст последнего успешного metadata/ticker refresh для обоих рынков.
Cadence600/300s сохраняется; допустимый refresh grace60s не меняет HTTP/backoff policy.
Delivery и clean capacity baseline требуют актуальных discovery и согласованного scope.

Network I/O выполняется вне DB transactions. Snapshot до market fetch имеет revision; смена settings во время fetch отменяет применение результата. Alerts маркируют использованную revision. SQLite update использует compare-and-swap; отправка сообщения не удерживает DB/state lock.

Analytics calculations выполняются через `analytics_worker.py` в отдельном thread с собственным read-only WAL snapshot connection и `query_only`. Application event loop остаётся единственным writer: применяет результат только при совпадении сохранённой job с прочитанной, атомарно сохраняет revision/protected sources/intents и фиксирует фактический completion time как availability. Отмена reader не меняет pending work. Outcome/comparator batches ограничены256/64 jobs и2s между завершёнными calculations; отдельный долгий calculation не обрывается с fabricated metrics. Готовая партия распределяется между максимум четырьмя конечными coroutine на том же event loop; каждая берёт очередной результат из общего iterator, выполняет синхронную CAS/apply transaction и делает yield. Записи SQL не перекрываются; failure/cancellation прекращает и дожидается всех coroutine до выхода из партии. Это уменьшает задержку готовой аналитики среди множества ready WebSocket sessions без нового writer, очереди или изменения batch/read limits. Telegram analytics/diagnostics также читаются в отдельном snapshot; дорогие diagnostic counts имеют общий3s budget и показывают PENDING вместо выдуманного0. WS/recovery snapshot публикуется отдельным5s task независимо от REST discovery/backoff; возраст snapshot виден явно.

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

Context tile publication проверяет duplicate hot/cold ownership через один
декодированный список времени и indexed point lookups. `CROSS JOIN` фиксирует
порядок обхода: существующий hot prefix не запускает повторный decode пакета.
Explicit installer атомарно заменяет только известный прежний trigger; неизвестный
trigger сохраняется и блокирует upgrade. Открытие Store не меняет schema/guards.

Image: pinned Python base digest, hash-locked dependencies, UID/GID10001, read-only
root, no capabilities. `/data/sqlite` и backups принадлежат приложению; исходный
JSON migration archive сохраняется. Health проверяет recent heartbeat, а market
availability и обе delivery — отдельные diagnostics. Runtime hard resource limits
не назначены без измеренного основания.

Routine schema2 delivery выполняет full source integrity и SQLite online backup
на диске, full backup integrity и streaming SHA256. Large-schema2 использует
приватный native disk capture с полным CHECK/FK перед encoding и независимым
readback стандартного ZSTD archive. Временный raw duplicate и disk reserves
входят в admission; failed capture сохраняется, qualified duplicate удаляется
после proof. Вся DB в RAM/tmpfs не копируется.
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
При memory gap current recovery запрашивает существующее bounded окно<=256candle
только affected stream: короткий calculation tail не чинит старую дыру в cache.
Если после REST окно остается blocked, сохраняется5s retry; это не successful
recovery. Обычный contiguous bootstrap остается коротким; старые данные никогда
не получают live eligibility. Лимиты окна/RAM/shared REST budget неизменны.


Текущий detector window восстанавливается первым. Длинный outage оставляет durable historical gap cursor; каждый worker берёт только одну страницу, уступая более важной recovery. Неполная historical page диагностируется, а missing outcome data не превращаются в zero return. Source retention/protection и outcome completeness реализует G3 worker с durable admission/holds.

Delivery readiness требует discovery обоих рынков, успешные subscription acks и хотя бы один fresh usable window каждого active market/TF. Полный initial bootstrap продолжает работу; `WARMING` не проходит existing receiver post-check. После bootstrap статус различает data errors и no-signal. Worker failure влияет на health; endpoint/clock/rate problems видны отдельно.

## G2B event → episode → consumer

`condition_state.py` объединяет до восьми mutable condition states одного stream
в одну bounded запись с независимыми member deadlines. Batch живёт только в
короткой writer transaction, не создаёт cache/thread/job и открывает согласованную
SQL-видимость перед synchronous callbacks. После callback перечитывается state;
конфликт legacy/group membership откатывает всю observation. `History` сохраняет
логический интерфейс, а retention отдельно истекает members. Неизвестные legacy
shapes не мигрируют. Формат/recovery границы описаны в
[state contract](state_contract.md#mutable-condition-state-v1).

Существующий ingestion work timing дополнен синхронными стадиями current recovery
source writes, referenced response validation/publication, repack slices и retention.
Каждая стадия измеряет active wall/thread CPU и failures без await внутри scope;
счётчики публикуются в уже существующем diagnostics snapshot. Они исключают REST
waits, recovery selection/retirement и shared readonly preparation; остаток CPU и
полный I/O определяются совместно с host/cgroup counters. Это attribution,
не доказательство steady throughput или нового ресурсного лимита.

Persisted closed observation запускает five detectors на одном settings snapshot. Ошибка family записывает reason/class без sensitive details; другие families продолжаются. Source inputs, immutable outcome reference и provenance сохраняются вместе с event/episode/decision/outbox в короткой transaction. Active episode prefix index ограничивает lifecycle работу текущим instrument; глобальный2s tick читает только active index. Recent history и pending consumers используют indexed prefix ranges; network не удерживает writer.

History всех families независима от Telegram selection/mute. Coalescing2s фиксирует episode snapshot и exact event revisions; text замораживается перед первой попыткой. Новые observations во время I/O остаются в history; после ack reconciliation создаёт meaningful follow-up. Settings revisions не смешиваются в pending payload/normalized-strength baseline. Исторический outcome update в будущем не может изменить уже frozen live context. Telegram batch≤20; retries переиндексируются по следующему attempt time и уступают более ранним ready deliveries.

Restart восстанавливает episodes, per-direction conditions и pending work; exchange clock устанавливается до expiry решения. Незавершённый IN_FLIGHT означает ambiguous ack, поэтому exactly-once не заявляется. Forced closure подавляет старое условие до re-arm; complete recovered non-signal bars могут подтвердить re-arm без historical live flood. Bootstrap/recovery сохраняют последнее проверенное наблюдение как historical; G2B не выполняет ретроспективный detector backtest всех raw candles.

## LIGHT-RADAR: episode analytics (текущая implementation-ветка)

Новые owner writes используют general candle window3days/72h и detail retention30d;
outcome horizons>4320min отклоняются до изменения state. Исторические snapshots
остаются читаемыми. Store.initialize не мигрирует существующий private state.
Только явный light transition сохраняет original private recovery, независимо
проверяет restore и меняет policy fields в candidate. Used source leases сохраняют
необходимые inputs до expiry details; general candles/context удаляются на72h.
Критические boundaries и полный72h outcome проверены controlled-clock tests.


Persisted active_detectors управляет domain admission отдельно от Telegram
alert_detectors. Settings содержит отдельный экран explicit on/off с owner access
и CAS revision; старые detector:* callbacks остаются notification-only.
Runtime читает актуальную activation в каждой observation. Отключение не удаляет
прежнюю history и не отменяет уже принятые episode outcomes. Status/diagnostics
различают поддерживаемые families, активные расчёты и разрешённые уведомления.


Новые события явно фиксируют outcome_unit=EPISODE и outcome_methodology_version=2.
Первая reference point и первый набор horizons неизменны. Повторное подтверждение
без значимого изменения не создаёт immutable event revision или новый расчёт.
Статистика связывает первый outcome с первыми revisions всех meaningful
подтверждений episode; внутри выбранной группы N уникален. Detector/TF/settings
группы пересекаются, их N не складывается. Исторический cutoff учитывает availability
самих подтверждений. Historical и suppressed наблюдения используют свои
persisted bindings и тот же meaningful gate. Для recovery учитывается market-clock
progress, для остановившейся обработки — receipt inactivity; restart не создаёт
новую исследовательскую цепочку для каждой следующей старой свечи. При fresh
historical→live promotion сохраняется origin/outcome set, первые live confirmations
получают отдельный meaningful update и могут создать первый свежий alert.
Historical closure не блокирует live re-arm и не порождает новый agent intent. Legacy event-reference results сохраняют собственную единицу.

После30d details новая модель сохраняет в той же SQLite числовые results по
episode/horizon/method, отдельные group memberships и общую provenance dictionary.
Полные settings/definitions не копируются для каждого result. FK/refcounts и
constant-size counters изменяются в одной transaction с удалением details;
неиспользуемая provenance удаляется bounded GC через partial index.
Ни свечи, ни source arrays, ни decision contexts в12month summaries не входят.
Expiry читает saved metadata/numeric metrics, не восстанавливает истёкшие окна.
При поэтапном expiry live membership продолжает использовать уже сохранённый
числовой result; это сохраняет N на границе удаления первого observation.
SQL aggregates и CSV/NDJSON используют одинаковые episode units и UTC-day policy
архивной части; quantitative facts и методология доступны без старых карточек.

Установка summary schema additive только на writable connection; readonly её
не создаёт. Старые market_quality codecs/readers остаются для legacy event facts,
method versions и comparator distributions. Их наличие не разрешает новый N20:
автоматический admission и runtime worker удалены. Старые manual commands/callbacks
ведут к описательной статистике, не создавая jobs. Canonical storage/retention scripts используют episode origins и unique membership;
N20 codec qualification запускается только явно как legacy. Finite offered/saved/due/delivery
replay проверяет новый путь отдельно от production throughput и суточной приёмки.

### Прогноз фактически включённой модели

Storage sample format13 связывает окно с settings и history generation; старые
samples сохраняются, но другая модель/конфигурация не задаёт частоту новой.
Episode counter увеличивается только при первой immutable revision episode,
meaningful event counter — при первой revision значимого observation. Годовая
модель считает один набор frozen horizons на episode плюс membership для
значимых events; пересекающиеся группы не умножают численные results.

`storage_model.measure_episodes` использует настоящий SQLite writer и все его
таблицы/index pages. Synthetic qualification включает полную отдельную provenance
для каждого origin/member без выгоды от sharing. Коэффициенты округлены вверх
от измерений512episodes с1/4/20members:2048physical bytes/result и2048/member,
1024logical bytes/result и1536/member. Это явно заданный сценарий, не bound:
реальная полнота definitions/settings, SQLite allocation и population mix
могут отличаться. Runtime не запускает синтетические измерения или dbstat scan.
Observed annual archive flow, если он больше модели, не отбрасывается.

Двухстрочный `market_source_usage` учитывает canonical byte weight уникальных
новых inputs и их первого lease в той же transaction. Transfer hot→tile,
повторное чтение, продление lease и rollback не создают повторных arrivals/usage.
Доля first-used/arrived применяется к дополнительному30d ownership сверх3d
общего окна. Для свечей conservatively учитывается ещё native lookback≤3d,
для context — использованный30d lease; N20 prior28d отсутствует. При отсутствии
или несогласованности счётчиков предполагаются100% used inputs. Доля canonical
bytes не является измерением physical compression; запоздалые first leases,
короткое окно и смена активности остаются явными ограничениями прогноза.

Non-summary categories используют actual flow/net occupancy с measured page
factor. Summary model уже включает собственные indexes: второй page factor к
нему не применяется. Free pages добавляются отдельно; journal reserve берёт максимум
текущих sidecars, наблюдавшегося peak в том же scope и сценария auto-checkpoint.
Auto-checkpoint — trigger, не cap WAL: долгие readers/backup требуют отдельной
проверки реального пика. Actual working files задают нижнюю границу.25GB — желательный ориентир,35GB — hard limit.
Backup/temp/restore copies и резерв остальных проектов в эти35GB не входят:
полный физический пик квалифицируется отдельно до тяжёлой серверной операции.
Этот прогноз и finite replay не подтверждают live capacity или30min/24h DoD.

## G3 legacy analytics и independent digest

N20/comparison readers и прежние вычислительные helpers ниже сохраняются для legacy
данных/fixtures; scheduler новой модели не запускает эти исследования. Current
episode outcome/summary path описан в разделе LIGHT-RADAR. Digest остаётся отдельным
consumer, количество и ranking в нём считаются по уникальным episodes.


`outcomes.py` принимает jobs в event transaction, затем bounded worker вычисляет полные future1m окна без смены reference; `analytics.py` вычисляет frozen prior comparator для каждого события в ограниченном batch и отделяет causal readout от late analytics. Cohort distributions сортирует/агрегирует SQLite, exporter не собирает все raw returns в памяти. Telegram/CLI views имеют явные ограничения выдачи; streaming export через отдельный readonly connection не усечён и pinning snapshot проверен concurrent-writer fixture.

В одном readonly transaction comparison worker держит одну prior population.
Для того же stream/шага/warmup/data version при движении обоих bounds вперёд
он сохраняет любую пересекающуюся часть и читает только новый tail, в том числе
для редких admissions дальше256native bars. Tail является подмножеством прежнего
разрешённого range, размер28day window с warmup не меняется. ATR/quantiles вычисляются
заново в прежнем порядке над точными свечами этого range. Обратный cutoff, отсутствие
пересечения или смена cache identity требуют полного чтения. Writer не использует эту
population; новый snapshot видит committed recovery через data version. Размеры
byte/range caches, CAS apply, methods и deadlines не меняются.

`source_context.py` архивирует public metadata/ticker availability; historical formula получает доступную тогда liquidity/tick context. Unavailable historical input означает limitation, не future substitution. `analytics_recovery.py` получает один referenced page через существующий HTTP client/budget; live gaps/bootstrap имеют приоритет. Он не создаёт memory windows/WS subscriptions inactive instruments и не переписывает conflicting used source. Outcome/retention holds переживают restart.
Retirement pending jobs сохраняет свой64/25ms quantum; existing REST admission
32pages/1s начинается после него. Scheduler delay во время retirement не должен
поглощать весь REST budget. Live priority проверяется до каждой страницы; shared
rate/concurrency, checkpoint-before-yield, retries и cancellation остаются прежними.

`digest.py` независимо от realtime alerts/mutes агрегирует общую event history обоих рынков, strongest period episodes и ongoing snapshot. Period identity, cursor/skip range, immutable text и attempts persist before send; network вне writer. Один pending latest period предотвращает historical burst после длительного outage. Disabled/reconfigured preference during I/O сохраняет фактический attempt с prior-preference/canceled result. Permanent Telegram target общий, enabled preferences раздельны.

Controller orchestration запускает bounded analytics job5s, episode/consumer delivery2s и heartbeat20s. Source errors, incomplete horizons, reference recovery и delivery target имеют отдельные diagnostics; analytics worker failure влияет на health. Отсутствие выгодной/большой live выборки не является failure расчёта. Logical v1 namespaces не определяют physical schema; schema2 и compatibility/recovery ограничения перечислены в [state contract](state_contract.md).

## G5 optional enrichment и manual calibration

`enrichment.py` содержит pure exact-asset mapping и per-metric calculations/units/freshness; `enrichment_runtime.py` использует общий BybitRadar client/budget. Owner setting первоначально OFF. При включении ticker refresh обоих рынков coalesced lock/cadence1min, OI latest2 points batch≤4/worker2s, cadence5min. Optional work уступает current recovery; failures остаются локальными, не меняя core heartbeat/Spot formula. Immutable mapping revisions и public OI availability переживают restart; ambiguous aliases не угадываются. Диагностика явно показывает mapping и ошибки OI с ограничением выдачи.

Event callback синхронно выбирает mapping/ticker/OI, доступные до candle observation cutoff, и сохраняет snapshot в общей event transaction. Новые current metrics/outcomes/retries не заменяют прежний context. Funding%/next funding UTCms, OI BASE/asset и5min absolute/percent change, snapshot basis с individual times сохраняются независимо; stale/missing metric не скрывает другие. Perpetual event продолжает самостоятельный detector path. Current owner карточка отдельно показывает latest snapshot, alert не добавляет выключенный/N/A context.

Analytics grouping включает frozen profile, detector/version/settings/market/TF/horizon. Owner `/recompare` requeues только prior comparator, сохраняя исходный event revision/settings/reference и прежнюю analytics revision. Новые inputs/availability дают отдельный late result, строго prior observation; без owner действия completed comparison не меняется. Никакой threshold write не выполняется. NDJSON включает frozen event context и methodology definitions; CSV сохраняет profile grouping. Эта функция не требует новой physical migration; schema2 и forward recovery остаются действующими.

## Bounded market layout v2 (текущий storage этап)

Нормализованные candle/context revisions и shared settings питают прежние detector/outcome formulas без копирования полных windows. History/consumer APIs сохраняются; serialized signed bytes не пересобираются при retry. `market_storage.py`, `record_codec.py`, `quality_summary.py`, `storage_lifecycle.py` и `storage_meter.py` реализуют этот формат. Подробные ownership/expiry/reset contracts: [state contract](state_contract.md#market-layout-v2-новый-период). Schema1 остается legacy compatibility input; fresh schema2 создается только отдельной reset preparation, не ordinary startup. Текущий runtime уже использует schema2; actual reset/delivery Evidence находится в плане.

Automatic cleanup использует expiry/GC indexes и bounded batches вместо обхода всего архива. Record body refs и context body refs имеют transactional counters. Quality summaries сохраняются до удаления подробностей; pending service/recovery ownership имеет конечный срок. Контроль объема отделяет измеренные bytes/counts, extrapolation и данные которые еще не подтверждены. Наличие этих modules само по себе не подтверждает поставку/reset; их завершение проверяется по primary CI/CD и runtime Evidence плана. Повторный reset обычным restart/deploy запрещен.

Repack и analytics backfill имеют отдельные single-instance jobs с cadence1s;
они не ждут завершения долгих outcome/comparator calculations. Все записи выполняет
тот же writer, network вне transactions, между slices/pages есть scheduler yield.
Repack фиксирует конечные key bounds с durable cursor и проверяет уже целевой typed
envelope без повторного сжатия; новые записи получают compact format при сохранении.
Backfill ограничен32jobs/проход и1s admission budget (один уже начатый HTTP request
ограничен прежним timeout клиента), общий REST5req/s/concurrency4 не изменяется.
Current recovery перепроверяется между pages, superseded in-flight job не оживает.
Ошибки независимых workers явно блокируют stable capacity baseline.
Maintenance ждет fresh usable OK/DEGRADED poll именно текущего запуска и нулевой live
bootstrap/recovery/blocked queue; старый сохраненный OK не разрешает раннюю работу.
Production repack первые600s процесса использует один40ms slice/1s tick.
После startup допускает до4 прежних slices при coverage>=95%, ingestion age<=5s,
poll age<=120s, analytics OK/current<=60s и отсутствии фактического live REST work.
Каждый slice имеет отдельный checkpoint/yield; между ними перепроверяются guards.
Общий intended wall deadline200ms ограничивает burst, отдельный SQL/commit может
превысить time check. COMPLETE прекращает лишние slices. При потере условий остается
один slice; общий startup/current recovery guard сохраняется. RAM/cache не меняются.
Тот же существующий1s maintenance callback после startup/initial reclaim сначала
обслуживает backlog, подтверждённый successful retention telemetry не старше60s:
до4прежних retention calls с0.5s admission window. Каждый call сохраняет50ms
admission и atomic archive-before-delete; отдельный event/SQL может превысить time
check. Между calls выполняется yield и повторная проверка live recovery/coverage/
analytics freshness; analytics lock приостанавливает priority. Обычный analytics
path и priority делят одну retention lock. Новые callbacks/timers не создаются.
При backlog выполняется один40ms repack slice без повторной parent-bundle
compaction уже истекающих details; transfer/GC/остальные phases продолжаются,
pending_work остаётся видимым. Initial compaction/reclaim не откладывается.
После очистки backlog обычная compaction возобновляется. Retention failure виден
в singleton worker/diagnostics и блокирует clean baseline; successful regular
call снимает failure. RAM4GiB/CPU1, retained data/N/settings и cache неизменны.

Фазы records/decisions/expiry/quality имеют один deadline и сохраняют очередность
первого хода. Slow SQL может превысить time check, но не вытесняет всегда одну фазу.
Records/decision/quality scans фиксируют physical high water и следующий rowid.
Прежний logical cursor сохраняется как неизменный floor; display cursor после
перехода не используется как новый floor. Ограниченный SQL читает не более batch headers,
CASE не загружает payload вне scope. Sparse/reused IDs не расширяют конечный interval.
Каждый slice гарантирует первый header даже после slow SELECT, затем проверяет
deadline. Обычный restart сохраняет позицию. Чтение schema_version обнаруживает
DDL/VACUUM и консервативно начинает recheck оставшегося logical scope с новой
physical границей; уже преобразованные public данные сохраняются. Публичные IDs
остаются ключами; rowid не становится API identity. Полного scan/копии в startup нет.
Readiness в начале процесса обновляется отдельным5s job до первого OK/usable
DEGRADED poll; затем job no-op. BybitRadar.snapshot читает shared cached observations,
не выполняет REST и не публикует events. Normal owner poll cadence и legacy REST
adapter не ускоряются; first-ready marker не переносится между restart.
После first-ready cached UI WARMING больше не останавливает maintenance при idle
live queues; реальные bootstrap/recovery/blocked состояния по-прежнему приоритетны.
Новые maintenance/status jobs допускают один coalesced late tick в пределах30s
и max_instances1, без replay всех пропущенных slots. Freshness при отправке не меняется.

Storage extension3 сохраняет physical schema2 и public contracts. Legacy expiry/quality
rows остаются видимыми через compatibility views до bounded in-place drain. Internal
row IDs/shared dimensions/provenance не заменяют внешние event IDs. Recovery убирает
уже заполненные local ranges без REST и coalesce overlapping work, сохраняя source
conflict checks/owner expiry. Детали upgrade/rollback boundaries — в [state contract](state_contract.md#storage-extension3-совместимое-уплотнение).

QF1 хранит logical CQ5 packets в shared IF1 containers с exact numeric leases.
Logical domain consumers читают record packets через `domain_packets.read`;
generic `packet_store.read` сохраняет opaque physical bytes, hash и binding
contract для publication/relocation. Legacy read остается byte-identical.
Explicit `shared_records.install` поддерживает DS2 descriptors и immutable
RP1/SB1 groups с FK members, full logical transition proof и CAS в существующем
writer savepoint. Store/startup не устанавливают формат. Резервированный tail
на retention day отделяет48byte descriptors от обычного packet admission;
проверки append positions/digests остаются общими. Явный known2-to3 upgrade
добавляет конечную discovery scan, live inbox и одну resumable staged group.
Compression готовит один read-only task; существующий maintenance callback
переводит по одному пакету с full equality/CAS и сохраняет старый источник до
атомарного перехода. Pending edges защищают группу от GC. Sparse groups
возвращаются через обычный catalogue relocation; первые timestamps очереди
сохраняются. Worker shutdown присоединяет фактический reader без поздней записи.
Observer различает initial reclaim, live coalesce/service deadline, stale clock,
missing cleanup checkpoint и writer switch; boundary читает actual DS2 bytes.
Новые counters/indexes и временный DB2 staging входят в physical storage model.
Opt-in SB2 восстанавливает исходные typed bytes через frozen integer-unit transform;
сжатый вектор размеров каждого slot подтверждает полный прежний allocation bound.
Выбор slot не требует восстановления остальных записей. SB1/SV1 читаются как прежде,
RP1 сохраняет свой frozen SB1 container; shared cache остаётся1MiB. Подготовка заранее
проверяет полный512KiB DB3 wrapper и уменьшает prefix до staging при переполнении.
Если даже singleton достигает исходного предела, группа сохраняет его два
исходных authenticated IF1 frame и возвращает byte-identical DB2 без дополнительной
view wrapper. Только paired IF1/IF1 иinitial1 допустимы; mixed formats/partial spans
отклоняются, current digest/bindings и typed equality проверяются как прежде.
Это явный fallback без обещания compression saving, нового schema или увеличения
512KiB/2MiB/cache limits; запись не пропускается и очередь продолжает работу.
Store/startup не устанавливают shared schema. После явной offline активации layout3
Controller предпочитает SB2, когда он меньше SB1; библиотечный Worker без явного
параметра сохраняет SB1. Existing writer switch0 сохраняет паузу и при повторной
операторской команде. Readonly `shared-storage-status` дает согласованный token
generation/config/private; `enable-shared-storage` требует этот exact token,
совпадающий source и exclusive RuntimeLease, делает полный обычный backup до DDL,
проверяет identity/reopen и сохраняет все прежние файлы. Известная частичная layout2
возобновляется только совместимым artifact; старый image после DDL не является recovery.
Context timestamp projection сохраняет только компактный immutable index в прежнем
shared1MiB cache. Если stats/points уже проверили full values, индекс использует этот
результат; отдельный index miss не удерживает full JSON. Каждый вызов вновь проверяет
актуальные header/leases/mask/seal и payload bytes; объекты и SQL proofs не кэшируются.
SQL registry pack/unpack используют тот же pure1MiB/FIFO budget для frozen
преобразований полного ключа. Cached result сохраняет исходный str/bytes type;
IDs, строки БД, ownership и leases читаются заново. Plain connection без cache
сохраняет прежние codecs. Observation producer получает ticker/instrument и
optional enrichment один раз при первом новом событии в синхронном callback,
с теми же captured cutoff. Все новые families используют этот snapshot;
пустой/duplicate callback не материализует context. Ошибка provider откатывает
всю запись observation, прежний eager input продолжает поддерживаться.

После отдельной явной adoption source marker2 разрешает optional SB3 для старых
source tiles. Normal new/hot writer сохраняет SB1/SB2; последние256bars остаются
в быстром формате. SB3 хранит все исходные типы/float values/signed zero и leases
в bounded32slot/256row/512KiB authenticated envelope. Перекодирование выбирается
только при меньшем размере и полной canonical equality. Private synchronous
authority разрешает exact CAS delete/reinsert до8проверенных строк с прежними
rowids; guards/counters/FK действуют, ошибка откатывает весь batch. Прямое удаление
retained sources по-прежнему запрещено. Normal corrections/lease updates сохраняют
SB3; mixed old/current readers и обычная expiry policy остаются общими.
Existing maintenance cadence выполняет конечный initial scan/reclaim и последующие
проходы; observer отдельно учитывает их незавершённость. Store/startup не меняют
source marker. Operator backup/token/900s/forward recovery охватывает shared
и source adoption; current full capacity qualification требуется до её выполнения.
Store/startup не активируют3; current native/image, runtime activation/recovery,
Linux/native совместный50s boundary и mature25GB еще требуют qualification перед production.
Тот же maintenance worker обслуживает конечную initial scan, live eligibility
queue и partial-slot GC. Открытый expiry-frontier tail остаётся CQ5 до закрытия
UTC дня; его факты уже доступны. Body initial reclaim сохраняется при upgrade,
поэтому retention priority может работать параллельно с finite frame pass.
Observer отдельно требует frame initial reclaim; overdue/unknown clock не
становятся current из-за нового checkpoint. Capacity counters различают CQ5
staging, retained QF1 и transient queue, включая новые physical indexes.

Перед baseline finite capacity observer перепроверяет только единственный blocker
ANALYTICS_RECOVERY_PENDING максимум3 раза с10s pause, quick read-only probes без
deep scans. Это устраняет привязку одного5min sample к краткой current-candle queue.
Каждый quick probe внутри sample имеет durable PENDING перед вызовом и полный
snapshot либо sanitized ERROR после; restart закрывает незавершённый subprobe как
INTERRUPTED. Уже captured snapshots не теряются при ошибке следующей перепроверки.
Актуальная очередь должна быть действительно пуста. Main loop сохраняет300s pause,
минимум две чистые проверки и не менее30min устойчивости, а также
исходную deep boundary/12h wait policy. Другие blockers не retry; после baseline
каждый interruption остается видимым. Resource caps и24h boundary не меняются.
