# CryptoWatcher — delivery and recovery

## Фактический target

Источник initial baseline: owner-assisted read-only SSH inventory 2026-10-10 09:23:18, 10:08:53 и 10:49:10 UTC; Git blob/archive comparison в G1. Scope — только CryptoWatcher. Таблица сохраняет прежний runtime для compatibility/recovery; актуальная первая поставка зафиксирована ниже.

| Объект | Проверенное значение |
| --- | --- |
| Host / account | Contabo `167.86.68.98`, SSH root:22, owner MobaXterm; локальный BatchMode SSH не авторизован |
| App directory / Compose file | `/opt/crypto_watcher`, `/opt/crypto_watcher/docker-compose.yml` |
| Unit / container | `crypto_watcher` / `crypto-watcher` |
| Compose project | `crypto_watcher`, подтверждён container project label |
| Persistent bind | `/opt/crypto_watcher/data` → `/data`, rw |
| Legacy runtime image | `sha256:153f7a33fa4a96bdc5e63ff53afa1ee5350931625ff9d3d19bee141d83975dcc`; source label отсутствует; после первой migration не запускать с устаревшим JSON |
| Code identity | Startup file SHA256 `b38b2209adc4fc931d5ce93c7d0469e4b26382e61b6aab2354d011437474a6ed`, requirements `3bfa2c84e43ed8ad947dbee85c486294205f48a9be1696083f1fa8882311326f`: совпадают с blobs проверенного main33e0e6; полный image source revision UNSET |
| Versions | Python3.11.15, PTB20.8, requests2.33.1, dotenv1.2.2; Docker29.8.1, Compose5.5.1 |
| Data inventory | JSON55915 bytes и legacy `.bak.2026-04-19-152305`14704 bytes; пригодность старого backup UNSET |
| Time / resource snapshot | NTP synchronized, Europe/Berlin; RAM11960MiB / available7441MiB, disk36GiB free, swap0; достаточность будущего full-universe workload не доказана |
| Architecture / storage | amd64; Docker overlayfs с `io.containerd.snapshotter.v1` |
| Config owners / Git | App/data root:root0755; `.env` и legacy JSON root:root0644; checkout main8ace3638b9193444948d0beb88e0c49e9f2f068e clean. Restrict `.env`0600 в reviewed first procedure |
| Owner mapping / legacy state | Schema2, единственный private chat233382575, configured bot7523865123; alerts enabled, custom list явно пустой, mutes0, baselines380. Settings Spot/Top100/3m/2%/x3/1M USDT/poll60s/cooldown10min |
| Runtime environment | Единственное production приложение; Spot/Perpetual и profiles — продуктовые настройки |

Trading agent в `/opt/claude-bybit-trading-agent` — отдельный runtime и не target этих scripts. Inventory не доказывает Telegram smoke, data restore либо все файлы baseline.

### Первая production migration — подтверждённый snapshot

Owner-authorized MobaXterm procedure2026-10-10T17:22:15Z: source `065e49d112637d5eff4fd309c13aaab30fb53756`, release38046940245, host image/OCI manifest `sha256:a2d59bc276d6e8bd4034593bf5aa48706f746ba18a52d4cc1178bf67a56398fe`, config digest `sha256:14a9a6ebfc4664296d84d46c6c8438906aa061d5f1eaba52e3097cc74c9ac0e2`. Migration/restore/all-table readback/source/health/persistence PASS; usable Bybit data100/100. Settings/config revision1 сохранены, custom empty list/mutes0/alerts ON. Env root0600 после comments-only UTF8 repair с проверкой неизменных active bytes и Compose values.

Primary record: `/opt/crypto_watcher/releases/first-38046940245-20261010T172100Z-775978f9/deployment.json`; recovery copies в его `recovery/`. Runtime запущен из release Compose в этом же directory, а не из прежнего checkout Compose. Migrated DB: `/opt/crypto_watcher/data/sqlite/cryptowatcher.sqlite3`, migration receipt `.g1-migration.json` в том же sqlite directory. Legacy writer остановлен; повтор first-delivery helper запрещён при существующей migrated DB. Следующие schema1/NONE revisions используют routine procedure с online backup, без повторного JSON import или DB downgrade.

