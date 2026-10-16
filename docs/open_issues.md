# CryptoWatcher — delivery plan

Canonical spec: [product_scope.md](product_scope.md). Этот план содержит текущие AC/Goal/findings/Evidence; подробный audit report, raw logs и исследовательские fixtures не включаются в tracked repository. История предыдущих implementation checkpoints сохранена в Git, отмененная full-history conversion не является текущим обязательством.

## Аудиторская оценка G6 — 2026-10-16

Проверенный `origin/main`: **130225d2f641b4d9d8c616aa791ef6919afd9c31**. Требования: полностью прочитанный S01 modified2026-10-16T08:48:31.536Z (170 paragraphs/t.0, native revision в spec) и последующие owner решения S05. Git/main/protections/Environment, исходные CI/CD и bounded actual runtime records сверены16Oct2026. Audit PR только актуализирует spec/plan; не меняет runtime.

Готовность = **64/65 READY AC = 98.5%**. E09/AC-09-02 остается BLOCKED B02: код/автоматические контракты существуют, но actual authorized delivery не подтверждена без owner credential/binding. READY остальных критериев ограничен перечисленными fixtures/revisions; оценка не означает, что integration уже активна, 20GB steady-state доказаны или defaults прибыльны.

Предыдущий полный audit:2026-10-10, `8ace3638b9193444948d0beb88e0c49e9f2f068e`, **7/58=12.1%**. Разница+86.4п.п. объясняется поставкой G1–G5 и storage scope после того аудита; denominator увеличен на7 E13 из обновленных требований, прежние58 не исключены. Реализованные незамерженные изменения в готовность не включаются. F35 исправлен до текущей оценки; F36 совместимость receiver проверяется по отдельным primary records EVID-05.

| Эпик | READY/все AC | Готовность | Ограничение |
| --- | --- | --- | --- |
| E01 Назначение/владелец | 5/5 | 100.0% | Evidence ниже; READY кода не обещает долгосрочную capacity |
| E02 Universe/watchlist | 4/4 | 100.0% | Evidence ниже; READY кода не обещает долгосрочную capacity |
| E03 Realtime/TF | 5/5 | 100.0% | Evidence ниже; READY кода не обещает долгосрочную capacity |
| E04 Detectors/metrics | 5/5 | 100.0% | Evidence ниже; READY кода не обещает долгосрочную capacity |
| E05 Episodes/freshness | 4/4 | 100.0% | Evidence ниже; READY кода не обещает долгосрочную capacity |
| E06 History/outcomes | 7/7 | 100.0% | Evidence ниже; READY кода не обещает долгосрочную capacity |
| E07 Telegram/manual | 7/7 | 100.0% | Evidence ниже; READY кода не обещает долгосрочную capacity |
| E08 Owner settings | 3/3 | 100.0% | Evidence ниже; READY кода не обещает долгосрочную capacity |
| E09 Trading agent | 5/6 | 83.3% | B02: actual signed delivery не активна |
| E10 Persistence/recovery | 4/4 | 100.0% | Evidence ниже; READY кода не обещает долгосрочную capacity |
| E11 Digest/enrichment | 4/4 | 100.0% | Evidence ниже; READY кода не обещает долгосрочную capacity |
| E12 CI/CD/operations | 4/4 | 100.0% | Evidence ниже; READY кода не обещает долгосрочную capacity |
| E13 Bounded storage/reset | 7/7 | 100.0% | Evidence ниже; READY кода не обещает долгосрочную capacity |

## Current Goal — весь согласованный scope

Основание: поручение владельца11Oct расширить Goal на всю roadmap;16Oct — исправить новое накопление, отменить сохранение старой тестовой market history, разово начать историю заново; затем свериться с обновленным документом требований. Встроенная Goal остается active. Наличие полномочий на код/CI/CD не разрешает создавать от имени владельца отложенный credential.

**Текущий результат:** storage/reset этап выполнен по коду **7/7 AC E13**, доставлен и проверен. Вся Goal: **64/65 AC полностью подтверждены**, B02 остается. G6 completion review нашел F35/F36; F35 исправлен compatible hotfix39 без нового reset. Полный Goal DoD еще не достигнут из-за authorized integration smoke/configuration; не ожидать месяц статистики и не возобновлять старую conversion.

**Non-goals:** сокращение markets/TF/detectors/events/качества аналитики, auto calibration, LLM/trading/orders, PostgreSQL/Redis, удаление backups/experiments, увеличение RAM или обычного CW backup/CD deadline, повтор reset, длительное наблюдение в Actions. Отмененные conversion budgets не используются.

**Последовательность и status:**

| Goal/этап | Результат / status | Зависимости/оставшееся |
| --- | --- | --- |
| G1 | Owner access/config/migration/regression/CI/CD DONE | First migration не повторять; owner prefs/hash сохранены |
| G2A | Spot+Perpetual metadata/universe/WS/concurrent1/3/5/15m DONE | Gap/reconnect/unknown instrument diagnostic остаются штатным поведением |
| G2B | Все5 families/profiles/episodes/durable Telegram/history DONE | Данные и formulas не сокращались storage работой |
| G3 | Outcomes/comparator/cohorts/export/digest DONE по автоматическим fixtures | Малый N/неполнота после reset показываются явно; F35 устранен |
| G4 | Producer/consumer v2/retry/replay/idempotency реализованы; actual activation BLOCKED | B02, отдельный owner credential/TOTP/draft и binding/max-age900s; F36 compatible receiver hotfix |
| G5 | Optional enrichment/manual comparison tools DONE | Enrichment OFF допустим; thresholds меняются только владельцем |
| Storage E13 | Compact new accumulation/retention/metering/one-time reset DONE | 20GB target не доказан; F34 DOCUMENT/DEFER для отдельного репрезентативного observation |
| G6 | Полный audit актуального scope выполнен, docs PR40 merged; F02 contracts consolidation в delivery | Findings имеют dispositions; external activation не выдается за DONE |

