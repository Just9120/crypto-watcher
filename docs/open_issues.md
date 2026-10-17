# CryptoWatcher — delivery plan

Canonical spec: [product_scope.md](product_scope.md). Этот план содержит текущие AC/Goal/findings/Evidence; подробный audit report, raw logs и исследовательские fixtures не включаются в tracked repository. История предыдущих implementation checkpoints сохранена в Git, отмененная full-history conversion не является текущим обязательством.

## Историческая аудиторская оценка G6 — 2026-10-16, до S06

Проверенный `origin/main`: **130225d2f641b4d9d8c616aa791ef6919afd9c31**. Требования: полностью прочитанный S01 modified2026-10-16T08:48:31.536Z (170 paragraphs/t.0, native revision в spec) и последующие owner решения S05. Git/main/protections/Environment, исходные CI/CD и bounded actual runtime records сверены16Oct2026. Audit PR только актуализирует spec/plan; не меняет runtime.

На момент audit, до разделения проектов S06/D23, готовность = **64/65 READY AC = 98.5%**. Тогда E09/AC-09-02 был BLOCKED B02: код/автоматические контракты существуют, но actual authorized delivery не подтверждена без owner credential/binding. READY остальных критериев ограничен перечисленными fixtures/revisions; оценка не означает, что integration уже активна, 20GB steady-state доказаны или defaults прибыльны.

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
| E09 Trading agent | 5/6 | 83.3% | Исторический cross-project scope до S06; actual signed delivery не активна |
| E10 Persistence/recovery | 4/4 | 100.0% | Evidence ниже; READY кода не обещает долгосрочную capacity |
| E11 Digest/enrichment | 4/4 | 100.0% | Evidence ниже; READY кода не обещает долгосрочную capacity |
| E12 CI/CD/operations | 4/4 | 100.0% | Evidence ниже; READY кода не обещает долгосрочную capacity |
| E13 Bounded storage/reset | 7/7 | 100.0% | Evidence ниже; READY кода не обещает долгосрочную capacity |

## Current Goal — MAINTENANCE: завершение backfill/repack и старт24h

Основание: поручение владельца16Oct2026 устранить узкие места backfill/repack,
сохранить history/analytics/alerts, RAM и ordinary backup/CD<=900s. Результат:
независимое bounded обслуживание очередей, конечный durable repack и фактический
stable baseline конечного24h observer. Сам24h итог — последующее наблюдение.

Проверенный main/source `1b4e0cd4e903de8b2b0084d75150784b507ebc48`, PR55:
CI38994262366/CD38994598740 PASS. Production278s, full disk backup54.154s;
source/image/health/persistence/market PASS. Private/env/config3/generation и64
immutable public samples совпали с исходным состоянием Goal. Оба рынка, четыре TF,
1120active streams/allACK, analytics и Telegram delivery подтверждены. Own branches
PR48–55 удалены после merge/delivery/tree equality; root worktree/чужие сохранены.

Repack завершен17Oct03:08:19.910UTC: records1674197, headers1787604,
next3441866>captured upper3441865, records/decisions/legacy expiry/quality COMPLETE.
Новые writes не расширили interval; история не сбрасывалась. Старый referenced
backlog достиг0; обычные новые задания на1–4 закрытые свечи продолжают появляться
и обрабатываться. Readonly03:19:47UTC: backfill0/live queues0/empty blockers,
analytics/recovery/repack OK, свежая Telegram delivery. Working2288357560bytes
включает DB2066104320 (с индексами), WAL222220472 и SHM32768; это не суточный темп.

Necessary hotfix `codex/observer-settling-20261017`, base указанного main.
Наблюдатель в03:15/20/25/33UTC попадал в краткую текущую analytics queue и сбрасывал
stable count. Отдельные bounded проверки фиксировали0, ошибочных/старых jobs нет.
Однократный restart только observer со всем сохраненным state дал одну чистую
проверку, но не устранил phase sensitivity. Scope: только WAITING_STABLE и только
единственный blocker ANALYTICS_RECOVERY_PENDING перепроверять максимум3 раза с
10s паузой. Сохранять timestamps/blockers попыток в sample; не считать их чистыми.
Две фактически чистые проверки по-прежнему разделены >=300s; deep boundary,
исходный12h wait limit и все остальные blockers сохраняются. OBSERVING samples
не retry: interruptions не скрываются. RAM/CPU caps, history, расчеты и pipeline
не меняются. Обычная CI/CD поставка и final-image observer обязательны.

| Задача / DoD | Состояние |
| --- | --- |
| M01: independent backfill/fairness/retry/restart и source integrity | PASS: PR48–55/CI, historical backlog0, новые current jobs обрабатываются |
| M02: finite repack/equality/checkpoint/rollback/yield/snapshot | PASS: actual COMPLETE03:08:19UTC, все фазы/finite physical boundary |
| M03: required CI/merge/CD<=900s, прежняя RAM, private/history/alerts | PR55 PASS278s/backup54.154s; necessary observer hotfix CI/CD PENDING |
| M04: actual completion и baseline final finite observer | IN_PROGRESS: transient pending queue мешает двум чистым samples; baseline отсутствует |

