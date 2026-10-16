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
