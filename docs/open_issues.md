# CryptoWatcher — delivery plan
Canonical plan: этот файл. Canonical spec: [product_scope.md](product_scope.md). Прежние follow-up темы сохранены в F09/F22/F24/F25/F32; старые номерные разделы не были AC IDs. Исторический audit snapshot ниже не обновляет процент во время IMPLEMENT. Текущее branch evidence отдельно; подробный audit report остаётся вне repository.

## Audit baseline и независимая оценка

Snapshot: 2026-10-10, проверка около 10:00–10:15 Europe/Moscow; `origin/main = 8ace3638b9193444948d0beb88e0c49e9f2f068e`. Git fetch/remote API согласованы, initial local main совпадал, worktree был чистый. Оценка относится к коду этой revision и S01, а не к фактически работающему VPS.

Готовность = **7/58 READY AC = 12.1%**. Статусы: READY 7, IN_PROGRESS 24, BACKLOG 18, BLOCKED 9. Продуктовые и технические AC равноправны; проценты эпиков не усредняются. Denominator известен; существенные numeric/consumer/delivery unknowns оформлены как SPEC gaps, не заменены assumptions. Предыдущей численной оценки в доступных records нет; сравнение изменения >10 п.п. N/A. Прежний MVP и весь согласованный результат — разные scopes.

Реализация — небольшой Spot REST/composite Telegram prototype с persistent per-chat JSON. Основные части нового scope не реализованы. Валидация кода не подтверждает VPS health, deployed revision или качество trading decisions. Наличие ready closed-candle predicate не закрывает gaps/freshness/episodes.

| Эпик | READY / все AC | Готовность | Остальная работа |
| --- | --- | --- | --- |
| E01 Назначение, границы и владелец | 3/5 | 60.0% | AC-01-03 IN_PROGRESS; AC-01-04 IN_PROGRESS |
| E02 Universe и watchlist | 0/4 | 0.0% | AC-02-01 IN_PROGRESS; AC-02-02 BACKLOG; AC-02-03 IN_PROGRESS; AC-02-04 IN_PROGRESS |
| E03 Realtime ingestion и timeframes | 2/5 | 40.0% | AC-03-01 BACKLOG; AC-03-02 IN_PROGRESS; AC-03-04 BACKLOG |
| E04 Detectors и metrics | 2/5 | 40.0% | AC-04-02 BLOCKED; AC-04-03 IN_PROGRESS; AC-04-04 IN_PROGRESS |
| E05 Episodes и freshness | 0/4 | 0.0% | AC-05-01 BACKLOG; AC-05-02 IN_PROGRESS; AC-05-03 BLOCKED; AC-05-04 BLOCKED |
| E06 History и outcome analytics | 0/7 | 0.0% | AC-06-01 BACKLOG; AC-06-02 BACKLOG; AC-06-03 BLOCKED; AC-06-04 BACKLOG; AC-06-05 BLOCKED; AC-06-06 BACKLOG; AC-06-07 BACKLOG |
| E07 Telegram alerts, control deck и manual use | 0/7 | 0.0% | AC-07-01 IN_PROGRESS; AC-07-02 IN_PROGRESS; AC-07-03 IN_PROGRESS; AC-07-04 IN_PROGRESS; AC-07-05 IN_PROGRESS; AC-07-06 BACKLOG; AC-07-07 IN_PROGRESS |
| E08 Owner settings и presets | 0/3 | 0.0% | AC-08-01 IN_PROGRESS; AC-08-02 IN_PROGRESS; AC-08-03 IN_PROGRESS |
| E09 Интеграция trading agent | 0/6 | 0.0% | AC-09-01 BLOCKED; AC-09-02 BLOCKED; AC-09-03 BACKLOG; AC-09-04 BACKLOG; AC-09-05 BACKLOG; AC-09-06 BLOCKED |
| E10 Persistence, migration и recoverability | 0/4 | 0.0% | AC-10-01 IN_PROGRESS; AC-10-02 BACKLOG; AC-10-03 IN_PROGRESS; AC-10-04 IN_PROGRESS |
| E11 Digest и derivatives context | 0/4 | 0.0% | AC-11-01 BACKLOG; AC-11-02 BACKLOG; AC-11-03 BACKLOG; AC-11-04 BACKLOG |
| E12 Технические границы, CI/CD и эксплуатация | 0/4 | 0.0% | AC-12-01 IN_PROGRESS; AC-12-02 BLOCKED; AC-12-03 IN_PROGRESS; AC-12-04 IN_PROGRESS |

## Исходный audit roadmap (superseded детализацией Current Goal)

| Этап / предложенная Goal | Результат | Приоритет / зависимости |
| --- | --- | --- |
| G1 — Безопасная owner foundation и проверяемая поставка | Fail-closed owner access, validated persisted owner config/delivery separation, безопасная legacy migration/recovery, базовая suite, минимальный CI и подтверждённая Docker/VPS delivery; существующий Spot path продолжает работать | P1; B01/Q03; сначала закрыть риск потери state и owner scope |
| G2 — Рабочий radar обоих рынков | Широкие eligibility universe, shared WS ingestion/REST recovery, concurrent TF, versioned базовые detectors/events/episodes/freshness, правдивые Telegram alerts/diagnostics | P1; G1, Q01; первый продуктовый этап S01 |
| G3 — История, outcomes и digest | Immutable event/revision history с автоматическими horizons, MFE/MAE/cohorts/export, digest из общей history | P1/P2; базовая history начинается в G2; развивается параллельно G4, Q04 |
| G4 — Advisory integration | Authorized schema/outbox/idempotency/replay и independent diagnostics; joint consumer contract | P1; G2, B02; запускать после стабилизации radar, не ждать calibration |
| G5 — Derivatives enrichment / calibration | Optional Spot derivatives context и ручная статистическая калибровка profiles/detectors | P2; G2–G3; нет automatic production tuning |
| Cleanup/consolidation | Убрать superseded CMC/reset UX, подтвердить helpers, объединить docs и commands | По риску вместе с выбранной Goal; F02–F04/F23–F24; preserve migration/state |

## Current Goal — полный scope CryptoWatcher (IMPLEMENT)

15Oct owner поручил уменьшить накопленную БД, сохранив useful history и работу
алертов; общий предел whole conversion900s не меняется. Baseline2efd9093/PR35
и automaticCD38625409154 PASS. Текущий storage sub-scope: lossless compression
старых immutable records и быстрый проверяемый converter без повторной сборки
больших JSON windows; disk-backed compressed snapshot для обязательной полной
проверки при memory cap4GiB. Branch `codex/compact-history-records-20261015`,
base2efd9093d9639f767f2a0df697bfd060c2ce47e0. Scope58/code54; не DONE.
Validation REQUIRED: frame bounds/hash/checksum/corruption и exact numeric/value
roundtrip; legacy/compact/new reads, heads/export/outcomes/consumer contracts;
disk snapshot byte identity, full CHECK/negative controls, owner/private equality;
current whole source/candidate/recovery proof≤900s/CPU1/RAM4GiB и reviewed
exact-target promotion, required PR CI и applicable CD. Уменьшение physical файла
не объявляется по reusable pages или маленькому fixture. Source32GB не изменена.
Read-only prototypes не подтвердили whole fit: лучший sequential preview120064
heads за180.341s,644852915 redundant bytes только в проверенной выборке; prefetch
отклонён за лишний I/O без gain, probe остановлен/removed, bot healthy/unchanged.
Новый алгоритм ещё не qualified. Gates/retention/формулы/settings не ослабляются.
Local components validation:134 tests PASS, canonical quality PASS; Linux pinned
native VFS synthetic fixture `record-disk-linux-jp_zkzk0/receipt.json` PASS:
exact bytes/types, four-family history/head compatibility, disk-backed full
integrity/read-only, damaged CHECK refusal. Production unchanged; converter and
current whole900s qualification still PENDING. No new PR/CI/CD dispatched.
Integrated private-candidate pipeline prepared: one absolute≤900s deadline, full
frozen source integrity, exact stored-value manifests/all-row readback, old-head
revision/projection checks, framed immutable records, full candidate integrity
and independently decoded recovery with encoded/decoded hashes. Linux synthetic
whole-path control `framed-whole-linux-6g9xgerh/receipt.json` PASS; this is not the
current32GB dataset or promotion proof. Historical/current large qualification,
operator recovery/promotion and applicable CI/CD still PENDING.
Follow-up:193 targeted storage/history/export/analytics/agent tests PASS,2 Linux
native skips локально; canonical quality PASS. Exact raw restore и complete
filtered NDJSON/cohorts/cutoff before/after conversion fixtures PASS. Linux
`framed-final-linux-34327q4d/receipt.json` PASS (native VFS/projection, SQL frame
read, four-family compatibility/full CHECK negative control/whole fixture). Первый historical5.42GB trial `_scjzkra` UNQUALIFIED:900s истекли до полного
readback/recovery; source archive/bot сохранены. Исправлены выбранный native VFS
path и writer cache; повтор проверяет изменённый алгоритм. Historical read-only
qualification идёт в отдельном private
container; production не mounted/paused. Optimized current path выбирает pinned
native projection/VFS и bounded source128MiB/writer≤512MiB caches; все full
source/candidate/recovery gates сохранены. Historical result и current dataset
fit не предполагать до primary receipt.
Дополнение: compact candle fields сохраняют11 исходных values/units/timestamps
без дублирования names. Source/outcome parity, exact canonical bytes и отказ для
неизвестного layout проверены. Linux `framed-candle-linux-19yauuwj/receipt.json`
PASS включает borrowed native map после trim, full CHECK, recovery и atomic
qualification receipt. Historical engine вернул полную candidate qualification
за~635s/2,154,967,040bytes, но operator report сломался при duplicate elapsed_seconds
и ошибочно записал UNQUALIFIED; этот receipt не объявлять успешной поставкой.
Исправлен reporting и добавлена primary qualification запись внутри общего deadline.
Current33,682,788,352bytes read-only snapshot qualification начата в
`framed-current-jj7s59n9`: CPU1/RAM4GiB/no swap/whole900s, no pause/replacement.
Current trial завершён UNQUALIFIED при900.652s: capture/freeze/hash33,720,209,408
bytes завершились только872.523s, full source scan не закончился до deadline.
Candidate отсутствует; source inode/container unchanged и healthy. Raw error
DATABASE_CORRUPT был misclassification VFS deadline, не доказанной corruption;
priority deadline classification исправлена/regression PASS. Current whole fit
и actual physical shrink остаются PENDING. Следующий вариант — прямой closed-file
capture с теми же full gates/общим900s и reserve для restore. Linux
closed-source-linux-e_dyhshs/receipt.json PASS; production stop/cleanup/promotion
ещё не выполнены. По коду54/58 AC текущей Goal, процент не пересчитывался.
Maintenance preparation дополнена controller/adapter: один deadline со150s
restore reserve, same-unit recovery даже при lost stop ACK/worker failure,
исчерпании срока и cleanup failure; failed post-check никогда не PASS.
31 targeted controller/adapter tests и canonical quality PASS. Typed systemd
InvocationID/RuntimeMaxUSec/ExecStopPost bind проверяет exact restore argv;
worker code bundle/hash и fresh disk/RAM admission до stop обязательны.
Изолированный Linux `watchdog-control-xseglams/receipt.json` PASS подтверждает
вызов independent recovery hook при deadline и SIGKILL parent; production unit
в fixture не участвовала. Production maintenance/cleanup остаются PENDING
owner reply к ранее заданному вопросу; новых больших scans/CI/CD не запущено.
Launcher завершён: default preflight не имеет --execute, exact restore argv
и systemd900s/UMask0077 проверены; unsafe/relative/specifier paths отвергаются.
38 maintenance tests PASS, quality PASS. Prepared code-only bundle286720bytes
с manifest каждого module; data/secrets отсутствуют. Production staging/execute
не запускались, большие проверки не повторялись; owner approval остаётся PENDING.




Владелец разрешил разовую code-only поставку PR35 (reply
call_ad7a2863d9c8418baf778d671670d3ca, «Разрешаю разовую поставку PR35»).
Exact scoped receiver/bootstrap/tests требуют fresh CI final reviewed tree.
Original ARMED failed permit34 и receipt34 сохраняются побайтно/private;
новый root0600 permit35 связан с exact tree/target/current predecessor23bb.
Только для этой поставки используется approved bounded source-readonly config/compact
proof вместо full history scan/backup; отсутствие fresh whole-history recovery принято.
Полные ACK, fresh both-market4TF, CI/image/host/lock/health/private readback обязательны;
deadline150s, conversion900s/CPU1/RAM4GiB и ordinary backup/CD policy сохраняются.
DB cleanup/conversion/replacement не входят; Scope58 AC, по коду54/58, Goal не DONE.

Latest delivery: PR34 merged23bb023e, required CI38613962526/ci-required116026604698 PASS;
automatic CD38615205711 переключил code image, но FAILED вPOST_CHECKS. Primary record
`23bb023e3872d5791fb270745635da1048dce682-917596cd661973ee/deployment.json`
остаётсяPOST_CHECK_FAILED, permit34 ARMED, не переиспользуется. Runtime18:34UTC
running/healthy/source23bb023e/imagec7af20a7, private revision3/hash unchanged;
ingestionFRESH, linear fullyCONNECTED, Spot300/348 ACK и pollWARMING.
No history cleanup/replacement, deadline не расширен. Поставка не объявленаPASS.
Necessary hotfix наbranch `codex/subscribe-before-candle-work-20261014`, base23bb023e:
отделить subscription ACK phase от дорогой обработки closed candles с bounded buffer,
receipt timestamps, явным overflow→gap recovery и yield между replay observations.
Полнота ACK и existing delivery_ready/freshness остаются обязательными. Non-goals:
увеличение startup/CD deadline, отключение analytics, удаление/перезапись истории,
изменение formula/settings/schema/retention/REST budget. Validation Plan: REQUIRED
local real subscribe-before-observation ordering, buffer overflow/recovery, restart,
duplicate/stale/gap/closed-candle и provenance scenarios, canonical quality,
fresh required CI; actual applicable CD gate ещё не разрешён для нового tree.

Hotfix local validation:33 ingestion/source scenarios и canonical quality PASS,
включая all-ACK-before-candle-work, preserved receipt, stale replay, оба buffer limits,
overflow→blocked/priority recovery, rejected subscription без candle writes;
old duplicate/gap/out-of-order/provenance cases сохранены. Late readback18:47:46UTC:
same source23bb023e/container1f111ab7 running/healthy, no restart/OOM, обе subscriptions
CONNECTED, ingestionFRESH, bootstrap/recovery_pending0, real Telegram DELIVERED
после переключения, owner3 и poll1155/1156 (однаzero-baseline ошибка).
Это подтверждение восстановившегося runtime, не исправление failed CD38615205711.
Новый startup code требует fresh PR CI и отдельный exact delivery gate; ничего на
VPS этим hotfix пока не менялось, old history shrink/full900s proof остаютсяPENDING.

Владелец разрешил разовую code-only поставку PR34 (reply
call_857acc1269ee48b9a32326f98d8651e7, «Разрешаю разовую поставку PR34»).
Этап возобновлён: exact scoped receiver/bootstrap/tests → fresh CI → gated install
нового root0600 permit → merge/automatic CD. Новый permit связан с final reviewed
tree и actual predecessor runtimeef5; исходные ARMED permit33 и failed receipt
сохраняются, не выдаются за consumed/success. Scope всей Goal58 AC сохранён,
по коду54/58; готовность в процентах не пересчитывалась. Goal не DONE.

Проверенный origin/mainef5daeb0 содержит PR33. CI38603388275 PASS;
CD38604004891 переключил code image, но FAILED вPOST_CHECKS, не переименован вPASS.
Поздний readback подтверждает healthy/fresh оба рынка/4TF и сохранность private config,
16 новых compact events; primary linked proof указан в latest checkpoint ниже.
Исправление cold startup PR34/head5f219f6c готово, CI38607037711 PASS, draft/open;
approved gate меняет code head и требует свежий required CI до merge.
Branch `codex/responsive-ingestion-startup-20261014`, baseef5daeb0; локальные docs
checkpoint commits после опубликованного head сохраняются для следующего batch push.
История не очищена/не заменена; full900s current conversion и physical shrink не
qualified. Consumer credential/TOTP явно отложен владельцем; activation не выполнена.
Следующий шаг: scoped receiver/bootstrap/tests, один grouped push
со свежим required CI, exact new permit с сохранением failed PR33 receipt, merge/CD.
Старые PR31/PR32 records ниже — history, не инструкция повторной поставки.

2026-10-11 владелец явно расширил Goal до всего согласованного проекта и поручил непрерывную реализацию, с переносом внешних blockers в конец. Scope: все58 исходных AC с утверждёнными уточнениями и dispositions полного реестра findings. G1/G2A/G2B/G3/G4/G5 — последовательные поставляемые этапы общей Goal по уже утверждённому PR разбиению; G6 — финальный полный AUDIT и audit docs PR. Отдельное повторное разрешение перехода между этапами не требуется. Встроенная active Goal сохраняется: её objective уже включает продолжение approved roadmap после G1.

Общий DoD: весь agreed scope реализован и проверен, latest required CI/review и applicable CD всех runtime PR подтверждены, все findings исправлены либо получили обоснованное approved disposition; final audit проверяет requirements↔code↔runtime Evidence и заново рассчитывает готовность. Неизвестный результат или блокер не считается READY/DONE. Повтор первого JSON import, legacy rollback, restore поверх live data, ослабление auth/CI/host checks и торговая активация в это поручение не входят.

Порядок blockers: установить точную зависимость/доказательство/нужное действие, продолжать независимый scope; в конце собрать единый remaining register и последовательно закрыть доступные зависимости. При failed delivery устранить безопасный forward-fix в этом этапе; dependent promotion не выполняется, независимая read-only подготовка продолжается. Не скрывать неисправный runtime переносом delivery gate на конец. Shared validation каждой стадии: canonical local quality + meaningful risk tests → required latest PR CI/image → automatic immutable CD/backup/source/health/persistence/application post-checks. Outcomes/causal completeness, signed consumer isolation и optional enrichment проверяются соответствующими stage plans до их кода. Final capacity/recovery/access limitations фиксируются в G6.

### Текущий этап — G4: signed advisory delivery / consumer-first v2

#### Текущий hotfix — производительность полной backup validation

##### Приоритетное продолжение: компактное хранение и уменьшение накопленной базы

Owner steering2026-10-14: сначала поставить через CD исправление new writes,
сохранив alerts и тот же production DB; уменьшение накопленной базы выполнять
отдельно. Current-dataset conversion больше не является prerequisite проверки
new-write runtime кода. При этом действующий routine CD требует full source и
persistent backup до switch; автоматического исключения из этих gates нет.
Разрешена конкретная one-time delivery exception, owner approval14Oct:
scope-bound permit, bounded legacy/compact readback и маленький private config
snapshot вместо full-history scan/backup; ordinary defaults не меняются.
У этой операции не будет нового полного backup актуальной history; после compact
writes прежний G5 reader несовместим, recovery только compatible forward-fix.
Owner reply call_1ece72ffadf74d4cb986afc82314cf27: «Разрешаю разовую поставку по
процедуре». Точная реализация: root-owned tree/previous-runtime/target permit в
[receiver](../scripts/deploy_receiver.py), выдаётся reviewed
[operator update](../scripts/update_cd_receiver.py) с explicit approval flag;
[bounded checker](../cryptowatcher/new_write_check.py) имеет source read-only,
private config snapshot/restore и disposable legacy/compact write/restart.
Permit consumed после PASS; missing/other-tree/consumed => обычный full-backup
flow. Full-history integrity/backup явно NOT_RUN_OWNER_EXCEPTION, не PASS.
Local53 gate/probe/capture/receiver regressions и quality PASS. Linux93 runtime/
new-gate tests PASS; bounded current source sample32 records/write-restart PASS,
config-recovery payload38,528bytes/revision2 (read-only, fixture-only writes).
Primary probe/gate record `new-write-probe-gate-linux.jsonl` вне repository.
Required latest
CI и actual CD/source/health/ingestion/new-write readback ещё PENDING.
Подробный review и measurements остаются вне repository.

New-write-only regression подтверждает mixed legacy/compact write/restart на той
же DB без rebuild, unchanged immutable revision/private serialized transport,
общие bodies и неизменные schema/settings. Local85 PASS,1 Linux-only N/A;
quality PASS. Isolated Linux3.11 runtime84 PASS11.19s/06:30:16UTC; два isolated
conversion сценария вне этого rollout deselected, прежние qualification Evidence
сохраняются; production не монтировался/container unchanged. Package SHA24392fec…,
primary local record `new-write-only-linux-runtime.jsonl` вне repository.
Read-only VPS06:20:50UTC: working29.63GB/free1.48GB/healthy G5afd107.
Это размер snapshot, не текущая гарантия capacity и не full integrity result.
Полный scope58 AC/progress54/58 и obligation old-data compaction сохраняются;
new-write rollout сам по себе не закрывает F34 или оставшиеся capacity AC.

Основание: explicit owner request2026-10-12 — исправить дублирование, уменьшить
накопленную базу и остановить чрезмерный рост, сохранив полезную историю и alerts.
Текущий PR31/branch `codex/backup-validation-performance-20261011`, base9634558a;
до изменения fresh main/PR/checks подтверждены, head8300614, clean worktree.
Scope: shared content-addressed lossless snapshots для settings/context/source
windows; compact revision/head representation; совместимые history/export/analytics
readers; isolated conversion всех накопленных записей с семантическим readback,
full integrity и source/candidate hashes; конкретная production promotion/recovery
procedure после квалификации. Затрагивает F34/F22/F15 и AC-06-01/02/05/06,
AC-10-02/03/04, AC-12-02/03. Thresholds, detector formulas, alerts preferences,
event identity/reference/cutoff, immutable signed transport bytes и retention90days
не меняются. Budget proposal30/70 не согласован и не применяется.