Validation Plan, root repository:

| Риск/результат | Команда/tool и среда | Этап / обязательность |
| --- | --- | --- |
| Conventions/types/docs | `python scripts/checks.py quality`, локальный Windows env | Local REQUIRED |
| Pending clears/persists, other blockers, observed interruptions и two-clean-sample gate | `python -m pytest tests/test_capacity.py`, controlled clock/isolated DB | Local REQUIRED |
| Retry/restart/cancel, finite boundaries/source integrity | Existing maintenance/recovery/storage tests, текущая полная CI suite | CI REQUIRED; локально по затронутому риску |
| Полная suite/native/image | Existing `ci-required`, Python3.11/Linux | CI REQUIRED |
| Сохранность и поставка | Normal CD/full backup<=900s; source/image/private/env/config/generation/public samples, health/market/Telegram | Production REQUIRED |
| Запуск24h | Final-image finite observer, source/config/generation/StartedAt, actual completion и две stable samples>=300s | Post-delivery REQUIRED;24h результат последующий |

Local20capacity scenarios и canonical quality PASS; old single-probe behavior
воспроизводит regression FAIL, bounded settling проходит. Self-review PASS:
только WAITING_STABLE/sole queue retry, persist attempt timestamps, other blockers
и OBSERVING interruptions не скрываются;300s separation/deep/12h limits прежние.
Required Linux CI/image и обычный CD PENDING. Нет новой schema, конвертации/копии
рабочей DB, новых codecs или публичного контракта.
Ожидание в observer выполняется вне приложения; исходные read-only probe caps,
zero-queue gate и запрет baseline при других blockers не ослабляются.

Observer55 `cryptowatcher-capacity-20261017T022950Z-d17381ba.service`, private path
`/opt/crypto_watcher/observations/capacity-20261017T022950Z-d17381ba`, PID1546498
после одного phase restart; no failures/WAITING_STABLE, baseline/final/result нет.
При смене source сохранить records и проверить точный unit/path/наличие baseline
до replacement. Не объединять epochs/revisions. Existing one-shot17Oct06:40MSK
пока привязана к55; после поставки обновить тот же объект. После actual baseline
финальная проверка через24h+20min, без ожидания агентом суток.

Primary history: PR48/4c3a362 CI38966791666 PASS/CD38967393380 POST_CHECK_FAILED;
PR49/7015ff6 CI38969740225/CD38970152846 PASS297s;
PR50/15daa13 CI38971799676 PASS/CD38972288968 POST_CHECK_FAILED;
PR51/444f9c2 CI38974437399/CD38974896223 PASS355s/backup50.858s;
PR52/b841f81 CI38982180165/CD38982604072 PASS223s/backup50.42s;
PR53/4c5684e CI38986875524/CD38987333983 PASS221s/backup51.639s;
PR54/5e6c6f8 CI38989052236/CD38989302666 PASS212s/backup52.108s.
Failures не переписаны как PASS. Primary records восстанавливают delivery, даже
если Markdown checkpoint до merge. Полные исследовательские материалы вне repo.

Non-goals: reset/отмененная test-history conversion/cleanup backups/experiments,
изменение codecs/retention/coverage/точности/REST/RAM/gates. Проценты проекта не
пересчитываются;20GB UNPROVEN. Далее targeted validation/self-review, necessary
hotfix PR, normal CI/CD и actual stable baseline. Goal ACTIVE.

## Previous Goal — CAPACITY: реальные источники и заполненная база

Основание: поручение владельца16Oct2026 после GROW handoff: измерить источники,
кардинальность cohorts/revisions/comparators/services; исправлять только подтвержденную
избыточность; доказать controlled-time retention и организовать автоматическое24h
наблюдение после стабилизации без ожидания агентом. Один основной PR.
Base `c922700c6db7a9cc6cb543f3170d0d83917b547b`, branch
`codex/capacity-evidence-20261016`; Git/GitHub/protections/SSH проверены.
Основной PR45 merged `ef5652d99c2fb898fa12ccfd7d5b06c81d211a8f`;
CI38955487956 PASS766/image, CD38956621801 production PASS330s, full disk backup
1,234,268,160bytes/copy+integrity43.378s/hash. После поставки hotfix branch
`codex/capacity-stability-hotfix-20261016`, base `ef5652d99c2fb898fa12ccfd7d5b06c81d211a8f`:
условие старта observer должно учитывать отдельную analytics recovery queue.
Hotfix46 merged `dd8bf441e14b07fb84587c855d7bb5f1325b0ff8`, CI38958054568 PASS;
CD38958643381 POST_CHECK_FAILED после switch (source/image healthy, full gates
до switch PASS). Observer не запускался. Compatible startup hotfix branch
`codex/startup-bucket-fairness-20261016`, base `dd8bf441e14b07fb84587c855d7bb5f1325b0ff8`:
seed каждого market/TF до ordinary gap/bootstrap/history priorities, не пропуск gate.

