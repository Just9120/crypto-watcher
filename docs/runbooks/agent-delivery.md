# Handoff CryptoWatcher → trading agent

## Handoff принимающему проекту

**Решение владельца S06/D23,16Oct2026:** CryptoWatcher отвечает за standalone
alerts под ручную торговлю и готовый producer/contract. Подключение принадлежит
проекту `claude-bybit-trading-agent`. Credential/binding и выключенный ingest
не блокируют CW DoD. Этот handoff не утверждает завершение live интеграции.

### Граница ответственности

| CryptoWatcher — подготовлено здесь | Принимающий проект — выполняется там |
| --- | --- |
| Public Bybit data,5detectors,оба markets/4TF,episodes/history/outcomes/Telegram | Использование advisory context в своей архитектуре без неявного исполнения |
| Versioned envelope, source/settings/method provenance, signed immutable outbox | Receiver/schema/auth validation, persistent idempotency/revision conflicts и causal reads |
| Default integration UNSET/OFF, independent diagnostics/retry/replay | Dedicated credential/recovery/binding, HTTPS ingress и max-age900s |
| Автоматические producer HMAC/ack/outage/restart/expiry/isolation fixtures | Согласованная producer infrastructure config/enable и actual end-to-end acceptance |
| History/export в пределах D22; полнота/малый N/expired detail обозначены | Собственные retention/availability rules; не предполагать наличие старых raw inputs |

Принимающая задача координирует изменения обеих integration configurations с их
владельцами. Handoff сам не разрешает менять producer credentials/host config,
перезапускать CW, выполнять reset или затрагивать trading flags. В этой CW задаче
ничего из перечисленного не включается. Новый secret вводит владелец штатным
способом принимающего проекта; plaintext не передается через чат/artifacts.

### Пакет передачи и доказательства

