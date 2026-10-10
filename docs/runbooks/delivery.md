# CryptoWatcher — delivery and recovery

## Фактический target

Источник: owner-assisted read-only SSH inventory 2026-10-10 09:23:18 UTC; Git blob comparison в локальной G1. Scope — только CryptoWatcher.

| Объект | Проверенное значение |
| --- | --- |
| Host / account | Contabo `167.86.68.98`, SSH root:22, owner MobaXterm; локальный BatchMode SSH не авторизован |
| App directory / Compose file | `/opt/crypto_watcher`, `/opt/crypto_watcher/docker-compose.yml` |
| Unit / container | `crypto_watcher` / `crypto-watcher` |
| Compose project | UNSET до readback container project label; script требует exact target configuration |
| Persistent bind | `/opt/crypto_watcher/data` → `/data`, rw |
| Runtime image | `sha256:153f7a33fa4a96bdc5e63ff53afa1ee5350931625ff9d3d19bee141d83975dcc`; source label отсутствует |
| Code identity | Startup file SHA256 `b38b2209adc4fc931d5ce93c7d0469e4b26382e61b6aab2354d011437474a6ed`, requirements `3bfa2c84e43ed8ad947dbee85c486294205f48a9be1696083f1fa8882311326f`: совпадают с blobs проверенного main33e0e6; полный image source revision UNSET |
| Versions | Python3.11.15, PTB20.8, requests2.33.1, dotenv1.2.2; Docker29.8.1, Compose5.5.1 |
| Data inventory | JSON55915 bytes и legacy `.bak.2026-04-19-152305`14704 bytes; пригодность старого backup UNSET |
| Time / resource snapshot | NTP synchronized, Europe/Berlin; RAM11960MiB / available7441MiB, disk36GiB free, swap0; достаточность будущего full-universe workload не доказана |
| Architecture / config owners / Git changes | UNSET до следующего owner readback |
| Runtime environment | Единственное production приложение; Spot/Perpetual и profiles — продуктовые настройки |

Trading agent в `/opt/claude-bybit-trading-agent` — отдельный runtime и не target этих scripts. Inventory не доказывает Telegram smoke, data restore либо все файлы baseline.

## GitHub checks, artifact и environment

PR workflow: `pull_request` к main, GitHub-hosted Ubuntu24.04, Python3.11, `contents:read`, без production secrets. Checkout проверяет PR test merge SHA. Select conservatively запускает suite/image для любых недокументационных/неизвестных inputs; docs-only — link/fence check. Aggregate `ci-required` при `always()` проверяет success каждого применимого job; fail/cancel/неожиданный skip не становится green. Branch protection должен требовать этот gate, forbid force-push/delete и учитывать admin; default human approvals0 + self-review. Фактический protection readback до настройки PENDING.

Trusted release: push main либо ручной dispatch только main текущего repository. Exact merged SHA проверяется заново; автоматический reuse пока не реализован. External Actions pinned full SHA. Default Docker build — Linux target architecture; проверенная VPS architecture должна совпасть, иначе stop.

Artifact — immutable GitHub Actions artifact `cryptowatcher-SHA-RUN_ID`, retention90 days, внутри bundle tar с allowlist `manifest.json`, `docker-compose.yml`, `image.tar.gz`. Manifest связывает source SHA, build run, Docker image ID, image archive SHA256, Compose SHA256, schema и architecture. Это Docker config digest/image ID и archive SHA256; registry manifest digest N/A, registry в этой модели не используется. PR image пересобирается trusted build после merge; его не выдаём за тот же binary artifact. На host загружается именно archive trusted build.

Один GitHub Environment `production`: eligible main builds/deploys; target только таблица выше, owner runtime configuration — host `.env`, owner domain settings — SQLite. Environment должен разрешать только protected main. До verified первой миграции `DEPLOY_ENABLED` отсутствует/false: build/artifact success означает delivery PENDING gate. После настройки нужны environment secrets `CW_DEPLOY_SSH_KEY`, `CW_SSH_KNOWN_HOSTS` и variable `DEPLOY_ENABLED=true`. Нет человеческого approval для каждого routine CD после первой проверенной поставки; future protections не обходятся.

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
8. После PASS установить root-owned `/usr/local/lib/cryptowatcher/deploy_receiver.py` и target JSON `/etc/cryptowatcher/deploy-target.json` (root, без group/world write). Exact app/data/env/service/project/architecture + automatic_delivery_enabled true; установить отдельный SSH public key с forced command на этот receiver и `restrict`. Не менять существующий access key. Private key сохраняется только у owner и GitHub Environment secret; server host key сверяется с доверенным known_hosts, `StrictHostKeyChecking=yes`. После credentials readback включить DEPLOY_ENABLED и выполнить bounded automatic delivery validation той же текущей main revision. Только после неё G1 delivery DONE.

## Routine CD: schema1 / NONE

Canonical receiver — [deploy_receiver.py](../../scripts/deploy_receiver.py). Он принимает только `cryptowatcher-deploy SHA BUNDLE_SHA256 BYTE_COUNT`, не shell/произвольную команду. Проверяет established root-owned target, migration receipt и migrated DB, lock, latest main, free disk, stream length/hash, tar allowlist/regular members, schema/migration class, image/architecture/source, exact Compose service и persistent mount.

До switch: config validation без вывода token, DB integrity/owner/import identity, online SQLite backup с новым уникальным именем. Compose `up -d --no-build --pull never --no-deps crypto_watcher` не затрагивает другие services. Image/config/heartbeat revision и configuration revision readback обязательны; bounded poll post-check допускает explained per-symbol DEGRADED при доступных данных либо intentional empty custom list. Source failure без usable data блокирует success. Health не перезапускает бот лишь из-за Bybit outage.

Records: Actions tested SHA/check/run/attempt, build artifact/manifest, target release `deployment.json` (previous image, backup, switch and post-check state), uploaded deployment artifact. Секреты и resolved config не логируются. Failed operation сохраняет attempted release; повтор с тем же bundle отвергается до разбора state. После transient failure новая artifact attempt допустима только после known state inspection. Advanced cleanup/backups retention пока manual, без system prune или удаления живых данных.

## Recovery и stop criteria

До first switch при import/rehearsal failure: legacy JSON не изменён, new bot не запущен; после проверки единственного writer можно по согласованной procedure запустить прежний unit/image с исходным JSON. Частичный новый DB не подменяет legacy. Не удалять его для скрытого retry.

После первого new-runtime write: legacy rollback читает устаревший JSON и несовместим с SQLite runtime. Automatic downgrade/restore запрещён. Forward-fix предпочтителен; app-only rollback разрешается только к verified schema1-compatible image по отдельному review. Restore поверх live DB требует отдельного scoped authorization/quiescence и проверенного backup; наличие старого `.bak` недостаточно.

Routine post-check failure: дальнейшее продвижение остановлено, primary failed record сохраняется; inspect expected/running artifact, health, DB и sanitized diagnostics. Не выполнять `compose down`, volume delete, broad clean/reset/prune. Ошибка SSH/auth/host identity не устраняется отключением проверки. Новый stateful class либо DB version требует новой reviewed migration procedure, не через routine receiver.