Owner export17:27:31–17:28:47 UTC подтвердил основные commands/settings; regression sample исправлена PR22 и доставлена CD38087285142. Owner screenshot SHA256401ddabd… и ответ «вроде работает» подтвердили sample/Mutes/settings smoke: rendering/buttons видны, clicks — owner assertion. Automatic CD enabled после verified bootstrap/access/secret/Environment readback; повторный ручной image transfer не нужен. Snapshot не гарантирует неизменность runtime после этого момента; последующие операции сверяют actual primary records/version.

## GitHub checks, artifact и environment

PR workflow: `pull_request` к main, GitHub-hosted Ubuntu24.04, Python3.11, `contents:read`, без production secrets. Checkout проверяет PR test merge SHA. Select conservatively запускает suite/image для любых недокументационных/неизвестных inputs; docs-only — link/fence check. Aggregate `ci-required` при `always()` проверяет success каждого применимого job; fail/cancel/неожиданный skip не становится green. Фактический admin API readback2026-10-10: strict `ci-required` от GitHub Actions app15368, enforce admins, forbid force-push/delete, conversation resolution; required human approvals0 + self-review. PR20 latest run38042697848 PASS; новые revisions требуют своих applicable checks.

Trusted release: push main либо ручной dispatch только main текущего repository. Exact merged SHA проверяется заново; автоматический reuse пока не реализован. External Actions pinned full SHA. Default Docker build — Linux target architecture; проверенная VPS architecture должна совпасть, иначе stop.

Artifact — immutable GitHub Actions artifact `cryptowatcher-SHA-RUN_ID`, retention90 days, внутри bundle tar с allowlist `manifest.json`, `docker-compose.yml`, `image.tar.gz`. Release v1 связывает source SHA, build run, build Docker image ID, image archive SHA256, Compose SHA256, schema и architecture. Дополнительные `image_config_digest` и `image_manifest_digest` выводятся из config bytes и OCI manifest bytes самого архива; старый v1 без этих полей проверяется тем же способом. Registry digest N/A: registry в этой модели не используется, OCI archive manifest digest доступен отдельно.

Classic Docker и containerd могут иметь разные image ID для одного архива. Проверенный G1 artifact run38042801619 имеет config digest4ce9bd17… и OCI manifest digest50ba81d6…; VPS containerd возвращает второй. Receiver читает exact archived tag только для resolution, проверяет returned ID по этим криптографически связанным digest, OS/architecture/rootfs/source/build/user, затем запускает immutable host ID. Один matching tag/label без digest недостаточен; неизвестный ID блокирует switch. Archive должен содержать одну image/один OCI manifest. PR CI и trusted packaging проверяют save→load→identity round-trip; cross-store/подмена проверяются regression fixtures. Это не заменяет actual target rehearsal.

PR image пересобирается trusted build после merge; его не выдаём за тот же binary artifact. На host загружается именно archive trusted build. Deployment record хранит host `image_id`, build image ID и оба archive digest; для post-check сравнивается immutable host ID.

Один GitHub Environment `production`: eligible main builds/deploys; target только таблица выше, owner runtime configuration — host `.env`, owner domain settings — SQLite. Environment создан и protected-branches-only policy подтверждена admin API2026-10-10. До verified первой миграции `DEPLOY_ENABLED` отсутствует/false: build/artifact success означает delivery PENDING gate. После настройки нужны environment secrets `CW_DEPLOY_SSH_KEY`, `CW_SSH_KNOWN_HOSTS` и variable `DEPLOY_ENABLED=true`. Нет человеческого approval для каждого routine CD после первой проверенной поставки; future protections не обходятся.

GitHub workflow concurrency без cancellation stateful операции плюс host `flock` сериализуют switch/post-checks. Receiver повторно читает public primary main SHA после target lock и перед switch; старый candidate не перезапишет новую main revision. Новая main во время switch допускает завершение операции, затем следующий queued deploy. Если docs merge superseded pending code candidate, stale deploy прекращается; reviewed manual dispatch на текущем main восстанавливает delivery. Не повторять deployment ради RESUME.