| Задача / критерий завершения | Статус |
| --- | --- |
| CAP-01: ledger физических bytes/индексов/WAL отдельно от payloads; counts/TTL/formulas, cohorts/revisions/comparators/jobs и limits | CODE READY; exact runtime ledger и full prospective cohort study PASS |
| CAP-02: подтвержденные повторы исправлены с equality/restart/retry/causal evidence; новые codec/batching только после оценки эффекта | CODE READY; decision pointer / ownerless recovery / terminal re-arm / sliced repack tests PASS |
| CAP-03: репрезентативный controlled-clock полный цикл30/35d/12mo, GC/dependencies/аналитика/plateau | CODE READY;62week fixture и20-comparator/multiple-method boundaries PASS |
| CAP-04: auto24h после ingestion/bootstrap/repack stability, размер/прирост/data/index/WAL/cleanup; finite sampling, без Actions ожидания | CODE READY; unit/start после applicable CD, actual24h PENDING |
| CAP-05: validation/self-review/CI/merge/CD<=900s, отчет actual/model/range и оставшиеся причины >20GB | IN_PROGRESS; PR45 PASS,46 post-check FAIL; startup fairness CI/runtime PENDING |

DoD текущей реализации: measurable ledger и подтвержденные fixes, meaningful tests,
ordinary gates и post-checks PASS, автоматический конечный observation подготовлен/
запущен. Финальный24h результат имеет отдельный PENDING до фактического окончания;
capacity PASS не следует из tests/CD. Наблюдение не заменять короткой экстраполяцией.
Область — E06/E10/E12/E13 и findings;65AC и исторические проценты не пересчитываются.
Non-goals: новый reset, старая test-history conversion, удаление backups/experiments,
новые sources, снижение точности/markets/detectors/TF/events/retention, consumer setup,
увеличение RAM/ordinary backup/CD900s или ослабление gates.

Validation: local canonical quality и targeted existing storage/analytics/retention
fixtures; новые tests только для подтвержденных gaps/рисков; required full Linux CI/
image. Runtime readonly budgets явно ограничены;24h sampler только CW metadata,
bounded payload samples/counters, physical index walk только на границах. Во всех
этапах сохраняются settings/generation, retry/dedup и signed immutable delivery.

## Previous Goal — GROW: хранение без потери аналитики

Основание: явное поручение владельца16Oct2026 реализовать предложенные исправления
роста базы и поставить через CD. Base `447a08a703f7c864e419be504d8c340da1863e1c`,
основной PR43 merged `e9cc24b94ed35d206033973ef82f31b1514d11e0`.
Hotfix branch `codex/local-window-recovery-20261016`, base `e9cc24b`;
clean Git/GitHub/protections/worktree повторно проверены.
Предыдущий handoff PR42 merged447a08a, CI38919152279 и postmerge38919292133 PASS;
consumer activation остается вне проекта по D23.

Результат: recovery не накапливает уже заполненные запросы; подробные records и
годовые numeric facts используют обратимое компактное представление; существующие
rows сохраняются и постепенно уплотняются с durable progress; forecast различает
оборот очередей, retained data, измерения и модели. Один основной PR.

| Задача / closure criterion | Статус |
| --- | --- |
| GROW-01 / F37: local completeness, overlap recovery, bounded retirement/restart, без лишнего REST | CODE READY; targeted PASS |
| GROW-02 / F34: typed lossless records и resumable bounded repack накопленного, public bytes/results сохранены | CODE READY; CI38935830076 PASS745 tests |
| GROW-03 / F34: compact numeric quality blocks, methods/settings/comparators/completeness и controlled expiry equality | CODE READY; CI/image PASS |
| GROW-04 / F38: measured category rates, queue lifetime/turnover, versioned forecast model и ограничения | CODE READY; CI/image PASS |
| GROW-05: required CI, merge, normal CD<=900s, source/image/settings/history/alerts readback и measured post-report | DONE: PR44/c922700, CI38939657096 PASS754; CD38940357976 PASS298s, full backup27.639s; private/env/generation equality |

Затрагиваются E06/E10/E12/E13; существующие65AC/исторические проценты сохраняются,
новые AC не вводятся. Работа отслеживается по пяти задачам. AC-13-02/13-05 уточняются
по выявленным gaps; code READY не заменяет required checks и delivery.

DoD: задачи подтверждены meaningful regression/equality/restart/corruption/retention
checks и измерением bytes/event/outcomes/services; CI на latest revision PASS;
merge+CD по действующей full source/backup validation, health/ingestion/private
settings preservation, no historical alert replay. Показать фактический объем,
скорость и новый прогноз относительно20GB с ограничениями; не объявлять fit без
измерений. Постоянное ожидание статистики не входит в DoD.

Non-goals: reset/удаление полезной истории, сокращение markets/detectors/TF/events/
метрик/сроков, PostgreSQL/Redis, owner threshold changes, consumer activation,
удаление backups/experiments, повышение RAM или backup/CD900s. Старые форматы
остаются читаемыми; schema/encoding changes сопровождаются compatibility evidence.

## Validation Plan

