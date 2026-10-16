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

После reset автоматические delivery используют full source integrity и SQLite online backup на диске с полной integrity и streaming SHA256. Вся DB в RAM/tmpfs не копируется; cap4GiB/CPU1/no swap остается. Source+backup делят оставшееся время общего900s deadline с резервом переключения/post-checks. При нехватке места/времени stage завершается failure до switch; gates не ослабляются. Target20GB не является гарантией выполнения полного scan любой20GB базы за15min: фактическая скорость и длительность видны в records, превышение требует переработки, не автоматического увеличения deadline.

Retention работает batches через indexes/transactional counters, summaries12 календарных месяцев; routine control не сканирует все DB pages. `storage-indexes` — отдельное ограниченное измерение, размер snapshot помечен временем. Его отсутствие/timeout не считается доказанным размером индексов. Forecast отражает short-window/model limitations и предупреждает о20GB без скрытого сокращения данных. Последующее наблюдение — отдельная задача владельца после завершения исправлений/reset.

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

Runtime repack получает один прежний40ms slice за независимый1s tick, с отдельным
transaction/checkpoint и scheduler yield. Intended work<=40ms/tick;
отдельный SQLite I/O/commit может выйти за time check. COMPLETE прекращает slices.
Это не полный scan внутри одной транзакции, не CI job и не изменение900s CD gate.
Backfill имеет отдельный bounded batch job и прежний shared REST limiter/live priority.
Оба maintenance jobs ждут fresh OK poll и нулевой live bootstrap/recovery/blocked
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
по себе не подтверждают population mix следующего года или20GB fit.

Rehearsal моделирует62 weekly epochs, settings changes, restart, partial horizons,
all markets/families/TF и весь30d/35d/calendar12mo lifecycle. Numeric/comparator facts
сравниваются как точные multiset; private settings сохраняются. Это структурное
доказательство finite retention, не имитация непрерывного universe или load/SLO.
Отдельный regression case проверяет20 comparator windows и несколько methods.