**DoD остатка G4:** owner создает/сохраняет dedicated radar signing credential штатно; проверяется recovery/binding, consumer max-age900s и producer HTTPS config. Обязательны signed/tampering/idempotency/restart/expired replay checks, фактические producer/consumer revision/health и отсутствие LLM/order side effects. Telegram mute/outage не должен влиять на consumer. Конкретная activation выполняется по [процедуре](runbooks/agent-delivery.md) без изменения иных credentials/trading flags.

## Validation Plan

Canonical команды — [README](../README.md#checks); из root repository. Local Python3.12/Windows имеет ограничение native checks; required CI/image — Linux/Python3.11. Все test data disposable, secrets не нужны.

| AC/риск | Проверка / ожидаемый результат | Среда/этап | Gate/результат |
| --- | --- | --- | --- |
| Spec/commands/config | `python scripts/checks.py quality`: docs/lint/format/types | local до push; docs CI | REQUIRED PASS до последнего code push; audit docs проверить отдельно |
| Auth/config/migration/revision race | Existing pytest bot/config/storage/CLI; strangers/groups/invalid/future/corrupt/empty-list/CAS | local targeted, full PR CI | REQUIRED EVID-02 PASS |
| Markets/TF/formulas/episodes | Ingestion/universe/detectors/events replay/gap/reconnect/re-arm/coalescing/fault tests | local targeted/full CI/image | REQUIRED PASS; runtime EVID-04 |
| Analytics/retention/services | Known series, neutral/directed/gaps/late worker, all methods/frozen event revisions, comparator/no future leakage, controlled expiry30/35d/12mo, terminal queues/restart/dedup | targeted local + `python scripts/checks.py all` CI | REQUIRED EVID-02 PASS731 tests; real 30d expiry не ожидался |
| Export/Telegram/integration | NDJSON completion/pagination/filter/provenance, UI/outcome navigation, independent consumers, signed immutable retries/authority guards | isolated CI/image; live доступный контур | REQUIRED code PASS; actual signed consumer delivery B02 |
| Reset/private settings | Exact-target/allowlist/refusal/restore/equality/queues/cursors/generation/no historical alerts | isolated fixture + approved one-time CD37 | REQUIRED EVID-01 PASS; reset permit CONSUMED |
| Storage/capacity | Synthetic event+4outcomes+services/indexes; cheap counts/files/pages/sample model; bounded index snapshot; category/external inventory | isolated image + bounded readonly VPS | REQUIRED EVID-04 PASS; 20GB fit НЕ доказан, forecast warning виден |
| Ordinary CD/recovery | Required protection/latest CI/tree/source/image, full source integrity + disk snapshot/hash/readback, target lock, health/ingestion/private equality | production CW CD38/39 | REQUIRED EVID-03 PASS; entire job<=900s, CPU1/RAM4GiB/no swap |
| Representative load / longer trend | Separate owner observation; CPU/RAM/backlog/disk by representative workload, stop criteria до load | не запускался как длительный Actions monitoring | RECOMMENDED; не блокирует завершенный storage/reset scope |

## Текущее состояние AC

Статусы ниже относятся к проверенному main и EVID-01…06. AC-09-02 operational blocker не превращается в READY из-за mocked transport. Продуктовые и технические AC равноправны.

| AC / критерий | Статус | Evidence / проверка |
| --- | --- | --- |
| AC-01-01 Детерминированный Python runtime | READY | test_bot/test_config/test_logging; config.authorized; EVID-02/04 |
| AC-01-02 Нет торговых полномочий | READY | test_bot/test_config/test_logging; config.authorized; EVID-02/04 |
| AC-01-03 Bybit-only ingestion | READY | test_bot/test_config/test_logging; config.authorized; EVID-02/04 |
| AC-01-04 Доступ только владельцу | READY | test_bot/test_config/test_logging; config.authorized; EVID-02/04 |
| AC-01-05 Отдельный сервис без собственного Web UI | READY | test_bot/test_config/test_logging; config.authorized; EVID-02/04 |
| AC-02-01 Два широких universe | READY | test_universe/test_ingestion/test_bot; universe.py; EVID-04 |
| AC-02-02 Eligibility инструментов | READY | test_universe/test_ingestion/test_bot; universe.py; EVID-04 |
| AC-02-03 Настраиваемая liquidity | READY | test_universe/test_ingestion/test_bot; universe.py; EVID-04 |
| AC-02-04 Watchlist по рынкам | READY | test_universe/test_ingestion/test_bot; universe.py; EVID-04 |
| AC-03-01 WebSocket обоих рынков | READY | test_ingestion/test_sources/test_async_runtime; ingestion.py; EVID-04 |
| AC-03-02 Одновременные timeframes | READY | test_ingestion/test_sources/test_async_runtime; ingestion.py; EVID-04 |
| AC-03-03 Закрытые свечи | READY | test_ingestion/test_sources/test_async_runtime; ingestion.py; EVID-04 |
| AC-03-04 Gaps и recovery | READY | test_ingestion/test_sources/test_async_runtime; ingestion.py; EVID-04 |
| AC-03-05 Разделение cadence и TF | READY | test_ingestion/test_sources/test_async_runtime; ingestion.py; EVID-04 |
| AC-04-01 Composite formula | READY | test_detectors/test_events/test_enrichment_runtime; detectors.py; EVID-02/04 |
| AC-04-02 Набор detectors на двух рынках | READY | test_detectors/test_events/test_enrichment_runtime; detectors.py; EVID-02/04 |
| AC-04-03 Provenance и direction | READY | test_detectors/test_events/test_enrichment_runtime; detectors.py; EVID-02/04 |
| AC-04-04 Изоляция ошибок | READY | test_detectors/test_events/test_enrichment_runtime; detectors.py; EVID-02/04 |
| AC-04-05 Без auto calibration | READY | test_detectors/test_events/test_enrichment_runtime; detectors.py; EVID-02/04 |
| AC-05-01 Episode aggregation | READY | test_events/test_agent_delivery; EventEngine/TelegramDelivery; EVID-02/04 |
| AC-05-02 Meaningful notifications | READY | test_events/test_agent_delivery; EventEngine/TelegramDelivery; EVID-02/04 |
| AC-05-03 Завершение и re-arm | READY | test_events/test_agent_delivery; EventEngine/TelegramDelivery; EVID-02/04 |
| AC-05-04 Freshness и timestamps | READY | test_events/test_agent_delivery; EventEngine/TelegramDelivery; EVID-02/04 |
| AC-06-01 Полная история | READY | test_outcomes/test_analytics/test_export/test_bounded_storage; F35; EVID-02/04 |
| AC-06-02 Настраиваемые horizons | READY | test_outcomes/test_analytics/test_export/test_bounded_storage; F35; EVID-02/04 |
| AC-06-03 Returns и развитие | READY | test_outcomes/test_analytics/test_export/test_bounded_storage; F35; EVID-02/04 |
| AC-06-04 MFE/MAE | READY | test_outcomes/test_analytics/test_export/test_bounded_storage; F35; EVID-02/04 |
| AC-06-05 Статистика качества | READY | test_outcomes/test_analytics/test_export/test_bounded_storage; F35; EVID-02/04 |
| AC-06-06 Export и неизменность | READY | test_outcomes/test_analytics/test_export/test_bounded_storage; F35; EVID-02/04 |
| AC-06-07 Нет future leakage | READY | test_outcomes/test_analytics/test_export/test_bounded_storage; F35; EVID-02/04 |
| AC-07-01 Полный realtime alert | READY | test_bot/test_outcome_ui/test_events/test_digest; bot/render; EVID-01/04 |
| AC-07-02 Ссылка на правильный рынок | READY | test_bot/test_outcome_ui/test_events/test_digest; bot/render; EVID-01/04 |
| AC-07-03 Control deck | READY | test_bot/test_outcome_ui/test_events/test_digest; bot/render; EVID-01/04 |
| AC-07-04 Правдивый active status | READY | test_bot/test_outcome_ui/test_events/test_digest; bot/render; EVID-01/04 |
| AC-07-05 Управление доставкой | READY | test_bot/test_outcome_ui/test_events/test_digest; bot/render; EVID-01/04 |
| AC-07-06 Просмотр outcome | READY | test_bot/test_outcome_ui/test_events/test_digest; bot/render; EVID-01/04 |
| AC-07-07 Объяснение manual | READY | test_bot/test_outcome_ui/test_events/test_digest; bot/render; EVID-01/04 |
| AC-08-01 Owner configuration | READY | test_config/test_storage/test_bot/test_enrichment_runtime; EVID-02/04 |
| AC-08-02 Изменение без restart | READY | test_config/test_storage/test_bot/test_enrichment_runtime; EVID-02/04 |
| AC-08-03 Profiles и manual parameters | READY | test_config/test_storage/test_bot/test_enrichment_runtime; EVID-02/04 |
| AC-09-01 Structured contract | READY | test_agent_delivery; consumer context/v2 tests; EVID-05; B02 |
| AC-09-02 Авторизованная независимая доставка | BLOCKED B02 | Auth/retry/isolation code tests PASS; credential/binding/900s config и actual signed delivery PENDING |
| AC-09-03 Outbox и recovery | READY | test_agent_delivery; consumer context/v2 tests; EVID-05; B02 |
| AC-09-04 Idempotency revisions | READY | test_agent_delivery; consumer context/v2 tests; EVID-05; B02 |
| AC-09-05 Delivery diagnostics | READY | test_agent_delivery; consumer context/v2 tests; EVID-05; B02 |
| AC-09-06 Consumer independence и analytics | READY | test_agent_delivery; consumer context/v2 tests; EVID-05; B02 |
| AC-10-01 Разделение хранения | READY | test_storage/test_history_heads/test_history_codec/test_market_reset_delivery; EVID-01/03/04 |
| AC-10-02 Расширяемая история | READY | test_storage/test_history_heads/test_history_codec/test_market_reset_delivery; EVID-01/03/04 |
| AC-10-03 Безопасная миграция/recovery | READY | test_storage/test_history_heads/test_history_codec/test_market_reset_delivery; EVID-01/03/04 |
| AC-10-04 Restart continuity | READY | test_storage/test_history_heads/test_history_codec/test_market_reset_delivery; EVID-01/03/04 |
| AC-11-01 Digest aggregation | READY | test_digest/test_enrichment/test_enrichment_runtime; EVID-02 |
| AC-11-02 Digest schedule/toggles | READY | test_digest/test_enrichment/test_enrichment_runtime; EVID-02 |
| AC-11-03 Безопасный derivatives enrichment | READY | test_digest/test_enrichment/test_enrichment_runtime; EVID-02 |
| AC-11-04 Provenance enrichment | READY | test_digest/test_enrichment/test_enrichment_runtime; EVID-02 |
| AC-12-01 Docker/VPS runtime | READY | test_delivery/test_cd_bootstrap/test_async_runtime/test_logging; EVID-02/03/04/06 |
| AC-12-02 GitHub Actions delivery | READY | test_delivery/test_cd_bootstrap/test_async_runtime/test_logging; EVID-02/03/04/06 |
| AC-12-03 Domain boundaries и diagnostics | READY | test_delivery/test_cd_bootstrap/test_async_runtime/test_logging; EVID-02/03/04/06 |
| AC-12-04 Secrets и проверяемые изменения | READY | test_delivery/test_cd_bootstrap/test_async_runtime/test_logging; EVID-02/03/04/06 |
| AC-13-01 Общие данные без полных дублей | READY | test_bounded_storage/test_market_reset_delivery; EVID-01/02/03/04 |
| AC-13-02 Автоматическая bounded retention | READY | test_bounded_storage/test_market_reset_delivery; EVID-01/02/03/04 |
| AC-13-03 Качество detectors 12 месяцев | READY | test_bounded_storage/test_market_reset_delivery; EVID-01/02/03/04 |
| AC-13-04 Укрупненный context | READY | test_bounded_storage/test_market_reset_delivery; EVID-01/02/03/04 |
| AC-13-05 Конечные служебные зависимости | READY | test_bounded_storage/test_market_reset_delivery; EVID-01/02/03/04 |
| AC-13-06 Контроль 20 GB | READY | test_bounded_storage/test_market_reset_delivery; EVID-01/02/03/04 |
| AC-13-07 Разовый reset | READY | test_bounded_storage/test_market_reset_delivery; EVID-01/02/03/04 |

## Evidence и baseline

- **EVID-01/reset:** PR37 merged6ba915e3135db5cdd2fbf482fcbb6dbd92f02954, CI38889583594(719 tests/image), CD38890043475 PASS10:54:43–10:57:42UTC179s. Exact-target permit применен однократно. Private source/fresh/recovery/post-start SHA256 `1bcf2bbad2880bb2b03d6e3975cdb75574f76a0007943d9fb2e6b220079543df`, config revision3. EnvSHA `96cc5d7016e395ce63e3c263ce528186c2dcd5117746c5ba8fdb7828aede297a` сохранен. Удалены только прежние3 working DB/WAL/SHM files. Другие backups/experiments/проекты не очищались.
- **Новая history generation:** `78ed847e25554612bdfa34b29362787d`, начало2026-10-16T10:56:23.914Z. Candidate188416bytes; initial working11859800bytes. Seed/bootstrap только calculation window; prior details unavailable, insufficient comparator history явно отмечена. Старую history не восстанавливать из retention cursors/backfill как новые alerts.
- **EVID-02/CI:** PR38 main5376911, CI38892270186(727 tests); PR39 main130225d2, CI38897051479 **731 tests PASS** Linux3.11 + native artifacts + image checks. F35 isolated mixed-method/revision/comparator/split-expiry fixtures PASS; Windows targeted125 PASS/1native skip, затем34 bounded tests PASS с legacy-method regression. Image200events/800complete outcomes/оба markets/5families/4TF: analytics equality PASS, working4521984bytes (22609.92/event с sources/services/indexes), indexes1445888bytes, quality316400encodedbytes (1582/event). Это synthetic mix, sparse cohorts и один outcome-method, не production rate/годовой размер.
- **EVID-03/routine delivery:** PR38 CD38892625097 PASS production133s; PR39 CD38897380425 PASS12:01:35–12:04:36UTC181s. PR39 full source integrity DISK/schema2, full disk backup195670016bytes/6.021s/SHA `10526013be85ec0be99396df22ea9f58b380d586055374b49e98ec83256d93bc`. Нет delivery_policy/reset exception. Primary record `/opt/crypto_watcher/releases/130225d2f641b4d9d8c616aa791ef6919afd9c31-4391145a462bf16a/deployment.json`; adjacent bounded-storage-postchecks.json и bounded-storage-warm-postchecks.json. Running image `sha256:26b64aeea1712f7e88e2356bc522423221ea01269db756622ac5bbbf5d87d767`/source130225d2 healthy, private/env hashes прежние.
- **EVID-04/runtime:** bounded readback16Oct12:11:28UTC:1248 active streams, bootstrap/gap/recovery0, оба markets/4TF; poll1245/1248 DEGRADED по3 insufficient_history_or_zero_baseline (не missing feed). Sample2000outcome heads truncated:1952 COMPLETE по5/15/30/60min,48INCOMPLETE, не полный census. TelegramDELIVERED, analyticsOK, sampling/retention timestamps свежие. Working247078264bytes/index78925824; net3.247MB/min over4493s включает bootstrap/restarts/outcome mix. Annual/retention model462.15GB — EXTRAPOLATION, не steady-state доказательство и не гарантия верхнего предела; largest logical model quality/details/service indexes. Forecast>20GB явно предупреждает. Density compression/terminal turnover/retention и page-factor могут менять модель; scope/сроки не уменьшались.
- **External files отдельно:**12:05UTC metadata inventory COMPLETE: backups1.069GB, experiments14.471GB logical/11.372GB unique allocated, release artifacts1.653GB, reset-private/staging109713bytes. Файлы не удалялись.12:13:50UTC one-point app115MiB/82.75% CPU, host free44.215GB/NTPyes. Не является load/SLO доказательством. App UID10001/read-only root, hard CPU/RAM limits0; maintenance CPU1/RAM4GiB не увеличены.
- **EVID-05/consumer:** Consumer PR949 merged8c207edf6fc21e050bdca937e8f3bca49380a2ee; required PR CI38898629578 и push CI38899151685 PASS (включая v2 contract/PostgreSQL concurrency). Normal API CD38899376113 PASS12:16:32–12:22:28UTC356s, migration already_at_target_head/source fingerprint matched/DEPLOY_OK. Actual image sha256:d4e16629d6bf44efa9837eb70e0a5dda1b6a0f43f88e884e91d29a8ee089a8a2; readonly parser12:23:54UTC40 producer payloads PASS/40 execution-authority rejects, readyz200; CW/Postgres container IDs unchanged, ни одно event не отправлено. Ранее receiver v2 PR935/936/CI/CD11Oct PASS, current schema checked actual parser16Oct; producer v2 default settings с новыми TTLs потребовал F36. Consumer activation остается owner-deferred; actual ingest OFF/binding false/max-ageUNSET подтверждены16Oct12:13:50UTC. Zero Trust не добавлялся; прежний urllib403/CF1010 не означает Access requirement, HTTPx доходил до503 ingest_disabled.
- **EVID-06/dependencies/codebase:** 44 app Python modules,41 test modules,241 tracked files/112 vendor files на audit inventory; после добавления fixtures количество test cases731. One entrypoint wrapper, one deployable, native/build scripts, config/hash locks. Source/vendor pins yyjson0.12.0/SQLite3.53.4/Zstd1.5.7 проверяются build. Actual CPython3.11.17/SQLite3.46.1 inventory21 distributions/OSV query16Oct: runtime app deps matches0; pip24.0 шесть advisories (GHSA-4xh5-x5gv-qwph/58qw-9mgm-455v/6vgw-5pg2-w6jp/jp4c-xjxw-mgf9/qwm4-qh6w-59xr/wf93-45jw-7689), setuptools79.0.1 GHSA-h35f-9h28-mq5c. F26 reachability/disposition ниже, отсутствие matching advisory не доказывает отсутствие всех vulnerabilities.

## Полный реестр findings

Исходные проблемы F01…33 относятся к audit8ace3638 и сохранены для трассировки; Evidence ниже заменяет устаревшие «IN_BRANCH/PENDING». Confidence HIGH — код/fixtures/primary records; MEDIUM — неподтвержденные orphan/hardening/UX candidates. DEFER/DOCUMENT не означает исправление или новое требование. Detailed historical logs/snapshots остаются вне tracked документов.

| ID | Проблема / область / исходный объект | Влияние / priority | Действие / обоснование / зависимости | Evidence | Confidence / disposition |
| --- | --- | --- | --- | --- | --- |
| F01 DOC/SPEC | S01 полностью меняет прежний Spot MVP; отсутствовали AC и трассировка; product_scope.md / все эпики | P1 | CONSOLIDATE: Scope источника, без новых features. | S01/S05 полностью сверены; D22/E13,65 stable AC; PR19 и PR37/39. | HIGH / CLOSED |
| F02 DOC | Legacy schema1/storage/recovery формулировки противоречили current schema2; README/state_contract/architecture/agent-delivery | P2: риск выбора неверного backup/rollback path | CONSOLIDATE: current contract поднят перед legacy, logical v1 отделен от physical2, routine disk backup имеет один canonical runbook; useful historical anchors/tooling сохранены | G1/PR37 адаптация + текущая branch codex/current-storage-contracts-20261016/base6799395. Сверка Store/Sources/History/receiver dispatch, relative links и local docs PASS; required docs CI/merge PENDING | HIGH / FIX_IMPLEMENTED_DELIVERY_PENDING |
| F03 DOC/PROCEDURE | AGENTS.md и ci-cd-rules.md отсутствуют в repository; root; AC-12-02 | P1 | DOCUMENT: Процедуры существуют и используются. | Root AGENTS/ci-cd-rules/router tracked; strict ci-required и production Environment подтверждены16Oct. | HIGH / CLOSED |
| F04 DOC/COMMANDS | Quickstart обрывается, нет canonical install/validation/environment процедуры; README.md: конец незакрытого bash fence; requirements/Dockerfile | P2 | DOCUMENT: Format/links/fences проверены; команды не дублируются в плане. | README canonical Python3.11/hash-lock/CLI/Docker commands; validate_docs и image checks CI38897051479 PASS. | HIGH / CLOSED |
| F05 SECURITY | Access policy fail-open и неполный guard; код 261–277, 1408–1415; AC-01-04 | P1 | FIX: Owner private233382575; доступ не открывается при missing config. | config.authorized + единый Controller.handle guard; stranger/group/empty/invalid/bot identity fixtures, full CI731 PASS. | HIGH / CLOSED |
| F06 DATA LOSS | load_state перезаписывает corrupt/unknown-version state; код 421–450; AC-10-03 | P1 | FIX: Не заменять corrupt state пустой DB. | Store.initialize/migrate/restore отказывают unknown/corrupt state; tests_storage/market_reset_delivery и реальный reset private equality PASS. | HIGH / CLOSED |
| F07 CONFIG/STARTUP | Невалидные persisted settings могут прервать весь startup; код 344–399; AC-08-02/10-03 | P1 | FIX: Startup не подменяет сохраненные настройки defaults. | OwnerSettings валидирует finite/ranges/lookbacks/horizons/retention; CAS update сохраняет прошлую config при отказе; config/bot tests PASS. | HIGH / CLOSED |
| F08 DATA INTEGRITY | Нет проверки непрерывности и полноты candles; код 918–961; AC-03-04 | P1 | FIX: Отсутствие полных данных дает INCOMPLETE/diagnostic, не zero return. | Sources/BybitRadar/AnalyticsRecovery: continuity, duplicate/out-of-order/gaps/backfill tests; runtime gap/recovery0 в bounded readback. | HIGH / CLOSED |
| F09 FRESHNESS | Нет event timestamps/TTL; closed определяется только local clock; код 919–945; AC-05-04 | P1 | IMPLEMENT: Freshness независима от worker/outcome времени. | Bybit response clock/WS confirm, type/TF TTL и replay/future-clock tests PASS; receipt/retry не освежает market time. | HIGH / CLOSED |
| F10 UNIVERSE | Fixed100/одна category, eligibility без metadata; код 899–913, 987–1009; AC-02-01..03 | P1 | IMPLEMENT: Нет fixed Top100; watchlist typed. | Metadata Spot без pagination/linear cursor, type/status/USDT/quarantine/liquidity; test_universe/ingestion, runtime оба рынка1248 streams. | HIGH / CLOSED |
| F11 INGESTION | Нет WS и одновременно работающих TF; код 963–1027, requirements; AC-03-01/02 | P1 | IMPLEMENT: REST recovery имеет shared budget, historical data не дает live flood. | Concurrent1/3/5/15m, ACK/read loop separation PR35; CI ingestion/reconnect tests и normal CD39 PASS. | HIGH / CLOSED |
| F12 DETECTORS | Реализован только legacy composite; код 918–961; AC-04-02/03 | P1 | IMPLEMENT: Composite развивается как одно семейство. | Five pure versioned detector families/effective parameters; fixtures boundaries/negative/neutral/zero/gaps и live event sample обоих markets/TF. | HIGH / CLOSED |
| F13 EPISODES | Cooldown без event identity не заменяет episodes; код 2090–2105; AC-05-01..03 | P1 | IMPLEMENT: Episode aggregation не удаляет source events. | EventEngine episode/re-arm/maxlife/strength/coalescing/restart fixtures, durable source revisions. | HIGH / CLOSED |
| F14 DELIVERY | Failed Telegram send расходует cooldown без retry; код 2101–2120; AC-07-01/05 | P1 | FIX: Failed send не расходует successful notification state. | TelegramDelivery durable retries/retry_after/ambiguous ACK/permanent target и success-baseline fixtures; runtime DELIVERED. | HIGH / CLOSED |
| F15 ANALYTICS | Нет event history, outcome worker, analytics/export; state 331–450, весь tracked inventory; E06 | P1 | IMPLEMENT: История ограничена D22; после detail expiry явно только summaries. | G3 outcomes/cohorts/export/digest + PR37 retention + PR39 F35; original reference/method/revisions preserved, controlled-time equality PASS. | HIGH / CLOSED |
| F16 OWNER SETTINGS | Market domain зависит от chat; profiles/manual settings не реализованы; код 302–399, 1084–1133, 1759–1795; E08/AC-02-04 | P1 | IMPLEMENT: CAS/no-op/invalid settings tests и hash readback PASS. | Owner revision3 private preservation; profiles/manual/typed watchlist/enrichment/horizons/retention отдельно от delivery preferences/secrets. | HIGH / CLOSED |
| F17 INTEGRATION | Нет advisory API/автоматической передачи trading agent; все entrypoints/dependencies; E09 | P1 | IMPLEMENT: Code/contract отдельно от реальной signed delivery; не создавать credential за владельца. | Producer v2/HMAC/immutable outbox/retry/replay и consumer receiver существуют; F36 выявлен actual parser и исправляется отдельным compatible hotfix. Ingest/binding OFF/UNSET по owner-deferred credential. | HIGH / BLOCKED B02 |
| F18 DIGEST | Нет periodic digest и независимых delivery toggles; код 2171 и handlers; AC-11-01/02/07-05 | P2 | IMPLEMENT: Default digest OFF не является дефектом. | Digest fixed-period identity/latest-only restart/frozen bytes/retry/preferences race; tests_digest PASS, alerts независимы. | HIGH / CLOSED |
| F19 ENRICHMENT | Нет derivatives context для Spot; весь fetch layer; AC-11-03/04 | P2 | IMPLEMENT: Optional feature может быть OFF; new market source не добавлен. | G5 exact asset mapping/OI/funding/basis/source timestamps/cadence/fault isolation; CI/CD27 PASS, current suite includes enrichment regressions. | HIGH / CLOSED |
| F20 OBSERVABILITY | Symbol errors/timeouts скрываются как отсутствующие данные; код 963–1023; AC-04-04/12-03 | P1 | FIX: 3 insufficient-history/zero-baseline streams показываются DEGRADED отдельно от missing feed. | Bounded readonly CLI/Telegram diagnostics, independent stream reporter, per-family/source/consumer errors, ingestion age and PENDING-on-budget; CI/actual readback PASS. | HIGH / CLOSED |
| F21 CONCURRENCY | Settings могут смениться во время fetch, old result доставляется как active; код 2068–2112, status_cmd; AC-07-04/08-02 | P1 | FIX: Нет silent применения старого результата к новой config. | Settings revision CAS/frozen notification and worker snapshots, old-result discard/send-time race fixtures PASS. | HIGH / CLOSED |
| F22 ARCHITECTURE/PERFORMANCE | Monolith и per-chat REST fanout мешают расширению; telegram_crypto_watcher.py 2177 строк; AC-12-03 | P2 | REFACTOR: Основной monolith/fanout устранен; дальнейший measured tuning по representative workload без silent cuts. Рост/индексы F34 контролируются отдельно. | 44 Python modules, shared dataset, bounded SQL/worker batches, no network under DB locks. Реальный scope1248 streams; one-point115MiB/82.75% CPU16Oct12:13:50UTC не является steady-state capacity/load доказательством. | HIGH / DOCUMENT / DEFER |
| F23 LEGACY | CMC/source/core/reset действия остаются достижимыми вне v1; код 536–874, 1583–1643, 1798–1881, 1967–2041, 2163–2168; AC-01-03 | P2 | DEPRECATE: Основание удаления UX — D01-D04, не отсутствие imports. | Активных CMC/reset network handlers нет; old callbacks объясняют изменение. Startup wrapper и legacy import compatibility сохранены. | HIGH / CLOSED |
| F24 ORPHAN CANDIDATES | Невызванные legacy helpers/caches; внешние consumers неизвестны; код _fetch_async/_prepare_core_context/tracked_desc/price_unit/_mode_label/_rank_prefix/_sub_metrics/_directional_marker | P3 | DEFER: Новое удаление без необходимости не выполнено. Consolidate duplicate helper при затрагивании diagnostics; source-pinned tooling сохраняется. | Original private helpers отсутствуют в текущем wrapper/module inventory после G1. Historical converter/native helpers используются CLI/tests/retained archives, не dead. Controller.diagnostics старый дублирующий helper не вызывается production CLI; внешние users UNSET. | MEDIUM / DOCUMENT / DEFER |
| F25 TEST GAP | Нет tracked воспроизводимой regression suite; EV01 tracked inventory; E01/E03/E08/E10/E12 | P1 | IMPLEMENT: Не заявлять line coverage как выполнение AC; consumer actual signed smoke отдельно B02. | 41 test modules; CI38897051479:731 tests PASS Linux3.11 плюс native/image measurement; regressions auth/migration/retention/restart/contracts. | HIGH / CLOSED |
| F26 DEPENDENCIES | Нет lock/hash constraints; разрешены уязвимые requests releases; requirements.txt:2–3; Dockerfile:1,9 | P2 | FIX: Pip paths относятся установке artifacts, setuptools case macOS sdist; текущий Linux app не устанавливает packages по market input. Плановый build-tool upgrade отдельно; matched advisory не равен подтвержденной runtime exploitability. | Hash locks/base/native pins и actual21-distribution inventory проверены16Oct. OSV: app dependency matches0; pip24.0 шесть advisories, setuptools79.0.1 GHSA-h35f-9h28-mq5c. | HIGH / DOCUMENT / DEFER |
| F27 CI/CD | CI/protections/build foundation и new-write CD подтверждены; ordinary full current-history gate ещё не qualified; AC-12-02 | P1 | FIX: Полные обычные gates восстановлены <=900s/CPU1/RAM4GiB; прежние failed records не переименованы. | PR37 reset CD38890043475 PASS179s; обычные PR38/39 CD38892625097/38897380425 PASS133/181s. Source integrity DISK/full backup/hash/health/source; без reset exception. | HIGH / CLOSED |
| F28 OPS EVIDENCE | Target/config/source inventory и first migration/restore/runtime подтверждены; automatic CD ещё не завершён; AC-12-01/02/10-04 | P1 | DOCUMENT: Не повторять deploy/migration ради статуса. | Contabo target/unit/path/config ownership/hostkey/SSH/CD primary records известны; normal current source/image/private hash checked16Oct. | HIGH / CLOSED |
| F29 CONFIG/UX | Env category/runtime knobs могут противоречить Spot UI и raw input; BYBIT_CATEGORY, settings_text/status_text/alert_keyboard, .env.example | P2 | CONSOLIDATE: Legacy selected market не ограничивает shared ingestion. | Typed spot/linear identity, Bybit URLs, current universe/TF/effective settings и env precedence fixtures; source config revision3 readback. | HIGH / CLOSED |
| F30 BUILD HYGIENE | Docker context не исключает data/ и .git;  .dockerignore, Dockerfile | P3 | FIX: Data не передаются builder через широкий COPY. | Docker whitelist исключает .git/.env/data/cache; native source-only включен явно. CI build/image checks PASS. | MEDIUM / CLOSED |
| F31 HARDENING | Docker runtime использует root default; resource/health policy UNSET; Dockerfile/Compose; AC-12-01 | P3 | DEFER: Новых runtime hard limits без измеренного основания не вводить; отсутствие limit само по себе не defect. | Actual app UID10001/read-only root/cap-drop/no-new-privileges/health PASS. CPU/RAM hard app limits0; single-point115MiB/82.75% CPU. Maintenance только CPU1/4GiB. | MEDIUM / DOCUMENT / DEFER |
| F32 UX CANDIDATE | Legacy polish/preset strategy без конкретного нового дефекта; прежний open_issues #6/#7; E07/E08 | P3 | DEFER: Неподтвержденный visual polish/подбор прибыльных thresholds вне текущих обязательств; по owner feedback. | Five-family terms/explanations/profiles/history→outcomes/comparison views и no-auto-calibration fixtures PASS. | MEDIUM / DOCUMENT / DEFER |
| F33 G1 REGRESSION | `/sample_alert` потерял baseline Bybit/mute/coin controls и не учитывает active TF; cryptowatcher/bot.py/render.py; G1 preservation / AC-07-03 | P2: неверный sample и потеря manual controls | FIX: Ограничение: owner assertion не заменяет все browser scenarios. | PR22/CD38087285142 + owner screenshot/«вроде работает»; current sample/menu/no-op tests PASS. | HIGH / CLOSED |
| F34 HISTORY CAPACITY | Full heads/shared overlapping windows и indefinite service/source references увеличивают БД; AC-06/10/12 и13-01…07 | P1: disk/write amplification,20GB target не доказан | REFACTOR: Исправления/reset DONE. После отдельного наблюдения сравнить category mix/dense summary compression/index overhead; варианты schema/агрегатов требуют сохранения качества или согласованного изменения. Старую conversion не возобновлять. | Compact shared candles/settings/context, bounded services/30d details/35d candles/12mo quality, reset37 и normal CD38/39. Working247078264bytes/index78925824,3.247MB/min over4493s. Model462.15GB предупреждает;20GB fit не доказан. | HIGH / DOCUMENT / DEFER |
| F35 ANALYTICS COMPATIBILITY | AC-06-05/06-06/10-02/13-03; quality_summary.archive_event выбирал global method/latest event revision | P1 | FIX: все methods/comparator versions, frozen revisions и metadata-only expiry reader | Repros before expiry методы1/2→после только1 и strength1→101. PR39/main130225d2, CI38897051479 731 PASS/image equality; normal CD38897380425 PASS. Source/candidate quality0 еще не достиг30d; переписывать summaries не требовалось | HIGH / CLOSED |
| F36 CONSUMER COMPATIBILITY | AC-09-01/09-02; actual consumer PublicSettings отвергает новые retention metadata | P1 | FIX: backward-compatible optional positive ints, полный snapshot/hash без fabricated defaults; unknown/secret fields остаются forbidden | Actual cacdfda parser40 fixtures FAIL только detail_retention_days/candle_retention_days. Consumer PR949 merged8c207edf; CI38898629578/38899151685 и CD38899376113 PASS. Actual parser40/40 payloads и40/40 authority rejects, snapshot/hash preservation/legacy/invalid metadata fixtures PASS; no events sent | HIGH / CLOSED |

## Документы, legacy и artifacts

Canonical README — назначение/commands/navigation; spec — requirements/decisions/AC; этот plan — statuses/findings/Goal/Evidence; state_contract — ownership/schema1 compatibility и schema2; bounded-storage-reset — current retention/reset/routine disk backup. Полезные historical converter/native CLI и source licenses сохраняются: их наличие не означает текущую обязанность конвертировать discarded history.

F02 consolidation выполнена в текущей branch: canonical paths и полезные historical anchors сохранены; schema2 contract перенесен в начало state_contract, architecture и agent runbook описывают actual normalized/disk flow. Destination — existing state_contract schema2 + bounded runbook; links README/architecture/agent-delivery проверены. Новый конкурирующий документ не создан. Исторические rules D12/raw90/indefinite details явно superseded D22. F24 private duplicate helpers — DOCUMENT/DEFER, удаления только из-за отсутствия imports не выполнялись. Local ignored `.cache-g1`, env/caches/pycache и прежние branches/worktrees учитываются отдельно от tracked source; статус ignored не означает ненужность.

## Blockers и следующий шаг

| ID | Состояние | Необходимое действие |
| --- | --- | --- |
| B01 | RESOLVED: first migration/reset и ordinary current source/backup/CD подтверждены | Не повторять reset/ручную передачу образа |
| B02 | BLOCKED owner-deferred: dedicated radar credential/binding отсутствует; ingestOFF/max-ageUNSET | Owner TOTP/draft по штатной форме; после recovery/binding checks применить согласованные900s и выполнить signed smoke/replay без торговых side effects |
| B03 | RESOLVED: numeric policies/owner mapping/retention определены D09-D22 | Сохранять актуальные правила и settings equality |
| B04 | RESOLVED: GitHub admin, strict ci-required/production Environment/DEPLOY_ENABLEDtrue, restricted SSH | Перед каждым merge reread head/protections; не обходить gates |

F34 budget/capacity — ограничение Evidence и отдельный follow-up, не ожидание статистики агентом. Контроль работает автоматически каждую минуту через counters/files/pages; exact later index walk — отдельная bounded snapshot. При устойчивом forecast>20GB сначала проверить largest category/dense compression/index/service cost; затем предложить решения с сохранением аналитики. Сокращать сроки/события/метрики скрытно запрещено. Remaining included Actions minutes UNSET; speculative monitoring/reruns не запускались.

## Audit quality review

Охват: весь updated S01/S05,65AC и обратная трассировка active features; entrypoint/routers/config/lock/source schema/queues/delivery/analytics/WS/REST/TG/agent boundaries; tracked и vendor/native/generation/scripts/workflows; separate ignored/runtime backups/experiments; primary GitHub gates/CI/CD/actual image/private hashes. Required code gates PASS; operating integration B02 не выдается за PASS. Обнаруженные F35 и F36 показывают предел прежних single-method/producer-only fixtures; исправления проверяются на смешанных версиях и фактическом consumer parser.

False positives проверены: optional enrichment/digest OFF — preference; три insufficient-history/zero-baseline streams не названы outage; совпадающие symbols разделены market identity; runtime hard limits не объявлены обязательными без requirement; old converter/vendor не назван мусором; package advisory match не равен exploit; 20GB не объявлены доказанными initial small DB; сохранение точных numeric summary metrics не названо хранением полной старой event history.

Confidence HIGH по auth/config/atomic persistence/formulas/retention/replay/CI/source identity на проверенных fixtures. MEDIUM по long-term capacity/оптимальной частоте/optional UX/external private helper usage. Limitations/possible false negatives: steady-state annual/dense cohort layout и новый реальный30d expiry еще не наблюдались; controlled-clock tests их моделируют. Полный stream load/SLO, третьесторонние native/dependency undisclosed bugs и все Telegram/browser/client paths не доказаны этим аудитом. Этот audit не заменяет отдельный exhaustive security scan. Real integration signed receipt/consumer cutoff smoke остается B02.

## Checkpoint

2026-10-16: audit PR40 merged679939593b82e8a07139bf60d92bc00fed57beb0,
required docs CI38900513591 PASS, postmerge38900699749 selectPASS/build+productionSKIPPED
по docs-only scope. Local main синхронизирован; прежний audit baseline64/65 на
130225d2 не пересчитывается. Runtime39 и consumer949 доставлены/проверены, B02 остается.

Current F02 branch `codex/current-storage-contracts-20261016`, base6799395;
пять затронутых docs, без code/config/policy changes. Local docs/links/manual
source alignment/self-review PASS; required docs CI/merge PENDING. Runtime CD N/A.
Parent Goal active. Собственные текущие PR branches/worktrees очищаются после
merge/proof; старые user/unknown branches и private evidence сохраняются.

## F02 — консолидация текущих storage/recovery contracts

Разрешенный scope всей Goal включает F02. Completion review после audit40 выявил
конкретные опасные противоречия: architecture/state contract называют physical
schema1 текущей, agent runbook обещает schema1/NONE, architecture описывает RAM/VFS
как обычный backup schema2. Код receiver уже выбирает disk source/backup для schema2.
Branch `codex/current-storage-contracts-20261016`, base679939593b82e8a07139bf60d92bc00fed57beb0;
clean main/GitHub/protections/open PR проверены до изменений.

Результат/DoD: текущие physical schema2, logical v1 и historical schema1 явно
разделены; единственная действующая процедура routine disk backup/reset остается
в bounded-storage-reset, useful legacy qualification/commands/anchors сохраняются.
Новые settings snapshot TTL fields объяснены как provenance, не consumer freshness.
Non-goals: runtime/schema/CI/CD changes, новый reset/conversion, cleanup данных,
credential activation и увеличение budgets. Validation REQUIRED: сверка с
Store/History/Sources/receiver dispatch, относительные links/anchors и canonical
`python scripts/checks.py docs`; self-review/required docs CI/merge. Runtime deploy
N/A: только документация, без изменения delivery policy или команд исполнения.
Документы исправлены; сверка с actual bounded/schema dispatch и relative links PASS, self-review PASS. Required docs CI/merge PENDING; аудиторский snapshot64/65 на130225d2 не пересчитывается. После merge останется owner-deferred B02; новую storage migration/reset не запускать.
