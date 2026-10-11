# Advisory delivery в trading agent

## Contract и qualification

Producer передаёт `cryptowatcher.market_event.v2` в `POST /integrations/cryptowatcher/events`.
Consumer сохраняет приём v1; v2 поставлен в main67716b3282bf9fefe1a84077dfae7c6c99c6b1f0,
production CD38100792664 подтвердил source/image/readiness 2026-10-11 01:12UTC.
Это подтверждение binary, не активации binding или HTTPS ingress. Runtime endpoint,
vault/keyring readiness, exact binding, consumer max age и machine Access policy
пока UNSET до фактической qualification по существующим процедурам consumer.
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

## Owner control и infrastructure

`/agent status`, `/agent on`, `/agent off`, `/agent retry`, `/agent replay EVENT_ID`
доступны только владельцу в private chat через общий guard. Preference schema1 хранится
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
Permanent error блокирует target до owner on/retry, записи не теряются. Oversized payload
BLOCKED индивидуально. Failed send не останавливает history или другой consumer.

Live payload, истёкший либо устаревший по settings revision до отправки, сохраняется как
SUPERSEDED, получает новый explicit historical transport revision. Source event revision
не переписывается. Manual replay fresh текущего confirmation может восстановить live
context; expired/closed/replaced confirmation и replay с outcomes всегда historical.
Future source/episode timestamps дают WAIT_CLOCK; local clock не подделывает produced time.
Нельзя использовать synthetic LIVE production probes: bounded smoke выполняется на
существующем expired event/historical envelope и проверяет receipt/readback/cutoff.

## Delivery и recovery

Producer G4: schema1/NONE, без DDL или JSON re-import. Все agent logical namespaces входят
в existing all-row backup; startup сохраняет preference/bytes/attempts/pending jobs. CD
прежний, automatic, с expected merged source/image, online backup, health/persistence и
dual-market/4TF checks. Activation требует qualified consumer config/secret/HTTPS и bounded
joint runtime verification; binary deployment не выдаётся за включённую integration.
Forward-fix/compatible binary; без DB downgrade/restore поверх живых producer/consumer данных.
Operational activation не меняет Strategies, allowed symbols или trading credentials.
