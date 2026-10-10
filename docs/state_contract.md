# CryptoWatcher — state contract

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
| mutes | Нормализованный symbol и timestamp; текущая G1 имеет выбранный market, typed identity расширяется G2A |
| runtime | Heartbeat, poll/delivery diagnostics, successful notifications, owner input workflow, preserved legacy baselines |
| legacy_import | Source SHA256, полный original payload, имя exact-byte backup; другие chats архивируются, не становятся consumers |
| Events / episodes / outcomes / outbox | Ещё отсутствуют в G1; versioned schema G2B/G3/G4, поэтому полный AC-10-01/10-02 не закрыт одной foundation |

Owner settings: category spot/linear, top/custom, custom_pairs, selected signal TF, price/turnover/liquidity thresholds, poll/cooldown/SMA. Finite positive validation, spike>1, integer cadence/cooldown/lookback, SMA>1; invalid field остаётся без mutation. CAS expected_revision предотвращает lost update. Результат market fetch со старой revision отменяется; successful notification сохраняет применённую revision и candle identity. Failed send не расходует successful cooldown. Durable retry/outbox не заявляется до G2B.

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

Legacy source и archived other chats автоматически не удаляются. G1 backup names уникальны; housekeeping manual до measured policy. Согласованные 90-day candles и permanent referenced event inputs относятся к будущей history schema, не к удалению JSON/backups.

До new-runtime write можно восстановить прежний application с сохранённым JSON по approved sequence. После write legacy JSON отстаёт от SQLite: blind rollback приложения теряет новые settings/runtime. Нужен compatible schema1 rollback или forward-fix; DB restore поверх live данных требует отдельного scoped approval и writer quiescence. Наличие файла `.bak` само по себе не подтверждает пригодность восстановления.