## First delivery: EXPLICITLY_GATED

Эта section задаёт sequence и stop/recovery criteria; production commands выдаются с конкретными SHA/path после review, CI artifact и inventory. Не исполнять placeholder значения. Host bootstrap, изменение permissions, остановка legacy writer и JSON→SQLite import требуют owner-authorized concrete procedure. Оператор возвращает только sanitized results, secrets не отправляет в чат.

1. Подтвердить Compose project/service, host architecture, `.env` owner/mode, Git status, owner record и schema. Сохранить unknown changes. Не выполнять pull/reset/build поверх working checkout.
2. После CI/merge получить exact release artifact, verify outer SHA256 и manifest/image hashes/source/architecture. Save прежний image ID и Compose/config ownership как recovery evidence. Не заменять host `.env` шаблоном.
3. Rehearsal: сделать копию JSON в новый изолированный directory; запустить new image без сети с миграцией **только этой копии**, explicit owner233382575. Проверить original hash, enabled/custom empty list/settings/mutes, archived other chats. Backup новой SQLite через CLI; reopen backup, integrity/owner/settings hashes readback. Unknown future/corrupt schema блокирует import. Результат не подтверждает ещё production migration.
4. Перед первой production mutation согласовать exact maintenance sequence: остановить **только** подтверждённый old `crypto_watcher`, проверить отсутствие второго JSON writer и сохранить согласованную JSON/config/Compose backup с hashes. Исходный JSON оставить неизменным. Создать **новый** `/opt/crypto_watcher/data/sqlite` и subdirectory backups UID/GID10001: mode700; не делать recursive chown существующего data. `.env` permissions ограничить root0600 после проверки существующего доступа. Другие chats архивируются, конфликты не объединяются.
5. Запустить new image `migrate` без сети: legacy `/data/state_crypto_watcher.json`, DB `/data/sqlite/cryptowatcher.sqlite3`, backup-dir `/data/sqlite/backups`, owner233382575. Saved owner values выше defaults; explicit empty list не заселяется seed. Проверить state-check, original hash и backup restore на отдельной копии. Записать non-secret receipt `/data/sqlite/.g1-migration.json`: schema1, legacy SHA256, restore_rehearsal PASS и source/build/время/actor. Receipt создаётся только по фактическому PASS.
6. Switch тем же verified image и release Compose, exact existing project и `--no-deps crypto_watcher`; сохранять host `.env`/data. Не запускать два экземпляра с одним Telegram token/DB. CLI lease запрещает второго нового application writer.
7. Пост-check: exact image/source, non-root user/mount, health recent heartbeat; state-check integrity/import/settings/mutes/delivery preference readback; Bybit market data либо объяснённая partial completeness; owner Telegram status/settings/list/terms/no-op/mutes smoke, чужой/group rejected fixtures. Alerts volume/thresholds сохранены. Отсутствие реального нового signal не мешает smoke; sample помечен как sample.
8. После подтверждённой migration/source/health/persistence подключить CD через одноразовую процедуру ниже. Известная sample-only regression не требует предварительного ручного hotfix: она исправляется первой CD и проверяется до G1 DONE. Failures auth/data/migration/source/health не допускают bootstrap. После credentials readback включить DEPLOY_ENABLED и выполнить bounded automatic delivery validation текущей main revision. Только после неё и обязательных owner checks G1 delivery DONE.

## Одноразовое подключение CD

Canonical operator script — [bootstrap_cd.py](../../scripts/bootstrap_cd.py). Выполняется в существующей root SSH-сессии на проверенном host. Перед оператором фиксируются immutable current-main SHA и hashes script/receiver; оба небольших файла скачиваются непосредственно на VPS и проверяются до исполнения. SFTP и ручной transfer Docker bundle не нужны. Public key передаётся отдельно с проверенным fingerprint; private key на VPS не копируется. Canonical CLI принимает `--expected-main`, `--public-key-file`; запись разрешается только с `--execute --approve-key` и точным fingerprint этого public key.