Canonical команды — [README](../README.md#checks); из root repository. Local Python3.12/Windows имеет ограничение native checks; required CI/image — Linux/Python3.11. Все test data disposable, secrets не нужны.

| AC/риск | Проверка / ожидаемый результат | Среда/этап | Gate/результат |
| --- | --- | --- | --- |
| Spec/commands/config | `python scripts/checks.py quality`: docs/lint/format/types | local до push; docs CI | REQUIRED PASS до последнего code push; audit docs проверить отдельно |
| Auth/config/migration/revision race | Existing pytest bot/config/storage/CLI; strangers/groups/invalid/future/corrupt/empty-list/CAS | local targeted, full PR CI | REQUIRED EVID-02 PASS |
| Markets/TF/formulas/episodes | Ingestion/universe/detectors/events replay/gap/reconnect/re-arm/coalescing/fault tests | local targeted/full CI/image | REQUIRED PASS; runtime EVID-04 |
| Analytics/retention/services | Known series, neutral/directed/gaps/late worker, all methods/frozen event revisions, comparator/no future leakage, controlled expiry30/35d/12mo, terminal queues/restart/dedup | targeted local + `python scripts/checks.py all` CI | REQUIRED EVID-02 PASS731 tests; real 30d expiry не ожидался |
| Export/Telegram/integration | NDJSON completion/pagination/filter/provenance, UI/outcome navigation, independent consumers, signed immutable retries/authority guards | isolated CI/image; live доступный контур | REQUIRED producer/mock contract PASS; live signed consumer acceptance передана receiving project (S06) |
| Reset/private settings | Exact-target/allowlist/refusal/restore/equality/queues/cursors/generation/no historical alerts | isolated fixture + approved one-time CD37 | REQUIRED EVID-01 PASS; reset permit CONSUMED |
| Storage/capacity | Synthetic event+4outcomes+services/indexes; cheap counts/files/pages/sample model; bounded index snapshot; category/external inventory | isolated image + bounded readonly VPS | REQUIRED EVID-04 PASS; 20GB fit НЕ доказан, forecast warning виден |
| Ordinary CD/recovery | Required protection/latest CI/tree/source/image, full source integrity + disk snapshot/hash/readback, target lock, health/ingestion/private equality | production CW CD38/39 | REQUIRED EVID-03 PASS; entire job<=900s, CPU1/RAM4GiB/no swap |
| Handoff / S06 | Existing producer5+14 targeted fixtures, exact example/schema/hash/source equality, stable65 AC и6TA-CW items, bounded readonly CW smoke | local + required docs CI | REQUIRED local PASS; latest PR CI/merge PENDING; runtime deploy N/A |
| Representative load / longer trend | Separate owner observation; CPU/RAM/backlog/disk by representative workload, stop criteria до load | не запускался как длительный Actions monitoring | RECOMMENDED; не блокирует завершенный storage/reset scope |

### GROW Validation Plan

| Риск / критерий | Проверка, команда из root | Среда/этап / gate |
| --- | --- | --- |
| Recovery F37 | Existing analytics_recovery + bounded fixtures: local fill, overlapping requests, priority/restart/conflict and terminal cleanup | Local targeted pytest / REQUIRED |
| Lossless records/quality | Old/new decode/public canonical equality, corrupt/truncated/bounds, mixed formats, all methods/frozen revisions, NDJSON/analytics before/after expiry | Local targeted pytest + full CI / REQUIRED |
| Progress/history preservation | Bounded repack interruption/restart/concurrent new write/rollback, counters/links/private and no re-notification | Local fixture + Linux image / REQUIRED |
| Capacity F38 | Existing isolated measurement, dense multi-event numeric blocks and comparator case; measured bytes separately from forecast; queue turnover plateau | Local targeted + CI measurement / REQUIRED |
| Delivery | Canonical quality locally, full PR CI/image; existing ordinary full disk source+backup checks and bounded post-checks | Production only after merge / REQUIRED;900s unchanged |
| Restart hotfix | Local current/complete persisted windows skip redundant REST; stale/gap/short/conflict/settings cases retain recovery, both markets/4TF, cooperative scheduling/no historical live replay | Targeted ingestion tests, full required CI, ordinary CD / REQUIRED |
| Wide WS ACK | Shards<=100topics с полным покрытием, strict15s ACK deadline даже при непрерывных updates, independent shard ACK до обработки closed observations | Targeted fake WS burst/timeout + required CI / REQUIRED |

## Текущее состояние AC

Текущие статусы учитывают explicit S06/D23: E09 оценивается по producer и handoff, а actual consumer activation передана в TA-CW checklist. Это изменение ответственности владельцем, не утверждение о выполненном live E2E. Исторический audit snapshot не пересчитывается. Продуктовые и технические AC равноправны.

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
| AC-03-01 WebSocket обоих рынков | IN_PROGRESS | F39 startup bucket starvation на runtime46; compatible fairness hotfix local52 PASS, required CI/runtime PENDING |
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
| AC-09-01 Structured contract | READY | test_agent_delivery; reference consumer qualification EVID-05; current handoff |
| AC-09-02 Готовность авторизованного producer | READY | Existing HMAC raw-body/retry/restart/expiry/independent-consumer fixtures PASS; config absent сохраняет manual radar; live activation transferred TA-CW-01…06 по S06 |
| AC-09-03 Outbox и recovery | READY | test_agent_delivery; reference consumer qualification EVID-05; current handoff |
| AC-09-04 Identity/revisions для consumer | READY | test_agent_delivery; reference consumer qualification EVID-05; current handoff |
| AC-09-05 Delivery diagnostics | READY | test_agent_delivery; reference consumer qualification EVID-05; current handoff |
| AC-09-06 Handoff и causal analytics | READY | Canonical handoff в agent-delivery, exact producer example/schema/hash, responsibility+TA-CW checklist, prior contract tests и producer context/export/causal fixtures; receiving runtime acceptance не выполнялась |
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
| AC-13-05 Конечные служебные зависимости | READY | F37 исправлен; local/restart/overlap tests и Linux CI38935043692 PASS; actual delivery pending |
| AC-13-06 Контроль 20 GB | READY | F38 separated queue turnover/occupancy/current-layout scenarios PASS; stale index cache явно STALE; required latest CI/delivery pending, fit не заявлен |
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

### EVID-07 — S06 handoff readiness

- Source/base2e242abf; producer five positive HMAC/restart/expiry/isolation/projection
  scenarios PASS,14 invalid-config/ack/header-isolation cases PASS. Все controlled,
  без production requests/credentials. Новые runtime tests/code не добавлялись.
- Example7716bytes/SHA256 `2a65a5d0de157deb6039c0dfb9c4551ded95f89d00135604d36a483c88bc6bbf`,
  exact output текущего40-envelope producer fixture; canonical settings hash и
  false authority проверены; secret/config credential fields отсутствуют.
  Contract/example/ownership/checklist источники связаны относительными ссылками.
- Readonly CW smoke16Oct2026T14:42:55UTC (17:42:55МСК): source130225d2/image26b64aee…,
  healthy/config revision3/same history generation/alertsON/custom/composite preference.
 1248streams/4TF/оба рынка, bootstrap/recovery/blocked0; poll1245/1248 с3 явно
 insufficient-history/zero-baseline. AnalyticsOK, TelegramDELIVERED; sample200outcome
 heads truncated:197COMPLETE/3INCOMPLETE. AgentUNSET/OFF не мешает manual работе.
 Ни сообщения, ни config/DB writes, ни обращения к consumer не выполнялись.
- Working625864056bytes (DB605556736/WAL20274552/SHM32768), net2.748MB/min over13580s;
  forecast425.12GB — model extrapolation,20GB fit не доказан. Автоматические sampler/
  retention timestamps свежие. Это ограниченный smoke, не новое длительное наблюдение
  и не пересчет аудиторских процентов.

## Полный реестр findings

Исходные проблемы F01…33 относятся к audit8ace3638 и сохранены для трассировки; Evidence ниже заменяет устаревшие «IN_BRANCH/PENDING». Confidence HIGH — код/fixtures/primary records; MEDIUM — неподтвержденные orphan/hardening/UX candidates. DEFER/DOCUMENT не означает исправление или новое требование. Detailed historical logs/snapshots остаются вне tracked документов.

| ID | Проблема / область / исходный объект | Влияние / priority | Действие / обоснование / зависимости | Evidence | Confidence / disposition |
| --- | --- | --- | --- | --- | --- |
| F01 DOC/SPEC | S01 полностью меняет прежний Spot MVP; отсутствовали AC и трассировка; product_scope.md / все эпики | P1 | CONSOLIDATE: Scope источника, без новых features. | S01/S05 полностью сверены; D22/E13,65 stable AC; PR19 и PR37/39. | HIGH / CLOSED |
| F02 DOC | Legacy schema1/storage/recovery формулировки противоречили current schema2; README/state_contract/architecture/agent-delivery | P2: риск выбора неверного backup/rollback path | CONSOLIDATE: current contract поднят перед legacy, logical v1 отделен от physical2, routine disk backup имеет один canonical runbook; useful historical anchors/tooling сохранены | G1/PR37 адаптация + PR41/main2e242abf. Сверка Store/Sources/History/receiver dispatch, relative links и local docs PASS; CI38901694618 PASS, merged41, postmerge38901939152 docs-only PASS | HIGH / CLOSED |
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
| F17 INTEGRATION | Исторически отсутствовали advisory contract/delivery; E09 | P1 | IMPLEMENT/DOCUMENT: producer готов и передается через handoff; activation принадлежит принимающему проекту по S06/D23 | Producer v2/HMAC/outbox/retry/replay/diagnostics fixtures PASS; receiver snapshots/CI/CD в EVID-05; contract/example/TA-CW-01…06 handoff в текущей branch. Credential/binding не создавались | HIGH / PRODUCER_READY_HANDOFF; external activation transferred |
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
| F34 HISTORY CAPACITY | Full heads/shared overlapping windows и indefinite service/source references увеличивают БД; AC-06/10/12 и13-01…07 | P1: disk/write amplification,20GB target не доказан | REFACTOR: Reset37 выполнен. GROW реализует typed records, shared quality dimensions/provenance, normalized expiry, durable repack и honest forecast; сроки/метрики/охват сохраняются. Старую conversion не возобновлять. | Compact shared candles/settings/context, bounded services/30d details/35d candles/12mo quality, reset37 и normal CD38/39. Working247078264bytes/index78925824,3.247MB/min over4493s. Model462.15GB предупреждает;20GB fit не доказан. | HIGH / GROW IN_PROGRESS; target fit UNPROVEN |
| F35 ANALYTICS COMPATIBILITY | AC-06-05/06-06/10-02/13-03; quality_summary.archive_event выбирал global method/latest event revision | P1 | FIX: все methods/comparator versions, frozen revisions и metadata-only expiry reader | Repros before expiry методы1/2→после только1 и strength1→101. PR39/main130225d2, CI38897051479 731 PASS/image equality; normal CD38897380425 PASS. Source/candidate quality0 еще не достиг30d; переписывать summaries не требовалось | HIGH / CLOSED |
| F36 CONSUMER COMPATIBILITY | AC-09-01/09-02; actual consumer PublicSettings отвергает новые retention metadata | P1 | FIX: backward-compatible optional positive ints, полный snapshot/hash без fabricated defaults; unknown/secret fields остаются forbidden | Actual cacdfda parser40 fixtures FAIL только detail_retention_days/candle_retention_days. Consumer PR949 merged8c207edf; CI38898629578/38899151685 и CD38899376113 PASS. Actual parser40/40 payloads и40/40 authority rejects, snapshot/hash preservation/legacy/invalid metadata fixtures PASS; no events sent | HIGH / CLOSED |

| F37 RECOVERY QUEUE | Уже заполненные локально ranges остаются PENDING; analytics_recovery.work не перепроверяет наличие перед REST | P1 | FIX: local completeness/reuse и bounded retirement, сохранить реальные gaps/retries | Read-only16Oct18:48MSK:16/16 checked PENDING ranges complete, attempts0/age48–266min; bounded non-random sample | HIGH / IN_PROGRESS |
| F38 FORECAST | Валовый оборот временных очередей умножается на30d; общий page factor применяется к годовым numeric facts | P1 | FIX: разные lifetimes/queue occupancy/retained slope и measured format model; не скрывать превышение | storage_meter.report и measured438.2GB; details alone30.1GB encoded30d при текущей частоте | HIGH / IN_PROGRESS |
| F39 RESTART RECOVERY | Cold windows/recovery priority задерживали первые market/TF buckets; AC-03-01 | P1: wide-universe warmup/CD failure | FIX: local restore/yield, seed fairness и affected-gap repair; cached first-ready status | PR51–55 CI, final normalCD55 PASS278s; оба markets/4TF recovered,1120active streams/allACK/current queues0/source healthy03:19UTC. Old failed CD46/48/50 сохранены | HIGH / CLOSED |
| F40 SUBSCRIPTION BACKLOG | Один linear shard788topics; continuously ready recv может продлевать ACK ожидание после deadline | P1: reconnect/blocked streams при широком universe | FIX: shards<=100topics без потери покрытия; strict15s ACK deadline | Bounded read-only17:23UTC25s: ACK200/788,buffer_overflows22; fake continuous-open stream воспроизводит обход deadline | HIGH / IN_PROGRESS; тот же hotfix |
| F41 DECISION DUPLICATE | Актуальная decision и immutable revision полностью дублируются;104,860rows на52,430events | P2: лишний payload и writes | FIX: bounded checked pointer на latest immutable revision; отдельный durable repack cursor, public read/retry/restart/expiry unchanged | Exact ledger18:36UTC; sample128heads17,071→3,456bytes до реализации | HIGH / CODE READY; CI/CD PENDING |
| F42 CAPACITY EVIDENCE | Нет полноценного per-namespace ledger/24h physical deltas; старая annual density синтетическая | P1:20GB не подтверждены | IMPLEMENT: explicit readonly ledger/cardinality/formulas и finite stable24h observation, не менять scope | Actual summaries0; prospective20,684cohort-days/211,000facts, median5; historical110.55GB/model, not daily measurement | HIGH / IN_PROGRESS; observation/result PENDING |
| F43 RETENTION PROOF | Прежний малый fixture не доказывал rolling filled-window/restart и реальные20 comparator arrays вместе с methods | P1: lifecycle/capacity evidence gap | IMPLEMENT: controlled62week population plateau, exact facts/group equality и full GC; separate richer comparator/method boundary fixture | Local tests/rehearsal PASS; production annual mix unobserved | HIGH / CODE READY; CI PENDING |
| F45 MAINTENANCE THROUGHPUT | Backfill/repack starvation и transient-queue sampling перед baseline; E06/E13 | P1: stable24h baseline задержан | FIX: independent jobs/guarded duty/finite snapshot/gap repair/admission55 поставлены; bounded retry только перед baseline при единственном current-queue blocker | PR48–55 CI, finalCD55 PASS; actual repack COMPLETE03:08UTC/historical backlog0, immutable/private preservation PASS. Observer repeatedly samples transient new jobs; final-image baseline PENDING | HIGH code/runtime; IN_PROGRESS |
| F44 RECOVERY TERMINAL REUSE | Ownerless requests читают candles до owner check; terminal receipt блокирует новый valid owner до cleanup | P1: лишний scan/backlog и задержка repair | FIX: owner-first terminal retirement без source mutation; terminal job re-arm при valid retained owner; dedup/retry preserved |65/128pending без owner; regression re-arm/arrived data/noREST PASS | HIGH / CODE READY; CI/CD PENDING |

## Документы, legacy и artifacts

Canonical README — назначение/commands/navigation; spec — requirements/decisions/AC; этот plan — statuses/findings/Goal/Evidence; state_contract — ownership/schema1 compatibility и schema2; bounded-storage-reset — current retention/reset/routine disk backup. Полезные historical converter/native CLI и source licenses сохраняются: их наличие не означает текущую обязанность конвертировать discarded history.

F02 consolidation выполнена PR41: canonical paths и полезные historical anchors сохранены; schema2 contract перенесен в начало state_contract, architecture и agent runbook описывают actual normalized/disk flow. Destination — existing state_contract schema2 + bounded runbook; links README/architecture/agent-delivery проверены. Новый конкурирующий документ не создан. Исторические rules D12/raw90/indefinite details явно superseded D22. F24 private duplicate helpers — DOCUMENT/DEFER, удаления только из-за отсутствия imports не выполнялись. Local ignored `.cache-g1`, env/caches/pycache и прежние branches/worktrees учитываются отдельно от tracked source; статус ignored не означает ненужность.

## Blockers и следующий шаг

| ID | Состояние | Необходимое действие |
| --- | --- | --- |
| B01 | RESOLVED: first migration/reset и ordinary current source/backup/CD подтверждены | Не повторять reset/ручную передачу образа |
| B02 | Передан принимающему проекту по S06/D23; не блокер manual radar/handoff. Live integration по последней проверке OFF/UNSET | TA-CW-01…06 в handoff: receiver credential/recovery/binding/max-age900s, coordinated producer config, signed acceptance; актуализировать внешнее Evidence при подключении |
| B03 | RESOLVED: numeric policies/owner mapping/retention определены D09-D22 | Сохранять актуальные правила и settings equality |
| B04 | RESOLVED: GitHub admin, strict ci-required/production Environment/DEPLOY_ENABLEDtrue, restricted SSH | Перед каждым merge reread head/protections; не обходить gates |

F34 budget/capacity выбран владельцем в текущую GROW Goal; fit остается проверяемой целью, не ожиданием статистики агентом. Контроль работает автоматически каждую минуту через counters/files/pages; exact later index walk — отдельная bounded snapshot. При устойчивом forecast>20GB сначала проверить largest category/dense compression/index/service cost; затем предложить решения с сохранением аналитики. Сокращать сроки/события/метрики скрытно запрещено. Remaining included Actions minutes UNSET; speculative monitoring/reruns не запускались.

## Audit quality review

Охват: весь updated S01/S05,65AC и обратная трассировка active features; entrypoint/routers/config/lock/source schema/queues/delivery/analytics/WS/REST/TG/agent boundaries; tracked и vendor/native/generation/scripts/workflows; separate ignored/runtime backups/experiments; primary GitHub gates/CI/CD/actual image/private hashes. Required code gates PASS; operating integration не выдается за PASS; по S06 она вне CW DoD и передана принимающему проекту. Обнаруженные F35 и F36 показывают предел прежних single-method/producer-only fixtures; исправления проверяются на смешанных версиях и фактическом consumer parser.

False positives проверены: optional enrichment/digest OFF — preference; три insufficient-history/zero-baseline streams не названы outage; совпадающие symbols разделены market identity; runtime hard limits не объявлены обязательными без requirement; old converter/vendor не назван мусором; package advisory match не равен exploit; 20GB не объявлены доказанными initial small DB; сохранение точных numeric summary metrics не названо хранением полной старой event history.

Confidence HIGH по auth/config/atomic persistence/formulas/retention/replay/CI/source identity на проверенных fixtures. MEDIUM по long-term capacity/оптимальной частоте/optional UX/external private helper usage. Limitations/possible false negatives: steady-state annual/dense cohort layout и новый реальный30d expiry еще не наблюдались; controlled-clock tests их моделируют. Полный stream load/SLO, третьесторонние native/dependency undisclosed bugs и все Telegram/browser/client paths не доказаны этим аудитом. Этот audit не заменяет отдельный exhaustive security scan. Real signed receipt/consumer cutoff smoke передан принимающему проекту; отсутствие этого Evidence не мешает standalone use и готовности handoff по S06.

## Checkpoint

CAPACITY active16Oct2026, branch/base above; one main PR. Source baseline c922700,
PR44 CI38939657096 PASS754, CD38940357976 PASS298s; backup1,015,951,360bytes full
copy/hash/integrity PASS27.639s,CPU1/RAM4GiB. No reset/persistent history deletion.
Discovery18:28UTC healthy/TelegramDELIVERED/analyticsOK;1192streams/4TF, bootstrap/
blocked/live recovery/historical gaps0. Repack IN_PROGRESS31,869rows; completion
не заявлено. Working1,180,349,680bytes; forecast133.42GB — short model, не суточный факт.

Exact sequential runtime snapshot18:36UTC:1,073,959rows. Event52,430rev/52,430heads;
episode65,697/30,282; outcome251,658/197,222; comparison52,408/52,408. Encoded payload:
event52,026,714 +heads11,868,318; episode25,577,769+6,191,607; outcomes126,727,124+
34,635,179; comparison22,007,119+9,294,149bytes. Keys/physical pages/indexes отдельно.
Decision104,860rows/13,970,138payloadbytes — по две копии на event. Actual year facts0
(еще не наступило30d expiry). Full prospective study52,750event revisions→211,000facts/
20,684cohort-days; density mean10.20/median5. Instrument иTF/horizon — главные факторы;
strength49,372unique хранится в facts, не cohort key. Published method/settings/version
по одной; future variation и comparator availability не измерены.

Bounded samples128 comparison revisions:128 empty sets; не доказательство полезного
nonempty dedup. Episode/outcome revisions меняют confirmations/closure/metrics/
completeness, event revisions=1 в полном study; безосновательное удаление не сделано.
Ownerless recovery65/128ordered pending (не случайная выборка), source scan не нужен.
Expected effect до изменения: decision heads17,071→3,456encoded bytes на128rows (~80%);
малый выигрыш не объявлен решением20GB. Новый compressor/batch format не вводился.

Local Windows/Python3.12:99 targeted PASS включая62week controlled fixture33s;
после owner-first/re-arm fix25 targeted PASS. Rehearsal: обоих рынков/5families/4TF,
settings changes/restart, точный multiset numeric/comparator/group equality, private
unchanged. После заполнения windows stable50detail events/840source rows/1920quality
facts; file3.14–3.15MB на synthetic weekly workload. Final400d drain удаляет истекшие
source/context/holds/summaries/dictionaries/body refs; producer DB не затронута.
Отдельный test20 matching comparator windows + multiple methodology versions PASS
через30d/35d/12mo, без duplicate summary admissions. Это не capacity/load proof.

Latest local quality/docs/Ruff/mypy PASS;24 focused tests PASS после compatibility
guard. Self-review: tiny decision heads сохраняют immutable revisions/public bytes;
atomic repack/checkpoint и retention counters rollback проверены fault injection.
Source/gen/config changes и probe interruptions исключают stable-day extrapolation.
Runtime readback19:28UTC: bootstrap/recovery0, полные1192ACK; repack47,710records,
за час+15,841 при>1m runtime rows. Added bounded asynchronous8x40ms slices, yield
между отдельными transactions/checkpoints; общий intended work320ms/cycle, no long
single lock. Local76 capacity/bot/storage checks и canonical quality PASS; fairness,
finite completion и действующие bot paths проверены. Actual throughput после CD
измеряется отдельно, ускорение8x заранее не объявляется фактом.

CI38954699435: tests PASS, image build FAIL — новый observer был исключен Docker
allowlist. Добавлено разрешение только `scripts/observe_capacity.py`; прочие scripts,
private experiments и runtime data в build context не включаются. Required latest
CI/image, merge и ordinary CD пока PENDING.

PR45 latest CI38955487956 PASS766/image; merged ef5652d, ordinary CD38956621801
PASS330s. Readback19:50UTC: exact source/image healthy, private/env/generation
preserved, full backup43.378s; working1,353,592,216bytes включает113,798,552WAL.
После restart bootstrap еще выполняется — это не steady interval. При проверке
gate выявлен пропуск отдельной referenced analytics recovery queue: hotfix добавляет
indexed existence lookup/LIMIT1 без count/scan; нормальные future horizons допустимы.

Hotfix46/CD38958643381: POST_CHECK_FAILED, после150s нет fresh coverage всех8buckets;
original failed record сохранен.20:08 runtime healthy/all ACK, poll WARMING862/1168,
recovery еще не завершено. Worker выбирал priority0 gaps до bootstrap10 без bucket
seed. Compatible hotfix вводит первые успешные windows8market/TF, без изменения
REST budgets/coverage/150s post-check/900s CD. Потом прежние priorities0/10/20.
Working bucket исключен из параллельного seed; failure не отмечает bucket готовым.
Local52 ingestion/source/async tests и quality PASS; actual delivery PENDING.
Seed требует свежую usable composite baseline; zero baseline/stale window не дают
ложной готовности. Условие не заменяет unchanged full ACK/fresh coverage post-check.

Next: self-review/latest required CI/image startup hotfix, merge и normal CD900s,
source/private readback, затем finite observer на окончательном artifact.
Observation waits up to12h for two5min stable samples and COMPLETE repack/admission;
then24h. Sample statuses/partial deep boundary/failure explicit; actual24h PENDING.
No unrelated branch/worktree cleanup. Backups/experiments сохраняются.
