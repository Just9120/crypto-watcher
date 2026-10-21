# Ограниченное хранение и разовый reset market history

## Основание и границы

Поручение владельца2026-10-16 разрешает удалить старую тестовую рыночную историю CryptoWatcher после исправлений и проверок. Это EXPLICITLY_GATED операция, не startup migration. Старый full-history conversion отменен. Правила хранения заданы в [D22](../product_scope.md#d22--новое-накопление-и-bounded-retention-2026-10-16).

Target: Contabo167.86.68.98, `/opt/crypto_watcher`, Compose project/service `crypto_watcher`, container `crypto-watcher`. Другие проекты не затрагиваются. Все Docker операции используют локальный daemon socket; SSH host verification и existing deploy lock сохраняются.

## Точный scope reset

Прежний working `/opt/crypto_watcher/data/sqlite/cryptowatcher.sqlite3` и существующие его `-wal`, `-shm`, `-journal` заменяются новой schema2. После успешных post-checks удаляются только эти прежние файлы, временно переименованные в созданный для операции `history-reset-*/discarded-market.sqlite3{suffix}`. Иные имена не допускаются.

Удаляются старые events/revisions/heads/history indexes, episodes/conditions, outcomes/comparisons/sources/context, market metadata/runtime, outboxes/attempts/pending indexes, recovery gaps/cursors, bootstrap/delivery/digest cursors и временный conversion journal. Они не импортируются в новую БД. Новая generation отмечает начало истории; observations до нее не создают episodes/events/live alerts. Bootstrap берет только нужные расчетам lookbacks; analytics явно показывает малый N/missing history.

Сохраняются дословно SQL rows `owner_settings`, `delivery_preferences`, `mutes`, `legacy_import`; runtime preferences `radar:v1:digest-preferences`, `radar:v1:agent:preference`. Только `digest-preferences.next_end_ms` переносится к следующему новому interval, чтобы старый digest не отправился заново. Watchlist, thresholds, profiles, enabled/toggles, пользовательские настройки сохраняются. `.env`, credentials, SSH/GitHub настройки, migration receipt, старые backups/rehearsals/experiments не очищаются и не заменяются.

## Проверки и исполнение

1. Рабочая ветка от проверенного main; required `ci-required` на latest PR revision. Regression suite: analytics equality, replay/restart, pending/delivery, controlled retention cycle, aggregate-before-delete, metering, reset/fault/private restore. Linux image fixture проверяет реальную schema2, сохранность private settings, dbstat и disk backup без API calls.
2. После PASS точный installer [install_history_reset.py](../../scripts/install_history_reset.py) проверяет primary PR/base/head/CI, pinned hashes receiver/helper, текущий runtime/source inode/private signature/config hash. `--execute --approve-reset-test-history` устанавливает root-owned [receiver](../../scripts/deploy_receiver.py)/[reset helper](../../scripts/reset_delivery.py), сохраняя прежний receiver; arm root0600 permit с exact Git tree и predecessor. Операцию уже разрешил владелец; новые параметры/target не подставлять по предположению. Installer не останавливает бот и не меняет БД.
3. Merge по protections запускает trusted build; отдельный production job имеет timeout15min. Общий передаваемый deadline включает checkout/artifact/SSH/backup/reset/post-checks, не продлевается отдельными stages. Весь receiver<=900s, cleanup reserve30s. CI/build выполняются GitHub Actions, на VPS — только delivery/pre/postchecks.
4. Receiver проверяет artifact/source/main/tree/target/lock. Только matching armed permit разрешает пропуск scan/backup **удаляемой тестовой истории**; это не утверждение о ее integrity. Останавливается один старый writer. [prepare](../../cryptowatcher/reset_history.py) читает только private rows, сохраняет private0600 recovery, проверяет его независимым restore и fresh SQLite integrity/reopen. Candidate<=32MiB, CPU1/RAM512MiB/no swap/network none.
5. Перед promotion проверяются закрытый candidate hash, source inode/ownership/отсутствие handles и точные sidecars. Durable record фиксирует начало promotion до rename. Новый container запускается из verified artifact/Compose. Post-checks: source/image/health, fresh usable data, config revision, schema2/generation и полный private readback (кроме advancing digest cursor). Лишь после них прежние working files удаляются по повторной identity check. Permit CONSUMED предотвращает повторный reset.
6. Readback primary deployment record и bounded runtime diagnostics/storage/indexes; counts/bytes/WAL, начальная скорость и extrapolation явно раздельны. Отдельный metadata-only [inventory](../../scripts/storage_inventory.py) показывает backups/experiments/release artifacts. Не ждать месяц и не держать job ради статистики.

## Ошибки и recovery

До promotion при failure запускается тот же прежний container с неизменной рабочей БД. После начала rename автоматический возврат запрещен: проверить exact record/paths; при новом schema2 использовать совместимый forward-fix. Старый schema1 image не запускать на schema2. Failed post-check не дает PASS и не разрешает удалять прежние рабочие файлы. После ambiguous SSH outcome сначала читать state, не повторять reset/installation вслепую. Private recovery остается в operation directory; старую удаленную market history намеренно не обещаем восстановить.

## Routine schema2 backup/CD

### Чистая БД для нового замера с сохранением всей истории

Отдельное поручение владельца17Oct2026 разрешает новую working DB, но требует
сохранить все прежние данные. Это не повтор consumed reset permit: receiver,
его policy и permit не меняются. Сначала обычные PR/CI/merge/CD exact revision.
Затем оператор сохраняет/останавливает только identified capacity observer,
проверяет все его files/hashes и запускает reviewed
[fresh_measurement.py](../../scripts/fresh_measurement.py) из нового exact image.

На подтверждённом host/root, под existing `.deploy.lock`, команда имеет вид:

```bash
python3 fresh_measurement.py --expected-source MERGED_SOURCE_SHA --expected-generation CURRENT_GENERATION_ID --approve-fresh-database
```

Значения берутся из фактических primary CD/runtime records. Script/hash сверяются
с merged source; approval flag выражает уже данное поручение, не новое разрешение.
Операция требует отсутствие active observer/conflicting delivery и disk reserve,
проверяет source/image/project/mount/config/generation, останавливает один writer,
создаёт полную SQLite snapshot copy с full integrity/SHA256 и готовит fresh schema2.
Точный expected generation и hash проверенного архива обязательны для нового
explicit CLI пути reset_history; обычный repeated reset остаётся запрещённым.

Private tables/preferences независимо восстанавливаются/reopen-проверяются.
После отсутствия handles прежние working DB и sidecars переименовываются в
private `fresh-measurement-*/previous-working/`, candidate занимает working path.
Ни один прежний файл не удаляется. Дополнительно остаются qualified archive,
private recovery, candidate/operation receipts. Новый период помечен
`PRESERVED_IN_ARCHIVE`, старые events/queues не импортируются. После запуска
проверяются exact image/source, health, generation/private/config/hash и все
архивы; рабочий bootstrap и его partial coverage явно проверяет следующий observer.

Общий предел операции900s с recovery reserve; проверка archive CPU1/RAM4GiB,
candidate/default guards прежние. До promotion failure разрешает запуск того же
writer на прежней DB. После начала rename — exact record inspection/forward-fix,
без blind retry, overwrite или автоматического restore поверх новых данных.
Проверяющий observer устанавливается после этой операции, один на новый
source/config/generation. Все прежние observation folders сохраняются;
запланированные Codex задачи не создаются.

При недостатке места для второй полной копии explicit `--archive-original`
выполняет прежний full integrity на остановленной исходной БД и фиксирует
SHA256 каждого data file. Эта же проверенная БД/sidecars становится архивом
через exact rename; private candidate готовится до promotion и требует тот же
archive hash/generation. После promotion hashes/identities проверяются снова.
Дополнительная полная копия не нужна; резерв1536MiB для нового накопления
остаётся обязательным. Это не отменяет full integrity или routine CD backup.

Если operator-only исправление нужно для устранения disk prerequisite следующего
обычного CD, его допустимо выполнить из exact reviewed PR head после всех
required CI/self-review, до merge этого hotfix. Отдельно проверяются актуальная
runtime source/image и operator SHA256/head/CI; helper в running artifact должен
уже поддерживать используемый API. После операции hotfix проходит обычный merge/CD
на новой малой working DB. Receiver/permit/protections/budgets не меняются.

После reset автоматические delivery используют full source integrity и SQLite online backup на диске с полной integrity и streaming SHA256. Вся DB в RAM/tmpfs не копируется; cap4GiB/CPU1/no swap остается. Source+backup делят оставшееся время общего900s deadline с резервом переключения/post-checks. При нехватке места/времени stage завершается failure до switch; gates не ослабляются. Текущий target25GB не гарантирует полный scan любой25GB базы за15min: фактическая скорость и длительность видны в records, превышение требует переработки, не автоматического увеличения deadline.

Retention работает batches через indexes/transactional counters, summaries12 календарных месяцев; routine control не сканирует все DB pages. `storage-indexes` — отдельное ограниченное измерение, размер snapshot помечен временем. Его отсутствие/timeout не считается доказанным размером индексов. Forecast отражает short-window/model limitations и текущий предел25,000,000,000bytes без скрытого сокращения данных. Исторические20GB records остаются snapshots прежнего требования. Последующее наблюдение входит в текущую Goal после завершения исправлений и штатной поставки, без нового reset.

## Storage extension3: routine delivery без reset

Класс миграции — additive schema2 layout extension и обратимое кодирование records;
source DB/private settings/history generation сохраняются. До switch действуют full
source integrity и ordinary disk backup/hash/validation под общим900s deadline.
После switch новый writer atomically создает compact expiry/quality tables и
compatibility views, не копируя всю history в startup. Затем bounded repack/drain
с durable progress выполняется обычным runtime; old/new formats читаются совместно.
State/cursor записываются в той же транзакции, что и проверенные replacement bytes;
никакой потери прогресса или восстановления старых событий как новых alerts.

Post-checks: expected source/image/health, оба markets/4TF, owner config/private hash,
прежняя generation, outcomes/Telegram, `storage.repack` progress и свежие samples.
Repack может продолжаться после ограниченного delivery window; это не повторный
backup/конвертация в Actions. Статус IN_PROGRESS показывается явно. После encoding3
writes или view promotion старый binary без обоих readers не является допустимым
rollback: совместимый forward-fix, без DB downgrade/автоматического restore поверх
новых данных. Reset permit не переустанавливается. Бэкапы/эксперименты не удаляются.

Startup recovery сначала дает успешное calculation window каждому market/TF;
одновременно один seed worker на bucket, failed window не считается готовым.
После этого возвращаются обычные live-gap/bootstrap/history priorities. REST budget,
full-universe subscriptions и post-check gate150s/whole CD900s сохраняются.
`startup_recovered_buckets` — diagnostic подтвержденных windows, не замена проверки
fresh data всех market/TF и полного ACK в `delivery_ready`.

### Disk integrity на базе больше2GiB

Disk `state-check` использует уже pinned APSW/SQLite и logical read/write handle с
`query_only=ON`: SQLite пропускает CHECK-constraints при SQLITE_OPEN_READONLY.
Валидатор выполняет полный `PRAGMA integrity_check`, включая CHECK/index/freelist,
держит один согласованный read snapshot и проверяет тот же shared deadline.
Записи SQL запрещены query_only; приложение продолжает использовать стандартный
SQLite writer. APSW позволяет mmap до3GiB вместо stdlib ceiling2GiB; cache128MiB и
существующие отдельные CPU1/hard4GiB/no-swap limits остаются. Это настройка только
валидации, без изменения memory/CPU приложения или900s production gate.

Для больших closed disk backups выбирается тот же validator после закрытия copy
connection и освобождения live WAL snapshot. Полный SHA256/размер, source integrity,
backup integrity и все post-checks сохраняются. Small backups и TMPFS/compressed
qualification не меняются. Qualification17Oct на live2.89GB:57.11s, peakRSS2.95GB,
zero data writes; это snapshot текущего размера, не обещание времени для любой
будущей базы и не доказательство текущего25GB capacity. Failed attempt сохраняется; повтор
делается после исправления через новый validated artifact, без bypass/reset.

## Измерение ёмкости и конечное24h наблюдение

Canonical команды из root, existing env/owner binding:

```bash
python -m cryptowatcher capacity
python -m cryptowatcher capacity --capacity-ledger --capacity-cohorts
python scripts/rehearse_retention.py --database PATH_TO_NEW_DISPOSABLE_DB
```

Первый вариант читает постоянное число counters/состояний/files. Deep flags — явные
разовые snapshot scans: runtime ledger50s, prospective cohorts60s, physical dbstat10s,
quality count5s; timeout дает PARTIAL/UNSET, не нулевой размер. Sequential rowid
используется только внутри read snapshot, не как persistent cursor. Logical payload/
key bytes не являются физическими страницами. DB file уже включает indexes/free
pages; total=DB+WAL+SHM+journal. Табличные/index snapshots показывают разложение
внутри DB, не прибавляются повторно. Snapshot SQL согласован; размеры files снимаются
отдельно по времени, writer может продвинуть WAL во время deep probe.

`forecast_explanation` восстанавливает арифметику прежней модели. Разделение details
по namespaces пропорционально текущему payload mix — model allocation, не измеренный
посуточный insert flow. Cohort study считает frozen event grouping по активной
outcome methodology1; при других methods не угадывает Cartesian product. Actual
quality facts и prospective cohorts показываются отдельно. Empty comparator sets
не доказывают выигрыш от dedup полноценных будущих comparators. Additional methods/
settings, плотность cohorts, реальные no-signal windows и source leases — явные
ограничения прогноза. Использованные исходники сравнений могут переживать raw35d
до истечения30d parent detail; это subset необходимых revisions, не indefinite hold.

Runtime repack первые600s процесса получает один прежний40ms slice за независимый1s
tick, с отдельным transaction/checkpoint и scheduler yield. После startup при
coverage>=95%, ingestion age<=5s, poll age<=120s, analytics OK/age<=60s и отсутствии
фактических live REST requests допускает до4 таких slices. Общий intended wall cap
200ms; readiness/live priority перепроверяются между slices. При потере условий
дополнительные slices не выполняются, startup/current recovery guard сохраняется;
отдельный SQLite I/O/commit может выйти за time check. COMPLETE прекращает slices.
В уже существующем1s callback successful fresh retention backlog получает до4
calls/0.5s admission с yield/recheck между calls, после600s startup и initial
reclaim. Каждый call сохраняет50ms admission/atomic archive-before-delete;
начатый event/SQL не обрывается по таймеру. Analytics и priority сериализованы
одной retention lock. Нового scheduled callback/host task нет. Пока backlog
подтверждён, один40ms repack slice сохраняет transfer/GC, но откладывает только
последующую parent-bundle compaction; pending_work не скрывается. Initial reclaim
не откладывается, нормальная compaction возобновляется после backlog. Worker
failure виден в diagnostics/capacity и запрещает clean baseline; regular success
восстанавливает работу. Native one-CPU throughput проверяется отдельно от local
microbenchmark; RAM/CPU и CD/observer budgets не увеличиваются.

Durable rotation phases сохраняет fairness после slow SQL и restart; каждая phase
использует остаток общего deadline, не создает новый таймер внутри slice.
Records/decision heads/quality используют fixed rowid interval и durable next ID,
с прежним logical key floor для продолжения начатого ранее прохода. Дополнительные
headers вне scope учитываются в rows_scanned; payload для них не загружается.
Новые записи сохраняются сразу в current format и не расширяют physical interval.
Display cursor после перехода не задаёт остаток: для оценки используй next ID/high
water, а не key>cursor. Разность ID — верхняя граница позиций, не число записей при
удалениях/sparse IDs; точный count требует отдельного bounded запроса.
Обычный restart продолжает durable position. schema_version только читается;
DDL/VACUUM инвалидирует physical position и вызывает conservative recheck с тем же
logical floor, без потери преобразованных данных. Production VACUUM этим flow не
разрешается и не запускается. Budget/RAM/cache и обычные backup/CD остаются прежними.
Это не полный scan внутри одной транзакции, не CI job и не изменение900s CD gate.
Backfill имеет отдельный bounded batch job и прежний shared REST limiter/live priority.
Оба maintenance jobs ждут fresh usable OK/DEGRADED poll и нулевой live bootstrap/recovery/blocked
queue. При transient live recovery уступают диск, затем продолжают durable progress.
Новый PASS CD меняет expected source observer: завершить только прежний identified
WAITING_STABLE unit после readback, сохранить его records, запустить один observer
из нового exact image в новой private folder. Существующую baseline другого source
не продолжать; gates stability/12h wait/24h observation остаются прежними.

Для24h используется reviewed `scripts/observe_capacity.py`, также включенный в image
как `/app/operator/observe_capacity.py`. После PASS CD operator копирует его из
точного running artifact, проверяет SHA256 против merged source и запускает один
transient unit `cryptowatcher-capacity-*` на установленном target. Никаких producer/
Telegram/config writes. Metadata — отдельная root-owned0700 папка
`/opt/crypto_watcher/observations/capacity-*`, файлы0600; они не входят в рабочую БД.
Existing directories/status с другим source/schema и symlinks отклоняются.

Sample cadence5min; до старта нужны два последовательных samples без bootstrap/
ingestion recovery/gaps и отдельной analytics-gap-pending queue, с полными ACK,
current analytics и COMPLETE repack/admission. Analytics queue проверяется одним
indexed existence lookup с LIMIT1, без count/scan. Нормальные ожидающие наступления
outcome horizons сами по себе не являются backfill и не блокируют baseline.
Также обязательны healthy named ingestion tasks, согласованные active streams /
groups/sessions/connections, обе readiness flags и свежие metadata/tickers обоих
рынков:600/300s +60s request/reconcile grace, включая возраст самого snapshot.
Каждый ACKed subscription принадлежит RUNNING task с receipt<=90s и clock skew<=5s.
Свежий heartbeat/общий WS snapshot не заменяет эти проверки; старый runtime без
таких diagnostic fields получает явные reasons и не принимается как clean.
Время завершения bootstrap/repack фиксируется как observed, не выдуманный exact end.
Ожидание ограничено12h; затем WAIT_LIMIT_REACHED с причинами. После baseline —24h,
deep scans только на baseline/final. Обычные samples не обходят runtime/quality/indexes.
Probe: readonly, nice15, address space256MiB, CPU90s, wall145s; обычные runtime/
maintenance RAM4GiB/CPU1 и backup/CD900s не изменяются. Host unit имеет отдельный
малый memory/CPU limit и конечный RuntimeMaxSec; GitHub Actions не ожидает observation.

Status/каждый sample/baseline/final/result сохраняются atomically. Restart host observer
продолжает ту же baseline; source change завершает наблюдение как invalidated/failure.
Три подряд probe failures прекращают loop. Ошибка внутри24h отмечает interruption;
короткие пропуски между5min samples могут быть не замечены. Короткому/нестабильному
интервалу не присваивается устойчивый суточный темп. Interval report содержит physical
DB/index/WAL deltas, logical namespace deltas и cumulative cleanup counters; отсутствие
полного boundary ledger явно UNSET. Даже успешные24h и controlled-clock tests сами
по себе не подтверждают population mix следующего года или25GB fit.

`sample-attempt-NNNN.json.probes` сохраняет каждый из максимум4quick calls до
baseline и единственный call после него: номер, время начала, PENDING, затем
полный snapshot либо sanitized ERROR/timeout/returncode. Ошибка поздней
перепроверки не удаляет предыдущие snapshots; restart завершает PENDING subprobe
как INTERRUPTED вместе с sample. Старые attempt files без `probes` остаются readable.

Каждый deep boundary имеет отдельный `baseline-attempt-NNNN.json` либо
`final-attempt-NNNN.json`: PENDING до probe, полный snapshot/blockers и решение
после него, sanitized error class/returncode при failure. `status.phase` показывает
текущую фазу; `last_sample_blockers` относится только к quick sample,
`last_baseline_attempt`/`last_baseline_rejection` — к реальной deep проверке.
Отклонённый snapshot не удаляется при сбросе stable counter. Restart сохраняет
in-flight attempt как INTERRUPTED и безопасно возвращается к прежним gates;
exception body/command/stderr/env не публикуются. Ранее потерянные причины нельзя
восстановить по пустому last_blockers: такой исторический пробел явно отмечается.

Rehearsal моделирует62 weekly epochs, settings changes, restart, partial horizons,
all markets/families/TF и весь30d/35d/calendar12mo lifecycle. Numeric/comparator facts
сравниваются как точные multiset; private settings сохраняются. Это структурное
доказательство finite retention, не имитация непрерывного universe или load/SLO.
Отдельный regression case проверяет20 comparator windows и несколько methods.