Preflight разрешает только подтверждённый G1 migrated runtime065e49d или подтверждённый sample runtime54b2ce0, проверяя primary record, source/build/config/OCI digests, unit/user/mount, текущий health и readonly DB integrity/owner/migration identity. First record и all-table restore остаются обязательными в обоих случаях. Неизвестная revision, отсутствующий migration receipt, symlink, иной SSH host key/config или существующий неизвестный destination блокируют процедуру.

Bootstrap берёт existing target lock, сохраняет backup `authorized_keys`, устанавливает root-owned receiver0644 и exact target0600, затем добавляет одну строку `restrict` с forced command только для CryptoWatcher. Прежние bytes ключей сохраняются; конкурентное изменение останавливает append. SSH daemon config/restart, bot restart, DB migration и GitHub settings этим script не выполняются. Primary metadata-only receipt сохраняется в новом `/opt/crypto_watcher/rehearsals/cd-bootstrap-*/bootstrap.json`; partial installation требует read-only inspection, без blind retry и восстановления поверх новых keys.

После receipt PASS агент проверяет strict host identity и dedicated SSH безопасным неподдерживаемым probe: receiver должен отклонить его, не предоставляя shell. Затем с verified account/admin и main revision устанавливаются только production secrets `CW_DEPLOY_SSH_KEY` / `CW_SSH_KNOWN_HOSTS`, readback names и `DEPLOY_ENABLED=true`. Первая bounded Actions delivery текущего main подтверждает transport, immutable artifact, backup, source/image/health/persistence/market и owner sample. Все дальнейшие eligible runtime merges используют существующий release workflow. Routine failures сначала исследуются по primary record; ручной image transfer не является штатным обходом CD.

## Routine CD: schema1 / NONE

### Полная проверка многогигабайтной SQLite

Source и persistent backup остаются обязательными full integrity gates до switch.
Проверочные контейнеры имеют отдельный private tmpfs `/validation`, UID/GID10001,
read-only root, network none, cap-drop ALL, CPU1 и memory-swap=memory (без swap).
Бот и другие service units эти limits не получают. RAM allocation определяется
по WAL-aware `database.snapshot_bytes` diagnostics: округление до MiB,
scratch=bytes+64MiB, memory=bytes+512MiB; host MemAvailable должен оставить
1536MiB reserve сверх memory cap. Недоступный размер, недостаток RAM/space или
рост сверх allocation блокирует поставку до switch. Увеличение VPS resources,
очистка данных и ослабление validation не выполняются автоматически.

CLI [validation.py](../../cryptowatcher/validation.py) использует explicit
`--validation-directory /validation` только для `state-check` и `backup`.
Directory должен быть отдельным tmpfs mount текущего UID с mode700, без symlinks.
Для source state-check SQLite backup API копирует pinned WAL snapshot в RAM;
live reader освобождается до full writable integrity, а owner/settings/import
metadata возвращаются из того же проверенного snapshot. Persistent backup
по-прежнему создаётся online copy в established backup directory. После закрытия
он копируется byte-for-byte в RAM, проходит full writable integrity и SHA256
сверку RAM/persistent bytes до и после validation. Temporary workspace удаляется
при любом результате; persistent incomplete backup сохраняется для диагностики.
Default CLI без flag сохраняет прежний disk mode, CD использует mandatory TMPFS.
Все copy/hash/check действия входят в прежний общий stage budget, максимум900s.

В deployment record сохраняются resource allocations, completed backup hash и
source/image/health/persistence/market post-checks. CI capture принимает PASS только
с COMPLETE/TMPFS/full-backup SHA256 proof, source/build/artifact identity и
успешным transport. Неизвестные fields и secret-bearing errors не экспортируются.
Existing root-owned receiver9569f32c обновляется один раз до merge hash-bound
operator installer; SSH key/target и forced-command permissions сохраняются.
Rehearsal2026-10-11T15:44–15:48UTC: closed5,422,329,856byte copy/hash/full writable
integrity PASS201.800s, actual integrity31.721s; constraint negative control и
original/live preservation PASS. Это snapshot qualification, не гарантия capacity
растущей production DB. Required Linux CI/image checks и actual CD остаются gates.