| Artifact / source | Что передается / предел Evidence |
| --- | --- |
| [Producer projection](../../cryptowatcher/agent_contract.py) | Точный v2 envelope, enum mappings, timestamp/units/settings digest, episode/enrichment/outcome snapshots |
| [Transport/outbox](../../cryptowatcher/agent_delivery.py) | URL/config validation, HMAC raw bytes, immutable retry body, ack/expiry/replay/target diagnostics |
| [Synthetic JSON example](../contracts/cryptowatcher.market_event.v2.example.md) | Один реальный output текущего producer fixture: Spot/3m/price, полный snapshot/hash и episode confirmations; **не рыночное событие** |
| [Producer regression suite](../../tests/test_agent_delivery.py) | Controlled receiver проверяет подпись/identity/false authority; restart/retry/outage/Telegram isolation/expired historical replay |
| [History/export contract](../state_contract.md), [CLI navigation](../../README.md#outcomes-digest-и-export) | Frozen event/reference/availability, NDJSON/CSV, causal context и bounded retention |
| [Primary records](../open_issues.md#evidence-и-baseline) | CW runtime130225d2/image26b64aee…/CI38897051479/CD38897380425; подробные identities и ограничения в плане |
| Reference consumer | Внешний repo `claude-bybit-trading-agent`, `app/schemas/cryptowatcher_v2.py`; последняя проверенная revision8c207edf, PR949/CD38899376113. Current consumer main/runtime перед activation перепроверяет принимающая задача |

Example generated при контролируемом времени **2027-01-15**, producer_ref
`test-radar`, producer source SHA из fixture (повторяющаяся `a`), synthesized prices
и settings. Его snapshot hash проверен по canonical JSON; actual request HMAC
вычисляется над точными отправляемыми bytes, а не над разобранным/пересериализованным
JSON. Не использовать example для production POST, live-smoke, credentials или
доказательства текущей market freshness. Пример не содержит ключа/подписи/transport
credentials; полный payload меньше65536bytes.

Последний reference consumer parser принял40 synthetic producer envelopes и
отклонил40 authority-tampering случаев без ingest/DB writes. Это schema qualification,
не actual signed delivery. Последняя metadata проверка16Oct12:39:21UTC: producer
UNSET, consumer ingestOFF/binding absent/max-ageUNSET, radar credential versions0.
Этот snapshot описывает состояние на свое время; принимающая задача получает fresh
Evidence. Zero Trust не является установленной предпосылкой: прежний urllib403/
CF1010 и HTTPx503 ingest_disabled описаны ниже, причину своего ingress отказа
проверяет принимающая сторона.

### Acceptance checklist задачи подключения (вне CW Goal)

| ID | Задача принимающей стороны / ожидаемый результат |
| --- | --- |
| TA-CW-01 | Установить свой актуальный root/main/runtime/receiver version и действующие процедуры; owner создает dedicated signing credential, recovery подтвержден. Existing vault master/другие credentials сохраняются |
| TA-CW-02 | Настроить точный binding producer_ref/account_ref, provider EXTERNAL_DATA/purpose cryptowatcher_context/environment PRODUCTION; HTTPS POST route и согласованные900s max-age. Проверить missing/unauthorized/tampered rejection без unsafe ingress bypass |
| TA-CW-03 | Согласовать producer host/env secret delivery и enable с CW configuration owner по существующей процедуре. `AGENT_*` config и `/agent` controls описаны ниже; alerts/digest/watchlist/thresholds не меняются |
| TA-CW-04 | Bounded smoke на реальном сохраненном **expired** event с explicit historical envelope: signed ack/receipt/readback, duplicate/conflict/reorder/expired replay. Synthetic LIVE probes не использовать; freshness receipt/retry не освежает observation |
| TA-CW-05 | Проверить outage/restart/immutable retries и независимость Telegram mute/outage; свои history/episode revisions/outcomes и causal cutoff не получают future facts. Consumer базово работает без radar; прием event не вызывает model/order |
| TA-CW-06 | Зафиксировать exact producer+consumer source/image/config versions, environment/time, post-checks, delivery diagnostics, stop/recovery actions. Trade authorization остается false; failed gate не заменять PASS и не выполнять повтор вслепую |

### Условия использования контекста

Детектор описывает наблюдение, а не BUY/SELL/position/order instruction. Тип/рынок/TF
берутся из event identity; legacy `bybit_category`/`signal_timeframe` в settings
описывают owner card/compatibility и не ограничивают shared both-market/4TF pipeline.
Raw source windows выдаются producer-side в пределах хранения, не помещаются в
bounded envelope. Details30d, small-TF candles35d с конечными used dependencies,
quality12calendar months; summaries не восстанавливают старый decision context.
History началась заново16Oct2026: prior no-signal N ограничен новой generation.
20GB steady-state target пока не доказан; автоматический growth warning и
измерения/ограничения доступны в плане. Receiver не должен трактовать missing/
incomplete/stale context как zero return, fresh signal или торговое разрешение.

## Contract и qualification

Physical state producer сейчас schema2; logical agent preference/event/outbox
versions независимы от нее. Действующий backup/recovery определяется
[bounded-storage процедурой](bounded-storage-reset.md#routine-schema2-backupcd).
Ни наличие binaries, ни приведенные исторические qualifications не включают
owner credential/binding; последний activation snapshot и primary records —
в [плане](../open_issues.md#blockers-и-следующий-шаг).

Producer передаёт `cryptowatcher.market_event.v2` в `POST /integrations/cryptowatcher/events`.
Consumer сохраняет приём v1; v2 поставлен в main67716b3282bf9fefe1a84077dfae7c6c99c6b1f0,
production CD38100792664 подтвердил source/image/readiness 2026-10-11 01:12UTC.
Это подтверждение binary, не активации binding или HTTPS ingress. Read-only
qualification12Oct2026: actual consumer2bfeda8, existing keyring crypto roundtrip
и wrong-key/binding/tamper controls PASS; active master совпал с material ранее
verified encrypted recovery package. Это recovery master key, не текущей DB.
Actual producer httpx/TLS unsigned POST достигает приложения и получает503:
consumer ingest OFF, binding отсутствует, context max age UNSET. Отдельных radar
credential versions/active bindings0. Zero Trust/Access не добавляется. До activation
нужны dedicated encrypted binding, owner-approved900s config и actual signed smoke
после qualified producer delivery; прочие credentials/config сохраняются.
Базовая работа agent сохраняется при выключенном/недоступном radar; receipt не запускает
LLM или order execution. `trading_instruction`, `trading_authorized`, `model_invoked` — false.

Envelope содержит immutable event/source revision, market identity, detector/version,
settings revision/raw snapshot/hash и effective parameters, TF/direction/metrics/units,
observation/detection/receipt/reference times, source hash/completeness, freshness boundaries,
episode revision/confirmations и optional frozen enrichment. Raw candle windows остаются
в producer history/export. Original G2B snapshots без поздних outcome/digest/enrichment
fields не заполняются новыми defaults. Поздние outcomes имеют отдельный explicit
`HISTORICAL_OUTCOME_UPDATE`; они не меняют source metrics/settings/reference или live cutoff.
Episode closure передаёт CLOSED snapshot без освежения исходного наблюдения.

Known `detail_retention_days`/`candle_retention_days` в полном settings snapshot
принимаются consumer как optional strictly positive integers; legacy отсутствие
не дополняется выдуманными defaults. Исходные snapshot/hash сохраняются. Эти поля
описывают storage policy и не продлевают market freshness/consumer max-age.
Unknown/secret settings и execution fields по-прежнему запрещены.

## Owner control и infrastructure для будущего подключения

`/agent status`, `/agent on`, `/agent off`, `/agent retry`, `/agent replay EVENT_ID`
доступны только владельцу в private chat через общий guard. Preference logical version1 хранится
отдельно от domain settings, Telegram alerts/mutes и digest; default OFF. Unknown/corrupt
preference сохраняется и fail-closed. `/diagnostics` и read-only CLI `diagnostics` показывают
enabled/configuration/intents/pending/delivered/blocked и target state, без credentials.

В host `.env` задаются вместе `AGENT_EVENT_URL`, `AGENT_PRODUCER_REF`, `AGENT_HMAC_SECRET`.
URL — HTTPS, точный route выше, port443, без userinfo/query/fragment. Producer identity
должен точно соответствовать существующему consumer vault binding: provider EXTERNAL_DATA,
purpose cryptowatcher_context, account_ref producer_ref, environment PRODUCTION.
Secret — dedicated api_key, минимум32 UTF-8 bytes. Не создавать/заменять неизвестный vault
master key. Consumer самостоятельно ограничивает freshness своей явно установленной policy.
Owner decision2026-10-11: `CRYPTOWATCHER_CONTEXT_MAX_AGE_SECONDS=900`. Этот cap действует
от market observation, одновременно с type/TF producer fresh_until и active episode;
более короткая freshness сохраняется. Значение публикуется в consumer config только
по qualified activation procedure; spec decision не подтверждает running setting.
При необходимости qualified service authentication разрешены paired
`AGENT_ACCESS_CLIENT_ID`/`AGENT_ACCESS_CLIENT_SECRET`; headers ставятся только на agent
request, не на public Bybit. Они не обходят Access policy. Secrets не входят в DB/export/logs.
Zero Trust/Access сейчас не добавляется.403 сам по себе не доказывает Access block:
diagnostic urllib и actual shared httpx client проверяются раздельно, с verified TLS,
без redirects и credentials. Safe unsigned POST с пустым object не может создать event;
ожидается configured-auth rejection или disabled-consumer503. Для qualification нужны
source host/runtime, request method/client, timestamp, status и при наличии Cloudflare
error code/Ray ID; нельзя считать GET404 доказательством отсутствия POST route.
Все значения отсутствуют: integration UNSET, radar работает. Partial/invalid configuration:
только agent BLOCKED, deterministic core/Telegram/outcomes продолжают работать.

## Persistence и retries

Event/episode/source/outcome и consumer intents записываются в одной SQLite транзакции.
Live confirmations coalesce2s, уведомление при первом event, новой family/TF либо strength
>=1.5 от последнего успешно принятого уведомления. Agent baseline независим от Telegram.
Источник и episode snapshot замораживаются в canonical UTF-8 JSON bytes до network I/O.
SHA256 body и raw-body HMAC-SHA256 `x-cryptowatcher-signature` относятся к этим же bytes.
Envelope <=65536 bytes, ack body <=4096 bytes; overall request timeout12s, TLS verification,
no redirects. Shared async client используется вне state/DB locks.

Ack200 должен совпадать по event_id/revision, содержать64hex receipt_id и false authority
flags. Только такой ack расходует successful live notification state. Потерянный ack и
interrupted IN_FLIGHT повторяют исходные bytes/id/revision; ambiguity хранится отдельно.
Transient network/408/425/429/5xx: exponential1–300s; Retry-After seconds/HTTP-date учитывается.
Permanent error блокирует target до owner on/retry. Pending записи сохраняются в пределах D22; после допустимого срока получают terminal status и освобождают dependencies, не удерживая всю history бессрочно. Oversized payload
BLOCKED индивидуально. Failed send не останавливает history или другой consumer.

Live payload, истёкший либо устаревший по settings revision до отправки, сохраняется как
SUPERSEDED, получает новый explicit historical transport revision. Source event revision
не переписывается. Manual replay fresh текущего confirmation может восстановить live
context; expired/closed/replaced confirmation и replay с outcomes всегда historical.
Future source/episode timestamps дают WAIT_CLOCK; local clock не подделывает produced time.
Нельзя использовать synthetic LIVE production probes: bounded smoke выполняется на
существующем expired event/historical envelope и проверяет receipt/readback/cutoff.

## Delivery и recovery при подключении принимающим проектом

Producer physical schema2, logical agent state v1; activation сама по себе не
выполняет DDL, JSON re-import или reset. SQLite backup включает agent namespaces,
preferences и необходимые immutable bytes/pending work. Current routine CD
выбирает disk source integrity/online backup/full backup integrity/hash и
expected merged source/image/health/persistence/dual-market/4TF checks по
[действующей процедуре](bounded-storage-reset.md#routine-schema2-backupcd).
Нельзя брать historical schema1 compressed-RAM validation за routine schema2 flow.
Activation требует qualified consumer config/secret/HTTPS и bounded
joint runtime verification; binary deployment не выдаётся за включённую integration.
Forward-fix/compatible binary; без DB downgrade/restore поверх живых producer/consumer данных.
Operational activation не меняет Strategies, allowed symbols или trading credentials.