Owner decision2026-10-14: разрешён конкретный разовый recovery gate по полному
совпадению bytes с qualified source той же операции. Полные source/CHECK,
unchanged source и candidate integrity/all5 manifests/private bytes сохраняются;
для recovery — strict single checksummed Zstd frame, независимый full decode,
encoded/decoded SHA256 и размеры. Canonical procedure:
[разовая qualification](runbooks/delivery.md#разовый-recovery-gate-по-идентичности-проверенных-bytes).
Обычный backup/CD,900s/CPU1/RAM4GiB/no swap и запрет production DB replacement
сохраняются. Approval относится к этому gate, не к fit текущего dataset или
повтору прежней остановки бота. Prototype33 controls, actual historical decode16.135s,
small integrated fixture и4 Linux upstream/hash/trailer refusal controls PASS;
canonical integration PASS:51 Windows tests (native-only scenario N/A наWindows),
208 Linux3.11/native storage regressions и final52 targeted Linux recovery/conversion tests;
quality PASS. Primary Linux receipt:
`cw-recovery-canonical-419eba4c04eb41b58f7e7d8220d12765.json` и
`cw-recovery-canonical-c326c0f3d37f4d02b9a656a3db95f150.json` (13OctUTC/14Octlocal).
Read-only fixtures, production unchanged; current whole qualification PENDING.

Результат/DoD: new writes перестают дублировать большие snapshots; public records
и every revision восстанавливаются без потери типов/значений; накопленная база
уменьшена по квалифицированной exact-target procedure; latest required CI, merge,
delivery и runtime source/health/persistence/alerts checks подтверждены. До этого
storage finding остаётся PARTIAL, progress54/58 не пересчитывается как audit.

| Риск / AC | Проверка | Команда / environment | Этап / обязательность |
| --- | --- | --- | --- |
| Shared snapshot corruption, loss of history | Mixed legacy/compact records, identity/hash checks, revisions, settings race | Targeted pytest; repository root, locked dev Python | Local REQUIRED |
| Public contracts / AC-06/07/09 | Export completeness, cohorts/cutoff, signed replay, event/episode alerts regression | Existing affected tests плюс compact fixtures; locked Python | Local targeted / full Linux PR CI REQUIRED |
| Existing state / AC-10-03 | Isolated source→candidate semantic equality всех записей, owner/mutes/queues, full integrity, source unchanged and hashes | Canonical history_rebuild CLI; exact qualified copy, bounded offline resources | Isolated qualification REQUIRED |
| Actual capacity / AC-12-03 | Before/after file size и short CPU/RAM/I/O snapshot, no historical alert flood | Read-only VPS inventory и reviewed delivery post-checks | Before promotion / runtime REQUIRED |

Evidence before fix: readonly VPS2026-10-12T14:31UTC G5afd107: source history
1124 streams8Oct06:00→12Oct14:30UTC; events10Oct22:45→12Oct14:31UTC (1.6569days).
SQLite17.1GB/page free0, previous short resource snapshot14:19UTC main17.0GB/WAL0.98GB,
working set≈270MiB/CPU21–28%one core, rehearsal regular files≈11.25GB depth≤3.
Это short bounded measurement, не peak/SLO или full payload attribution.

Code checkpoint: PR31/branch `codex/backup-validation-performance-20261011`,
base9634558a, runtime-code head1cfe64a. Shared lossless snapshots, identity-checked
heads всех четырёх kinds, compatible history/export/analytics/agent readers и
complete typed logical manifests реализованы. Runtime пишет16-candle blocks;
historical conversion сохраняет целые frozen windows как compressed shared values.
Одинаковые целые windows разделяют body, разные пересекающиеся исторические windows
сохраняются независимо. Readers поддерживают оба варианта; window>32MiB использует
bounded blocks. Это не удаление inputs/provenance и не изменение retention.

Owner decision:900 секунд на всю конвертацию, включая copy/full verification;
checkpointed multi-run и сбросы deadline исключены. CPU1/RAM4GiB/no swap,
required source/archive/CI/CD gates и ordinary CD limits сохранены. Budget
proposals30/70/60min не применялись. Exact JSON real tokens декодируются в Python:
SQLite3.46.1 иначе менял последнюю значащую цифру production strength. Large integers,
numeric types, corruption/deletion/rollback, immutable private bytes и mixed formats
покрыты regressions. Local128 affected tests/quality PASS; required Linux/image
CI38300184149/ci-required114981497402 PASS на1cfe64a. Последующий docs-only checkpoint
требует latest PR gate; прежний PASS не предсказывает его результат.

Validation evidence: paired4000-revision sample дал exact public equality PASS,
whole-window copy CPU2.101/2.666s против4.143/4.446s и bodies5.97MB против6.83MB.
Это sample, не full throughput/savings. Native grouping/private SQL copy,
compressed-source backend, expanded caches и boundary-regex не дали существенного
устойчивого общего выигрыша и не включены; raw исследовательские logs вне repository.

Full qualification `shared-qualification-n6_zdys5` stopped2026-10-12T20:02UTC:
operator STOP_REQUESTED до900s по недостаточной скорости, exit137/OOMfalse;
это не timeout и не qualified conversion. Last COPY evidence575.962s:
1,108,235 runtime rows,39,142 из95,759 event revisions. Candidate629,420,032bytes
с journal — UNQUALIFIED, не использовать для promotion. Источник — закрытая
qualified5.42GB историческая копия61e4f087; текущая production база этим источником
не представлена. Primary records: `shared-qualification-n6_zdys5/status.json`,
`operator-stop.json`, `operation.log` в VPS rehearsals. Owned verifier removed;
CryptoWatcher7039151/G5afd107 и оба trading-agent units unchanged/running.
Production compaction/migration NOT_RUN; ни один полный copy/integrity/readback
результат в общем900s budget пока не получен.

Remaining: существенно иной способ полной исторической/current conversion≤900s,
current dataset/recovery proof и reviewed exact-target promotion → merge/applicable
delivery → actual disk/growth/alerts checks. Не повторять unchanged full run и
не подменять qualified dataset sample-ом. Read-only current WAL snapshot tool
promotion_eligible=false не подтверждает full physical source integrity. PR31
open/unmerged, F34 PARTIAL_IN_BRANCH; общая Goal по коду54/58, audit percentage
не пересчитывается. Полный scope сохраняется, Goal не завершена.

Independent requalification12Oct2026: current CryptoWatcherG5afd107 healthy,
SQLite19,466,387,456bytes/WAL980,131,552, disk free9,228,931,072bytes на20:35UTC.
Full-record archival prototype на4000 revisions отклонён: representation16.85MB
против12.83MB и отсутствие устойчивого выигрыша verification; code не изменён,
full rerun не запускался. Consumer2bfeda8: current keyring crypto20:46UTC PASS,
active master соответствует verified recovery material/package20:52UTC PASS.
Это master-key recovery qualification, не current PostgreSQL restore. Все runtime
units unchanged/running. Actual producer httpx0.26/TLS unsigned POST503 подтверждает
доступ к disabled receiver. Radar credential/binding0, ingest OFF, max age UNSET;
consumer production/dry_run, exact API/image/Compose/config-owner target установлен.
Следующий независимый шаг — qualified exact-target HMAC/config bootstrap с сохранением
other credentials/Strategies и private bytes; end-to-end delivery ждёт producer CD.
Historical/current conversion≤900s и F34 остаются незавершёнными,54/58 unchanged.

Read-only revalidation12Oct22:53UTC: producerG5afd107 healthy/тот же7039151,
SQLite20,520,615,936bytes/WAL980,131,552, free disk7,304,253,440bytes; consumer2bfeda8
и PostgreSQL unchanged/running. Fresh main9634558a/PR31 head1cfe64a и required
CI38300184149 SUCCESS подтверждены без workflow rerun. Bounded complete-path profile
на disposable fixture32,296 runtime rows, derived из старой qualified копии,
дал full fixture integrity/readback PASS9.502s; это не current/full qualification.
Убран неиспользуемый повторный JSON decode при final shared-body scan: `load_raw`
сохраняет encoding/size/SHA/canonical проверки.41 targeted rebuild/snapshot tests
и canonical quality PASS; изменённый converter ещё не qualified на full dataset.
Однократный JSONB parse на4k revisions сохранил exact bytes, но не дал устойчивого
выигрыша всего copy/proof пути и отклонён; buffered manifest framing оказался
медленнее и также отклонён. Полный повторный запуск не выполнялся.

Consumer activation preparation на actual API2bfeda8 и PostgreSQL16.14: isolated
SAVE/SELECT/decrypt, transaction rollback, UUID replay, stale CAS и preservation
unrelated credential PASS12Oct22:14UTC. Initial cleanup assertion ошибочно считала
inherited Compose label признаком чужого контейнера; exact owned fixtures удалены
с отдельным cleanup/live-readback PASS22:20UTC. Production config/credentials не
менялись; это qualification operator procedure, не activation или signed smoke.
Primary records: `consumer-vault-pg-fixture-fixed.jsonl`, `vault-fixture-cleanup.jsonl`,
`profile-complete-rebuild.jsonl`, `bench-parsed-json-once.jsonl` вне repository.

Experimental native prototype (вне tracked runtime): SQL field assembly отклонена
после paired4k проверок — public bytes совпали, но warm copy CPU2.95s против1.78s.
Stateless C extractor/head projection на fixed yyjson0.12.0/commit8b4a38dc и
host GCC13.3.0 прошёл actual Python3.11/SQLite3.46.1 fixtures.4k revision copy
CPU1.105/1.327s против1.795/1.809s; existing row admission/compression/hash checks
сохранены. Без accelerated canonical validation полный32,296-row fixture дал
CPU4.471s против4.862s; parser-only оптимизация не устраняет основной bottleneck.
Отдельный exact checker использует CPython stable-ABI float conversion/repr,
проверяет key order/duplicate keys, canonical escaping/numbers и array count;
unsupported depth/number cases сохраняют Python fallback. Differential2,027 cases
PASS, включая malformed/nonfinite, random float bits, large integers и Unicode.
Complete fixture с checker: all-table row manifests/integrity PASS, CPU4.475s
против6.011s и wall4.524s против6.387s в одной паре. Timing variation не позволяет
выдать это за sustained throughput или proof≤900s на historical/current dataset.
Owned test containers removed, source/live unchanged. Native code/dependency пока
не включены в app/CI/image; compiler был только в bounded transient prototype job.
Primary records: `build-native-json-prototype-v3.jsonl`, `bench-native-json-extract.jsonl`,
`qualify-native-canonical.jsonl`, `bench-native-canonical-complete.jsonl` вне repository.
Next: проверить ускорение full readback на сопоставимых сериях и изоляцию/native
error handling; только подтверждённый выигрыш интегрировать в existing PR31 с
reproducible build/meaningful tests. Whole conversion900s/1CPU/4GiB, full gates и
production promotion requirements сохраняются. Code54/58, Goal ACTIVE.

Native follow-up13Oct: controlled ABBA на той же32,296-row fixture дал native
wall4.258/4.325s и CPU4.148/4.289s против baseline5.161/5.520s и5.141/5.461s.
В каждом native run canonical checker вызван2351 раз; all-table manifests/integrity
PASS и совпадают. Actual Linux3.11/SQLite3.46.1 integrated loader/backend прошёл
93 targeted regressions, включая existing corruption, rollback, private bytes,
owner state и mixed-format checks. Initial isolated harness failures относились к
pytest shim/metadata/TMPDIR и insufficient virtual disk reserve; gates не менялись,
owned containers удалены. Canonical checker дополнительно имеет2027-case differential
proof из предыдущего шага. Это не whole/current conversion proof.
В PR31 подготовлены converter-only loader, Python fallback для unsupported input,
explicit required `--backend native`, source/field-map validation, pinned
GCC13.3.0-bookworm Docker stage/yyjson source hashes и mandatory image native check.
Ordinary runtime Codec и schema/data/retention policy сохраняются. Local Windows
87 tests PASS,6 Linux-native cases N/A; integrated Linux93 PASS. Latest required
CI/build identity и qualified full historical/current dataset ещё REQUIRED.
Полный heavy rerun до artifact/throughput qualification не выполняется.

Archive-source follow-up13Oct: converter принимает explicit gzip/Zstandard source
через frozen compressed VFS без дополнительной raw source copy. Decoded source
SHA256 и writable full integrity/CHECK, encoded archive hashes до/после, полный
candidate readback/manifests и immutable source identity обязательны. Общий900s
deadline не сбрасывается; candidate byte cap для archive задаётся явно. Ordinary
runtime/retention/CD gates сохраняются. Local Windows45 rebuild/snapshot tests и
canonical quality PASS: обе archive encodings дают те же all-table manifests,
exact float/signed-zero/large-int/Unicode values; wrong hash, truncated archive и
реальное CHECK violation блокируют qualification. Actual Linux3.11/SQLite3.46.1
с hash-locked APSW3.53.4.0/Zstandard0.25.0 и native loader прошёл97 targeted tests;
owned container/staging удалены, production unchanged. Required CI нового diff
и full old/current dataset proof ещё PENDING; последний CI38328025722 PASS относится
к предыдущему f8f55f6.
Bounded current-code cost profile на32,296-row disposable fixture13Oct01:11UTC
подтвердил full fixture manifests/integrity; profile timing не является uninstrumented
throughput или full conversion. Exact три terminal UNQUALIFIED candidate files
могут освободить4.66GB после отдельного owner approval; preflight/свежий сохраняемый
archive hash PASS01:19UTC, cleanup NOT_RUN. Рабочая база/source/archive сохранены.

Full archived-source trial13Oct01:45–01:54UTC завершён BLOCKED/exit2 за565.565s,
до общего900s deadline. Источник5.42GB читался через compressed image; readonly
mid-run metadata: physical reads0.78GB/RSS≈1.5GiB, candidate353MB. Transaction
откатился, итоговый candidate36KiB UNQUALIFIED; container removed/live unchanged.
Исходный CLI не сохранил класс отказа: причина UNSET, предел candidate2GiB только
кандидат объяснения. Corruption, fit/time полного dataset и READY этим не доказаны.
Follow-up классифицирует реальные SQLite FULL/CONSTRAINT/I/O и безопасные State
refusals без exception text/private bytes. Local41 rebuild regressions/quality PASS;
required CI этого follow-up PENDING. Новый полный запуск до диагностики и проверки
доступной capacity не выполняется; whole900s/CPU1/4GiB policy сохраняется.

Historical full qualification13Oct02:39–02:53UTC PASS: тот же decoded source
5,422,329,856bytes/hash61e4f087 преобразован в2,174,074,880bytes/hash1810335b
за833.369s общего времени, включая cleanup. Candidate cap3GiB, CPU1/memory4GiB;
все3,179,499 исходных runtime rows, private bytes/owner state, heads/revisions и
manifests всех5 таблиц совпали; source/candidate full integrity и hashes PASS.
Added188240 shared bodies/converted298250 heads. Root primary receipt/readback
PASS, container removed/G5 unchanged. Full input diagnostic до запуска:
686585 public rows/head identities PASS, fallback0, runtime column lower-bound1.586GB.
Candidate file превысил прежний2GiB cap: он был недостаточен, без ослабления gates
предел нового isolated output выбран по fresh disk reserve как3GiB. Это
qualification сохранённого historical dataset; текущая production DB не заменена
и не подтверждена этим PASS. Current-data/archive/CD/activation/G6 ещё REQUIRED.
Свежий read-only snapshot02:58UTC: G5healthy, DB22,361,518,080bytes/free3,201,523,712.
Free ниже CD archive preflight3GiB+512MiB. Exact cleanup трёх terminal failed
candidates4.66GB на этом snapshot ожидал explicit owner approval;
другие source/archive/qualified outputs сохраняются. Percentage не пересчитан.

Current checkpoint13Oct05:15–05:39UTC: после явного owner approval удалены ровно
три terminal UNQUALIFIED candidate files из reviewed whitelist. Повторные exact
path/identity, terminal PID/exit, отсутствие mounts/open handles и полный SHA256
сохраняемого recovery archive PASS. Освобождено4,663,693,312 allocated bytes;
source/archive identities сохранены, production DB/config не изменены. Primary
receipt: `rehearsals/failed-candidate-cleanup-20261013T051549Z.json` на VPS.
Read-only post-check05:37UTC: прежний CryptoWatcher7039151/G5afd107 healthy,
SQLite23,574,638,592bytes/WAL980,131,552, free6,657,507,328bytes; owned probes0.
Fresh GitHub05:39UTC: main9634558a, PR31c168ba8 open/unmerged,
required CI38335422635/ci-required115094771521 SUCCESS. Local docs checkpoint
остаётся unpublished; старый required PASS не относится к его будущему push.
Очищены временные результаты, рабочая база ещё не уменьшена. Historical PASS
5.42GB→2.17GB не квалифицирует текущие23.57GB. Next: доказать whole current-data
conversion/capacity/recovery в900s/CPU1/4GiB перед exact-target promotion и CD;
остальные archives/source/qualified outputs сохраняются. Progress54/58 по коду,
Goal ACTIVE, production compaction NOT_RUN; общий DoD ещё не выполнен.

Следующий bounded improvement13Oct: isolated copy использует один lazy
`executemany` на таблицу вместо отдельного Python execute на каждую строку.
Порядок source rows, полный typed manifest, проверки каждого shared body,
deadline/disk reserve, одна transaction и full readback сохранены; growing row
buffer не создаётся. Local48 rebuild/snapshot regressions и canonical quality
PASS. Linux native comparison на одинаковом32,296-row historical fixture:
baseline median3.545s / lazy3.349s в6 чередующихся прогонах, все5 table manifests,
heads/counts/shared bodies, candidate bytes и full integrity совпали. Это
ограниченный benchmark, не throughput/SLO или proof current conversion≤900s.
Required CI изменённой revision PENDING до нового grouped push; предыдущий
published CI38335422635 относится кc168ba8. Read-only current count probe превысил
observation timeout; exact child PID623799 при проверке уже отсутствовал. Его
результат UNSET, новый полный COUNT не запускается ради повторения. Current
source/archive qualification, promotion и CD по-прежнему незавершены.

Owner-approved continuation13Oct06:38UTC: после повторных encoded/decoded SHA256,
exact identity и mount/open-handle проверок удалены только две служебные копии
`rebuild-known-isdw3myk/work/source.sqlite3` и `rebuild-known-isdw3myk/archive.gz`.
Освобождено6,203,174,912 allocated bytes; root recovery archive и qualified
historical candidate сохранены. Primary receipt:
`rehearsals/duplicate-input-cleanup-20261013T063825Z.json`. Post-check06:40UTC:
тот же G5healthy, free12,426,526,720bytes. Это cleanup, не compaction live DB.

Current-data qualification13Oct06:55UTC: native online snapshot → compact copy →
full logical readback/persisted recovery qualification использовали один deadline
900s, CPU1/RAM4GiB/no swap, read-only production mount. Disposable127-row fixture
PASS5.002s с full source/candidate/restore integrity и hashes. Actual dataset
`current-dataset-qualification-oomrk9ni/status.json` завершён BLOCKED/exit137,
OOMtrue за644.404s на стадии capture/source integrity; полный source result,
conversion и recovery archive НЕ подтверждены. Probe container removed,
live container/source inode unchanged. Read-only07:20UTC: G5healthy,
SQLite24,259,596,288bytes, free12,100,636,672bytes, owned probes0.
Такой же full run не повторяется; fixture PASS не выдаётся за current proof.

Memory-admission correction учитывает фактический Linux resident memory наряду
с compressed image bytes до выделения SQLite cache. Native allocations/allocator
overhead не гарантированно покрывались старым logical-byte расчётом. Сохраняются
cache minimum512MiB, maximum1GiB, общий4GiB cap и все full integrity/CHECK/hash gates;
при недостатке headroom input отклоняется, cache не выделяется. Local75 affected
regressions/quality PASS, включая heap-pressure refusal без изменения CHECK policy.
Точная внутренняя причина peak RSS в прежнем combined capture/source phase пока
UNSET; этот fix не доказывает fit/time текущего dataset. Required CI новой revision
PENDING до grouped push. Next: ограничить и измерить peak source-validation memory,
затем current qualification≤900s; production replacement остаётся NOT_RUN.

Follow-up с точными phase/image diagnostics на CI-qualified9942665:
`current-dataset-qualification-8666hjv1` остановлен оператором за677.511s,
exit137/OOMfalse, до900s. Last sample: capture20,510,355,456 raw bytes,
retained compressed2,561,701,937bytes, peak RSS3,892,543,488bytes;
`image_frozen=false`, full integrity/cache admission ещё не начались.
Это ранний capture footprint, не доказанный дефект CHECK либо corruption DB.
Primary `operator-stop.json`/`status.json`; owned container removed,
live container/source inode unchanged. Source/candidate/archive qualification
НЕ получена. Resident admission fix local75/quality и required
CI38363013439/ci-required115182217179 PASS на9942665; он защищает следующий
этап cache, но не исправляет этот ранний capture allocation. Next: bounded
contiguous/spooled compressed capture buffer вместо отдельных heap frames;
сначала controlled resource/byte-equivalence qualification, затем whole current
run≤900s. Memory/time/full-check gates и retention не меняются.

Capture buffer correction: mutable native snapshot хранит compressed frames в
одной anonymous mapping с компактными offset/length/capacity arrays, а не в
отдельных долговечных Python bytes objects. Frame bytes/Zstandard/checksums,
holes/partial writes/truncate/freeze, compressed cap3GiB и full checks сохраняются.
Mapping резервирует virtual capacity; реальные pages заполняются по мере capture,
growing-buffer realloc с временной второй копией не нужен. Oversized frame growth
отклоняется до изменения прежних bytes; stale frame spans не выходят за arena cap.
Local77 affected regressions/quality PASS, включая repeated grow/shrink/truncate
byte equality и refusal preservation. Controlled Linux fresh-process ABBA на
synthetic1GiB/4KiB-page fixture: baseline RSS231.38–232.10MB, arena164.52MB;
wall7.06–7.24s против5.71–5.91s. Полные input/reconstructed SHA одинаковые;
retained compressed bytes136,331,264 неизменны. Это capture benchmark, не full
SQLite/current qualification. Primary `bench-arena-capture-resources.jsonl`
вне repository; owned test containers removed/live unchanged. Required CI новой
revision PENDING до grouped push. Next: qualified Linux native snapshot/recovery
fixture и один whole-current attempt с прежними900s/CPU1/4GiB, после required CI.

Arena actual-current proof attempt наe096715 завершён TIME_BUDGET_EXCEEDED,
exit3/OOMfalse за888.557s общего времени. Primary:
`current-dataset-qualification-gl9nijrj/status.json`. Snapshot24,689,618,944bytes
достиг frozen state около841s, retained3,066,605,344bytes/peak RSS3,104,382,976bytes.
На20.39GB capture RSS2.586GB против прежних3.893GB на20.51GB: ранний memory
overhead уменьшен в actual observed conditions. Полный source integrity, copy,
candidate и persisted recovery НЕ подтверждены; promotion NOT_RUN. Owned
container removed, live container/source inode unchanged. Native capture/byte
reconstruction из mutable WAL source расходуют почти весь900s budget до full
checks. Required CI38368296557/ci-required115199421362 SUCCESS наe096715.
Next: квалифицировать более быстрый способ получения согласованного source
без увеличения limits и без ослабления checks; возможная quiescence production
writer требует отдельной reviewed procedure/authorization до остановки бота.
Same online capture run не повторять; current compaction/Goal DoD не завершены.

Current-source maintenance13Oct2026 (owner-authorized stop/checkpoint/read/restart):
первый whole triala1rdgi00 завершён OOM/exit137 за200.535s с healthy baseline
recovery. Closed inputs теперь используют bounded contiguous frame arena,
как mutable capture: raw/gzip/ZSTD bytes/formats,3GiB compressed cap и все gates
сохранены.77 affected regressions/quality и required CI38384497657/gate115253623995
SUCCESS наa356b59. Synthetic Linux1GiB ABBA сохраняет SHA9cfb00a5…;
peak RSS233.65–233.70MB→165.66MB. Closed127-row full source/candidate/private-byte/
all5 typed manifests/persisted recovery fixture PASS4.737s; actual dataset не закрывает.

Fixed whole trial `current-dataset-qualification-tahz5_xs/status.json` завершён
TIME_BUDGET_EXCEEDED/exit3/OOMfalse за826.201s. Source25,532,657,664bytes
загружен около225s; retained3,170,524,825bytes/peak RSS3,827,253,248bytes. Full
source integrity не завершён; candidate/archive не созданы (readback11:00UTC).
Same crypto-watcher7039151/G5afd107 restart/health PASS, source inode unchanged,
owned probe removed, production replacement NOT_RUN. Прежняя15-minute whole
boundary/CPU1/RAM4GiB/no swap сохранена;90s зарезервированы внутри неё для recovery.
Candidate cap ужесточён5→4.75GiB; archive3GiB/disk1.5GiB/growth0.5GiB reserves сохранены.
Operator initial check ошибочно назывался PERSISTED_RECOVERY_FULL_INTEGRITY;
control flow/нет artifacts подтверждают SOURCE_FULL_INTEGRITY, future diagnostic
wrapper исправлен. Raw logs и подробные profiles находятся вне repository.

Independent preserved5.42GB archive integrity profile: encoded/decoded hashes
подтверждены; completed cases с SQLite512MiB/1GiB full integrity PASS86.882/89.486s
(cProfile overhead included), остальные ABBA cases остановлены по общему300s.
Отдельное completed comparison при SQLite512MiB: VFS32/256MiB full integrity
PASS81.464/88.173s, decompressions413,708/320,195. Эти limited samples не показывают
wall-time выигрыша; production/default cache не меняется. Owned probes removed,
live unchanged. Current read-only page metadata: source25.62GB,6,255,829pages,
freelist1page/4096bytes; точный эффект VACUUM по partially occupied pages не измерен.
Next: подтвердить ускорение verifier до новых production stops, сохраняя full
source/candidate/all-row/private-byte/restore proofs и900s/CPU1/4GiB. Простое
повторение текущего trial не разрешает обойти deadline. Current compaction,
receiver update, merge/CD, consumer activation и G6 остаются незавершёнными;
по коду54/58, новый audit percentage не рассчитан.

Verifier engineering preparation: local single-block read path сохраняет
EOF/boundary/deadline checks;77 targeted regressions/quality PASS. Controlled
500k hot4KiB-page ABBA: previous1.233–1.244s/current1.174–1.184s, same SHA625dbc06…;
это не whole-current qualification. Source-pinned native VFS prototype вне
repository использует SQLite3.53.4 extension API и Zstd1.5.7,32MiB cache и тот же
full integrity/CHECK. Isolated all5 typed rows/readonly write/CHECK corruption/
broken frame/closed WAL/lifetime controls PASS. Same-engine fixture выявила:
RO handle SQLite3.53.4 не сообщил нарушенный CHECK, RW handle сообщил; prototype
повторяет RW metadata + query-only existing VFS, физические source writes запрещены.
Эту особенность нельзя использовать для ложного source PASS.

Historical5.42GB full-integrity ABBA benchmark завершён PASS за374.079s:
source hash61e4f087…, CPU1/RAM4GiB/600s, same SQLite cache512MiB. Python76.780/
75.148s, native55.989/54.273s; все4 full integrity и final byte-identity PASS.
Observed gain≈27% относится к этому preserved dataset; fit текущего25GB≤900s
не подтверждён. Primary `native-vfs-benchmark.jsonl` вне repository, observer50104
terminal/exit0, owned probe removed/live unchanged. Не ждать и не перезапускать
завершённый benchmark. Native integration, bound/corruption tests, required CI
и whole current conversion ещё REQUIRED; production G5afd107 остаётся healthy,
main9634558a/PR31 открыт. Next: review/integrate verified native read path и
квалифицировать весь pipeline с неизменными gates/лимитами.

Native verifier integration в текущем PR31: source-pinned SQLite3.53.4 headers
и Zstd1.5.7 decoder, same-engine full integrity/CHECK через extension API. Source
xWrite/xTruncate запрещены, RW metadata/query-only сохраняет CHECK coverage;
private WAL/journal sentinel delegates и data deadlines/3GiB image/4GiB container
не меняются. Mutable capture остаётся Python, frozen source/archive/recovery
выбирают native в Linux image. Present broken helper fail-closed; portable Python
fallback сохраняет прежние checks. C cache eviction сначала инвалидирует slot,
поэтому failed decode не оставляет старую mapping на изменённый buffer.

Canonical build_native/build_vfs commands проверяют upstream hashes; Docker
содержит оба helpers и licenses. Native Linux5 substantive corruption/index/frame/
closed-WAL/bounds/deadline/lifetime/write controls добавлены; existing native,
codec/heads/rebuild/snapshot regressions подтверждены isolated134 PASS. Final
query-only dirty package32536891… Linux134 PASS/owned probe removed before publication.
Publishedb5fe042: required CI38405497548/ci-required115324970264 SUCCESS13Oct13:34UTC,
native build/Linux suite/Docker/image checks PASS. Previous38404196470 failed
on vendor CRLF/hash mismatch;38404704373 failed on native image fixture using
a file with readonly-readback sidecars. Exact upstream bytes preserved by
gitattributes; image fixture uses untouched closed backup. Gates retained; no
workflow rerun or production change. Full conversion/capacity still REQUIRED. Windows
local77 PASS/5 Linux skips с основанием, quality PASS. Required CI нового published
local cache-release change и весь current26GB900s conversion ещё REQUIRED. Native benchmark27% не
заменяет full source/candidate/manifests/private-byte/restore gate. Code54/58,
production G5afd107 не менялся; data retention/formulas/preferences сохраняются.

Whole historical pipeline13Oct2026: `historical-full-pipeline-cs8j2ly0/status.json`
на preserved5,422,329,856-byte source61e4f087 завершён TIME_BUDGET_EXCEEDED/exit3
за888.585s, OOMfalse. Source full native integrity PASS и новый recovery archive
492,787,743bytes построен; candidate2,174,074,880bytes остался UNQUALIFIED:
полные manifests/readback и independent persisted recovery не завершены.
Общий900s/CPU1/RAM4GiB/no swap не увеличивался; owned probe удалён,
production container/main inode unchanged. Preflight предыдущего trial отказал
из-за несовпадения UID private work с root-owned input; исправлен только isolated
work owner, production permissions не менялись. Повтор unchanged full run не нужен.

Local cache-release correction освобождает SQLite pages и unused glibc arenas
перед copy и candidate validation, сохраняя frozen image bytes, source query-only,
все integrity/manifests и общий deadline.49 targeted Windows tests/quality и135
Linux native regressions PASS; disposable test container removed/live unchanged.
Actual capacity effect ещё REQUIRED. Это resource
optimization, не доказательство fit полной26GB конвертации в900s. Primary CI
38405497548 относится к publishedb5fe042 до этой local correction; новый CI не запускался.

Source lifetime correction13Oct: source owner завершает unchanged-byte proof и
закрывает connection/VFS сразу после commit всех пяти copied tables. Candidate
full integrity/readback используют зафиксированные counts/manifests; source shared
count захвачен до release. Frozen buffers освобождаются до candidate validation,
cache допускается по current RSS с reserve512MiB внутри existing4GiB cap, максимум
1GiB. Missing capacity/source-release failure блокируют verification.53 Windows
targeted tests/quality и139 Linux native regressions PASS. Closed-source pipeline
на127-row fixture полностью PASS0.991s, включая independent persisted restore.
Synthetic3000MiB arena pressure ABBA подтвердил те же manifests во всех4 cases;
RSS при early release снизился≈3.23GB→84–87MB. Это lifetime/resource fixture,
не actual26GB timing. Copy-only case timings исключают cleanup в retained variant
и не доказывают ускорение whole pipeline.

Manifest batching сохраняет existing v2 byte framing/type tags, bounded small
buffer и streaming large values. Synthetic262,784-row ABBA: old0.2940/0.2844s,
new0.2093/0.2083s, одинаковый SHA941ac4ed…; effect всего pipeline не измерен.
Canonical proof-cache эксперимент отклонён:14MB fixture не сократила число native
checks540 и не показала устойчивого выигрыша. В tracked code он не включён.
Новый historical whole trial `historical-full-pipeline-g9q3duky/status.json`
завершён TIME_BUDGET_EXCEEDED/exit3 за887.528s, OOMfalse. Source full integrity
и unchanged-byte checks перед release завершены; candidate full readback и
independent persisted restore не завершены. Owned container0bd81c22… удалён;
прежний production container/main inode unchanged/running.
Один deadline900s/CPU1/RAM4GiB/no swap; stricter output caps3GiB candidate/1GiB
archive и2GiB disk/growth reserve, original archive read-only, production pause
и replacement NOT_RUN. Final candidate/restore gates REQUIRED. Separate readonly
byte check16.772s подтвердил candidate SHA1810335b… и полное совпадение с ранее
qualified2.17GB историческим artifact. Это доказательство того же результата
копирования; новый whole pipeline900s остался FAIL и current26GB не квалифицирован.

Native historical-window и compressed-record-cache experiments отклонены:
complete fixture equality PASS, устойчивого whole-path ускорения нет; собственные
experimental diffs сохранены вне repository и reverted. Whole-value conversion
сохраняется. Они не подтверждают fit текущего dataset в900s.

Candidate readback correction13Oct: большой revision больше не распаковывается
сначала для проверки compact head, затем повторно для manifest. Identity/revision
и exact typed projection проверяются отдельно; полный canonical payload со всеми
shared references восстанавливается один раз. Synthetic256-candle/1000-head ABBA:
old0.3136/0.3047s, new0.1194/0.1080s, exact bytes PASS; bounded14MB whole-path ABBA
не доказала устойчивого ускорения. Это component Evidence, не whole900s proof.

Добавлен explicit opt-in `--compressed-candidate-validation`: frozen candidate
full writable integrity/CHECK через existing VFS, затем unchanged all-row manifests
и independent reconstructed/file hashes. Default/routine CD не изменён.98 Windows
storage/head/codec regressions и146 Linux native regressions PASS, включая CHECK,
ref corruption, file mutation и query-only rejection. Required CI38405497548/gate
115324970264 повторно подтверждены13Oct18:16UTC только для publishedb5fe042;
эти local corrections ещё не опубликованы. Full historical/current conversion,
receiver installation, merge и applicable delivery остаются REQUIRED/PENDING.

Полная readback диагностика сохранённого2.17GB artifact1810335b с новым opt-in
завершила writable physical integrity62.212s, но all-row logical/byte proof не
завершилась в выбранном diagnostic budget300s: TIME_BUDGET_EXCEEDED304.916s.
Это не corruption и не qualified whole conversion. Probe removed, preserved
artifact и production unchanged.

Scanned-body correction: full verifier проверяет payload уже прочитанной row,
без повторного SELECT; encoding/size/hash/canonical gates сохранены. Только frozen
candidate validated-body cache64MiB размещён внутри прежнего512MiB overhead reserve;
runtime/copy остаются4MiB.104 Windows storage/head/codec,59 outcome/analytics/export/
digest/agent и152 Linux native tests PASS; malformed scanned body не подменяется
valid DB/cache row. Full readback protected2.17GB historical artifact1810335b:
3,179,502 framed rows (3,179,499 runtime +3 configuration/import),188,240 shared
bodies, all5 source manifests и reconstructed/file SHA PASS391.133s/CPU1/4GiB.
Source evidence относится к старой5.42GB копии61e4f087, не live dataset. Новый
600s diagnostic cap не меняет owner900s whole conversion; source→copy→persisted
recovery/current fit остаётся REQUIRED. Owned probe removed/live unchanged.
 Диагностика не повышает owner limit900s и не
разрешает новый production pause; unchanged большой trial не повторяется.

Cleanup13Oct18:57–18:59UTC после explicit owner authorization: удалены ровно4
полностью повторно проверенных derived files из trials cs8j2ly0/g9q3duky, освобождено
5,336,481,792 allocated bytes. Receipt `derived-cleanup-20261013T185750Z-49ffd1b1.json`
в VPS rehearsals PASS; readback всех4 paths отсутствуют. Сохраняемые recovery gzip
и qualified candidate проверены по hashes/decode; baseline7039151/G5afd107 healthy.
Production DB не заменялась. Fresh metadata18:59UTC: DB27,195,658,240bytes/
WAL7,762,112bytes, free disk9,544,220,672bytes. Эти4 cleanup задачи закрыты;
F34/current conversion/delivery остаются открыты, Goal54/58.

Frozen readback continuation13Oct: bounded historical public profile47,343 rows/
15,000 events/episodes выявил8,152 shared-body SQL lookups. Profile intentionally
PARTIAL; не full qualification. Local correction повторно использует только
verified immutable body в frozen candidate VFS, сохраняя scanned-payload validation,
miss/eviction lookup и final unchanged hashes. Generic runtime/copy codec продолжает
проверять SQL при каждом чтении.106 Windows targeted tests и canonical quality
PASS;154 Linux native tests PASS11.04s, persisted receipt
`cw-frozen-cache-receipted-524a8bb254524838bf290eebf6123523.json`.
Полный actual historical readback PASS366.347s: all5 source manifests,
3,179,502 rows/188,240 bodies, CHECK/integrity и reconstructed/file hashes
совпали.444,120 из444,178 body requests использовали verified frozen cache.
Primary receipt `cw-cached-candidate-readback-f9b1c66c937a420bac7d631c2c892e07.json`
в VPS rehearsals; owned probe removed, live unchanged. Disconnected suite не
учитывается как PASS; diagnostic missing APSW устранён pinned tools overlay.
Whole current900s qualification/CD остаются REQUIRED; counter54/58 unchanged.

Writer-cache continuation13Oct: fixed256MiB заменён adaptive admission≤1GiB
внутри4GiB cap, с128MiB source-cache reserve +512MiB overhead. Unknown RSS
сохраняет256MiB; insufficient capacity отказывает без изменения cache/data.
66 Windows rebuild/snapshot и158 Linux/native tests PASS; canonical quality PASS.
Record `cw-copy-cache-regressions-2c6f426aa2f44d21bc0dd05a06af288d.json` в rehearsals.
Whole historical trial `historical-copy-cache-am7q8k0k/status.json` terminal
13Oct20:41UTC/codeee775e2: TIME_BUDGET_EXCEEDED/exit3 за890.202s, OOMfalse.
Source full integrity/copy/unchanged checks passed; candidate physical stage
completed, но весь logical readback и persisted recovery не завершились.
Candidate UNQUALIFIED_THIS_RUN, source/candidate/recovery whole gate FAIL.
Observed boundaries: copy≈225→676s, full candidate readback≈706s; private cache
admission не доказал достаточного throughput benefit, сравнение с прежним trial
ограничено изменениями кода и host performance. Пиковый RSS≈2.04GiB внутри4GiB.
Controller2007257 terminal, owned container removed; прежний bot healthy/unchanged.
Outputs сохранены, deletion/promotion не выполнялись; current dataset не qualified.
Unchanged900s whole cap/source/candidate/recovery checks сохраняются.


Recovery proposal13Oct21:02UTC: existing whole trial остаётся FAIL; proposed
one-time gate заменяет только повторный recovery SQL scan на strict frame/full
independent decode/encoded+decoded hashes/size equality с уже checked source.
Source/candidate full CHECK/manifests/private-byte gates неизменны.33 local
controls и actual historical5.42GB decode PASS16.135s/CPU1/RAM512MiB; integrated
Linux fixture/source2015232bytes PASS1.052s, fixture_only=true. Это prototype evidence,
не current qualification. Owner decision14Oct разрешил описанный разовый gate;
canonical implementation/local/Linux regressions PASS, ordinary backup/CD policy прежняя.
Подробный diff/review и первичные receipts вне repository; ordinary backup/CD,
900s/CPU1/RAM4GiB и production replacement prohibition сохраняются.

Bounded source-image diagnostics13Oct: Zstd level3 не дал стабильного выигрыша
и отклонён.256KiB frame prototype с64KiB decoded cache slices сохраняет32MiB
cache;6 Linux corruption/bounds/write/eviction tests PASS. Historical5.42GB
archive61e4f087: full CHECK/integrity, reconstructed/file hashes PASS258.407s,
encoded547,373,779bytes; previous coarse256KiB cache prototype320.602s.
Receipt `sliced256-archive-check.jsonl` вне repository; owned probe removed,
live unchanged. Это independent component measurement, не whole/current
conversion PASS; canonical64KiB defaults не изменены. Для принятия нужны
полная проверка влияния и whole900s qualification текущего dataset.


Уточнение scope12Oct2026 после owner вопроса о размере базы: сначала устранить
подтверждённую избыточность history representation, сохранив весь согласованный
dataset. Read-only snapshot09:54UTC/G5afd10770: SQLite15,220,908,032bytes,
WAL980,131,552bytes;1100 streams. Earlier same-revision sample:1,230,965 candles,
estimated JSON253,867,297bytes;16-record event sample mean7392.6bytes. Bounded
covering-index read09:59UTC:264,332 event heads. Это не полный byte attribution;
indices/page overhead и outcome volume требуют отдельного измерения.

Локальный scope в этом же PR: event/outcome head хранит reference на неизменную
revision и малую query projection, все публичные history readers возвращают
полный прежний record. Legacy full heads читаются рядом с новым форматом;
существующие revisions/inputs не переписываются. Bulk conversion/удаление live
данных и изменение delivery gates в этот блок не входят. Старые binaries не
считаются совместимыми после первых новых head writes; recovery — forward-fix
либо verified reader новой representation, без DB downgrade.

Validation Plan для representation: REQUIRED local mixed legacy/new readback,
reordered revisions/conflict/reference identity, missing/cross-record reference
fail-closed, source preservation и no duplicate large payload. Existing
event/outcome/export/analytics/digest/agent suites проверяют прежние observable
records, cutoff/filter/completeness и signed provenance. Canonical local quality,
latest Linux PR CI/image и actual full source/archive/CD gates остаются REQUIRED.
Owner ещё не разрешил предложенную корректировку900→1800s; budgets неизменны.
Implementation checkpoint54/58 AC не пересчитывается; новый findingF34 связан
сAC-10-02/F15/F22, не создаёт нового продуктового требования.

Дополнительная read-only выборка64 heads каждого kind10:10UTC: event query
projection меньше full head JSON на94.32%, outcome на90.95%. Полный covering-index
count непересекающихся ranges в одном WAL snapshot:468,080 outcome heads.
При этих sample means и earlier264,332 event count устранение second-copy payload
оценивается примерно4.7GB; это estimate JSON, не измеренный размер SQLite после
конвертации. Перезапись heads не обязана уменьшать file bytes: потребуется отдельно
квалифицированное освобождение/reuse страниц. Existing-data conversion/compaction
не выполнялись и не входят в автоматический startup.

Подготовлена отдельная copy-only команда
`python -m cryptowatcher.history_rebuild` по [процедуре](runbooks/delivery.md#изолированная-конвертация-legacy-history-backup).
Она читает только закрытый backup с expected qualified SHA256 и создаёт новый
exclusive candidate; original/не-head column values/types и revisions сохраняются.
Full candidate integrity, все5 table rows/counts, resolved-head readback и
source unchanged hash/metadata обязательны. Unknown tables/schema/collation,
конфликт reference или numeric provenance, source sidecars, destination collision,
byte cap/disk reserve/deadline блокируют result. Source full qualification —
отдельный precondition exact hash; tool не утверждает PASS current live source.
Startup/CD этот tool не вызывают; privileged production promotion не разрешена.

Local:38 storage/CLI/rebuild tests PASS до последних дополнительных guards;
итоговые18 rebuild risk tests и canonical quality31 sources PASS. Контролируемая
fixture уменьшилась более30%, source bytes/mutes/settings/original JSON сохранены;
это не measured production shrink/capacity. Linux image check включает non-root
CLI invocation, source full check и all-row/candidate proof; latest CI этого diff
ещё REQUIRED. Budget≤900s/cap/source-size+1.5GiB reserve не ослабляют delivery gates.

CI38238946968/head0802b06: unit/static/build stages прошли, job FAIL на cleanup
private reconstruction fixture: directory UID10001/mode0700 внутри host bind
не удаляется GitHub runner UID1001. Fixture перенесена в container-owned `/tmp`
tmpfs; tmpfs virtual capacity2GiB позволяет проверить1.5GiB free-space reserve
на малой fixture, hard memory/swap256MiB и CPU1 остаются прежними. Production
permissions, application health и source/backup checks не меняются. Canonical
quality PASS; latest actual Linux image run этого correction ещё REQUIRED.

CI38241572841/head3db9149 PASS, включая real non-root reconstruction fixture.
Изолированная qualification сохранённого old5.42GB archive:
rebuild-known-isdw3myk завершилась BLOCKED2026-10-12T11:50:27UTC.
Decoded exact61e4f087/5,422,329,856bytes PASS; candidate3,339,722,752bytes создан,
но complete all-row/integrity/hash proof отсутствует. Exception class IntegrityError
на911.826s, failure_code UNSET в старом wrapper; не выдавать это за corruption
либо за verified shrink. OOMfalse, actual runner terminal, owned container removed,
G5 live unchanged/running и original archive preserved. Это source-code overlay
на qualified base953 image, не released artifact и не current15.65GB dataset.
Source/candidate сохранены как isolated diagnostic material; production NOT_RUN.
Дополнительный bounded readonly row-count probe не завершён за25s/host40s,
complete counts UNSET; повтор полного scan без устранения причины не выполняется.

CLI reconstruction теперь сохраняет safe failure_code/exit status для
TIME_BUDGET_EXCEEDED/DATABASE_CORRUPT/IO_ERROR, без exception body или sensitive
contents.21 targeted reconstruction cases/canonical quality PASS; latest CI
classifier diff REQUIRED.900s source/backup/reconstruction bounds и40min CD job
не изменены; предложенное увеличение требует owner policy decision.

Checkpoint12Oct2026: PR31 остаётся открытым; main9634558a/running G5afd10770,
installed receiver9569f32c. Required CI38227994274/head8613839d PASS. Root qualification
`/tmp/cw-pr31-job-v4fo3g8t` завершилась BLOCKED09:33:59UTC:
known5.42GB closed full integrity/exact hash PASS252.627s; LIVE source
TIME_BUDGET_EXCEEDED886.087s в full integrity, OOMfalse. Actual runner отсутствует,
owned containers удалены, все3 live units unchanged/running; ARCHIVE/receiver/switch
NOT_RUN. После этого локальный compact-head diff проверен85 affected tests и
canonical quality, затем дополнительный signed complete-outcome replay PASS;
latest Linux CI этого diff ещё REQUIRED. Новый full validation
не запускается ради повторения результата без устранения причины или решения
о policy. Этот primary FAIL supersedes прежние running checkpoints.

По явному разрешению владельца и повторной проверке raw61e4f087/archive d0cc5a88
удалён только exact qualified raw pre-9634558a…-1791716917.sqlite3.
Receipt `/opt/crypto_watcher/rehearsals/backup-archive-6crp6k3t/raw-removal.json`
PASS2026-10-12T07:15:52UTC: freed allocated5,422,342,144bytes, gzip780,833,702bytes
и archive-preparation receipt сохранены; other backups/production DB untouched.
Readback07:19:08UTC подтвердил raw отсутствует/archive присутствует/free13,625,688,064bytes.
Growing live history не позволяет рассчитывать на полный raw pre-deploy duplicate.

В этом же PR подготовлен validation-only Zstandard0.25.0/hash locks и bounded
persistent `.sqlite3.zst` вместо нового raw CD backup. Full snapshot integrity,
независимый persisted archive full integrity, decoded/encoded SHA256/size,
private4GiB cap/CPU1/no swap и900s stage gates сохранены. Source snapshot/native
SQLite bytes не меняются. Incomplete archive сохраняется, existing backups не
переписываются; recovery сначала распаковывает новый isolated raw файл и сверяет
native integrity/все5 таблиц по [процедуре](runbooks/delivery.md).
126 targeted Windows tests и canonical quality PASS: WAL race/reader release,
native all-table restore/exact hashes, CRC/truncated/corrupt archive, output limit,
deadline, existing-output preservation, capture/CLI/storage/bootstrap regressions.
OSV querybatch12Oct07:36:09UTC для new zstandard0.25.0 — active findings0;
это не assessment OS packages. Self-review подтверждает exclusive destination,
fsync, released live reader, независимую проверку persisted archive до switch,
safe hash/size proof в capture и native restore в реальном Linux image check.
Новый latest Linux CI/image и3-stage VPS qualification ещё REQUIRED;
после PASS — exact receiver update, merge, automatic CD и bounded application checks.
Implementation checkpoint54/58 AC сохраняется; delivery/activation/G6 остаются незавершёнными.

CI38218817527/head05388ec:429 tests PASS,5 operator fixture failures до image build.
Fixtures ещё ожидали2-stage/one-wheel interface. Исправлены в том же PR:3-stage
fixed deadlines, отказ при timeout live либо archive, hash-pinned second wheel,
3 readonly inputs и единственный owned temporary writable output, cap/cleanup/all3
unit identity assertions сохранены.8 targeted qualification tests и canonical
quality PASS; полный required Linux прогон актуальной revision остаётся gate.

CI38219144588/heada61b5a4:434 tests PASS, одна existing analytics recovery fixture
FAIL до image build. Причина: request использует advanced exchange clock, а fixture
запускает work в constant NOW; при задержке CPU job ещё не due. Test exchange clock
зафиксирован в обеих affected fixtures, production scheduling/retry/conflict guards
не изменены.2 targeted recovery scenarios и canonical quality PASS; current CI
и actual source/backup qualification остаются REQUIRED.

CI38219504549/head4e2dd4f:435 tests/static/dependency/build PASS; real image check
обнаружил некорректное сравнение physical hash независимых stdlib/APSW backups.
Image check теперь требует archive encoded hash, decoded hash собственного pinned
snapshot и native restore/full integrity плюс точное совпадение всех rows5 таблиц
со stdlib backup. Cross-API header normalization не является потерей данных;
обязательная exact-byte identity archive↔snapshot и source row equivalence сохранены.
Full Linux image qualification актуальной revision остаётся REQUIRED.

CI38219821534/headec0dd5f PASS,435 tests/quality/build/native image restore checks.
Root operator `/tmp/cw-pr31-job-3x3mcvgs` PREFLIGHT PASS, затем setup FAIL07:51:18UTC
до SQLite execution: loop unpacking Zstandard wheel затёр Docker container name.
Изменений runtime/receiver/production DB нет; все3 live units unchanged/running.
Loop member имеет отдельное имя; fixture дополнительно требует valid owned
container prefix/name. Latest CI и новая qualification исправленного helper REQUIRED;
предыдущий setup failure не считается результатом integrity/performance measurement.

Root qualification `/tmp/cw-pr31-job-nms9x9rt`/head41f5f67 завершилась BLOCKED
2026-10-12T08:14:26UTC: closed5.42GB full integrity/exact61e4f087 PASS186.444s,
retained673,056,291bytes. Live TIME_BUDGET_EXCEEDED в final reconstructed hash;
trace показывает возврат full_check и metadata read до истечения общего stage
budget, но complete source proof отсутствует. Durable archive stage NOT_RUN,
receiver unchanged, OOMfalse, own container removed/all3 live units unchanged.

В том же PR prepared validation-only adaptive SQLite cache:512–1024MiB по
фактическому frozen compressed image/index, с512MiB reserve для interpreter/native/
decoded caches, суммарно≤4GiB. Source/backup checks, hashes, CPU1/no swap и885/900s
deadlines сохранены. Добавлены diagnostic native cache hits/misses/used bytes
при завершении/отказе integrity; ускорение пока не объявлено доказанным.
46 targeted archive/cache/qualification/RAM regressions и canonical quality PASS;
latest CI/image и новая complete source+durable archive qualification REQUIRED.

CI38224172390/head291fa8d PASS. Root `/tmp/cw-pr31-job-q_rqvjnm`08:37–08:48UTC:
closed archive PASS208.270s/cache1024MiB, затем LIVE_SNAPSHOT isolated OOM137 до
full integrity/adaptive cache. Процесс terminal, own container removed, all3
units unchanged/running, receiver NOT_RUN. Возможное удержание native allocator
предыдущего closed stage — ограничение combined-process verifier; это не PASS
или proof failure production CLI, где stages уже отдельные containers.

Оператор теперь изолирует каждый из3 REQUIRED этапов новым container/process,
каждый с885/900s fixed budgets/4GiB/CPU1/no swap, новым reserve check и exact
cleanup перед следующим. Failure любого этапа прекращает flow и блокирует
receiver/merge; all3 runtime IDs и preserved source archive проверяются в конце.
37 targeted cache/archive/qualification regressions и canonical quality PASS;
actual isolated live/durable archive qualification и latest CI/image REQUIRED.

Base `9634558a654d5f13279ff0e76710585bf1105d66`, branch `codex/backup-validation-performance-20261011`, root worktree. Fresh main/clean checkout, strict required `ci-required`, production Environment, отсутствие открытых PR/active Actions подтверждены GitHub API2026-10-11T11:36:44UTC. Основание — устранение failed delivery в согласованной whole-project Goal; отдельный implementation scope не добавляется.

PR30 merged2026-10-11T10:52:33UTC, head2495ac037/baseb13081e/squash9634558a, required CI38131846878 PASS370 tests. Operator receiver update10:50:50UTC PASS: installed receiver9569f32c, target/SSH/runtime/database сохранены. Automatic CD38133517275: merged Linux3.11 suite370 tests PASS/build PASS, delivery FAIL11:23:36UTC на BACKUP_AND_VALIDATE с `docker exit 3` — TIME_BUDGET_EXCEEDED. Полная проверка source DB прошла: backup stage следует после неё. Нельзя считать backup пригодной, corruption установленной или CD завершённой. Точные времена copy и destination validation UNSET; CLI885s является общим budget обеих фаз.

Owner snapshot11:10:37UTC: candidate9634558a/imagefe4ac4a, PRE_SWITCH/BACKUP_AND_VALIDATE, DB5,305,679,872bytes/budget900s; сохранённая копия `pre-9634558a654d5f13279ff0e76710585bf1105d66-1791716917.sqlite3`. Последующие readbacks11:40:56/11:57:57UTC подтвердили прежний running unit и отсутствие switch. До qualification никаких retries/restore/deletion. Предложенный validation-only cache512MiB проверен на closed copy, но не уложился в бюджет; изменение cache отклонено как неподтверждённое. Scope: установить I/O/limits по bounded read-only snapshot, затем выбрать исправление по Evidence. Установленный privileged receiver, target, SSH, schema, history и cap900s сохраняются; speculative initial push не выполняется.

Validation Plan этого hotfix: REQUIRED operator exact backup/image/latest runtime readback и bounded full integrity measurement в offline temporary container (networknone, backup-only read-only mount, bounded CPU/RAM, cleanup exact owned identity); REQUIRED local canonical quality и существующие storage/CLI/corruption/shared-deadline/WAL-snapshot tests в locked Windows environment; REQUIRED latest PR Linux3.11 CI и immutable build; REQUIRED automatic CD full source+backup checks/source/image/health/persistence/dual-market4TF readback; REQUIRED bounded actual backlog/diagnostic freshness qualification после поставки. Недоступная обязательная проверка остаётся PENDING. Non-goals: weakening integrity/timeout gates, data pruning, повтор migration, live restore/downgrade, receiver access expansion и торговая активация.

Operator readback2026-10-11T11:40:56UTC подтверждает FAILED_PRE_SWITCH/BACKUP_AND_VALIDATE, switch_started=null, прежний running sourceafd10770/container7039151a/image1120624b. Closed backup5,422,329,856bytes сохранена. Первый probe с read-only single-file mount не открыл SQLite connection (OperationalError), integrity measurement не началось; это не integrity FAIL. Temporary container удалён, running unit/backup не изменены. Исправленный diagnostic допускает immutable URI только для этой закрытой exact backup без sidecars; live database всегда сохраняет обычное WAL-aware чтение/locking.

EV-BACKUP-512, owner primary receipt2026-10-11T11:42:46–11:57:57UTC: exact closed backup5,422,329,856bytes/UID10001/mode0600/WAL header2,2, SQLite3.46.1, cache512MiB/effective mmap2,147,418,112bytes, offline networknone/read-only mount/CPU1/memory3GiB. Full integrity прервана SQLITE_INTERRUPT: BUDGET_EXCEEDED, elapsed906.861s при inner budget885s; userCPU28.384s/systemCPU120.513s/read_bytes2,907,975,680. Outer910s timeout, exact temporary container удалён; live unit/running state/backup неизменны, production files changed=false. Full integrity PASS/FAIL отсутствует. Progress callback не прерывает блокирующий I/O немедленно. Wall time существенно выше CPU; источник ожидания/ограничения ещё UNSET. Этот замер отличается от EV-INTEGRITY-FULL файлом, resource limits и состоянием host/cache, поэтому не доказывает сравнительное ускорение либо замедление от размера cache. Cache128MiB в коде сохранён, повтор full scan/deploy не запущен; следующий шаг — короткий read-only I/O/pressure/limits snapshot и bounded sequential read закрытой копии.

Local backup observability: CLI сохраняет прежний success status и добавляет safe copy/integrity/total durations; shared-budget failure содержит phase и durations, без DB payload/config/path. Timeout во время copy теперь тоже classified exit3; source и incomplete destination сохраняются.43 existing affected storage/CLI/recovery tests и canonical quality/docs/Ruff/format/mypy PASS, Windows locked environment2026-10-11; snapshot/WAL concurrency, shared deadline, corruption, no-mmap и existing-destination refusal остаются обязательными. Performance change и required CI/CD ещё PENDING; первичный failure не заменяется PASS по маленькой fixture.

EV-CD-FAILURE-ARTIFACT, primary GitHub readback2026-10-11T11:52:45UTC: CD38133517275/artifact11700577981, verified zip digestf6e08b5b7b42bf5ddda54a1556b625c15e81601bd0a2a53dbbe98bcd334c0b10, deployment.json0bytes. Подтверждён дополнительный gap F27: receiver пишет terminal failure JSON в stderr, workflow сохранял только stdout. Hotfix включает CI-side [deliver_release.py](../scripts/deliver_release.py): safe nonempty success/failure record, exact source/build/bundle/post-check proof для PASS, no raw SSH/exception output, no retries, strict existing host/key/forced command. Прерывание оставляет UNCONFIRMED; error никогда не повышается до PASS. Installed privileged receiver/SSH/target/permissions/job timeout не меняются.32 targeted capture/recovery tests и canonical quality PASS Windows/offline; original failed artifact сохранён, актуальная Linux CI и actual delivery остаются REQUIRED. Первичная ошибка parameter IDs в Windows fixture исправлена локально явными короткими IDs; behaviour assertions сохранены. Scope hotfix теперь application validation плюс исправление CI reporting, без нового host setup.

Self-review CI capture PASS: только trusted main/production job вызывает helper с теми же protected SSH inputs; bundle hash/source/build сверяются до единственной передачи; success требует receiver source/artifact/post-check proof, чужие fields/raw errors отбрасываются. Даже interim UNCONFIRMED record связывается с safe attempted source/build/target; итоговый artifact schema `cryptowatcher.delivery-record.v1`. Existing artifact не перезаписывается, callback не выполняет retries/cleanup или host setup.13 capture tests после final identity уточнения и canonical quality PASS. Initial push/PR всё ещё не выполнены: полный hotfix ждёт установления причины backup timeout и последующей qualification, а не speculative deploy.

EV-R2-RECOVERY, owner readback11:10:37UTC: existing private recovery bundle hash9560abdd совпадает, prepared master hasha4a54701 валиден и ещё не активирован; consumer67716b3, host config hashfb734710/0600root, все5 vault tables0, ingestfalse/master/max-age blank, trading flagsfalse/runtime dry_run-stopped. Existing encrypted R2 backup receipt10:47:17UTC подтверждает ciphertext/manifest readback и recovered-key decrypt, key-bucket AccessDenied. EV-R2-FULL-RESTORE: owner primary receipt в чате «Bybit Trading», 2026-10-11T11:29:48.413219UTC, ISOLATED_R2_RESTORE_PASS, source67716b3/schema20261010_0117, dump hash33041dd2;124 tables/239118 rows, inventory matches dump TOC, invalid indexes/unvalidated constraints0, dry_run-stopped; original config/container IDs сохранены, disposable test container удалён. Workspace `restore-20261011T112839Z-b27f46f33622404a94a6e4907e375592` внутри existing initial R2 backup directory. Source row-hash snapshot, roles/ownership/grants и scheduler этим не подтверждены; production vault был пуст. Recovered-master exact-image crypto ещё PENDING; повтор restore не нужен. Используется подготовленный ключ и существующий private owner-managed recovery bucket, второй master/bucket не создаются. Consumer R2 scheduler/retention остаются самостоятельной работой своего проекта. Context cap900s утверждён; первая production credential configuration сохраняет concrete operation gate.

EV-R2-KEY-CHECK, owner primary receipt в чате «Bybit Trading»2026-10-11T12:08:33.866812UTC: recovered bundle9560abdd/prepared master comparison выполнены до стадии recovered_key_crypto, но isolated exact-image helper завершился exit1; RECOVERED_VAULT_KEY_REHEARSAL_UNCONFIRMED. Контейнер удалён, master/config/production DB не активированы/не менялись, upload/provider calls отсутствуют. Причина runtime error UNSET; counterpart выдал synthetic diagnostic без owner keys/password. Повтор qualified full restore и повторная генерация master не нужны; credential activation остаётся заблокирована до crypto PASS.

G6 preparation requalification2026-10-11T12:53:36UTC: исследовательская матрица вне repository обновлена по fresh origin/main9634558a,58 AC/33 findings без потерь. Прежнее blanket code_implemented=true/58 на363bd04e superseded: текущий implementation checkpoint54/58, четыре AC-06-02/06-05/12-02/12-03 IN_PROGRESS. Это не новый полный AUDIT или READY review: все критерии требуют финальной индивидуальной сверки, code locators/прошлые fixtures не заменяют latest runtime Evidence. Учтены full R2 restore PASS, master crypto UNCONFIRMED, failed pre-switch backup и empty failure artifact; final percentage не пересчитан. Independent preparation завершена в доступном scope.

External blocker revalidated2026-10-11T12:54:55UTC: GitHub main9634558a unchanged, open PR0, active/queued/waiting/pending Actions0; deployment не перезапущен. Counterpart «Bybit Trading» завершил текущий turn, повторно получен тот же crypto receipt12:08:33UTC, нового PASS нет; там выданы две read-only проверки прав доступа, не повторять восстановление с паролем. Дальнейшее продвижение BLOCKED до owner I/O/pressure/limits64MiB readback и crypto qualification: forced CD key не предоставляет shell для этих операций, подтверждённого live process/job для ожидания нет. Этот же blocker повторился в последовательных goal continuations; доступный независимый scope/fixtures/preparation завершён. Сохранены локальные commits/полный scope/failed copies/user state; не выполнять speculative push/deploy, повышение cache/budget или privileged credential activation. Продолжение использует существующую Goal после получения нужного input, новая Goal не нужна.

EV-IO-KEY, owner primary receipt2026-10-11T14:39:48–14:40:26UTC: closed backup5,422,329,856bytes/unchanged; sequential64MiB read PASS0.730s/physical65MiB,87.67MiB/s.30.008s host snapshot: cgroups CPUmax/memorymax unlimited, io.max empty, OOM0/memory pressure0; available7185.83MiB/free17.33GiB. CryptoWatcher physical writes58.387MiB/5914 operations and reads8.117MiB/1471 operations, CPU13.125s; host IO/CPU pressure present. Это короткий current snapshot, не доказательство состояния при прежнем timeout, sustained disk throughput или конкретной причины SQLite задержки. Sequential bandwidth не объясняет full integrity автоматически; random page access/cache residency остаются гипотезой. Все3 live containers сохранили identity/running state; config/DB mutations отсутствуют.

Crypto runtime на consumer67716b3: UID65534 читает каталоги0755, но root-owned source0600 недоступен; contracts_import завершается PermissionError/errno13. UID0 actual synthetic encrypt/decrypt PASS. Это подтверждает mismatch пользователя прежнего isolated rehearsal и доступности image source; настоящий master не читался и не активирован, его recovered-key qualification по-прежнему PENDING. В чате «Bybit Trading» подготовлена исправленная isolated root rehearsal существующего master/bundle, без permission changes production или новой генерации ключа.

EV-BACKUP-PREFETCH, owner primary receipt2026-10-11T15:03:04–15:18:08UTC: exact closed backup5,422,329,856bytes/image9634558a, offline CPU1/memory7GiB/no swap, прежние cache128MiB/mmap policy и shared885s. Sequential read completed49.811s (103.82MiB/s); physical reads5,354,242,048bytes, cgroup memory.current2,585,202,688bytes. Затем full integrity TIME_BUDGET_EXCEEDED891.109s: userCPU24.548s/systemCPU144.373s/physical reads8,436,711,424bytes. После предварительного чтения потребовалось ещё3,082,469,376bytes физических чтений; полная копия в RAM не подтверждена. Причина reclaim/cache behaviour UNSET, отсутствие OOM не доказывает residency. Reporting helper exit0 не означает PASS. Exact owned container removed; backup/все3 live containers unchanged/running; production files changed=false. Prefetch отклонён: этот подход не включать в application/CD и не повторять тот же full scan. Cache128MiB/900s production cap/full checks сохранены; initial push/PR/deploy отсутствуют.

Следующая qualification — отдельная byte-identical копия closed backup в disposable tmpfs, затем existing full check_integrity на writable connection (как backup gate) с CHECK-constraint negative control и SHA256 original→RAM→after validation. Это новая проверка влияния физического I/O, а не ещё один read-ahead/cache experiment. Scope только closed backup read-only mount; scratch writes только внутри owned temporary container, networknone/user10001/capdropALL/read-only root/CPU1/no swap. Memory cap = rounded backup size+384MiB; MemAvailable требует cap+1536MiB reserve, иначе preflight останавливается без запуска. Tmpfs size = rounded backup size+32MiB. Shared diagnostic budget300s, outer325s; deadline включает copy/full validation/hash readbacks, actual production budget/gates не меняются. Cleanup exact name/label/ID/image/security/mount/tmpfs identity; original backup/live IDs обязательны в readback. Production performance default ещё не меняется; RAM approach требует PASS этой qualification, отдельного design review для растущей DB и subsequent actual CD, а не автоматического переноса diagnostic allocation в production. Local syntax/valid/corrupt header/deadline/changed-copy rejection PASS на маленьких Windows fixtures; это не Linux tmpfs или VPS timing Evidence. Дополнительное ограничение F25: SQLite3.49.1 Windows CHECK-constraint fixture в mode=ro отличалась от writable validation; соответствие Linux3.46.1 UNSET. Новая qualification явно проверяет writable negative control; существующая required backup constraint regression сохраняется.

EV-R2-KEY-PASS, owner primary receipt в чате «Bybit Trading»2026-10-11T14:47:06.871531UTC: RECOVERED_VAULT_KEY_REHEARSAL_PASS, consumer67716b32/schema0117/image972cb5e1. Existing recovered key roundtrip PASS; wrong binding/damaged ciphertext/wrong key rejected. Application env/live API/PG unchanged; owned container removed; no production DB writes/activation/provider calls/scheduler/R2 upload. Receipt `key-check-20261011T144652Z-5e0d4f67a19f4c4e862f20dce080efe9` в existing private R2 restore workspace. Этот receipt supersedes UNCONFIRMED12:08 для исправленной isolated rehearsal; qualification prerequisite снят. Production keyring/binding activation и recovery реальных новых encrypted rows остаются PENDING, synthetic payload не подтверждает их. Повтор восстановлений/key generation/permission changes не требуется; unrelated R2 HTTP501/scheduler work сохраняется в counterpart scope.

EV-BACKUP-RAM, owner receipt2026-10-11T15:44:52–15:48:32UTC: exact closed backup5,422,329,856bytes/image9634558a/SQLite3.46.1. Copy/hash complete109.940s, writable full integrity PASS141.661s (31.721s validation, no additional physical reads during it), CHECK negative control PASS. Whole copy/full validation/original+RAM hash readbacks PASS201.800s; SHA25661e4f08763b2db0dc437c5103af94bb3185269b0b731eff719a587c145ec6e22. Memory cap5,825,888,256bytes/no swap/CPU1, OOM=false, exact scratch container removed; original backup/all3 live containers unchanged/running. Это квалифицирует RAM validation этой копии, не растущую live DB или будущую CD revision.

Implementation design до кода: explicit CLI `--validation-directory` для state-check/backup, default legacy disk path сохраняется. CD receiver создаёт private tmpfs `/validation` только в offline check container, snapshot bytes получает из WAL-aware diagnostic page_count×page_size; cap=rounded snapshot bytes+512MiB, scratch=rounded bytes+64MiB, host reserve1536MiB/CPU1/no swap. Эти limits относятся к временной проверке, не runtime бота. Недостаток памяти/space или growth сверх allocation останавливает pre-switch stage, не переключает на ослабленную проверку. State-check копирует pinned WAL snapshot SQLite backup API→RAM, освобождает live reader до full writable integrity и возвращает metadata того же validated snapshot. Persistent backup создаётся прежним pinned online copy; после закрытия проверяется byte-identical RAM copy/full integrity/SHA256 persistent file до и после. Source/backup schema/history/settings не меняются; shared900s cap и все source+backup gates сохраняются. Рост DB требует fresh resource qualification; RAM не объявляется универсальным решением capacity. Installed receiver9569f32c требует одного hash-bound update до merge через existing operator installer; SSH/target/permissions не расширяются. Validation: meaningful WAL race/full constraint/corruption/hash mismatch/deadline/resource/mount/cleanup tests, canonical local quality, required Linux CI/image, operator receiver receipt, actual automatic CD и bounded backlog/freshness readback. Runtime fixes предыдущих PR остаются pending delivery.

Local RAM hotfix/self-review2026-10-11:70 affected real storage/CLI/closed-copy/hash/WAL/deadline/capture tests PASS;13 RAM tests,3 installer и16 capture scenarios PASS (32 successful non-bootstrap scenarios следующего batch).39 bootstrap identity/preservation/fault tests PASS после обновления LF-normalized receiver hash96d9b739. Initial bootstrap source pin не ослаблен; прежняя выполненная процедура не повторяется. Canonical docs/Ruff/format/mypy29 files PASS. Image checks расширены реальным isolated Linux tmpfs source/backup SHA256/negative constraint check; их execution ждёт required CI, локального Docker нет. Schema1/NONE, runtime config/JSON/history/settings не меняются. Initial push готовится только после scope/self-review/checkpoint; merge ждёт latest required CI и operator receiver receipt, actual CD/capacity/signed activation/G6 остаются PENDING.

#### Hotfix runtime capacity / CD recovery — 2026-10-11

PR31 head1de4e81, required CI38154596464:398 tests PASS, один existing workflow trust test FAIL до Docker build. Причина: test искал StrictHostKeyChecking в YAML после переноса SSH command в canonical deliver_release.py. Проверка перенесена на actual client source с сохранением strict host/known_hosts/identity/batch assertions и проверкой вызова helper из workflow; behavioral capture tests проверяют реальные SSH argv. Gate не ослаблен, первый CI не считается PASS; исправление в том же PR, latest CI/image и receiver receipt остаются обязательными. После нового head operator использует свежий immutable installer pin, не initial1de4e81.

PR31 latest head139102b/CI38155013933 PASS399 tests и real Linux Docker tmpfs/backup-hash/restart checks. Owner receiver preflight PASS, но live snapshot8,982,642,688bytes / available7,471,837,184bytes отказал по RAM до execute. Installed9569f32c/G5afd107/DB/SSH preserved; UTC owner receipt UNSET. Plain RAM design superseded для CD; failure не доказывает corruption. Update/merge/deploy не выполнены.

Correction design: offline validation cap4GiB/no swap/CPU1, compressed limit3GiB + margin1GiB, scratch64MiB, host reserve1536MiB, unchanged production900s stage cap. APSW3.53.4.0 сохраняет full CHECK/integrity и byte SHA proofs; source reader закрывается до scan. Latest pushed headbd3123d/CI38175858872 PASS. Actual level2 qualification2026-10-11T21:21–21:45UTC: known backup full/hash PASS561.612s, compressed1,483,937,093bytes; current WAL snapshot истёк внутри full integrity на собственном885s, proof отсутствует, OOMfalse. Owned probe removed; CryptoWatcher container unchanged/running. Consumer API independently changed, cleanup assertion masked primary timeout; receiver update NOT_RUN. Follow-up в том же PR:64KiB lossless blocks, bounded8-block write coalescing, direct streamed reconstruction hash,512MiB private SQLite validation cache within existing4GiB cap, physical sector4096. Source/schema/settings/data не меняются. Sample16MiB:64KiB blocks ratio0.163 / encoding0.137s vs4KiB:0.2745 /0.283s; это не whole-DB proof. Local24 targeted cases + canonical quality PASS; включены partial/sparse writes, truncation/zero extension, frozen hashes, complete readback при concurrent change и сохранение primary failure. Latest CI/Linux image/new-module VPS proof PENDING. Read-only metadata2026-10-12T05:18:42UTC: logical DB13,416,587,264bytes, WAL980,131,552bytes, free disk9,960,628,224bytes. Raw pre-switch backup не помещается. Existing CryptoWatcher image archives/rehearsals менее1GB, backups6.20GB (known qualified5.42GB основная часть); unneeded status не доказан самим размером. Подготавливается private gzip copy known backup и isolated restore byte/native readback без удаления оригинала; raw removal/cleanup требует конкретной operator authorization. F22/F27 открыты, код54/58; percentage не пересчитан.


Основание: согласованная whole-project Goal, recovery после failed production CD38102363430 и operator read-only snapshot 2026-10-11 07:13–07:14UTC. Base `363bd04e680621f1aa5a45da25a2f37d1a75bdc1`, branch `codex/runtime-backlog-recovery-20261011`, root worktree; fresh GitHub main/protections/production Environment и отсутствие открытых PR проверены07:25UTC. Runtime остаётся на G5 `afd10770aa3a330e6c5a28e3ea85a676060e6c29`, healthy; candidate image363bd04 загружен, deployment record/backup/lock отсутствуют, switch не выполнялся. Точный timeout stage старого receiver не установлен; исправление не предполагает, что backup уже запускался.

Под нагрузкой выявлены повторно открытые F20/F22/F27: synchronous analytics блокируют общий event loop; comparator cadence1/5s ниже наблюдаемого поступления; persistent ingestion snapshot отстаёт более30min при свежем heartbeat. DB3,711,741,952→3,719,077,888bytes за snapshot, pending outcomes158428/comparisons61150, events66459→66504. Это короткие замеры, без экстраполяции SLO или достаточности26GB свободного места. AC-06-02/06-05/12-02/12-03 IN_PROGRESS в текущей реализации до regressions и runtime qualification; прежнее58/58 по коду пересмотрено до54/58, denominator58 сохранён. Аудиторский процент не пересчитывается.

Scope hotfix: bounded asynchronous analytics на отдельном read-only snapshot connection, atomic writer-side apply/CAS и actual availability time; comparator/outcome batches по measured backlog, сохранение formulas/version/source/protected data; свежая bounded diagnostics и повторная проверка dual-market/TF readiness; bounded snapshot backup с полной integrity validation; stage-specific CD records/timeouts и exact-target installer обновления установленного receiver. При необходимости одного operator setup шага он готовится до merge, с immutable source/hash, preservation existing receiver/target/SSH/secrets и без deployment/DB restore. Не обходить current forced-command restriction.

Non-goals: удаление истории/queues/backups, изменение thresholds/profiles, повтор JSON migration, DB downgrade/restore, изменение trading Strategies/credentials, отключение Cloudflare/auth/gates. Consumer binary67716b3 и readyz200 подтверждены snapshot; integration disabled, keyring/binding/max-age unset, radar vault bindings/receipts0, public HTTPS403. Activation остаётся dependent blocker; GET404 не доказывает отсутствия POST route.

Validation Plan: REQUIRED local quality из root locked dev environment; targeted async responsiveness/CAS/causal availability, backlog/drain/restart, diagnostic freshness, backup/corruption/timeout and receiver preservation/stop fixtures. REQUIRED latest PR CI Python3.11/Linux/full regression + artifact checks; exact merged artifact/backup/source/image/health/persistence/dual-market4TF production post-checks. REQUIRED operator receiver update receipt, если установленный restricted receiver не может обслужить проверенную процедуру; недоступность не N/A. Extended controlled realistic history/backlog + snapshot restore измеряются без production mutations. Длительный live monitoring, очистка и восстановление поверх живых данных не поручены.

DoD: исправлены подтверждённые capacity/diagnostic/delivery defects, existing immutable records/settings/queues сохранены, CI/review PASS, hotfix merged и actual CD PASS. Следом qualified consumer keyring/binding/freshness/HTTPS + historical runtime smoke и финальный G6 audit/docs PR; readiness не объявляется по одному healthy heartbeat.

#### Необходимое продолжение delivery recovery после PR29

Primary records2026-10-11: PR29 head2139bf6/required CI38127039913 PASS, squash mainb13081e0e9d29bf8b6ff25fe538ef4736c263402, merged09:05:44UTC. Automatic CD38127147184 выполнил полный Linux3.11 прогон350 tests PASS, build/image validation PASS; production delivery FAIL09:15:41UTC: `Operation failed: docker exit 2`, phase `CHECK_STATE_INTEGRITY`. Эта phase предшествует backup/switch; actual running version и подробная причина CLI требуют operator readback. Увеличение budget, corruption или rollback не предполагаются. Новый deploy не запущен.

Recovery branch `codex/integrity-delivery-recovery-20261011`, baseb13081e/root, clean main безопасно синхронизирован после подтверждённого merge. Scope: установить точную причину обязательной integrity validation, исправить подтверждённый failure и его diagnostics в necessary hotfix, сохранив полную проверку state/backup, history и delivery gates. Non-goals: quick_check вместо full integrity, удаление DB/queues, повтор migration, restore/downgrade или изменение consumer Strategies. Validation: REQUIRED read-only operation record/DB metadata/current runtime; targeted regression по установленной причине, canonical quality и актуальный PR CI; после merge actual CD/full integrity/backup/source/image/health/persistence/dual-market4TF. Недоступный readback не N/A.

EV-INTEGRITY-READBACK, operator snapshot2026-10-11T09:27:30.305531UTC: failed candidateb13081e record `FAILED_PRE_SWITCH`, failure_phase CHECK_STATE_INTEGRITY, switch_started=null; на этапе DB4,498,558,976bytes/budget480s. Running sourceafd10770/container7039151a/image1120624b неизменны. Текущая DB4,651,769,856bytes/WAL564,258,752; pending outcomes192428, persisted ingestion snapshot отстаёт примерно2h46min от snapshot при свежем heartbeat. Это подтверждает отсутствие switch и сохраняющуюся необходимость capacity hotfix, но не устанавливает corruption либо причину CLI exit2.

EV-INTEGRITY-120, operator result получен2026-10-11 около09:46UTC, timestamp самого запуска UNSET: sourceafd10770/SQLite3.46.1/DB4,667,613,184bytes, full integrity с временным cache128MiB вместо2000KiB прерван SQLITE_INTERRUPT по120s budget, elapsed120.587s. Read-only/persistent_changes=false; полной integrity PASS/FAIL нет. Это не доказывает corruption или отсутствие ускорения относительно прежнего465s CLI budget.

EV-INTEGRITY-FULL, operator readonly measurement: metadata2026-10-11T09:49:43.487111UTC/sourceafd10770/SQLite3.46.1/DB4,820,512,768bytes, page_size4096/page_count1176897/freelist0/WAL. Полный integrity_check PASS, issue_count0, elapsed614.323s при cache128MiB/effective mmap2,147,418,112bytes/progress10000. До последнего progress586.23s userCPU37.23s/systemCPU90.12s/read_bytes4,791,803,904: wall time существенно выше CPU, что совместимо с I/O/wait pressure; конкретный источник contention не установлен. Namespace counts сняты последовательно на живой базе: candles596943/events85687/event-head85687/episodes109417/outcomes144240/outcome-jobs342748/comparisons7212; это не единый транзакционный snapshot и не число pending jobs. Проверка подтверждает целостность проверенного snapshot, не будущих записей либо достаточность throughput. Persistent changes=false. Даже tuned full check превышает прежний CLI465s; расширенный diagnostic не был повторной поставкой.

Confirmed diagnostic fix в recovery branch: shared full integrity helper различает собственный deadline, external interruption, SQLite corruption, I/O и прочие DB errors; безопасные CLI failure_code/exit3–7 доступны установленному receiver без обновления privileged helper. Исходные full state/backup checks и неперезаписываемый incomplete backup сохранены. Cache/budget optimization до actual probe Evidence не объявляется исправленной. Local validation2026-10-11: readonly state-check/real CHECK violation/error classification/deadline cleanup/WAL backup/storage targeted suite PASS; окончательная validation после связанных изменений ещё требуется.

Qualified readonly I/O variant: временные validation-only cache128MiB/mmap до2GiB, restore connection settings после full check, fallback при unsupported mmap и progress10000 вместо1000 instructions. Steady-state app connections/schema/history/settings не меняются; short-circuit/quick_check отсутствуют. По measured614.323s receiver budget180s+150s на начатыйGiB, cap900s: текущий размер получает900s outer/885s CLI, около270s margin для copy/variation. Это не гарантирует backup PASS; copy плюс полная проверка сохраняют общий bounded budget и actual CD gate.46 targeted storage/CLI/backup/error/fallback/receiver-preservation/budget tests PASS в locked Windows environment; required full Linux3.11 CI и production backup/CD ещё PENDING. Необходим новый exact-target receiver update до merge, с old installed hash aa2b1a7a и неизменным target/healthy G5 unit, backup/atomic replacement/receipt; restricted key не расширяется. При SQLite validation error сохраняется classified primary failure, dedicated connection закрывается; signal/process failure также блокирует текущий receiver до switch.

Self-review hotfix PASS: copy pin освобождается сразу после завершённой snapshot copy до долгой destination validation; concurrent writer/restore fixture подтверждает прежний backup revision и возможность live WAL checkpoint в фазе проверки. Общий copy+integrity deadline и все rows/constraints/indexes остаются обязательными, existing/incomplete backup не заменяется. Canonical quality/docs/Ruff/format/mypy PASS; safety scope и latest GitHub gates сохранены. Production efficacy остаётся отдельным gate, не выводится из локальных fixtures.

Hotfix PR30 открыт с baseb13081e. Initial required CI38131556499 FAIL: bootstrap receiver integrity pin не был синхронно обновлён, дополнительно обнаружен недетерминированный clock drift в ingestion notification fixture на coalesce boundary. В том же PR исправлены точный SHA256 Git blob receiver9569f32c (LF bytes, независимо от Windows CRLF checkout) и controlled exchange clock test; assertions/gates сохранены. Повторные60 targeted bootstrap/ingestion/receiver tests и canonical quality PASS; actual Linux3.11 required CI новой revision остаётся gate до operator update/merge. Installed receiver пока aa2b1a7a, production switch не выполнялся.

EV-ACTIVATION-TARGET, тот же operator snapshot: consumer source67716b3/containerf3ba7fe3/image972cb5e1, tracked main clean, Compose projectclaude-bybit-trading-agent/serviceapi, resolved servicesapi/postgres. Host `.env`0600root/hashfb734710, Composehash7bb75465; consumer Environment/config resolved values совпадают с runtime. Actual ingest=false, keyring/binding/max-age blank, vault_rows0, alembic20261010_0117. Autonomous/protection/close/perpetual submit flagsfalse, runtime state stopped/kill_switchfalse. Это снимает unknown target/config ownership/disarm/schema на дату проверки; до mutation обязательны повторная identity/CAS и отсутствие competing deploy. Encrypted owner recovery escrow, privileged activation и actual signed historical receipt остаются PENDING; plaintext credentials и все unrelated config сохраняются.

Operator receiver update и actual httpx HTTPS qualification PASS остаются применимыми к неизменным receiver/client bytes. Consumer main67716b3/open PR0/active Actions0 подтверждены09:06:59UTC; encrypted vault пуст, activation ещё не выполнялась. Isolated activation rehearsal09:13:30UTC PASS: existing vault encrypt/decrypt, password-protected AES-GCM/scrypt recovery roundtrip/wrong-key/tamper rejection, env byte preservation/parsing/idempotency/duplicate rejection; ephemeral fixture secrets не сохранены. Реальная config ownership/runtime disarm/schema и owner recovery source требуют qualification; никакие production credentials не созданы.

Independent activation preparation09:55:29UTC PASS: actual consumer SAVE/SELECT/decrypt functions на isolated in-memory SQLite, полный rollback после SAVE, UUID replay без duplicate versions/nonce/commands и stale CAS rejection. Keys только ephemeral; PostgreSQL locking/production activation этим fixture не подтверждены, local dependency versions отличаются от production lock. Fresh GitHub readback10:01:23UTC: consumer main67716b3/open PR0/active Actions0; CryptoWatcher mainb13081e unchanged. Dependency review10:01:07UTC: OSV querybatch для31 exact runtime/dev lock versions, active findings0; base OS/VPS packages этим не проверены. Evidence/raw fixtures остаются вне tracked repository.

EV-HOTFIX-RUNTIME, 2026-10-11, dirty branch/base выше: locked Windows local quality PASS (docs/Ruff/format/mypy28 modules), 96 affected async/backup/installer/controller/CLI/agent tests PASS; прежние24 outcome/comparator/UI и59 storage/delivery/CLI/outcome regressions остаются применимыми к неизменным формулам/контрактам. Новые scenarios подтверждают read-only worker responsiveness, CAS при concurrent job update, actual availability/cutoff, pending restart, diagnostic interruption вместо ложного0, independent snapshot freshness, WAL writer + consistent all-row backup, stage timeout identity и exact offline-only cleanup. Основной полный Linux3.11 прогон — required PR CI, ещё PENDING.

EV-HOTFIX-CAPACITY: isolated Windows SQLite clone контролируемого1100-stream prior burst, 2026-10-11T07:45:56Z;12 virtual ticks обработали3072 outcomes/768 comparisons за7.85s, очереди21000→17928/5450→4682, maximum heartbeat interval0.0142s, pending snapshot restore/integrity PASS. Formulas/settings/protected history сохранены. Это измерение isolated fixture/dirty diff, не throughput VPS или обещание срока закрытия реальных queues. Требуется actual merged runtime, свежий WS/recovery snapshot и bounded backlog/cadence/disk readback после CD.

Self-review обнаружил связанный immutable receiver hash в initial bootstrap; hash обновлён на проверенный LF-normalized source, comparison не ослаблен. Existing39 bootstrap preservation/identity/fault tests и6 async tests, включая cancel-with-pending/restart, PASS. Старый already-executed bootstrap40882 остаётся immutable в своей revision; production повторный bootstrap не запускается.

Owner update2026-10-11: context cap900s утверждён (D21), Zero Trust сейчас не добавлять; unknown max-age decision снят, actual consumer config/keyring/binding activation остаётся PENDING. EV-HOTFIX-HTTP, Windows/locked httpx0.26.0, 2026-10-11T08:46:48–08:46:50Z: default urllib GET403 с Cloudflare code1010, тот же urllib с descriptive User-Agent GET404; default httpx GET404 и safe unsigned POST503 с exact disabled-consumer reason. Operator VPS probe после08:55UTC подтвердил тот же результат: urllib403/code1010, httpx0.26.0 GET404 и unsigned POST503 с consumer_disabled=true. Штатный producer client достигает receiver; Cloudflare Access не является установленным blocker. Cloudflare policy/HMAC/TLS не менялись, valid events/orders/model calls в probe отсутствуют.

PR29 headfbb8f838/CI38122953293 `ci-required`/validation/documentation/select PASS подтверждены primary records. EV-HOTFIX-RECEIVER: operator receipt2026-10-11T08:55:26.953472Z PASS, `/opt/crypto_watcher/rehearsals/receiver-update-8l2psaov/receiver-update.json`; receiver413fb1dd→aa2b1a7a, verified previous_receiver.py backup. Running G5afd10770/container7039151a не перезапущен; database/SSH не менялись. Owner spec/procedure и этот receipt публикуются сгруппированным docs update в том же PR; receiver blob остаётся идентичным проверенному installer candidatefbb8f838. Required CI актуальной revision и actual merge/CD ещё PENDING. Нового PR и speculative workflow run не создавать.

Оставшиеся operation gates: latest PR CI/merge и actual hotfix CD/backup/dual-market4TF checks; exact-target consumer/producer configuration activation и historical advisory smoke. EV-HOTFIX-VAULT: operator read-only inventory после08:55UTC — keyring UNSET/CredentialVaultError, state_rows/version_rows/enabled_rows/distinct_key_ids все0, ingest=false, max_age=null. Encrypted vault пуст на момент проверки; plaintext trading/model credentials и другие runtime settings сохраняются. Перед activation повторить current revision/config ownership/CAS и подготовить dedicated master/binding, cap900s, host-only secret transfer и readback без раскрытия secret. HTTPS actual-client qualification PASS; GET404 не признан route defect, POST отвечает exact disabled-consumer reason.

CryptoWatcher base `afd10770aa3a330e6c5a28e3ea85a676060e6c29`, branch `codex/g4-agent-delivery-20261011`, root worktree. Fresh main/clean branch проверены после G5 actual CD и safe cleanup. G5 DONE3/3: PR27 head81c1acd6…, required CI38097142897/configured test merge02f2a44a…, squashafd10770…. Production CD38097262980/artifact11686671693/digestacc671cfac07b395b6087a06c6387af466fc125817be69b247ccdffb3363898a PASS, проверено2026-10-11T00:11:16Z. OCI1120624bffdf6d564eec9e392d60357a6e6f754c5808530c682dfb22e8995c3c, config14d8bfce222ce3efc7e7e8512dc9626c48174b803c1ef36bc1cf18493e5d25e8, online backup pre-afd10770aa3a330e6c5a28e3ea85a676060e6c29-1791677276.sqlite3; source/image/health/persistence/market1100/1100 OK PASS. Это bounded post-check snapshot, не measured capacity. Owned G5 local/remote branches удалены после exact head/tree/CD proof. Context default OFF; owner preferences не изменены поставкой.

G3 DONE10/10 primary +10-01/02: PR26/required CI38095335439/test merge d91d2cdb…/merge4f8614f…, automatic CD38095481200/artifact11685943093 PASS2026-10-10T23:39:59Z; OCI535589228fd3c9e8c576122982ad0ad36861455d3f9c3c14310f02f71260702b, config0f432fe9d2603c3c94a3665dbccb88fe6801d731060fe8961455224752d995fa, backup pre-4f8614f…-1791675418.sqlite3. Market1107/1108 DEGRADED с mandatory dual-market/TF readiness PASS. G1/G2A/G2B primary records сохраняются, bootstrap/JSON import не повторяются.

### Scope, зависимости и DoD G4

Результат: independent HMAC-authorized durable agent queue автоматически доставляет live event/meaningful episode context и отдельно historical outcomes. Receiver сохраняет v1, добавляет v2, immutable/idempotent revisions/history/episode reconstruction и independent cutoff/freshness. Получение не запускает LLM/order и не меняет Strategies/risk/allowed symbols. CryptoWatcher сохраняет самостоятельную Telegram/history работу при agent outage; оба consumers независимы.

Scope: все6 AC-09, завершение07-05/10-04/12-03; regression01-01/02/03/04,05-04,06-01/06/07,08-02/03,10-01/02/03. Findings F17/F18/F20/F25 и соответствующие runtime/configuration dependencies. Основных PR два по уже утверждённой repository границе: consumer в `claude-bybit-trading-agent` → его required CI/merge/actual CD, затем producer в CryptoWatcher → required CI/merge/actual CD. Consumer-first совместимость допускает независимую поставку receiver с ingest OFF; producer runtime transport remains disabled/blocked до verified endpoint/vault binding. Неподтверждённая consumer production revision блокирует dependent promotion, не independent preparation/G6 diagnostics. Non-goals: orders/LLM/trading credentials/Strategy changes, unrelated R2 backup/runtime/pipeline scope, включение trading windows, публичный CryptoWatcher HTTP server.

Prerequisite read-only2026-10-11T00:08Z: consumer maincc8c632cf574fdd7af389b54c1286082ee53bb22 содержит v1, v2 absent; strict required `checks` app15368/admin enforcement, human review gate отсутствует, open PR0. Последний automated CD38084741177 failed на VPS deploy step; previous38081459133/a7fe1db… PASS. Running revision/receiver activation/vault binding/qualified HTTPS target остаются UNSET до primary proof. Другой checkout содержит active R2 branch/unrelated test edit/untracked materials; он сохраняется. Consumer changes готовятся в отдельном чистом worktree от freshly verified origin/main, без reset/update этого checkout.

### Contract / runtime decisions до реализации

- v2 использует тот же POST `/integrations/cryptowatcher/events`, raw-body HMAC-SHA256 header и65536byte limit; v1 schema/accepted inputs/behavior сохраняются. Typed v2 adds detector/profile/settings provenance/hash, source event revision/window identity/completeness, explicit freshness policy/boundaries, episode revision/confirmation snapshot, optional enrichment и explicit LIVE_EVENT/EPISODE_UPDATE/HISTORICAL_OUTCOME_UPDATE kind. Execution fields запрещены, trading_authorized/model_invoked=false обязательны.
- Envelope revision принадлежит versioned delivered representation; source_event_revision отдельно связывает immutable producer event. Episode revision отдельна; retry использует тот же event_id/envelope revision и exact frozen bytes. Historical analytics не меняет reference time/price/original metrics и не попадает в исходный decision context; consumer выбирает только действительно received revisions до cutoff, затем ограничивает freshness собственной policy и producer expiry. Late update/receipt/retry не освежают market time.
- Public envelope не включает full raw windows или infrastructure/secrets: source-window identity и frozen structured provenance достаточны, full used sources доступны producer history/export. Oversize/invalid payload диагностируется, не обрезается незаметно. Unauthorized/tampered/conflicting/unknown schema/body limit requests отклоняются, сохраняющий transaction idempotent.
- Producer event/episode/required source/admission/intended deliveries atomic before send. Agent/TG decisions/preferences/outboxes независимы; meaningful family/TF/strength policy с2s coalescing и successful state только после validated ack. Failures/restart сохраняют pending и ambiguities. Infra config HTTPS endpoint/exact producer binding/separate secret; secrets не owner settings/export/logs. Empty optional integration не останавливает radar; partial/invalid transport BLOCKED с reason.
- Transient retries exponential1–300s, rate/retry_after bounded; permanent failures blocked/reviewable. Expired live queue item остаётся historical/replayable с original observation и явным kind, никогда fresh. Network outside DB/state locks, общий asynchronous HTTP client/connection bounds; Bybit public5requests/s budget сохраняется.
- Consumer config/vault activation не угадывается; подготовить exact-target reviewable bootstrap/verification только после runtime inventory, сохранив существующие secrets/bindings. Пустой keyring/неизвестная consumer freshness policy остаётся dependent blocker. Не ослаблять Cloudflare/TLS/auth/CI/protections. Обычные merges используют действующий automatic CD, без ручного SFTP.
- Consumer DB existing append-only JSON revision table поддерживает additive v2 без DDL; v2 writer compatibility для старого reader документировать до CD. CryptoWatcher schema1/NONE, versioned agent namespaces, all-row online backup; old events/settings/outcomes сохраняются, JSON import не повторяется. Forward-fix/verified compatible versions, без DB downgrade/restore поверх live.

### Задачи G4

Consumer definition: primary PR935 merged7456e1bc, required CI38099567857 PASS;
production CD38099917543 PASS11Oct2026 00:56:45UTC: DEPLOY_SHA7456e1bc,
API_SOURCE_FINGERPRINT matched, readyz/image/source gates и DEPLOY_OK подтверждены
primary job logs. Прежний failed retirement-scope run не описывает этот runtime.
Necessary compatibility hotfix PR936 merged67716b3282bf9fefe1a84077dfae7c6c99c6b1f0,
 required CI38100130663 PASS; applicable CD38100792664 PASS11Oct2026 01:12:26UTC,
 expected67716b3282bf9fefe1a84077dfae7c6c99c6b1f0/source fingerprint/readyz confirmed.
 Original G2B source86fa38bc
не содержит future outcome/digest/enrichment knobs; raw settings snapshot/hash
сохраняются, absent later fields не подставляются из новых defaults.

Producer preparation specifics: optional integration infrastructure допускает
all-unset/OFF без остановки radar; partial/invalid config диагностируется только
для agent. URL HTTPS, raw-body HMAC, no redirects, shared async client. Optional
paired Cloudflare service-token headers поддерживаются только для qualified
machine authentication, secrets не входят в outbox/export/logs. Реальные endpoint,
vault/binding/max-age и Access policy ещё UNSET; defaults/permissions не угадывать.
Agent preference отдельно от owner domain settings/TG/digest; restart сохраняет
immutable bytes/revisions/attempts. Source event/episode/outcome admission записывает
intents атомарно, payload freezing/coalescing и network I/O идут отдельно. Closures
также доставляются с latest known episode snapshot; late outcomes только historical.
Transport revisions не переписывают source events/metrics/reference/settings.

Independent G6 evidence подготовлено вне repository: Windows controlled1100-stream
burst (275 instruments,4TF) сохранил5500 events/550 episodes/22000 outcome pending/
5500 comparator pending/275 Telegram candidates; full queue restore/integrity PASS.
После50 virtual5s ticks processed1000 complete outcomes/50 limited comparators,
pending restore PASS. Это не VPS throughput/capacity доказательство; actual arrival,
disk growth/backlog cadence ещё требуют qualification, без выдуманного SLO/limits.

- [x] Подтвердить G5 delivery/cleanup, current producer/consumer baseline и закрепить whole-stage scope/Validation Plan до кода.
- [x] Consumer typed backward-compatible v2/records/cutoff/freshness/episode/history; meaningful contracts и preservation original v1.
- [x] Consumer docs/config compatibility/local gates/self-review, required CI/merge/actual CD.
- [x] Producer versioned bounded payload/HMAC and durable independent agent admission/outbox/retry/replay/analytics updates по коду и локальным checks; CI/CD ещё PENDING.
- [x] Owner diagnostics/control, existing read-only public history/export/context и meaningful cross-contract/outage/config/restore regressions; transport secrets/state не экспортируются.
- [ ] Producer required CI/merge/actual automatic CD и exact-target authenticated readback; operational blockers закрыть единым remaining register после independent scope.
- [ ] G6 full origin/main audit/capacity/recovery/finding dispositions и один audit docs PR.

### Validation Plan G4

EVG4-PRODUCER: 2026-10-11 Windows Python3.12, branch
`codex/g4-agent-delivery-20261011`, base afd10770aa3a330e6c5a28e3ea85a676060e6c29:
27 meaningful producer tests PASS (auth/optional config/atomic rollback/independent preferences,
HMAC/immutable bytes/ambiguous ACK/crash IN_FLIGHT/restart/escalation/manual replay/TTL/settings race,
permanent/transient/oversized response, secret and service-header isolation). Existing affected
controller/CLI/outcome/enrichment regressions PASS; Ruff/format/mypy/canonical docs PASS.
EVG4-JOINT: actual consumer code67716b3282bf9fefe1a84077dfae7c6c99c6b1f0 on isolated
SQLite/ASGI accepted40 real producer family/market/TF payloads; raw-body signing/tampering,
duplicate receipts, episode reconstruction/closure/receipt cutoff, actual complete outcome,
frozen enrichment и legacy settings projection PASS11Oct2026 01:23UTC. No live DB/vault/LLM/order
access. Real PostgreSQL concurrency evidence — consumer required CI38100130663, not this local fixture.

Pre-merge self-review выявил сокращённый INVALID_SOURCE outcome: original27-test regression
теперь подтверждает incomplete/empty-metrics projection и последующий independent live event.
Сокращённая запись не подставляет нулевые returns; horizon target выводится только из immutable
event reference и recorded horizon. Исправление включено в этот же PR28; прежний CI38102037503
не применяется к новой revision, требуется current CI.

По коду текущего producer PR подготовлены9/9 remaining AC: 09-01…06,07-05,10-04,12-03;
READY в branch по EVG4-PRODUCER/JOINT и ранее delivered restart suites. Общая Goal:
49/58 verified merged/delivered +9 branch implementation; required producer Linux CI/merge/CD
ещё PENDING, actual integration activation/endpoint/binding/max-age и G6 evidence остаются
операционными обязательствами. Это не новая процентная audit оценка и не DONE проекта.

| AC / риск | Проверка / ожидаемый результат | Canonical команда/tool и cwd | Environment / этап | Gate |
| --- | --- | --- | --- | --- |
| 09-01/04/06 | v1 accepted, typed v2 rejects extra/execution/invalid timestamp/settings/hash/unit; duplicate/conflict/reorder/cutoff/episode reconstruction; no LLM/order calls | Existing consumer `tests/test_cryptowatcher_context.py` + meaningful v2 tests, consumer root/isolated fixture DB | Local required affected suite + current required consumer CI/import | REQUIRED |
| 09-02/03/04 | Raw-body HMAC/tampering/size/immutable bytes/validated ack, independent queues, outage/restart/replay/TTL/rate/permanent failures | Controlled producer/receiver contract/fault tests; root; shared mock HTTP only | Local + required Linux CI | REQUIRED |
| 06-07/09-06 | Prior consumer receipt cutoff, late outcomes separate, market age not receipt; episode revisions available then only; no future enrichment | Joint known revisions/history/analytics fixtures, both repositories | Local + CI + bounded signed runtime verification | REQUIRED |
| 07-05/10-04/12-03 | Telegram mute/off/outage doesn't stop agent/history; persistent pending and diagnostics/owner guards after restart | Existing/new controller/store/outbox/backup fixtures, producer root | Local + CI | REQUIRED |
| Code/config integrity | Cheap changed-path format/type/compile, appropriate consumer required local checks; no unrelated checkout changes | Existing consumer README/CI profile; producer `python scripts/checks.py quality`; no speculative full-suite duplication | Before each initial push, isolated worktree | REQUIRED |
| Delivery safety | Exact current main/protections/required checks; preserve secrets/state/config owners, schema compatibility/backup; expected source/image/health/readiness | Existing respective runbooks/primary CI/CD artifacts | After each applicable merge, actual production | REQUIRED |
| Activation/E2E | Verified HTTPS target/receiver version/exact producer vault binding, dedicated secret fingerprint only, signed accepted id/revision/readback with no execution side effects | Reviewed exact-target procedure after inventory; bounded safe context-only event | Production, dependent on credentials/access; no trade/model/manual file transfer | REQUIRED; missing access не N/A |
| Capacity/recovery | Actual universe/load/backlog/disk/backup recovery measured, used snapshots retained; optional failures isolated | G6 isolated measurements + read-only VPS evidence, limits recorded | Expanded final audit | REQUIRED evidence; no invented SLO |

### Состояние общей Goal и checkpoint

Current verified main9634558a содержит G4 producer PR28, capacity/diagnostics PR29 и integrity classifier/budget PR30. Required CI38131846878 и merged Linux3.11 full370 tests PASS. Actual CD38133517275 FAIL на BACKUP_AND_VALIDATE11:23:36UTC до switch: source integrity завершена, backup copy+validation исчерпали общий CLI885s budget. Operator post-failure readback11:40:56UTC подтверждает running baselineG5afd107 и сохранённую backup. AC-06-02/06-05/12-02/12-03 IN_PROGRESS до runtime qualification, code baseline54/58. Installed receiver9569f32c/update PASS; новая app-only hotfix branch готовит measured validation performance без изменения safety gates. Actual integration activation ещё PENDING. Это не audit percentage и не DONE проекта.

G5 local/required CI/source/image/backup/health/persistence/two-market post-check PASS, Context OFF сохранён; actual public metric freshness/load не объявляются доказанными. Independent preparation G4 продолжается без нового выбора owner. External B02 runtime/vault/live policy переносится в конец при independent work; delivery failures не игнорируются и dependent promotion не выполняется. Built-in Goal active, metadata-only follow-up PR нет; primary receipts/raw diagnostics вне tracked repository.

### Утверждённый roadmap всего scope

| Goal | Результат / AC | Dependencies / delivery |
| --- | --- | --- |
| G1 | Safe foundation, access/config/migration/tests/CI/CD; scope выше | Один основной PR; verified first migration/delivery |
| G2A | E02/E03 + AC-04-01/07-02: оба рынка, широкий eligible universe, WS/4TF/gaps/typed watchlist/correct links | После G1 delivery; один основной PR |
| G2B | E04/E05 + AC-06-01/07-01/04/07/08-03: пять detectors, profiles/manual, episodes/freshness/history/Telegram pending | После G2A delivery; один основной PR |
| G4 | AC-09-01…05, independence часть09-06, agent часть07-05: signed v2 delivery/outbox/replay/diagnostics | После G2B и поставки подготовленного receiver основного проекта. Два основных PR обоснованы разными repositories: consumer-first backward-compatible v2 с CI/CD, затем producer с CI/CD |
| G3 | AC-06-02…07/07-06/11-01/02, analytics часть09-06/digest часть07-05: outcomes/cohorts/export/digest/retention | После G2B; независим от G4/calibration, история уже накапливается; один основной PR |
| G5 | AC-11-03/04 и завершение08-01/12-03: optional reliable derivatives enrichment + manual calibration tools | После стабильного core; один основной PR; no auto tuning |
| G6 | Полный AUDIT актуального origin/main, всех AC/Evidence/findings, capacity/recovery и consolidated docs | После deliveries; один audit docs PR; необходимые substantive fixes поставляются по implementation flow |

Связанные AC-07-05/09-06/10-04 становятся READY после всех соответствующих subsystems. Все32 findings остаются в реестре ниже; F24/F31/F32 остаются кандидатами до подтверждения необходимости. Проценты во время Goals не пересчитываются.

## Состояние AC и Evidence baseline

Все строки относятся к baseline audit SHA выше. READY подтверждает только соответствующий критерий, а не всю подсистему. BLOCKED хранит причину; backlog/partial не выдаются за отсутствие возможности реализации.

| AC | Status | Evidence / основание |
| --- | --- | --- |
| AC-01-01 | READY | EV02 boundaries, closed-composite |
| AC-01-02 | READY | EV02 boundaries; structured context ещё отсутствует, запрет действует при его появлении |
| AC-01-03 | IN_PROGRESS | Код 536–874, 1583–1626, 1798–1881: legacy CMC достижим через reset/settings callbacks |
| AC-01-04 | IN_PROGRESS | EV02 owner-access-default, terms-auth FAIL |
| AC-01-05 | READY | EV01 inventory; EV02 boundaries |
| AC-02-01 | IN_PROGRESS | Код 987–1009: одна category и fixed 100 |
| AC-02-02 | BACKLOG | EV02 universe-eligibility FAIL; metadata adapter отсутствует |
| AC-02-03 | IN_PROGRESS | Ликвидность есть только в composite predicate 942–959 |
| AC-02-04 | IN_PROGRESS | custom_pairs, handlers 1444–1519; нет Market Type или separate sensitivity |
| AC-03-01 | BACKLOG | WS отсутствует; только synchronous REST GET |
| AC-03-02 | IN_PROGRESS | Один signal_timeframe на chat, 256–258, 963–983 |
| AC-03-03 | READY | EV02 closed-composite PASS; текущий REST использует local clock, риск F09 |
| AC-03-04 | BACKLOG | EV02 gaps FAIL; gap tracker/backfill отсутствуют |
| AC-03-05 | READY | EV02 telegram-controls; radar_poll_sec отдельно от signal_timeframe |
| AC-04-01 | READY | EV02 closed-composite PASS; применение на обоих рынках покрывают E02/E03 |
| AC-04-02 | BLOCKED | B03: formulas/lookbacks/threshold defaults кроме старого composite UNSET; implementation отсутствует |
| AC-04-03 | IN_PROGRESS | Значения composite есть 946–959; event/version/units/source/timestamps отсутствуют |
| AC-04-04 | IN_PROGRESS | Ошибка symbol превращается в None 963–985; diagnostics причины нет |
| AC-04-05 | READY | EV01 writers: env/state и Telegram; analytics/auto-tuning отсутствуют |
| AC-05-01 | BACKLOG | Хранится только baselines, episodes нет |
| AC-05-02 | IN_PROGRESS | EV02 repeat-same-candle FAIL; cooldown без identity |
| AC-05-03 | BLOCKED | B03: lifecycle/max lifetime/strength policy UNSET; episodes отсутствуют |
| AC-05-04 | BLOCKED | B03: TTL policy UNSET; EV02 freshness FAIL |
| AC-06-01 | BACKLOG | State не содержит events/revisions/episodes |
| AC-06-02 | BACKLOG | Outcome worker отсутствует |
| AC-06-03 | BLOCKED | B03: neutral/continuation definitions UNSET; реализации нет |
| AC-06-04 | BACKLOG | Analytics отсутствует |
| AC-06-05 | BLOCKED | B03: comparator methodology UNSET; analytics отсутствует |
| AC-06-06 | BACKLOG | Export endpoint/command отсутствует |
| AC-06-07 | BACKLOG | Decision context/history отсутствуют |
| AC-07-01 | IN_PROGRESS | Alert 2107–2112: symbol/TF/процент/оборот; остальных полей нет |
| AC-07-02 | IN_PROGRESS | alert_keyboard 1149–1164 всегда Spot |
| AC-07-03 | IN_PROGRESS | EV02 telegram-controls/settings-noop PASS; mute доступен в alert, расширенный набор controls требует smoke |
| AC-07-04 | IN_PROGRESS | EV02 telegram-controls PASS, settings-revision-race FAIL; тексты Spot-only |
| AC-07-05 | IN_PROGRESS | mute есть, alerts toggle/digest/second consumer отсутствуют |
| AC-07-06 | BACKLOG | Нет истории/outcome, карточка только текущего symbol |
| AC-07-07 | IN_PROGRESS | terms/sample/card есть только для composite |
| AC-08-01 | IN_PROGRESS | _default_settings/_ensure_chat_shape: market behavior внутри chats |
| AC-08-02 | IN_PROGRESS | Только per-chat presets; EV02 invalid-state-settings/settings-revision-race FAIL |
| AC-08-03 | IN_PROGRESS | Отдельные кнопки presets есть; profile набор/ручные значения отсутствуют |
| AC-09-01 | BLOCKED | B02: trading agent contract/repository UNSET; реализации нет |
| AC-09-02 | BLOCKED | B02: endpoint/auth contract UNSET |
| AC-09-03 | BACKLOG | Outbox отсутствует |
| AC-09-04 | BACKLOG | Нет IDs/revisions |
| AC-09-05 | BACKLOG | Нет agent integration или независимой diagnostics |
| AC-09-06 | BLOCKED | B02: другой repository вне текущего доступа/scope, joint validation UNSET |
| AC-10-01 | IN_PROGRESS | Один JSON version=2 для chats/settings/runtime/baselines/mutes |
| AC-10-02 | BACKLOG | History schema отсутствует |
| AC-10-03 | IN_PROGRESS | EV02 legacy-migration PASS; state-corruption-forward-version FAIL; owner mapping B03 |
| AC-10-04 | IN_PROGRESS | Сохраняются только settings/mutes/baselines/runtime; новые subsystems отсутствуют |
| AC-11-01 | BACKLOG | Digest отсутствует |
| AC-11-02 | BACKLOG | Scheduler только poll_engine_job |
| AC-11-03 | BACKLOG | Derivatives context отсутствует |
| AC-11-04 | BACKLOG | Enrichment отсутствует |
| AC-12-01 | IN_PROGRESS | Dockerfile/Compose есть; Docker локально отсутствует, VPS revision UNSET |
| AC-12-02 | BLOCKED | B01: deployment procedure/SSH/version/recovery UNSET; EV03 workflows/runs/environments/deployments=0 |
| AC-12-03 | IN_PROGRESS | Один файл 2177 строк; symbol failures исчезают в None |
| AC-12-04 | IN_PROGRESS | EV01 .env ignored; EV02 only external diagnostics; suite/lockfile отсутствуют |

### Evidence records

| ID | Тип / результат | Источник / revision / environment / время | Ограничения |
| --- | --- | --- | --- |
| EV01 | Inventory/static review PASS | Все 11 tracked files, полный код 2177 строк, Git log/handlers/pools/config/docs на baseline SHA; 2026-10-10 | Initial untracked/ignored files отсутствовали; generated/vendor/DB/assets/build outputs/tests отсутствуют; local dependencies не являются VPS inventory |
| EV02 | Controlled diagnostics PARTIAL: 6 PASS, 10 reproduced FAIL | Внешний diagnostics script, compile in memory и fixtures; baseline code, PTB20.8, Python3.12 Windows; 2026-10-10 около10:08 MSK | Network/Bot API/save в poll mocked; destructive state scenarios только на временных copies; actual Python3.11 Docker/startup/live parse/VPS не проверены. FAIL — findings, не failures docs PR |
| EV03 | GitHub/Git primary snapshot PASS/PARTIAL | origin/main fetch + branches API: SHA совпадает, main protected=false; open PR=[], branches только main; rulesets=[]; workflows=0/runs=0/check-runs=0/Environments=0/deployments=[]; 2026-10-10 | protection endpoint403 connector/401 public: admin details unavailable. Existing gh token invalid; GitHub integration read/write доступна. Нет assertion об отсутствии ручного VPS deploy |
| EV04 | Dependencies PARTIAL | Isolated install requirements с PTB20.8 успешен; pip check PASS; PyPI JSON для18 runtime packages: advisories=[]; allowed requests2.31.0 содержит published advisory IDs; 2026-10-10 | Это fresh Windows resolution, не lock, не Linux3.11 image или installed VPS. `pip-audit` полный прогон не дал результата на момент checkpoint; проверка PyPI metadata успешно завершена отдельно |
| EV05 | Historical evidence PARTIAL | PR18 merged на baseline SHA: compile/keyboard/status/truncation checks заявлены в body; текущие применимые части заново проверены EV02 | Исторические ad-hoc assertions не являются tracked suite/CI; old scope не подтверждает новый проект |
| EV06 | Owner assertion PARTIAL | S04: VPS Contabo, бот давно работает; hostname/IP задан владельцем; 2026-10-10 | Ни SSH account/known host, ни unit/path/running revision/config/state/version/health не установлены; не запускался production deploy |

Baseline EV01–EV06 имел no tracked suite/commands. Current G1 canonical checks установлены в [README](../README.md) и scripts/checks.py; production candidate procedure — [runbook](runbooks/delivery.md). Наличие configuration не заменяет execution records.

### G1 Evidence records

| ID | Результат / источник | Revision / environment / ограничения |
| --- | --- | --- |
| EVG1 | Local regression/quality PASS; 96 collected, scoped successful runs описаны выше; git diff --check PASS | G1 diff base33e0e6 + docs a4ddae0; Windows Python3.12.10, mocked external APIs; Linux Docker ещё PENDING |
| EVG2 | Universal Python3.11 runtime/dev hash locks, uv pip check PASS30 packages; primary PyPI version metadata runtime15 advisories=[] | Exact runtime resolution G1, 2026-10-10; не whole security scan и не installed VPS evidence |
| EVG3 | Operator SSH read-only inventory + local Git blob hash comparison PASS | 2026-10-10 09:23:18 UTC, Contabo: app /opt/crypto_watcher, service crypto_watcher/container crypto-watcher, data bind, image153f7a33…, versions/resources/NTP, code/requirements hashes match verified main. Complete image provenance, project/architecture/config ownership/restore ещё не установлены |
| EVG4 | Existing Git credential in-memory primary API access PASS | Account Just9120 и repo permissions admin=true подтверждены; main protection GET404, Environments0/secrets0 before setup. Connector403 и invalid gh cache не отражают этот доступ. Credential не сохранён/не выведен |
| EVG5 | G1 PR20 latest CI/merge/trusted release PASS; protections/Environment readback PASS | PR CI38042697848:96 tests/Linux3.11/image fixtures PASS, test merge06d6203d…; main d3b0a4f…; release38042801619/artifact11666203721. Strict ci-required app15368/enforce-admins/no force-push/delete; production protected branches only. DEPLOY_ENABLED absent; CD PENDING gate |
| EVG6 | Actual target inventory PASS; isolated rehearsal FAIL before DB creation; artifact identity independently confirmed | Owner SSH readback2026-10-10 10:08:53/10:49:10 UTC: amd64/project crypto_watcher/root-owned config, clean checkout8ace3638…, schema2/only owner233382575/bot7523865123. Bundle hash PASS; docker load PASS, inspect by config ID failed. Actual containerd image tag resolves OCI ID50ba81d6…/source d3b0a4f…/build38042801619/user10001. Local archive config4ce9bd17… and OCI50ba81d6… verified. No production migration/switch or restore PASS |
| EVG7 | Hotfix local archive/identity tests PASS; CI PENDING | 37 targeted identity/delivery regressions PASS, including classic↔containerd, wrong image with matching labels, config/OCI tamper and rootfs/platform/provenance mismatch. Existing exact artifact also parsed successfully; no local Docker, actual target rehearsal still required |
| EVG8 | PR21/CI/release, actual restore rehearsal и first production migration/runtime PASS | CI38046767071:112 tests/Linux3.11/image PASS; merge065e49d…, release38046940245/artifact11668480091. Owner SSH record2026-10-10T17:22:15Z: source065e49d…, host image/OCI a2d59bc2…, config14a9a6eb…, restore/all-table readback/health/persistence/source PASS, market100/100, config revision1. Primary record `/opt/crypto_watcher/releases/first-38046940245-20261010T172100Z-775978f9/deployment.json`; owner explicitly approved first switch. Comment-only env repair preserved active bytes/Compose values, root0600. Automatic CD NOT_ENABLED; owner smoke separately EVG9 |
| EVG9 | Owner Telegram export PARTIAL; sample regression FAIL | `messages.html` from owner export2026-10-10 (2), SHA2565332d2b7c99954d7d9fd76eec2f4f73d98199ac8607f8ca442b1e46d401f19f9;48 messages/18 after deployment,17:27:31–17:28:47 UTC. start/help/status/settings/watchlist/terms replies, old settings/rev1/100 of100/empty list PASS. Msg656783 sample has hardcoded TF3m/no link/buttons, code confirms F33. `/mutes` not registered: previous instruction wrong, use menu button. HTML does not establish numeric identities, callback presses/ack or a fresh background live alert; export contains no scripts executed during review |
| EVG10 | Sample hotfix local regression PASS; CI/delivery PENDING | Current branch base065e49d…, Windows Python3.12.10:35 tests in tests/test_bot.py PASS, canonical docs/Ruff lint-format/mypy and diff checks PASS; self-review covers sample rendering/controls/state independence and delivery evidence. Mock market/Telegram, disposable SQLite. Spot1m/Perpetual15m/current manual thresholds, units, correct Bybit URL/mute/coin controls, no state/network side effects and finite extreme settings checked. Main/deployed image still old until merge/CD |

## Полный реестр findings

Priority: P1 — блокирует correctness/security/основной scope; P2 — существенный gap/долг; P3 — отложенная hygiene/кандидат. Confidence относится к доказанности finding, не к вероятности эксплуатации или масштабам production impact. High не означает production reproduce; условия evidence указаны отдельно. Action/включение в план не разрешают implementation.

| ID / область | Суть, path / AC | Evidence | Влияние / priority | Action и основание | Dependencies | Confidence / status |
| --- | --- | --- | --- | --- | --- | --- |
| F01 DOC/SPEC | S01 полностью меняет прежний Spot MVP; отсутствовали AC и трассировка; product_scope.md / все эпики | Сверка полного S01 с прежним scope: Top100, futures исключены, per-chat domain; G1 update: EVG1 spec уточняет approved D09–D20 без потери58 AC; audit PR19 остаётся source baseline | P1 | CONSOLIDATE: Восстановить весь S01 и AC в существующем spec, не создавать второй документ | Этот audit docs PR | HIGH / RESOLVED_IN_PR |
| F02 DOC | README/state_contract остаются на старом Spot/chat-based контракте; README.md, docs/state_contract.md; D01–D04 | Тексты fixed Top100/futures outside/per-chat domain противоречат S01; план раньше содержал follow-up вместо delivery tracking; G1 update: EVG1: README/state contract/architecture/runbook согласованы с G1 actual capabilities и future spec, docs validator PASS | P1 | CONSOLIDATE: Сохранить полезный raw migration/closed candle контракт, объединить ownership/schema с актуальным spec; legacy часть явно обозначить | Следующая выбранная Goal; ссылки/упоминания README, state_contract и header кода | HIGH / FIXED_IN_G1_BRANCH |
| F03 DOC/PROCEDURE | AGENTS.md и ci-cd-rules.md отсутствуют в repository; root; AC-12-02 | EV01 tracked inventory; комплект предоставлен владельцем отдельно во время аудита; G1 update: AGENTS/ci-cd rules адаптированы и committed a4ddae0; canonical links PASS | P1 | DOCUMENT: Адаптировать router к реальным canonical paths, зафиксировать применимые delivery gates; не копировать шаблон со ссылками на несуществующие файлы | Implementation Goal; приложенный комплект | HIGH / FIXED_IN_G1_BRANCH |
| F04 DOC/COMMANDS | Quickstart обрывается, нет canonical install/validation/environment процедуры; README.md: конец незакрытого bash fence; requirements/Dockerfile | README заканчивается docker compose up -d --build; install/config/test/lint/typecheck команды отсутствуют; G1 update: EVG1: complete quickstart, canonical checks.py/docs links/fences PASS; Docker check PENDING CI | P2 | DOCUMENT: Закрыть существующий quickstart, описать .env/data/secrets, runtime и реальные проверочные команды без второго списка | Goal docs consolidation | HIGH / FIXED_IN_G1_BRANCH |
| F05 SECURITY | Access policy fail-open и неполный guard; код 261–277, 1408–1415; AC-01-04 | EV02 owner-access-default и terms-auth FAIL; auth по chat_id не проверяет group sender; G1 update: EVG1: guard до domain reads/writes, config fail-closed, all registered commands/callbacks/group/terms tests PASS | P1 | FIX: Fail-closed owner validation и единые guards commands/callbacks/poll; проверить group/user identity и пустую/invalid config | G1; actual VPS whitelist UNSET | HIGH / FIXED_IN_G1_BRANCH |
| F06 DATA LOSS | load_state перезаписывает corrupt/unknown-version state; код 421–450; AC-10-03 | EV02 state-corruption-forward-version FAIL на копиях: corrupt JSON/version99 заменены пустым v2; G1 update: EVG1: original/future/corrupt/ambiguous source preserved; explicit migration/idempotency/backup restore tests PASS; production rehearsal PENDING | P1 | FIX: Сохранить исходный файл, fail safely/quarantine с явной diagnostics; versioned migration и backup/restore | G1; копия production state и owner mapping | HIGH / FIXED_IN_G1_BRANCH |
| F07 CONFIG/STARTUP | Невалидные persisted settings могут прервать весь startup; код 344–399; AC-08-02/10-03 | EV02 invalid-state-settings FAIL; float/int без schema/range/isfinite; env числа также парсятся на import; G1 update: EVG1: schema/finite/range validation; error class/field without values, invalid owner blocks preserving source; other chats archived | P1 | FIX: Validate schema/ranges/finite values; локализовать повреждённый record и не маскировать его пустым state | G1; safe fallback policy | HIGH / FIXED_IN_G1_BRANCH |
| F08 DATA INTEGRITY | Нет проверки непрерывности и полноты candles; код 918–961; AC-03-04 | EV02 gaps FAIL: часовой gap допустим для 1m composite; G1 update: EVG1: ordered closed composite rejects gaps/conflicting candles. Shared gap tracking/backfill recovery ещё G2A ; Current update: G2A ordered windows/duplicates/conflicts/current and durable long-gap page recovery fixtures PASS; historical worker yields to current recovery. Outcome horizons remain G3; G3: full future1m windows reject gaps/duplicates; durable referenced recovery supports inactive/older90day ranges, no historical live flood; targeted PASS, CI/CD PENDING. Scope code closure in branch. | P1 | FIX: Gap tracker/ordered candle validation; блокировать affected detector, REST backfill и пометка completeness | G2 ingestion | HIGH / CODE_READY_G3_BRANCH |
| F09 FRESHNESS | Нет event timestamps/TTL; closed определяется только local clock; код 919–945; AC-05-04 | EV02 freshness FAIL: суточные candles дают сигнал; legacy follow-up #1 server time sync; G1 update: EVG1 closure uses Bybit response time; EVG3 NTP synchronized. Event TTL/WS closure/freshness остаются G2A/G2B; G2B update: G2B: type/TF market-time TTL, stale/expired/retry/config replay fixtures PASS; WS closure/clock established G2A. New production delivery pending. | P1 | IMPLEMENT: Type/TF freshness, market/detected/received time; clock drift diagnostics/WS confirm; проверить VPS time sync | G2; B03 TTL policy | HIGH / READY_IN_G2B_BRANCH |
| F10 UNIVERSE | Fixed100/одна category, eligibility без metadata; код 899–913, 987–1009; AC-02-01..03 | EV02 universe-eligibility FAIL: ETHBTC/unknownUSDT попали в scan; нет instrument status/type фильтров ; Current update: G2A metadata/classification/liquidity/full cursor fixtures и public snapshot PASS, >100/no cap; actual production pending | P1 | IMPLEMENT: Широкие раздельные Spot USDT/USDT Perpetual universe по metadata/liquidity и полной pagination; safe unknown type exclusion | G2; Bybit metadata contract | HIGH / IMPLEMENTED_IN_BRANCH |
| F11 INGESTION | Нет WS и одновременно работающих TF; код 963–1027, requirements; AC-03-01/02 | Только REST kline, одно signal_timeframe per-chat ; Current update: G2A native4TF/shared WS, complete args/ack/reconnect fixtures и full-universe isolated public smoke PASS; production pending | P1 | IMPLEMENT: Shared owner ingestion, subscriptions обоих рынков и 1m/3m/5m/15m, REST bootstrap/recovery | G2 после G1 | HIGH / IMPLEMENTED_IN_BRANCH |
| F12 DETECTORS | Реализован только legacy composite; код 918–961; AC-04-02/03 | Отсутствуют impulse/spike-alone/breakout/volatility/versioned event contracts; G2B update: G2B: five versioned formulas, immutable registry/provenance/input replay and isolated errors PASS. | P1 | IMPLEMENT: Versioned deterministic detectors с metrics/units/direction/provenance и fixtures | G2; B03 formulas/defaults | HIGH / READY_IN_G2B_BRANCH |
| F13 EPISODES | Cooldown без event identity не заменяет episodes; код 2090–2105; AC-05-01..03 | EV02 repeat-same-candle FAIL; baselines содержит price/ts/last_alert, нет candle/event/revision ID; G2B update: G2B: episodes/coalescing/escalation/inactivity/maxlife/re-arm/recovery/restart tests PASS, duplicate observation is idempotent. | P1 | IMPLEMENT: Episode lifecycle/correlation/re-arm/max lifetime и meaningful notification policy, исходные events сохранить | G2; B03 | HIGH / READY_IN_G2B_BRANCH |
| F14 DELIVERY | Failed Telegram send расходует cooldown без retry; код 2101–2120; AC-07-01/05 | EV02 failed-delivery-cooldown FAIL: last_alert обновлён до send, следующий poll не повторяет; G1 update: EVG1 failed send no longer consumes successful cooldown, ack then dedup same candle PASS; durable retry/episode notifications ещё G2B; G2B update: G2B: durable due queue/attempts/ack/retry_after/blocked/ambiguous restart PASS; only ack advances baseline, mute/config/expiry preserve history. | P1 | FIX: Отдельное delivery состояние/pending retry; detector/history не зависят от Telegram успеха | G2 delivery; B03 retry policy | HIGH / READY_IN_G2B_BRANCH |
| F15 ANALYTICS | Нет event history, outcome worker, analytics/export; state 331–450, весь tracked inventory; E06 | Отсутствуют persisted events/revisions/episodes/outcomes, cohorts/MFE/MAE/export; G2B update: G2B: complete events/revisions/episodes/source snapshots persisted atomically, backup/replay/cutoff PASS. Outcomes/cohorts/export remain G3.; G3: frozen outcomes/comparisons, exact OHLC metrics, source protections, consistent filtered/revisioned NDJSON/CSV, causal readout and cohort fixtures PASS locally; CI/CD PENDING. | P1 | IMPLEMENT: История с settings snapshots и immutable observation/reference; horizons/outcomes/export с completeness и no future leakage | G3 параллельно G4; B03 methodology | HIGH / CODE_READY_G3_BRANCH |
| F16 OWNER SETTINGS | Market domain зависит от chat; profiles/manual settings не реализованы; код 302–399, 1084–1133, 1759–1795; E08/AC-02-04 | Settings вложены в chats; один market/TF, кнопки отдельных числовых presets; G1 update: EVG1 owner config revisions + delivery/runtime separation; parameters profiles/other subsystems ещё G2A–G5 ; Current update: G2A typed owner watchlist/multiplier and market-specific mutes persisted; other profiles/subsystems remain G2B–G5; G2B update: G2B: explicit profiles/manual values and Telegram family selection persisted; migrated custom values preserved. Outcome/agent/enrichment config remains G3-G5.; G3: validated owner horizons/digest interval, separate digest preference/cursor and preserved snapshots; agent/enrichment configuration still G4/G5. G5 update: G5: strict persistent enrichment bool/default OFF completes owner domain settings; guarded controls/revision cancellation and backup readback PASS; CI38097142897/PR27/actual CD38097262980 PASS; source/image/backup/health/persistence/two-market1100/1100 verified2026-10-11T00:11Z. | P1 | IMPLEMENT: Persisted owner domain config и отдельные delivery preferences; profiles плюс manual validated parameters | G1; B03 legacy owner mapping | HIGH / PARTIAL |
| F17 INTEGRATION | Нет advisory API/автоматической передачи trading agent; все entrypoints/dependencies; E09 | Нет HTTP server/webhook/outbox/consumer config, IDs/auth/replay/delivery diagnostics | P1 | IMPLEMENT: Согласовать contract и authorized idempotent delivery/replay; joint consumer tests; не ждать calibration | G4 после G2; B02 | HIGH / OPEN |
| F18 DIGEST | Нет periodic digest и независимых delivery toggles; код 2171 и handlers; AC-11-01/02/07-05 | JobQueue только poll_engine_job; digest state/aggregation отсутствуют; G2B update: G2B: common durable event/episode history and alert isolation foundation; digest aggregation/scheduling/toggle remains G3.; G3: both-market aggregation/period identity/latest-only restart/frozen attempts/independent toggles implemented, rate/permanent/ambiguous/preference-race fixtures PASS; CI/CD PENDING. | P2 | IMPLEMENT: Digest из общей history/episodes обоих рынков; interval и separate alerts/digest controls | G3 после history | HIGH / CODE_READY_G3_BRANCH |
| F19 ENRICHMENT | Нет derivatives context для Spot; весь fetch layer; AC-11-03/04 | Нет OI/funding/basis adapters или надёжной market/asset mapping G5 update: G5: exact qualified mapping, independent metrics/units/timestamps/freshness, shared coalesced ticker cadence and bounded persistent OI attempts implemented; event cutoff/export/independent markets tests PASS; CI38097142897/PR27/actual CD38097262980 PASS; source/image/backup/health/persistence/two-market1100/1100 verified2026-10-11T00:11Z. | P2 | IMPLEMENT: Optional enrichment с source/market/units/time, независимым cadence и nonblocking failure | G5 после G2 | HIGH / CLOSED_G5 |
| F20 OBSERVABILITY | Symbol errors/timeouts скрываются как отсутствующие данные; код 963–1023; AC-04-04/12-03 | except Exception→None; timeout cancels pending без error summary; status показывает нет данных, без gap/source reason; G1 update: EVG1 per-symbol/source errors и no-signal разделены; remaining source/consumer diagnostics G2A–G5 ; Current update: G2A source/market/quarantine/connection/recovery gaps and worker failures visible; no-signal separated, analytics/consumer diagnostics remain later; G2B update: G2B: safe per-family errors, detector registry, active episodes, pending attempts and blocked target diagnostics; other subsystem diagnostics remain G3-G5.; G3: incomplete/invalid horizons, analytics failure and referenced recovery reasons plus digest target diagnostics; agent/enrichment diagnostics still later. G5 update: G5: optional source/metric/mapping/retry errors visible, OI previous failure stays PARTIAL until recovery; core health unaffected; agent diagnostics still G4. Hotfix update EV-HOTFIX-RUNTIME: operator snapshot обнаружил stale ingestion32min при fresh heartbeat; independent5s reporter, explicit snapshot age и bounded readonly diagnostics с PENDING при budget interruption подготовлены; actual CD/runtime qualification PENDING. | P1 | FIX: Reasoned degraded diagnostics по market/detector/universe/gaps и доставкам; не показывать no-signal при missing data | G2; secret-safe error handling tests | HIGH / PARTIAL |
| F21 CONCURRENCY | Settings могут смениться во время fetch, old result доставляется как active; код 2068–2112, status_cmd; AC-07-04/08-02 | EV02 settings-revision-race FAIL: threshold вырос до99, old signal отправлен; UI заново читает current settings; G1 update: EVG1 CAS revision/settings snapshot и poll/status race discard tests PASS; G2B update: G2B: frozen settings provenance, pending old-config cancellation, in-flight prior-settings/reconciliation, no cross-revision strength baseline; regressions PASS. | P1 | FIX: Owner config revisions и snapshot provenance; согласованная apply/discard policy перед notification/status | G1/G2; не переписывать concurrent updates | HIGH / READY_IN_G2B_BRANCH |
| F22 ARCHITECTURE/PERFORMANCE | Monolith и per-chat REST fanout мешают расширению; telegram_crypto_watcher.py 2177 строк; AC-12-03 | Два pools 4/12; ~1 ticker+100 klines на scan/chat, без shared snapshot; awaits Telegram при state lock; legacy follow-up #2/#5; G1 update: EVG1 module boundaries, shared owner scan, bounded async REST outside DB transactions; WS dataset/load ещё G2A/G6 ; Current update: G2A shared dataset, bounded windows and one queued recovery per stream; all actual-universe subscriptions acknowledged in isolated smoke. VPS steady-state capacity remains G6; G2B update: G2B: pure detectors, atomic history/episode producer and separate consumer; indexed active/pending/history access, bounded batch20, network outside DB transactions. Actual capacity remains G6.; G3: SQLite aggregations instead of unbounded raw-return arrays, paged readonly export, one comparator/page and bounded outcomes/retention; measured actual capacity remains G6. Hotfix update EV-HOTFIX-RUNTIME/CAPACITY: synchronous analytics и comparator1/5s отстают от snapshot arrival; bounded readonly worker/CAS/actual availability/yield +256/64 batches prepared; isolated3072/768 drain and pending restore PASS; реальная VPS backlog/disk qualification PENDING. 14Oct: compact new writes доставлены/source047c66e5; readonly poll1092/1092 и analyticsOK. Historical whole conversion833.526s PASS, текущая DB отдельно PENDING. Existing shared revisions при rebuild создают дополнительные full-window bodies; regression на047c66e5 воспроизводит3 additions, local correction сохраняет refs/bytes и проходит existing full manifests/body checks; PR33/code51f86c48/CI38537110560 required PASS; current whole dataset ещё PENDING. | P2 | REFACTOR: Разделить ingestion/domain/state/render/delivery, общий owner dataset, измерять backlog/cadence/rate budget; load tests по реальной нагрузке | G1/G2; текущая performance/VPS limits UNSET | HIGH / PARTIAL |
| F23 LEGACY | CMC/source/core/reset действия остаются достижимыми вне v1; код 536–874, 1583–1643, 1798–1881, 1967–2041, 2163–2168; AC-01-03 | Commands resetbase/resetallbase зарегистрированы; старые inline callbacks src/core/cmctop поддерживаются. CMC main poll отключён, но пути живы; G1 update: EVG1 active CMC/reset code deprecation по D04/Q06; old callbacks explain change, baselines archived; startup entrypoint retained | P2 | DEPRECATE: После state compatibility review убрать superseded UX/network paths; baselines.last_alert не удалять как якобы полностью dead | Goal cleanup после G1; D04/Q06 | HIGH / FIXED_IN_G1_BRANCH |
| F24 ORPHAN CANDIDATES | Невызванные legacy helpers/caches; внешние consumers неизвестны; код _fetch_async/_prepare_core_context/tracked_desc/price_unit/_mode_label/_rank_prefix/_sub_metrics/_directional_marker | AST+rg и регистрация entrypoints: прямых callers нет; executor/handler functions исключены из кандидатов; legacy follow-up #3; G1 update: G1 сохраняет startup entrypoint; source/history/registrations reviewed for CMC/reset deprecation. Unknown external private helper consumers не доказаны; остальные orphan candidates DOCUMENT/DEFER до G6 | P3 | DEFER: Проверить историю/внешние imports/старые callbacks; затем REMOVE подтверждённых helpers либо DOCUMENT compatibility; imports alone недостаточно | Q06; external Python consumers UNSET | MEDIUM / DEFERRED |
| F25 TEST GAP | Нет tracked воспроизводимой regression suite; EV01 tracked inventory; E01/E03/E08/E10/E12 | Есть только assertions import token и исторические ad-hoc PR18 проверки; tests/config/fixtures отсутствуют; legacy #4; G1 update: EVG1 meaningful auth/config/migration/race/composite/send/CLI/delivery suite; PR21 CI112 tests PASS; sample regression added EVG10, current required CI PENDING; G2B update: G2B: meaningful formulas/episodes/transaction/restore/replay/Telegram fault/config/UI tests added; targeted PASS, latest Linux CI pending. Future subsystem suites grow with G3-G5.; G3: meaningful full-window/cohort/export/causal/toggle/race/recovery/upgrade/UI fixtures added; local targeted PASS, required Linux CI PENDING. G5 update: G5: mapping/missing/stale/cadence/identity/owner race/restart/frozen context/export/recompare/UI fixtures added; local targeted PASS; required Linux CI pending. Current qualification: EV-IO-KEY/synthetic root crypto PASS; readonly Windows SQLite3.49.1 CHECK fixture differs from writable mode, Linux3.46.1 behaviour UNSET. Preserve writable full-backup regression; readonly timing PASS alone does not qualify constraints. | P1 | IMPLEMENT: Минимальные auth/migration/config/composite/recovery tests в существующем Python stack; CI запускает meaningful suite | G1; external audit scripts не заменяют suite | HIGH / PARTIAL |
| F26 DEPENDENCIES | Нет lock/hash constraints; разрешены уязвимые requests releases; requirements.txt:2–3; Dockerfile:1,9 | requests>=2.31.0/python-dotenv>=1.0.0, unpinned base/pip; EV04 current18 packages: advisories=[]; 2.31.0 имеет GHSA-9wx4-h78v-vm56/GHSA-9hjg-9r4m-mvj7; G1 update: EVG2 hash locks/base digest; async HTTPx replaces requests;15 runtime advisories=[]; installed VPS requests2.33.1 observed, full artifact validation PENDING | P2 | FIX: Выбрать проверенный reproducible manifest/lock и safe bounds; audit точного Docker artifact. Не утверждать vulnerable package на VPS без inventory | G1; exploitability по fixed Bybit URLs не доказана | HIGH / PARTIAL |
| F27 CI/CD | CI/protections/build foundation и new-write CD подтверждены; ordinary full current-history gate ещё не qualified; AC-12-02 | Исходный EV03: workflows/settings отсутствовали. G1 PR20–23/required CI/main40882eb/production CD38087285142 PASS, restricted SSH/identity/settings/owner smoke подтверждены. Current: PR29/30 required CI PASS; CD38133517275 FAIL до switch на BACKUP_AND_VALIDATE/exit3; operator11:40/11:57UTC подтвердил G5afd107 unchanged. EV-CD-FAILURE-ARTIFACT: verified artifact11700577981 содержит0-byte JSON. EV-BACKUP-512: closed-copy full check906.861s/BUDGET_EXCEEDED, performance fix не квалифицирован, ожидается bounded I/O readback; safe CI-side capture fixtures/quality PASS, required latest CI/actual CD ещё gates. 14Oct primary update: PR31/32 merged, CI38524296127/38525193430 PASS, CD38525778657 PASS/source047c66e5. One-time owner new-write exception consumed; полный current-history backup/integrity не утверждается. Nonempty delivery record и actual source/image/health/persistence/markets PASS; current-data conversion и subsequent ordinary full gates PENDING. | P1 | FIX: Qualification full source+snapshot integrity в bounded budget, safe nonempty success/failure artifact и source/image/health/persistence/market post-checks. Installed receiver/SSH/gates сохраняются; retries/cleanup не автоматизировать | Current hotfix; F22/F31 capacity; owner measurement | HIGH / REOPENED_HOTFIX |
| F28 OPS EVIDENCE | Target/config/source inventory и first migration/restore/runtime подтверждены; automatic CD ещё не завершён; AC-12-01/02/10-04 | Исходно runtime/unit/access UNSET. G1 EVG3/6: app/unit/data/code hashes/NTP/resources, amd64/project, root-owned config/permissions, clean checkout8ace3638… и owner record confirmed. Legacy image полный source provenance UNSET; EVG8 actual isolated restore/first migration/source/health/persistence PASS; current hotfix и automatic CD PENDING ; Current update: G1 actual bootstrap/first migration/restore/source/image/health/persistence/CD PASS; primary records restored without redeploy. Future runtime capacity/source subsystem evidence checked in their Goals | P1 | DOCUMENT: Сохранить target evidence и выполнить exact artifact rehearsal/reviewed delivery/recovery/post-checks; наличие legacy backup не означает пригодность restore | B01; deployment не запускать ради статуса | HIGH / CLOSED_G1_SCOPE |
| F29 CONFIG/UX | Env category/runtime knobs могут противоречить Spot UI и raw input; BYBIT_CATEGORY, settings_text/status_text/alert_keyboard, .env.example | Env category unrestricted, UI/link всегда Spot; .env example не описывает many legacy knobs. env default SIGNAL_TIMEFRAME и state_contract фиксированный5m могут расходиться; G1 update: EVG1 validated market category, labels and links, owner state precedence; typed dual-market watchlist/eligibility remains G2A ; Current update: G2A typed validated market identity/card selection/dual-market status/links; selected market no longer limits ingestion | P2 | CONSOLIDATE: Явная market identity и validated current config; убрать конфликтующие legacy settings после compatibility review, синхронизировать UI/contract | G1/G2 и F02; actual VPS env UNSET | HIGH / IMPLEMENTED_IN_BRANCH |
| F30 BUILD HYGIENE | Docker context не исключает data/ и .git;  .dockerignore, Dockerfile | Ignore содержит .env/state file, но не data/ и .git; Dockerfile копирует только manifest/code, поэтому inclusion в image не доказан; G1 update: G1 Docker context whitelist excludes .git/.env/data/cache/test fixtures; actual image build review PENDING CI | P3 | FIX: Исключить runtime data/Git из build context для уменьшения context и риска передачи builder; verify context/image artifact | G1; deployment build location UNSET | MEDIUM / PARTIAL |
| F31 HARDENING | Docker runtime использует root default; resource/health policy UNSET; Dockerfile/Compose; AC-12-01 | Нет USER/healthcheck/resource settings; фактические VPS user/resource limits не проверены; G1 update: G1 image UID10001/cap-drop/read-only root/health prepared; EVG8 actual UID10001/SQLite ownership/health/persistence PASS; EVG3 resource snapshot only, future full-universe load unverified. No invented CPU/RAM limits | P3 | DEFER: После runtime inventory выбрать минимальные privileges/health/resources по необходимости; отсутствие инструмента само по себе не defect | B01; не превращать hardening в новый AC автоматически | MEDIUM / PARTIAL |
| F32 UX CANDIDATE | Legacy polish/preset strategy без конкретного нового дефекта; прежний open_issues #6/#7; E07/E08 | Следить за едиными русскими terms/threshold explanations; visual quality/чувствительность требуют user feedback/statistics, не подтверждены как bug; G2B update: G2B: five-detector explanations, actual profiles/manual parameters and event detail implemented. Statistical calibration/views G3/G5; unsupported polish remains DEFER.; G3: event→outcome/noise/completeness and descriptive cohorts/no-signal N доступны; enrichment/manual version comparison views G5, unsupported polish DEFER. G5 update: G5: recorded profile/version/settings comparison with N/noise/limits and explicit revision-preserving recompare; no auto threshold change. Unsupported visual polish remains DEFER. | P3 | DEFER: Сохранить полезные terms/presets; улучшать объяснения и выбор thresholds по согласованным detector profiles и outcome statistics | G3 calibration; Q01, не откладывает agent integration | MEDIUM / PARTIAL |
| F33 G1 REGRESSION | `/sample_alert` потерял baseline Bybit/mute/coin controls и не учитывает active TF; cryptowatcher/bot.py/render.py; G1 preservation / AC-07-03 | EVG9 msg656783 на deployed065e49d подтверждает code regression относительно legacy8ace3638; EVG10 исправляет current snapshot rendering/buttons без REST/state writes ; Current update: PR22 code/CI PASS, actual CD38087285142 main40882eb; owner screenshot401ddabd… and “вроде работает” smoke PASS with assertion limits | P2: неверный sample и потеря manual controls | FIX: Восстановить existing demonstration/controls, не менять live formula, auth, settings или schema; meaningful regression tests | Necessary G1 hotfix, required CI/merge/release/CD | HIGH / CLOSED |
| F34 HISTORY CAPACITY | Полные heads дублируют revisions; пересекающиеся inputs/settings/context повторяются. events.py/outcomes.py; AC-10-02/F15/F22 | Исходный G5afd107 readonly13Oct14:16UTC: SQLite26.25GB. PR31/32 compact heads/shared blocks доставлены CD38525778657/source047c66e5; latest5 events подтверждают compact heads/shared revisions на settings revision3. Current DB30.56GB не конвертирована. Historical5.42GB→2.107GB whole qualification833.526s PASS относится только к сохранённому старому source; две historical candidates побайтно сохранены в strict Zstd archives415MB/459MB. PR33 устраняет повторную упаковку уже shared blocks в rebuild; regression/required CI38537110560 PASS, merge/CD PENDING. Current whole900s qualification и recovery/promotion не подтверждены | P1: disk/write amplification, current capacity не доказана | REFACTOR: shared references/projections и bounded validated bytes; qualified existing-data conversion без потери provenance. Policy900s/1CPU/4GiB/retention сохраняется по owner decision | PR31/32 new writes delivered; PR33 rebuild correction; current source/archive/CD gates; production promotion только по конкретной reviewed procedure | HIGH / PARTIAL |

### План консолидации документов/кода

- Сохранить existing product_scope/open_issues как canonical spec/plan; audit PR19 изменил только эти два файла; G1 включает разрешённую консолидацию README/state contract/rules/runbook без отдельного docs PR.
- README сохранить для назначения/quickstart/navigation; при выбранной Goal заменить устаревший продуктовый scope кратким актуальным описанием и относительными ссылками, сохранить полезные commands. F04 — конкретная поломка quickstart, не косметическая переформулировка.
- State contract сохранить как техническое уточнение до implementation owner schema, затем перенести ownership/schema/migration в актуальный contract с index из spec; legacy часть при необходимости архивировать. Не удалять полезные empty-list/raw-state/backward-compatibility правила.
- Router адаптировать из комплекта к existing filenames; architecture/runbooks создавать при соответствующем реальном содержимом, не пустые placeholders.
- В прежнем open_issues старые deployment checks сохранены как risks/Validation Plan: migration/status/no-op/parse/startup/poll/TF/thresholds/truncation. Их прежнее наличие не доказывает PASS.
- Superseded Top100/single-TF/Spot-only/CMC/reset surfaces связаны с D01–D04. CMC/reset доступны, поэтому DEPRECATE, а не «dead code». Невызванные helpers остаются DEFER кандидатами до external consumer review. Operational baselines.last_alert используется и не подлежит слепому REMOVE.
- Затронутые ссылки/упоминания: README documentation list/source hierarchy/project structure/state ownership; state_contract sections2–12; module docstring/boot log/help/settings/status/command registry. G1 уже поручена; соответствующие изменения выполнены в текущем branch, runtime outcomes PENDING.

## Blockers / ограничения доступа

| ID | Что неизвестно / блокирует | Что нужно для снятия |
| --- | --- | --- |
| B01 | RESOLVED G1: first migration/restore/runtime and automatic CD38087285142 PASS; owner smoke accepted | Preserve existing restricted SSH/host verification/backup gates. No repeated first migration or manual image transfers |
| B02 | Consumer actual2bfeda8: current-keyring crypto и active-master/recovery-material match12Oct20:46/20:52UTC PASS. Dedicated radar credential/binding0, ingest OFF,900s setting UNSET; actual producer HTTPx/TLS reaches receiver503. Producer9634558a CD failed pre-switch, hotfix PR31 unmerged | Prepare/qualify exact-target bootstrap preserving existing master/other credentials/config/trading flags; configure encrypted radar binding and900s policy. Actual signed historical delivery requires qualified producer hotfix/CD. Master-key proof does not confirm current PostgreSQL restore; unrelated counterpart R2 scope preserved |
| B03 | Numeric SPEC gaps RESOLVED approved D09–D20; owner mapping EVG6 и actual migration/restore EVG8 PASS | Сохранять accepted policies и migrated settings/custom empty/delivery/mutes; новые unknown/conflicting states сохраняют diagnostic gate |
| B04 | RESOLVED: Just9120 admin; strict main protection/production Environment and DEPLOY_ENABLED=true readback PASS | Reread current protections/revision before each merge; runtime delivery via dedicated CD |

## Audit quality review

Полный S01 прочитан, все его обязательные разделы покрыты 12 эпиками/58 AC; дополнительно проверена обратная трассировка reachable legacy CMC/reset и старого UX. Scope расширен требованиями владельца, не новыми findings. Generated/vendor области отсутствуют в tracked inventory. Runtime data/cache/logs/backups отдельно: initial untracked/ignored inventory пустой; временные audit artifacts вне repository.

Проверены false positives: settings/no-op/legacy raw migration работают в обозначенных fixtures; CMC не назван активным main poll; отсутствующий Docker/tool сам по себе не баг; context data/ не доказано включённым в image; requests advisories не означают vulnerable installed VPS; timezone Compose не признан дефектом без требования; callbacks/executor functions не названы orphan только из-за отсутствия прямого вызова.

Confidence: HIGH по code/fixture-backed gaps и inventory; MEDIUM у orphan external usage/hygiene/optional hardening/visual polish; LOW production correctness не оценивается без records. Возможные false negatives: реальные stream/rate/Telegram parsing/time sync/concurrent process behavior, credentials in third-party exception logs, Docker3.11 startup, disk/backup/actual installed dependencies/VPS resource pressure не проверены. Весь READY ограничен baseline и описанными checks; missing live smoke не замаскирован PASS.

## Checkpoint и следующий шаг

2026-10-11 Europe/Moscow: G1 DONE по primary records, verified main40882eb и actual production run38087285142/artifact11682629335; image manifest38d238a6…/config7666f323…, backup pre-40882eb…-1791667365.sqlite3. Owner sample screenshot SHA256401ddabd… и ответ «вроде работает» подтверждают bounded owner smoke с указанными ограничениями. Production Environment DEPLOY_ENABLED=true; dedicated forced-command SSH и strict host key verification проверены; последующие eligible runtime merges доставляет CD. Первое failed CD38086969847 не дошло до authentication/target: Windows CRLF private-key stdin repaired binary transfer, verified bounded run38087285142 PASS. Credentials не хранятся в repository; existing SSH keys/unit другого приложения сохранены.

Текущий checkpoint2026-10-13: verified main9634558a; PR31/codex/backup-validation-performance-20261011/base9634558a открыт и не merged. Published code headb5fe042, required CI38405497548/ci-required115324970264 SUCCESS; fresh readback13Oct20:07UTC подтверждает эту revision/checks. Local cache-release/head/scanned-body corrections и документация ещё не published; CI прежней revision их не подтверждает.104 Windows storage,59 application и152 Linux native targeted tests, canonical quality PASS; separate protected candidate all5 manifests/body/hash readback PASS391.133s. Это не whole current conversion proof. Metadata-only push/PR не требуется.

Production остаётся на прежнем G5afd107/crypto-watcher7039151; установленный privileged receiver9569f32c не обновлён. Current-dataset capture `current-dataset-qualification-gl9nijrj/status.json` завершён TIME_BUDGET_EXCEEDED/exit3/OOMfalse за888.557s: snapshot24,689,618,944bytes frozen около841s, full source/candidate/persisted recovery не подтверждены. Процессы предыдущих trials завершены, owned probe containers удалены; не ждать старые PID и не повторять capture из-за исчезновения observer handle. Current production compaction/replacement NOT_RUN. Historical archive-source qualification PASS относится к прежней5.42GB копии и не разрешает её promotion поверх текущей истории.

Owner-approved cleanup завершён с повторной archive/hash/path/use проверкой: exact3 failed candidates освободили4,663,693,312 allocated bytes, exact2 duplicate history inputs —6,203,174,912. Primary receipts: `failed-candidate-cleanup-20261013T051549Z.json` и `duplicate-input-cleanup-20261013T063825Z.json`. Рабочая БД, qualified historical candidate и recovery archive `backup-archive-6crp6k3t/qualified.sqlite3.gz` сохранены. Эти удаления уже выполнены; не повторять и не расширять их scope.

Prepared closed-source qualification: exact one-container stop/checkpoint/read/restart, общий900s/CPU1/RAM4GiB/no swap и все source/candidate/archive gates сохранены; production replacement запрещён. Closed-file127-row full pipeline и isolated refusal/restart fixtures PASS; они не доказывают fit текущего dataset. Local cleanup-timeout/refusal regressions PASS: ошибка удаления owned probe сохраняет BLOCKED и не пропускает restart того же baseline. Fresh read-only maintenance preflight13Oct09:16UTC PASS/files_changed=false. Explicit owner authorization на временную остановку/checkpoint получена13Oct; первый execute завершён BLOCKED/OOMtrue за200.535s с возвратом healthy baseline. Memory fix qualified в local/Linux fixtures/CI и actual25.53GB загрузке; full source integrity/copy/recovery qualification ещё PENDING. До исполнения повторить exact target/resources/concurrency preflight; молчание и automatic Goal continuation не являются разрешением.

Consumer actual binding readback13Oct09:18UTC: radar credential versions0, active radar credentials0, binding отсутствует, ingest OFF, max age UNSET. Accepted policy900s остаётся требованием; runtime setting ещё не подтверждён. Unsigned probe из consumer не выполнен из-за отсутствующего diagnostic httpx; это не доказательство недоступности endpoint. Прежний actual producer HTTPx/TLS503 подтверждал disabled receiver в описанных условиях. Qualified master crypto/recovery-material match не заменяет current DB recovery или signed event delivery. Credential activation требует owner-protected qualified procedure; не обходить Admin/TOTP и не менять trading authority.

Последний whole historical trial13Oct: source full integrity/unchanged checks PASS, но весь source/candidate/restore proof не завершён за887.528s; result TIME_BUDGET_EXCEEDED, candidate UNQUALIFIED_THIS_RUN, OOMfalse, owned probe removed. Separate readonly candidate byte check подтвердил SHA1810335b и совпадение с ранее qualified artifact; production тот же G5afd107/7039151 running/healthy. Last DB-size snapshot14:16UTC: SQLite26,248,880,128bytes/WAL6,604,392bytes; он не является текущей free-disk оценкой после нового trial. Working DB compaction/replacement NOT_RUN. Нельзя выдавать candidate size reduction за уменьшение production DB или повторять unchanged pipeline/остановку бота без нового основания.

Current local optimization14Oct: frozen single-value field использует уже
проверенный body SHA вместо повторного hash; mutable runtime reader и joined
window hash прежние. ABBA cohort32k revisions сохраняет identical129366817 public
bytes/SHA4a458062; warm baseline16.853s против14.606/14.746s. Full protected
historical candidate readback PASS300.913s против прежних366.347s: all5 manifests,
3179502 logical rows и188240 bodies; source1810335b/production unchanged.
Primary receipts: `cw-field-hash-abba-025ae230bd494455841ea696178a4f4c.json`,
`cw-field-identity-readback-913dca8a19ec4791bbb1aab9be5efda6.json` и6 Linux
refusal controls `cw-field-identity-controls-055b1420613a484889b1e8a797ce6f5a.json`.

Isolated converter создаёт новые shared bodies вlossless Zstd v2; reader принимает
V1/V2, existing bodies/private bytes сохраняются, live writer default остаётсяV1.
Measured8k-body cohort37.84MB: encoded6.27MB против7.80MB; Zstd0.425/0.448s против
zlib3 0.823/0.909s, decoded SHAee3b767d совпадает. Это bounded cohort, не весь
copy/current DB. Менее плотный zlib1 вариант не выбран: stable выигрыш не подтверждён.
Positive mixed encodings/private bytes/rollback и negative frame size/window/checksum/
trailer/extra-frame/future-version checks PASS.129 local tests/quality PASS;
Linux3.11/native228 targeted regressions PASS, receipt
`cw-zstd-body-regressions-9dd35bd5b3d849c2b59ed50787ab2af5.json`.
Actual full source/copy/new-candidate/recovery qualification≤900s ещё PENDING.

Fresh read-only capacity13Oct21:32UTC: production afd107/7039151 healthy, main
SQLite27653779456bytes/WAL7762112bytes, free6426107904bytes. Новые исторические
candidate outputs требуют отдельного bounded preflight; этот snapshot не гарантирует
будущее free disk. DB replacement и снижение production размера NOT_RUN.

Whole historical qualification новогоb849dbe завершена14Oct local /
13Oct22:06UTC: TIME_BUDGET_EXCEEDED/exit3 за888.92s, OOMfalse, candidate
UNQUALIFIED_THIS_RUN. Source/copy дошли до frozen candidate phase; source5422329856bytes,
новый candidate2107183104bytes. Этот размер — unqualified artifact, не уменьшение
production DB. Полный source/candidate/recovery результат не получен; recovery
byte gate не превращает timeout вPASS. Source inode и afd107/7039151 healthy baseline
сохранены, probe удалён. Primary record:
`historical-zstd-gate-26m3jfsd/status.json`; operator job2141322 terminal.
Actual helper SHAe227871e подтверждён до запуска и readback; ошибочный old hash в
launcher metadata отмечен отдельным `historical-zstd-gate-job-yg0zbau9/launcher-correction.json`,
исходный launcher сохранён. Операция не перезапускалась.

Следующая performance зависимость — source/copy path: прежний per-record native
extractor возвращает packed JSON, затем Python заново materializes и сериализует
metadata после binding references. Допустим bounded prototype direct serialized
binding с exact semantic/numeric parity и refusal controls; текущий immutable/raw
source, full source/candidate gates и900s/CPU1/RAM4GiB не менять. Новый whole trial
без measured critical-path improvement не запускать. Live compaction, PR31 merge/CD,
consumer activation иG6 по-прежнему PENDING; progress54/58.

One-pass native packing14Oct: direct second-parse binding prototype отклонён
поABBA cost (same public SHA, slower copy). Callback-in-original-parse prototype
и canonical guarded path сохраняют same full12000-record public SHA99b49d0f;
warm baseline copy3.330s против1.659/2.410s. Это bounded cohort, не whole fit.
Canonical path сохраняет numeric lexemes и active outer transaction; per-record
savepoint откатывает partial bodies/admission proofs при late fallback/refusal.
Older helper fallback и Python backend сохраняются. Runtime radar этот helper
не выбирает; all source/candidate/manifests/recovery/budget gates прежние.
119 local tests/quality,144 Linux native/history/full fixture conversion tests
и3 canonical callback refusal tests PASS. Primary receipts:
`cw-one-pass-canonical-00e2a7c80f354ed982e422933e5f288b.json`,
`cw-canonical-saved-pack-abba-dab976579cda47d3b6059767f1c39132.json`,
`cw-saved-pack-refusals-576238ce9ed245b8b9f02d7060ff722a.json`.
No production changes/promotion; current dataset qualification PENDING.

Capacity/cleanup blocker14Oct: readonly13Oct22:43UTC production afd107/7039151
healthy; SQLite27920867328bytes, WAL8120552bytes, free3254681600bytes. Exact
four-file cleanup prepared, not executed: candidate.sqlite3 иsource.sqlite3.zst
вhistorical-copy-cache-am7q8k0k/work иhistorical-zstd-gate-26m3jfsd/work.
Allocated5268840448bytes. Read-only preflight повторён22:53UTC PASS66.937s:
protected gzip encoded/decoded identities иqualifiedV1 candidate1810335b, exact
paths/inodes/modes, terminal controllers, no open handles/no writable mounts.
NewV2 candidate1e6c9853 — воспроизводимый failed output known historical source,
не current live state. Original recovery source, qualified candidate иreceipts
сохраняются; удаление не разрешено. Owner approval запрошен на ровно эти4 файла;
прежние cleanup approvals имеют другие paths. До ответа только независимая работа,
без unlink или source/production DB replacement. После approval повторить весь
preflight/identities/use и incremental receipt; после неоднозначного результата
сначала readback. Prepared next historical controller требуетone-pass capability,
source/artifact identities итот же900s/CPU1/RAM4GiB; пока NOT_RUN.

Independent converted-data contract validation14Oct: disposable125-event fixture
with all revisions/episodes/outcomes/comparisons was rebuilt through Python and
native one-pass paths. Exact NDJSON/CSV, cohorts/distributions, prior causal cutoff,
unchanged input bytes иprivate serialized transport bytes match before/after;
private transport не экспортируется. Local3 tests PASS (nativeN/A onWindows),
Linux4 tests PASS3.64s; quality PASS. Primary receipt:
`cw-converted-public-contracts-fa0ff5c6b7c148a8ad34ea9e804c912c.json`.
Это controlled fixture, не whole/current DB qualification. Cleanup approval на
4 exact files по-прежнему PENDING; unlink/new whole test/production replacement
не выполнялись. Full Goal/54 of58 code scope сохраняется.

### Current Goal checkpoint — 14Oct2026

**Latest owner steering / actual state14Oct12:20UTC:** приоритет — сначала очистить рабочую SQLite, сохранив полезную историю/настройки/алерты; consumer activation отложена до этого шага. По коду по-прежнему54/58 AC, новый audit/проценты не выполнялись. WAL housekeeping11:56:17UTC PASS: `wal_checkpoint(TRUNCATE)` освободил260,392,272bytes, owner revision3/settings hash unchanged, application DML0, прежний контейнер healthy. Это не сокращение основного файла. Latest metadata12:20:11UTC: main31,270,428,672bytes, WAL107,688,592bytes, free7,388,295,168bytes; контейнер8168e8/source047c66e5 unchanged/healthy.

Подготовлен отдельный targeted duplicate-head maintenance: одной transaction заменить только старые full event/outcome/episode/comparison heads на существующий compact head format после полного equality с immutable revision. Сохраняются complete reader values, все history/source/body rows и private settings/preferences/mutes/legacy/transport; перед commit — private undo archive с полным independent decode/hash/count и все head manifests. Windows и exact production image Linux10case fixtures PASS: mixed readers, corruption rollback, deadline rollback, active writer lease refusal, archive/WAL capacity rollback, unknown trigger refusal, no-op compact rows, post-commit checkpoint failure classification, undo recovery с exact original bytes и full fixture integrity. Primary isolated fixture `duplicate-head-fixture-qwk3fu50`; durable COMMIT_ACK отделён от checkpoint outcome, resource guard до каждой mutation и bounded single-payload reads. production history mutation0.

**Owner-approved cleanup / runtime outcome14Oct13:33UTC:** владелец явно согласовал prepared targeted cleanup с5GiB floor/WAL3GiB/reserve1.5GiB, CPU1/RAM4GiB/no swap/общим900s и без full DB backup/scan (reply call_7080db15b7a44034bd85ae81a3f2ed21). Первичный trial `duplicate-head-cleanup-5_swn35_/operation.json`: после exact preflight остановлен только8168e8/source047c66e5; staged old heads проверялись одной transaction. Фактическая скорость приблизительно25k heads за8.5min не подтверждала fit полного scope. До commit/VERIFIED_READY marker отправлен SIGINT только owned maintenance worker; transaction откатилась, прежний контейнер возобновлён. Procedure elapsed544.203s, other projects unchanged; source file не заменён, history rows не удалены. Independent full archive readback PASS6.76s: все25,108 archived heads совпали с original bytes, owner3/settings hash unchanged; primary `duplicate-head-cleanup-5_swn35_/rollback-readback.json`. Получены running/healthy после restart, затем transient unhealthy; final readback13:33:01UTC healthy, последние3 checksPASS, OOMfalse/restarts0. **Очистка не завершена, committed representation changes0**. CI/CD не запускались, source/image/config не менялись. Полный current physical/logical conversion и ordinary CD gates по-прежнему PENDING; old consumed exception не переиспользован.

**Performance checkpoint14Oct15:20UTC:** выбор четырёх семейств через covering PK index с последующим rowid order подготовлен вместо обхода всей runtime table. Local/production-image Linux15 regression scenarios PASS, включая full fixture integrity, точные immutable/private bytes, undo restoration, failure rollback и post-commit checkpoint fault; первичный изолированный fixture `indexed-head-fixture-7y1zceph`, production rows changed0. Read-only key inventory в одной transaction завершён за28.174s: event494,262 / outcome1,403,432 / episode193,112 / comparison83,928. Это полные counts данного snapshot, а не число лишних записей.

Полный indexed read-only preview достиг600.002s до завершения: проверены348,835 legacy heads, full stored payload2,139,697,457bytes / compact projection175,821,355bytes. Logical redundancy1,963,876,102bytes подтверждена только в этой выборке; это не полный объём и не освобождённое место. Immutable byte equality/reference identity проверены, writes/undo/commit/последующий full manifest этим замером не подтверждены. Probe снят, бот не останавливался, production changes0; первичный итог `preview-indexed-all-heads-readonly`15:20:32UTC. Начальный resource limitCPU1/RAM1GiB достигнут; после проверки host reserve только тот же disposable probe переведён наCPU1/RAM4GiB/no swap без продления600s. Результат не является доказательством fit полного900s maintenance или whole DB conversion.

Следующий шаг — устранить оставшийся performance blocker до нового actual-data execution; повтор полного чтения требует конкретного изменения алгоритма, предыдущие проходы вслепую не повторять. Approved scope/resource/time/checks сохраняются, subset не выдавать за всю очистку. Уменьшение самого physical SQLite file всё ещё требует separate reviewed whole conversion/promotion с full source/candidate/recovery; текущие trials не подтвердили её. По коду54/58 AC, audit/проценты не пересчитывались. Владелец обсуждает необходимость объёмного хранения; это не отменяет согласованные analytics AC, retention и сохранение полезной истории без явного изменения требований. Consumer credential draft/TOTP остаётся отложен владельцем.

**F22 write amplification correction, текущий PR33:** заменить live `INSERT OR REPLACE` для mutable runtime на in-place UPSERT с пропуском byte-identical payload. Сохраняются logical values, existing keys/transactions, revision/settings/retention/encoding; новая миграция и очистка истории не выполняются. Validation: REQUIRED local runtime no-op/changed-value/rollback regressions, Source recovery, events/episodes и independent delivery tests; canonical quality и required PR CI до merge. Измерить SQLite `total_changes`, rowid и WAL на изолированной повторной записи. Предыдущие prototype batch/reader профили не подтверждают whole900s fit; полноценный current backup/CD остаётся отдельным незавершённым gate, consumed exception не переиспользовать.

Local validation correction PASS:120 tests (storage/sources/events/history heads/Telegram+agent delivery/ingestion), canonical quality PASS.250 identical replay transactions не изменили total_changes/WAL, changed1→1.0 сохранил rowid и exact numeric tokens, failure rollback и recovery correction PASS. Required CI38596816278/ci-required115966817127 SUCCESS наa8c81f33; прежний PASS на51f86c48 не применяется к runtime correction. Cleanup prototypes проверены отдельно: bounded batch16cases PASS на Windows/Linux, paired2000-event fixture даёт умеренное ускорение; full-reader альтернативный prototype медленнее и не выбирается.

**Разовая поставка PR33 согласована владельцем** (reply call_d41c0c4533b04a66981837e7aac4b49d, «Разрешаю разовую поставку PR33»). Для этой поставки вместо полного scan/backup current history применяется existing bounded compatibility/config recovery gate. Новый root-owned0600 permit связан с exact итоговым reviewed tree PR33, target hash, predecessor047c66e5 и actual container/image; consumed PR31 receipt сохраняется и не переиспользуется. Default full backup/CD, retention и900s/CPU1/RAM4GiB conversion gates не меняются. Риск принят: свежей полной самостоятельной recovery-копии current history перед переключением нет. Maintenance DML, conversion, DB replacement и удаление history не входят в эту поставку. Recovery использует совместимый предыдущий image/config047c66e5; старый G5 image и DB downgrade/restore недопустимы.

Existing image047c66e5 прочитал synthetic DB нового writer: exact canonical event/inputs, owner settings, condition/runtime и corrected source PASS, fixture full integrity PASS; isolated receipt `cw-upsert-reader-i0zvov85`. Whole current state-check14Oct16:44:22UTC завершился exit2 за882.852s; причины UNSET, helper не сохранил stderr JSON. Полного PASS нет; это не основание утверждать corruption, timeout или memory limit. Owned read-only probe удалён, production unchanged/running/healthy. Повтор ради потерянного stderr не нужен.

Validation Plan дополнение: REQUIRED local PR31/PR33 permit binding и rejection wrong baseline/unapproved PR/changed runtime/tree/consumed permit; canonical quality; fresh required CI итогового policy/code head. Local72 delivery/bootstrap regressions и canonical quality PASS. CI38603021030 выявил stale pinned receiver hash в bootstrap; pin обновлён на exact canonical Git bytes, integrity guard сохранён, следующий head требует fresh CI. REQUIRED installation: exact target/receiver hashes, no concurrent deploy, preserved consumed receipt, atomic root receiver/permit readback. После merge REQUIRED automatic CD: immutable source/image, bounded source-readonly compatibility и private config snapshot/readback, health/persistence и fresh ingestion обоих рынков/4TF, recent compact records. Permit consume только после PASS; failure/ambiguity сначала восстанавливается по records. Следующий шаг — fresh CI, exact installation/permit, merge и CD. Ordinary full gate и physical history shrink остаются PENDING; по коду54/58 AC.

**Actual delivery PR33 / necessary hotfix:** PR33 merged14Oct17:07:47UTC вef5daeb08a253c4062beed375f90f3b6628843a2, treee5733f4e40c8bf986546ee0a4cea0311d4fa4c70; required CI38603388275/ci-required115989167548 SUCCESS. Receiver install `pr33-compatible-delivery-ym97no0w/installation.json` PASS, consumed PR31 preserved; новый permit33 ARMED. Automatic CD38604004891 переключил unit на imagebf8b2b49/containerb8918b11, но завершился FAIL вPOST_CHECKS: sequential cold startup load примерно287k persisted candles задержал ingestion за предел150s. Primary record `ef5daeb08a253c4062beed375f90f3b6628843a2-e9c8c298937122cd/deployment.json` остаётсяPOST_CHECK_FAILED, не переписан какPASS; permit не consumed. Настройки revision3/private snapshot hasha8ee4620 сохранены, history не заменялась/не очищалась.

Ограниченный recovery readback17:23UTC: same candidate running/healthy, OOMfalse/restarts0, ingestionFRESH, оба рынкаCONNECTED и1144 acknowledged streams, recovery_pending0, analyticsOK. Last poll1143/1144 доступных windows, одинzero-baseline Spot stream диагностирован. Bounded source-readonly compact/config fixture17:25:19UTC PASS: exact private hash/revision сохранены,32 sampled public records,16 recent compact events, fixture write/restartPASS; первичный `readonly-recovery-proof-b971edb76809491baa358822c99ed004.json` внутри release. Это восстановившийся runtime, а не green исходного CD workflow; successful complete delivery ещёPENDING.

Необходимый hotfix F22/F27 вbranch `codex/responsive-ingestion-startup-20261014`, baseef5daeb0: subscriptions запускаются без eager загрузки всех source windows; bounded persisted window читается существующим recovery/closed-candle path по мере необходимости. Snapshot не вызывает глобальную lazy загрузку: отсутствующий window диагностируетсяbootstrap_pending и не участвует в signal; историческая recovery не становитсяlive. Diagnostics показываютbootstrap_pending_streams. Retention/schema/settings/formulas/REST budget не меняются. Non-goals: очистка/конвертация DB, расширение startup/CD deadline или изменение full backup gates. Validation Plan: REQUIRED local ingestion/source restart/recovery/gap/no-historical-live и snapshot readiness regressions, canonical quality, fresh full PR CI; REQUIRED delivery после отдельного точного разрешения applicable gate — immutable artifact, bounded readonly config/compact checks, actual health/fresh both-market4TF/readiness. Local28 targeted tests/quality PASS. Точный новый code-only gate ещё не согласован: approval PR33 не переносится на другой reviewed tree/PR. Переключать/redeploy вслепую нельзя.

PR34 published head5f219f6c/tree4d50d21e, required CI38607037711/ci-required116001650951 SUCCESS; draft/open, production gate approvalPENDING. Независимая проверка оптимизации старого cleanup не изменила approved procedure: fused head/revision readback сохранил все checks,16 Windows/Linux corruption/deadline/rollback/recovery scenarios PASS. Paired captured2000-event fixture на production imageef5: fused2.052/2.046s против прежнегоbatched1.896/1.801s; вариант отклонён как не ускоряющий эту выборку. Первичный isolated receipt `head-fused-readback-profile-7zm6kztu/profile.json`; working DB не монтировалась, original captured input hash и live container unchanged/healthy. Whole900s cleanup/conversion fit этим sample не доказан; новый full scan и production cleanup не запускались. До отдельного gate decision продолжается сохранение текущего работающего runtime; failed CD33 и его ARMED permit не переиспользуются.

**Решение для PR34 принято:** reply call_857acc1269ee48b9a32326f98d8651e7 разрешает exact prepared one-time code-only gate. Только для этой поставки full history scan/backup заменяется existing bounded source-readonly compatibility и private config recovery/readback; риск отсутствия fresh whole-history recovery принят. Deadline150s, conversion900s/CPU1/RAM4GiB, all CI/image/host/target-lock/health/fresh ingestion/compact post-checks и ordinary backup/CD policy сохраняются. DB cleanup/conversion/replacement не выполняются, current history/source/private settings сохраняются. Старый failed permit33 архивируется побайтно с failed deployment evidence; новый permit34 root0600 tree/target/current ef5 container+image bound. Consume только после всех PASS; failure/ambiguity сначала inspect primary/runtime и exact compatible recovery без DB downgrade/restore. Validation Plan: REQUIRED local scoped permit31/33/34 guards, bootstrap pin/tampering/recovery и ingestion/source tests; canonical quality; fresh required CI final head. REQUIRED installation exact approved receiver diff+hash, private predecessor receipt copies/readback, fresh main/PR/CI gate и exclusive lock. REQUIRED CD ожидаемой merge revision/artifact, private config equality, both-market4TF fresh polling/readiness и compact post-check; исключение не отменяет mandatory checks. Следующий шаг — grouped policy/checkpoint push и эти gates; финальные full conversion/consumer activation/G6 остаются незавершёнными.

Короткий runtime snapshot14Oct15:36UTC: SQLite31,987,564,544bytes, freelist0; это реальные allocated pages, не доказательство размера duplicates по всем namespaces. Metadata-only sample последних10,000 runtime rows15:38UTC подтверждает small new heads (event≤460/outcome≤580/episode≤400/comparison≤295bytes); размеры values/full growth этим sample не экстраполируются. Old history cleanup/physical shrink и current complete recovery остаются незавершёнными. По коду54/58 AC текущей Goal; процент не пересчитывался.


Полный scope58 AC и все findings сохраняются; по коду54/58. Процент не пересчитывался. Проверенный origin/main047c66e5 включает PR31/PR32; required CI38524296127/38525193430 PASS, automatic CD38525778657 PASS. Production source047c66e5/imagefa8b199c, owner revision3: широкий universe включён по явному поручению, прочие settings сохранены. Runtime readback14Oct07:51UTC:1092/1092 streams, fresh ingestion, recovery_pending0, poll/analyticsOK; real Telegram delivery ACK подтверждён. Counts pending jobs остаются PENDING при diagnostic query budget.

Первый CD compact writes выполнен по разрешённой one-time exception; permit CONSUMED. Полный backup/integrity текущей истории не объявляется PASS. Рабочая DB не конвертировалась и не заменялась. Owner-approved точечная очистка четырёх failed outputs выполнена после повторной hash/decode проверки:5,268,840,448 allocated bytes освобождены, protected archive/candidate/runtime сохранены. Primary receipt `new-derived-cleanup-20261014T074615Z-e9850cd2.json`.

Whole historical qualification source68843a9 завершена PASS14Oct08:01UTC за833.526s с CPU1/RAM4GiB/no swap. Source5,422,329,856bytes → candidate2,107,183,104bytes; full source/candidate CHECK/integrity, logical manifests всех пяти таблиц/private bytes и strict-frame independent recovery decode/hash PASS. Primary record `historical-saved-pack-p989q4jn/status.json`; historical_only=true, promotion_eligible=false, production_migration=NOT_RUN. Это не qualification нынешней DB около30GB. Новый whole historical test без изменения проверяемого кода/риска не нужен.

Necessary correction F22/F27: conversion core повторно распаковывал/упаковывал уже shared revisions и заменял их повторяемые16-candle blocks отдельными полными окнами. Controlled regression на source047c66e5 добавляет3 лишних bodies для3 overlapping windows; исправление сохраняет точные compact revision/body bytes без дополнительных bodies. Source/public logical manifests и full candidate body/reference validation выполняются полностью. Branch `codex/preserve-shared-history-20261014`, base047c66e5; schema/retention/time/resource/backup/CD policy не меняются. Production operations этой code correction не разрешены отдельно.

| AC/риск | Проверка и ожидаемый результат | Команда / environment | Этап / обязательность |
| --- | --- | --- | --- |
| F22/F27: shared windows / lossless history | Existing blocks/revisions не перепакованы; public values и original source bytes совпадают; malformed refs/body/projections отклоняются | `python -m pytest tests/test_history_rebuild.py tests/test_conversion_qualification.py -q`, root / dev lock | Local REQUIRED:81 suite PASS +2 targeted corruption cases PASS;1 Linux native skip на Windows; baseline regression FAIL ожидаемо |
| Conventions / docs | Canonical quality PASS | `python scripts/checks.py quality`, root / dev lock | Local REQUIRED, PASS |
| Native helper / full regressions | Required Linux3.11 suite/native/Docker build и aggregate ci-required PASS на code revision51f86c48 | PR33, run38537110560, ci-required115763348283 | CI REQUIRED, PASS для опубликованного code head; последующий local checkpoint не выдаётся за новую проверенную CI revision |
| Current dataset / capacity / recovery | Fresh source/config identity; independent whole conversion≤900s, CPU1/RAM4GiB, full source/candidate/manifests/recovery proof | Reviewed conversion procedure на isolated current snapshot | REQUIRED до любого reviewed DB replacement; PENDING |
| Applicable delivery | Expected merged artifact/config, ordinary full state gates, source/image/health/persistence/application checks | Existing production release/CD procedure | REQUIRED для поставки нового image; consumed exception не переиспользуется |

Capacity update14Oct09:22UTC: owner-approved единственный дубликат `rebuild-known-ybonsl1y/archive.gz` удалён после повторных полных encoded hashes, independent source decode, lineage/handles/mounts checks. Освобождено780,840,960 allocated bytes; original recovery gzip и bot unchanged/healthy. Primary receipt `duplicate-gzip-cleanup-20261014T092245Z-08d29082.json`. Readback09:27UTC: DB30,561,492,992bytes/WAL260,392,272bytes, свободно7,983,648,768bytes. Изменение свободного места всего VPS не приписывается только CryptoWatcher.

Post-qualification archival двух исторических candidates завершена отдельно: qualified2,107,183,104bytes →415,247,504bytes Zstd, receipt `qualified-candidate-archive-ms1nd4xl/receipt.json`; qualified2,174,074,880bytes →459,086,718bytes Zstd, receipt `qualified-candidate-v1-archive-zs927shq/receipt.json`. Полный strict-frame decode/encoded и decoded SHA256, immutable source identity PASS; raw candidates ещё сохранены. Эти операции сохраняют уже qualified historical bytes и не являются staged conversion нынешней DB, новым full SQL scan или production promotion. Первый V1 archive attempt отказал до создания archive из-за UID mismatch; исправленный isolated process использует фактического владельца файла без изменения source permissions/capabilities.

Current resource preflight09:50:18UTC установил actual owner revision3, source inode1930781, logical snapshot30,646,677,504bytes/WAL mode. RAM available8,389,541,888bytes; disk free9,884,598,272bytes при прежнем candidate/archive/reserve bound10,468,982,784bytes: RESOURCE_PREFLIGHT_REFUSED, shortfall584,384,512bytes. Full source/candidate/recovery и actual compressed fit остаются PENDING/UNSET; производственные операции не выполнялись. Старое разрешение stop/checkpoint/restart относилось к предыдущему container/source, нового exact maintenance gate для текущего8168e8 нет.

Consumer rebaseline14Oct09:48UTC: основной agent независимо доставлен42b3f809/image34b43c9/container30c1968; primary CI38542217992/CD38542460080 SUCCESS. Receiver API/schema/context и vault/Admin guards не изменены относительно2bfeda8; schema20261012_0118, radar_versions0, binding/ingestfalse, age capUNSET. Чужая dirty/diverged worktree сохранена без edits. Existing14Oct02:21UTC encrypted R2 backup/schema0118 независимо скачана, расшифрована с совпадением hashes и полностью восстановлена в disposable PostgreSQL:124 tables/249552rows, inventory/valid indexes/constraints PASS39.547s, OOMfalse; primary receipt `cw-consumer-schema-restore-77e6eb5d0f46455c944165b659d28c99/receipt.json`. Config/keys/archives и все три live containers unchanged; R2 uploads и новый capture NOT_RUN. Это backup point02:21UTC с radar_versions0, без утверждения current live row equality или recovery будущего credential. Earlier disposable startup/socket и512MiB tmpfs attempts FAILED, owned containers removed; successful fixture используетCPU1/RAM1.5GiB/no swap, ephemeral data542,232,576bytes. SQLite whole-conversion limits не менялись.

Оставшиеся blockers: whole900s qualification текущей DB и reviewed promotion ещё не закрыты. Exact consolidation preflight09:37:44UTC PASS114.329s: три raw/derived historical copies4,776,484,864 allocated bytes, full hashes/four independent archive restores/frozen identities/zero handles/no writable mount overlap/healthy unchanged bot. Удаление этих трёх exact copies требует отдельной authorization; original gzip и оба компактных candidate archives сохраняются. Количество текущих rowsUNSET: bounded diagnostic count не завершился; read-only probe завершён, unbounded query не оставлен. Общий лимит конвертации900s не увеличивается и не разбивается на continuation runs. Owner отложил draft/TOTP для dedicated consumer credential; ingest/binding/age cap пока не активированы, существующий keyring и другие credentials сохраняются. Новый backup/restore с сохранённым credential, reviewed activation/signed historical smoke остаются обязательными.

Correction self-review/quality и required CI опубликованного code head PASS; PR33 остаётся draft/unmerged, ordinary delivery PENDING. Следующий шаг: завершить exact historical consolidation review и resource preflight текущего dataset; после whole current qualification подготовить reviewed exact production switch. Не использовать старый historical candidate вместо живой истории и не переиспользовать consumed CD exception. Consumer activation и финальный G6 full audit/docs PR ещё не выполнены. Goal DoD не достигнут; unrelated changes и остальные VPS projects сохраняются. Raw research/logs остаются вне repository.