### Hotfix receiver update до merge

При подтверждённом mismatch установленных operation timeouts с multi-GiB history применяется [update_cd_receiver.py](../../scripts/update_cd_receiver.py). Это один operator setup step из существующей root SSH session; restricted Actions key не получает shell/self-update permissions. Подготовленный installer и receiver берутся из immutable reviewed PR head с проверенными hashes. Preflight и execute требуют unchanged expected main/base/head, trusted repository и latest successful `ci-required` app15368. До изменения проверяются exact host/account, target hash, old receiver hash/permissions, healthy running G5 revision и отсутствие competing deploy под target lock. Execute сохраняет verified old receiver, атомарно заменяет только root-owned receiver и записывает receipt; app/data/SSH/secrets не меняет. Unknown/partial state не повторяется вслепую. После receipt PASS агент merges PR через обычные gates; последующий CD проверяет новый main artifact.

Обновлённый receiver сохраняет pre-switch phases и error class в atomic `deployment.json`, не exception body/env. Full state integrity и full snapshot backup integrity остаются REQUIRED. Database budget180s+150s на каждый начатыйGiB, максимум900s на stage; внутренний CLI budget короче на15s для controlled exit. Основание: readonly production snapshot2026-10-11T09:49:43UTC, DB4,820,512,768bytes, full integrity PASS за614.323s с validation cache128MiB и effective mmap2,147,418,112bytes. При этом размере outer budget900s/CLI885s оставляет около270s сверх измеренной проверки для copy/variation; пригодность snapshot backup подтверждается отдельно actual CD. Cache/mmap/progress10000 применяются только на validation connection, затем восстанавливаются; steady-state runtime settings и DB format не меняются. Release job имеет40min upper bound для build и двух таких checks, это не monitoring или latency SLO. Online backup использует pinned read snapshot и incremental copy, чтобы live WAL writer не перезапускал его бесконечно. Incomplete destination сохраняется и не считается пригодной backup. После timeout останавливается только созданный этой operation named offline candidate с проверенной image/name и отсутствием Compose project; running application не затрагивается. Данные и старые failed releases не удаляются.

Qualification ограничена actual delivery. CD38133517275/9634558a прошёл source integrity, но завершился FAILED_PRE_SWITCH на BACKUP_AND_VALIDATE по общему copy+integrity budget. Отдельный read-only probe закрытой копии5,422,329,856bytes2026-10-11T11:42–11:57UTC с cache512MiB/CPU1/memory3GiB также превысил885s; полный результат отсутствует. Cache128MiB и gates сохраняются, efficacy увеличения cache не доказана. Перед следующей поставкой требуется установление причины ожидания и qualification исправления; повтор full scan/deploy не используется для speculative tuning. Primary receipt и ограничения — в [текущем hotfix](../open_issues.md#current-goal--полный-scope-cryptowatcher-implement).

CLI `state-check` и full integrity этап `backup` возвращают безопасный JSON `failure_code` и отдельный exit status:3 TIME_BUDGET_EXCEEDED,4 DATABASE_CORRUPT,5 INTERRUPTED,6 IO_ERROR,7 DATABASE_ERROR. Timeout во время backup copy тоже exit3; CLI backup result дополнительно содержит copy/integrity/total durations и phase без содержимого DB/config. Поэтому установленный receiver различает причины по `docker exit N` даже без публикации stderr/body; durations остаются CLI Evidence, текущий receiver их не переносит в host record. Общие ошибки configuration/state сохраняют exit2. TIME_BUDGET_EXCEEDED не доказывает повреждения; DATABASE_CORRUPT означает non-ok результат полной проверки либо SQLite CORRUPT/NOTADB. Полная проверка читает все возвращённые rows; interruption очищает progress handler. Ошибка или незавершённый результат блокируют switch, не заменяются quick_check или PASS.

CI-side [deliver_release.py](../../scripts/deliver_release.py) сохраняет непустой safe deployment artifact для success/failure. PASS требует exact revision/build run/bundle digest, image identity и обязательный receiver post-check result; raw stderr/exception text и неизвестные fields не публикуются. Receiver rejection из stderr даёт BLOCKED с allowed phase/constant failure code; unknown transport response остаётся UNCONFIRMED по runtime и требует primary host readback. Клиент сохраняет strict host verification, fixed target/forced command, единственную попытку и не повторяет поставку. Existing record не перезаписывается. Наличие failure artifact не подтверждает фактическое состояние runtime и не даёт разрешение retry.

Canonical receiver — [deploy_receiver.py](../../scripts/deploy_receiver.py). Он принимает только `cryptowatcher-deploy SHA BUNDLE_SHA256 BYTE_COUNT`, не shell/произвольную команду. Проверяет established root-owned target, migration receipt и migrated DB, lock, latest main, free disk, stream length/hash, tar allowlist/regular members, schema/migration class, image/architecture/source, exact Compose service и persistent mount.

До switch: config validation без вывода token, DB integrity/owner/import identity, online SQLite backup с новым уникальным именем. Compose `up -d --no-build --pull never --no-deps crypto_watcher` не затрагивает другие services. Image/config/heartbeat revision и configuration revision readback обязательны; bounded poll post-check допускает explained per-symbol DEGRADED при доступных данных либо intentional empty custom list. Source failure без usable data блокирует success. Health не перезапускает бот лишь из-за Bybit outage.

Records: Actions tested SHA/check/run/attempt, build artifact/manifest, target release `deployment.json` (previous image, backup, switch and post-check state), uploaded deployment artifact. Секреты и resolved config не логируются. Failed operation сохраняет attempted release; повтор с тем же bundle отвергается до разбора state. После transient failure новая artifact attempt допустима только после known state inspection. Advanced cleanup/backups retention пока manual, без system prune или удаления живых данных.

## Recovery и stop criteria

До first switch при import/rehearsal failure: legacy JSON не изменён, new bot не запущен; после проверки единственного writer можно по согласованной procedure запустить прежний unit/image с исходным JSON. Частичный новый DB не подменяет legacy. Не удалять его для скрытого retry.

После первого new-runtime write: legacy rollback читает устаревший JSON и несовместим с SQLite runtime. Automatic downgrade/restore запрещён. Forward-fix предпочтителен; app-only rollback разрешается только к verified schema1-compatible image по отдельному review. Restore поверх live DB требует отдельного scoped authorization/quiescence и проверенного backup; наличие старого `.bak` недостаточно.

Routine post-check failure: дальнейшее продвижение остановлено, primary failed record сохраняется; inspect expected/running artifact, health, DB и sanitized diagnostics. Не выполнять `compose down`, volume delete, broad clean/reset/prune. Ошибка SSH/auth/host identity не устраняется отключением проверки. Новый stateful class либо DB version требует новой reviewed migration procedure, не через routine receiver.

## Automatic CD — подтверждённая G1 поставка и G2A условия

2026-10-10T21:24:37Z: run38087285142, source40882eb8b555991ce1f22c87fe95b3951507d74c, deployment artifact11682629335, PASS. OCI manifest38d238a6c5e2e51efc4a8a63a9286291f74fb5ff0285519b7c1285b614fd431a, image config7666f3230744c47c7857c9b8443318a61d3e5754a833d866b423033152ef5068; online backup pre-40882eb8b555991ce1f22c87fe95b3951507d74c-1791667365.sqlite3. Actual primary record в release40882eb…-d5c977a0003450bc/deployment.json. Source/image/health/persistence/market100/100 PASS. Production DEPLOY_ENABLED=true, dedicated key/strict host verification и forced receiver operation проверены; существующие SSH keys сохранены.

G2A classification NONE/schema1: source namespaces не изменяют физический DB schema и покрыты существующим online backup. Перед switch candidate CLI должен читать saved owner/import/preferences; после switch expected source/image/health/persistence и poll prerequisites проверяет current receiver. `WARMING` не считается ready: требуется discovery обоих рынков, все subscription acknowledgements и fresh usable candles каждого active market/TF. Полный universe bootstrap может продолжаться; counts и причины partial state обязательны в diagnostics. Local bounded smoke не заменяет actual production post-check.

После изменения typed owner fields старый G1 image не является compatible rollback. Forward-fix/compatible G2A application допускается по проверенной процедуре; restore поверх живой DB не выполняется. Не менять unit trading agent, SSH daemon или receipt первой migration в routine delivery.

## G2B routine delivery условия

Classification NONE/schema1: versioned `radar:v1` records используют existing runtime table, physical DDL/JSON re-import отсутствуют. До switch existing receiver проверяет candidate config и сохранённые owner/import/preferences, затем делает online backup всех namespaces. После switch сохраняются dual-market/4TF readiness, expected source/image, health и persistence gates; новые detector metadata регистрируются атомарно при startup. Formula/version mismatch блокирует startup и требует forward-fix.

Saved custom thresholds/delivery preferences не заменяются; absent new fields означают custom profile и composite-only Telegram selection. Новые families собирают историю без notification до явного owner enable. No historical live flood обеспечивается producer policy. CLI/Telegram diagnostics показывают five detector registry, localized errors, active episodes, pending attempts и blocked Telegram target; `/alerts on` возобновляет target. Owner ad-hoc history/profiles/detectors smoke рекомендован, реальный market trigger не требуется для DoD.

После owner записи G2B fields прежний G2A image не считается rollback-compatible даже при SQLite schema1. Разрешённый recovery — forward-fix или verified compatible G2B image; downgrade/restore поверх живых данных запрещён. Automatic CD и restricted receiver остаются прежними; ручной SFTP/image transfer для этой Goal не нужен. Restart/retry/backup behavior подтверждается regression suite; actual runtime conclusions ограничиваются primary deployment records.

## G3 routine delivery условия

NONE/schema1: no physical DDL или JSON re-import, outcome/comparator/digest logical namespaces входят в existing all-row online backup. Candidate CLI читает прежний owner/import/preferences; initial history admission и referenced holds idempotent, retention запрещён до полного admission. Сохраняются old event/revision/reference bytes и immutable methodology records. New owner fields отсутствуют до явной записи; defaults outcome5/15/30/60 и digestOFF/60min не заменяют сохранённые preferences.

Automatic CD прежний: expected source/image/config digest, online backup, health/persistence и two-market/4TF readiness. Analytics worker errors влияют на heartbeat health; pending horizons/малый N/partial comparison history диагностируются и не объявляются отказом. Runtime CLI diagnostics включает outcomes admission/pending, analytics и digest preference/pending; schema/backup/source checks обязательны. Owner ad-hoc `/history`→Outcome, `/horizons`, `/analytics` и digest status рекомендованы; включение digest или ожидание реального60min event не требуется для delivery DoD.

G2B reader после новых owner settings не rollback-compatible. Forward-fix/verified compatible G3 image, без DB downgrade/restore поверх live state. Referenced old ranges могут восстанавливаться за пределами ordinary90day retention; original used snapshots не удаляются. Ручной перенос на VPS и повтор bootstrap не нужны; дальнейшие stages начинаются после подтверждённого CD.

## G5 routine delivery условия

NONE/schema1: logical mapping/OI/context/methodology records входят в existing all-row backup; physical DDL и JSON re-import отсутствуют. Owner enrichment первоначально OFF, existing numeric/delivery settings сохраняются. Required post-checks прежние: exact merged source/image/config, backup, health/persistence, both-market/4TF readiness. Optional enrichment failure не блокирует deterministic core health; enabled/worker/mapping/errors видны в Telegram и read-only CLI diagnostics. Малый N/no historical context не считается отсутствием реализации.

Owner ad-hoc `/derivatives spot/BTC`, `/compare`, `/enrichment` status рекомендованы; включение enrichment/ожидание реального signal не обязательны. Автоматический CD доставляет stage без ручного переноса файлов. После owner записи enrichment field G3 image не rollback-compatible; forward-fix/verified compatible G5, без downgrade/restore поверх live DB. Actual capacity/public metrics freshness требуют соответствующих measurements, не выводятся из workflow configuration.
