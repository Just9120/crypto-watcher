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

## Current Goal — G2A (IMPLEMENT)

Основание: владелец 2026-10-10 поручил весь roadmap G1 → G2A → G2B → G4/G3 → G5 → G6. G1 DONE: 8/8 AC, PR20–23 merged; actual production CD38087285142 на main `40882eb8b555991ce1f22c87fe95b3951507d74c` PASS 2026-10-10T21:24:37Z. Backup/source/image/health/persistence/market100/100 PASS. Владелец подтвердил sample/Mutes/settings controls («вроде работает»); screenshot подтверждает rendering/current thresholds/revision1/buttons, остальные clicks — owner assertion. Рабочие ветки G1 удалены после проверки exact merged heads; ignored operator artifacts сохранены отдельно.

Результат G2A: shared realtime данные широких Spot USDT и USDT Perpetual universe, native1m/3m/5m/15m одновременно, REST bootstrap/recovery, объяснимые completeness/freshness и typed watchlist; composite сохраняет formula/equality и owner thresholds.

Ветка `codex/g2a-dual-market-ingestion-20261011`, base `40882eb8b555991ce1f22c87fe95b3951507d74c`, root worktree, initial clean main; GitHub strict ci-required/protection и production CD проверены до начала. Один основной PR; до завершения scope/локальных checks push не выполняется.

### Scope, зависимости и DoD

11 затронутых AC: AC-02-01…04, AC-03-01…05, AC-04-01, AC-07-02. AC-03-04: ingestion/recovery часть G2A; полный criterion также включает incomplete outcome horizons будущей G3 и остаётся IN_PROGRESS до их проверки. Foundation не закрывает будущие episodes/provenance/outcome criteria. Findings F08–F11/F16/F20/F22/F29 обновляются по фактическому Evidence; F09/F16/F20/F22 имеют остаток в следующих Goals.

DoD G2A: scope реализован, meaningful tests и latest required CI/self-review PASS, PR merged, automatic CD на expected source/image PASS; bounded production smoke подтверждает metadata/subscriptions обоих рынков/4TF и usable persisted closed candles, отсутствие historical alert flood. G1 owner settings/preferences/data сохранены. Production schema1 сохраняется: новые source records используют versioned логические namespaces существующей runtime key/value table; physical DDL/JSON migration отсутствуют. Backup включает source records через тот же SQLite backup API. Если потребуется schema change, NONE не применяем: сначала отдельная reviewed delivery classification/procedure.

Non-goals: новые detector families/profiles кроме watchlist multiplier, episodes, durable Telegram/agent queues, outcomes/digest/enrichment, изменение trading agent. Эти части идут в G2B/G3/G4/G5. Сохранённые значения не заменяются preset; selected TF остаётся представлением карточки, все четыре streams активны. Широкий universe заменяет legacy fixed Top100 после verified metadata refresh.

### Задачи

- [x] Закрыть G1 по primary records и owner smoke; безопасно очистить ветки.
- [x] Проверить новую base/protections, Bybit metadata/WS contracts и закрепить scope/Validation Plan до implementation.
- [ ] Metadata classification/quarantine, полная linear pagination, ticker/liquidity refresh и typed watchlist с sensitivity.
- [ ] Shared concurrent WS streams, ack/heartbeat/reconnect, bounded REST bootstrap и priority gap recovery.
- [ ] Closed-candle persistence/provenance, bounded memory, duplicates/conflicts/out-of-order/gaps/freshness; historical data не публикуются как live.
- [ ] Подключить composite обоих рынков/4TF, market-specific controls/links/status и CLI diagnostics.
- [ ] Meaningful regression/fault/load fixtures; docs/state contract и self-review.
- [ ] Latest required PR CI, merge, automatic CD и bounded dual-market/4TF smoke.

### Validation Plan

| AC / риск | Проверка / expected result | Canonical tool, cwd | Environment / этап | Gate |
| --- | --- | --- | --- | --- |
| E02 metadata/liquidity | >100 eligible instruments, repeated cursor rejected, identical symbols isolated; TradFi/tokenized/non-USDT/prelisting/unknown type denied; boundary liquidity changes active set | Targeted pytest universe/ingestion fixtures, root | Disposable local; full Linux3.11 CI | REQUIRED |
| AC-02-04 configuration | Typed market identity validated against metadata, empty list preserved, multiplier finite/positive and survives restart; concurrent setting update cancels obsolete result | Targeted config/Telegram/storage fixtures, root | Local and PR CI | REQUIRED |
| E03 stream integrity | Native4TF both markets; only confirm=true; duplicates/out-of-order/conflicts/gap/reconnect/rate-limit bounded recovery; no historical live alerts | Controlled WS/HTTP adapters and SQLite, pytest, root | Local targeted fault tests; Linux full CI | REQUIRED |
| AC-04-01 / 07-02 | Composite inclusive equality/custom thresholds preserved on both markets; URL/callback identity correct | Existing plus targeted market/bot tests, root | Local and CI; safe link smoke after delivery | REQUIRED |
| Source persistence/restart | Versioned per-stream sources restore with original times; in-memory window bounded; same DB backup includes sources; no owner/import corruption | SQLite source/recovery fixtures, root | Local/CI and production persistence readback | REQUIRED |
| Conventions/installation | Docs/Ruff/mypy PASS; precise dependency/hash locks; Linux3.11 image/version/health/state checks | `python scripts/checks.py quality`, root; required CI all/image checks | Local cheap checks; CI full merge gate | REQUIRED |
| Delivery / live adapters | Actual expected revision/image; both markets metadata and four TF subscriptions acknowledged; usable closed candles, degraded causes visible; backup/persistence/source PASS | Current delivery runbook, Actions primary records and CLI diagnostics | Production automatic CD plus bounded smoke | REQUIRED; unavailable evidence is not N/A |
| Resource growth / broad universe | Shared dataset and bounded queues/windows; measure source rate/backlog/memory/disk against actual universe | Controlled load fixtures; bounded VPS diagnostics | Local/CI correctness; actual capacity expanded G6 | RECOMMENDED; no assumed CPU/RAM limits |

### AC progress текущей Goal

Начало G2A: существующие AC-03-03/05 и AC-04-01 сохраняют regression implementation; соответствие dual-market/shared ingestion ещё проверяется. Новые AC пока IN_PROGRESS; готовность всего проекта/эпиков не пересчитывается. После подтверждения частей обновляем X/11 с явным остатком AC-03-04 в G3.

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
| F08 DATA INTEGRITY | Нет проверки непрерывности и полноты candles; код 918–961; AC-03-04 | EV02 gaps FAIL: часовой gap допустим для 1m composite; G1 update: EVG1: ordered closed composite rejects gaps/conflicting candles. Shared gap tracking/backfill recovery ещё G2A | P1 | FIX: Gap tracker/ordered candle validation; блокировать affected detector, REST backfill и пометка completeness | G2 ingestion | HIGH / PARTIAL |
| F09 FRESHNESS | Нет event timestamps/TTL; closed определяется только local clock; код 919–945; AC-05-04 | EV02 freshness FAIL: суточные candles дают сигнал; legacy follow-up #1 server time sync; G1 update: EVG1 closure uses Bybit response time; EVG3 NTP synchronized. Event TTL/WS closure/freshness остаются G2A/G2B | P1 | IMPLEMENT: Type/TF freshness, market/detected/received time; clock drift diagnostics/WS confirm; проверить VPS time sync | G2; B03 TTL policy | HIGH / PARTIAL |
| F10 UNIVERSE | Fixed100/одна category, eligibility без metadata; код 899–913, 987–1009; AC-02-01..03 | EV02 universe-eligibility FAIL: ETHBTC/unknownUSDT попали в scan; нет instrument status/type фильтров | P1 | IMPLEMENT: Широкие раздельные Spot USDT/USDT Perpetual universe по metadata/liquidity и полной pagination; safe unknown type exclusion | G2; Bybit metadata contract | HIGH / OPEN |
| F11 INGESTION | Нет WS и одновременно работающих TF; код 963–1027, requirements; AC-03-01/02 | Только REST kline, одно signal_timeframe per-chat | P1 | IMPLEMENT: Shared owner ingestion, subscriptions обоих рынков и 1m/3m/5m/15m, REST bootstrap/recovery | G2 после G1 | HIGH / OPEN |
| F12 DETECTORS | Реализован только legacy composite; код 918–961; AC-04-02/03 | Отсутствуют impulse/spike-alone/breakout/volatility/versioned event contracts | P1 | IMPLEMENT: Versioned deterministic detectors с metrics/units/direction/provenance и fixtures | G2; B03 formulas/defaults | HIGH / OPEN |
| F13 EPISODES | Cooldown без event identity не заменяет episodes; код 2090–2105; AC-05-01..03 | EV02 repeat-same-candle FAIL; baselines содержит price/ts/last_alert, нет candle/event/revision ID | P1 | IMPLEMENT: Episode lifecycle/correlation/re-arm/max lifetime и meaningful notification policy, исходные events сохранить | G2; B03 | HIGH / OPEN |
| F14 DELIVERY | Failed Telegram send расходует cooldown без retry; код 2101–2120; AC-07-01/05 | EV02 failed-delivery-cooldown FAIL: last_alert обновлён до send, следующий poll не повторяет; G1 update: EVG1 failed send no longer consumes successful cooldown, ack then dedup same candle PASS; durable retry/episode notifications ещё G2B | P1 | FIX: Отдельное delivery состояние/pending retry; detector/history не зависят от Telegram успеха | G2 delivery; B03 retry policy | HIGH / PARTIAL |
| F15 ANALYTICS | Нет event history, outcome worker, analytics/export; state 331–450, весь tracked inventory; E06 | Отсутствуют persisted events/revisions/episodes/outcomes, cohorts/MFE/MAE/export | P1 | IMPLEMENT: История с settings snapshots и immutable observation/reference; horizons/outcomes/export с completeness и no future leakage | G3 параллельно G4; B03 methodology | HIGH / OPEN |
| F16 OWNER SETTINGS | Market domain зависит от chat; profiles/manual settings не реализованы; код 302–399, 1084–1133, 1759–1795; E08/AC-02-04 | Settings вложены в chats; один market/TF, кнопки отдельных числовых presets; G1 update: EVG1 owner config revisions + delivery/runtime separation; parameters profiles/other subsystems ещё G2A–G5 | P1 | IMPLEMENT: Persisted owner domain config и отдельные delivery preferences; profiles плюс manual validated parameters | G1; B03 legacy owner mapping | HIGH / PARTIAL |
| F17 INTEGRATION | Нет advisory API/автоматической передачи trading agent; все entrypoints/dependencies; E09 | Нет HTTP server/webhook/outbox/consumer config, IDs/auth/replay/delivery diagnostics | P1 | IMPLEMENT: Согласовать contract и authorized idempotent delivery/replay; joint consumer tests; не ждать calibration | G4 после G2; B02 | HIGH / OPEN |
| F18 DIGEST | Нет periodic digest и независимых delivery toggles; код 2171 и handlers; AC-11-01/02/07-05 | JobQueue только poll_engine_job; digest state/aggregation отсутствуют | P2 | IMPLEMENT: Digest из общей history/episodes обоих рынков; interval и separate alerts/digest controls | G3 после history | HIGH / OPEN |
| F19 ENRICHMENT | Нет derivatives context для Spot; весь fetch layer; AC-11-03/04 | Нет OI/funding/basis adapters или надёжной market/asset mapping | P2 | IMPLEMENT: Optional enrichment с source/market/units/time, независимым cadence и nonblocking failure | G5 после G2 | HIGH / OPEN |
| F20 OBSERVABILITY | Symbol errors/timeouts скрываются как отсутствующие данные; код 963–1023; AC-04-04/12-03 | except Exception→None; timeout cancels pending без error summary; status показывает нет данных, без gap/source reason; G1 update: EVG1 per-symbol/source errors и no-signal разделены; remaining source/consumer diagnostics G2A–G5 | P1 | FIX: Reasoned degraded diagnostics по market/detector/universe/gaps и доставкам; не показывать no-signal при missing data | G2; secret-safe error handling tests | HIGH / PARTIAL |
| F21 CONCURRENCY | Settings могут смениться во время fetch, old result доставляется как active; код 2068–2112, status_cmd; AC-07-04/08-02 | EV02 settings-revision-race FAIL: threshold вырос до99, old signal отправлен; UI заново читает current settings; G1 update: EVG1 CAS revision/settings snapshot и poll/status race discard tests PASS | P1 | FIX: Owner config revisions и snapshot provenance; согласованная apply/discard policy перед notification/status | G1/G2; не переписывать concurrent updates | HIGH / FIXED_IN_G1_BRANCH |
| F22 ARCHITECTURE/PERFORMANCE | Monolith и per-chat REST fanout мешают расширению; telegram_crypto_watcher.py 2177 строк; AC-12-03 | Два pools 4/12; ~1 ticker+100 klines на scan/chat, без shared snapshot; awaits Telegram при state lock; legacy follow-up #2/#5; G1 update: EVG1 module boundaries, shared owner scan, bounded async REST outside DB transactions; WS dataset/load ещё G2A/G6 | P2 | REFACTOR: Разделить ingestion/domain/state/render/delivery, общий owner dataset, измерять backlog/cadence/rate budget; load tests по реальной нагрузке | G1/G2; текущая performance/VPS limits UNSET | HIGH / PARTIAL |
| F23 LEGACY | CMC/source/core/reset действия остаются достижимыми вне v1; код 536–874, 1583–1643, 1798–1881, 1967–2041, 2163–2168; AC-01-03 | Commands resetbase/resetallbase зарегистрированы; старые inline callbacks src/core/cmctop поддерживаются. CMC main poll отключён, но пути живы; G1 update: EVG1 active CMC/reset code deprecation по D04/Q06; old callbacks explain change, baselines archived; startup entrypoint retained | P2 | DEPRECATE: После state compatibility review убрать superseded UX/network paths; baselines.last_alert не удалять как якобы полностью dead | Goal cleanup после G1; D04/Q06 | HIGH / FIXED_IN_G1_BRANCH |
| F24 ORPHAN CANDIDATES | Невызванные legacy helpers/caches; внешние consumers неизвестны; код _fetch_async/_prepare_core_context/tracked_desc/price_unit/_mode_label/_rank_prefix/_sub_metrics/_directional_marker | AST+rg и регистрация entrypoints: прямых callers нет; executor/handler functions исключены из кандидатов; legacy follow-up #3; G1 update: G1 сохраняет startup entrypoint; source/history/registrations reviewed for CMC/reset deprecation. Unknown external private helper consumers не доказаны; остальные orphan candidates DOCUMENT/DEFER до G6 | P3 | DEFER: Проверить историю/внешние imports/старые callbacks; затем REMOVE подтверждённых helpers либо DOCUMENT compatibility; imports alone недостаточно | Q06; external Python consumers UNSET | MEDIUM / DEFERRED |
| F25 TEST GAP | Нет tracked воспроизводимой regression suite; EV01 tracked inventory; E01/E03/E08/E10/E12 | Есть только assertions import token и исторические ad-hoc PR18 проверки; tests/config/fixtures отсутствуют; legacy #4; G1 update: EVG1 meaningful auth/config/migration/race/composite/send/CLI/delivery suite; PR21 CI112 tests PASS; sample regression added EVG10, current required CI PENDING | P1 | IMPLEMENT: Минимальные auth/migration/config/composite/recovery tests в существующем Python stack; CI запускает meaningful suite | G1; external audit scripts не заменяют suite | HIGH / PARTIAL |
| F26 DEPENDENCIES | Нет lock/hash constraints; разрешены уязвимые requests releases; requirements.txt:2–3; Dockerfile:1,9 | requests>=2.31.0/python-dotenv>=1.0.0, unpinned base/pip; EV04 current18 packages: advisories=[]; 2.31.0 имеет GHSA-9wx4-h78v-vm56/GHSA-9hjg-9r4m-mvj7; G1 update: EVG2 hash locks/base digest; async HTTPx replaces requests;15 runtime advisories=[]; installed VPS requests2.33.1 observed, full artifact validation PENDING | P2 | FIX: Выбрать проверенный reproducible manifest/lock и safe bounds; audit точного Docker artifact. Не утверждать vulnerable package на VPS без inventory | G1; exploitability по fixed Bybit URLs не доказана | HIGH / PARTIAL |
| F27 CI/CD | CI/protections/build foundation реализована; first actual delivery и portable image identity требуют завершения; AC-12-02 | Исходный EV03: workflows/runs/Environments/deployments=0, rulesets=[], main unprotected. G1 EVG5: CI/merge/release/protections/production Environment PASS. EVG6 обнаружил receiver config-ID/OCI-ID mismatch до DB; PR21/EVG8 verified identity, actual rehearsal/first migration/runtime PASS; automatic CD PENDING | P1 | IMPLEMENT: Сохранить verified archive/host identity и packaging round-trip; завершить sample hotfix и automatic CD/post-checks; не обходить digest проверки и закрытый gate | G1; B01; hotfix после PR20 merge | HIGH / PARTIAL |
| F28 OPS EVIDENCE | Target/config/source inventory и first migration/restore/runtime подтверждены; automatic CD ещё не завершён; AC-12-01/02/10-04 | Исходно runtime/unit/access UNSET. G1 EVG3/6: app/unit/data/code hashes/NTP/resources, amd64/project, root-owned config/permissions, clean checkout8ace3638… и owner record confirmed. Legacy image полный source provenance UNSET; EVG8 actual isolated restore/first migration/source/health/persistence PASS; current hotfix и automatic CD PENDING | P1 | DOCUMENT: Сохранить target evidence и выполнить exact artifact rehearsal/reviewed delivery/recovery/post-checks; наличие legacy backup не означает пригодность restore | B01; deployment не запускать ради статуса | HIGH / PARTIAL |
| F29 CONFIG/UX | Env category/runtime knobs могут противоречить Spot UI и raw input; BYBIT_CATEGORY, settings_text/status_text/alert_keyboard, .env.example | Env category unrestricted, UI/link всегда Spot; .env example не описывает many legacy knobs. env default SIGNAL_TIMEFRAME и state_contract фиксированный5m могут расходиться; G1 update: EVG1 validated market category, labels and links, owner state precedence; typed dual-market watchlist/eligibility remains G2A | P2 | CONSOLIDATE: Явная market identity и validated current config; убрать конфликтующие legacy settings после compatibility review, синхронизировать UI/contract | G1/G2 и F02; actual VPS env UNSET | HIGH / PARTIAL |
| F30 BUILD HYGIENE | Docker context не исключает data/ и .git;  .dockerignore, Dockerfile | Ignore содержит .env/state file, но не data/ и .git; Dockerfile копирует только manifest/code, поэтому inclusion в image не доказан; G1 update: G1 Docker context whitelist excludes .git/.env/data/cache/test fixtures; actual image build review PENDING CI | P3 | FIX: Исключить runtime data/Git из build context для уменьшения context и риска передачи builder; verify context/image artifact | G1; deployment build location UNSET | MEDIUM / PARTIAL |
| F31 HARDENING | Docker runtime использует root default; resource/health policy UNSET; Dockerfile/Compose; AC-12-01 | Нет USER/healthcheck/resource settings; фактические VPS user/resource limits не проверены; G1 update: G1 image UID10001/cap-drop/read-only root/health prepared; EVG8 actual UID10001/SQLite ownership/health/persistence PASS; EVG3 resource snapshot only, future full-universe load unverified. No invented CPU/RAM limits | P3 | DEFER: После runtime inventory выбрать минимальные privileges/health/resources по необходимости; отсутствие инструмента само по себе не defect | B01; не превращать hardening в новый AC автоматически | MEDIUM / PARTIAL |
| F32 UX CANDIDATE | Legacy polish/preset strategy без конкретного нового дефекта; прежний open_issues #6/#7; E07/E08 | Следить за едиными русскими terms/threshold explanations; visual quality/чувствительность требуют user feedback/statistics, не подтверждены как bug | P3 | DEFER: Сохранить полезные terms/presets; улучшать объяснения и выбор thresholds по согласованным detector profiles и outcome statistics | G3 calibration; Q01, не откладывает agent integration | MEDIUM / OPEN |
| F33 G1 REGRESSION | `/sample_alert` потерял baseline Bybit/mute/coin controls и не учитывает active TF; cryptowatcher/bot.py/render.py; G1 preservation / AC-07-03 | EVG9 msg656783 на deployed065e49d подтверждает code regression относительно legacy8ace3638; EVG10 исправляет current snapshot rendering/buttons без REST/state writes | P2: неверный sample и потеря manual controls | FIX: Восстановить existing demonstration/controls, не менять live formula, auth, settings или schema; meaningful regression tests | Necessary G1 hotfix, required CI/merge/release/CD | HIGH / FIXED_IN_HOTFIX_BRANCH; delivery PENDING |

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
| B01 | First migration/restore/runtime PASS EVG8; operator-assisted SSH. Automatic CD credentials/installed receiver ещё отсутствуют; effective SSH restrictions/host identity readback PENDING. Owner smoke PARTIAL EVG9, sample regression fixed only in branch | Hotfix CI/merge/release и compatible schema1 delivery; read-only SSH inventory → reviewed dedicated CD installation preserving existing access → bounded actual Actions CD. Не повторять first migration against migrated SQLite |
| B02 | Prepared trading-agent receiver ещё отсутствует в verified main40a3147172/production; v2 contract evolution только G4 | Поставка незавершённой counterpart Goal по её правилам, повторный contract/live check; не подменять её текущую Goal |
| B03 | Numeric SPEC gaps RESOLVED approved D09–D20; owner mapping EVG6 и actual migration/restore EVG8 PASS | Сохранять accepted policies и migrated settings/custom empty/delivery/mutes; новые unknown/conflicting states сохраняют diagnostic gate |
| B04 | ADMIN ACCESS/SETTINGS RESOLVED EVG4/5: Just9120 admin; actual protections и production Environment readback PASS, DEPLOY_ENABLED отсутствует | Сохранять gates и reread latest revision/protection перед merge; включение CD только после B01 |

## Audit quality review

Полный S01 прочитан, все его обязательные разделы покрыты 12 эпиками/58 AC; дополнительно проверена обратная трассировка reachable legacy CMC/reset и старого UX. Scope расширен требованиями владельца, не новыми findings. Generated/vendor области отсутствуют в tracked inventory. Runtime data/cache/logs/backups отдельно: initial untracked/ignored inventory пустой; временные audit artifacts вне repository.

Проверены false positives: settings/no-op/legacy raw migration работают в обозначенных fixtures; CMC не назван активным main poll; отсутствующий Docker/tool сам по себе не баг; context data/ не доказано включённым в image; requests advisories не означают vulnerable installed VPS; timezone Compose не признан дефектом без требования; callbacks/executor functions не названы orphan только из-за отсутствия прямого вызова.

Confidence: HIGH по code/fixture-backed gaps и inventory; MEDIUM у orphan external usage/hygiene/optional hardening/visual polish; LOW production correctness не оценивается без records. Возможные false negatives: реальные stream/rate/Telegram parsing/time sync/concurrent process behavior, credentials in third-party exception logs, Docker3.11 startup, disk/backup/actual installed dependencies/VPS resource pressure не проверены. Весь READY ограничен baseline и описанными checks; missing live smoke не замаскирован PASS.

## Checkpoint и следующий шаг

2026-10-11 Europe/Moscow: G1 DONE по primary records, verified main40882eb и actual production run38087285142/artifact11682629335; image manifest38d238a6…/config7666f323…, backup pre-40882eb…-1791667365.sqlite3. Owner sample screenshot SHA256401ddabd… и ответ «вроде работает» подтверждают bounded owner smoke с указанными ограничениями. Production Environment DEPLOY_ENABLED=true; dedicated forced-command SSH и strict host key verification проверены; последующие eligible runtime merges доставляет CD. Первое failed CD38086969847 не дошло до authentication/target: Windows CRLF private-key stdin repaired binary transfer, verified bounded run38087285142 PASS. Credentials не хранятся в repository; existing SSH keys/unit другого приложения сохранены.

Current G2A branch/base указаны выше; реализации новых features ещё нет. Следующий шаг — shared universe/source/WS ingestion и meaningful fixtures, затем Telegram/composite wiring и один основной PR. Не повторять first migration или deploy ради восстановления статуса. Отдельный metadata-only follow-up PR не создаётся; G1 результат включён в substantive G2A commit. Snapshot audit7/58 остаётся историческим; полный пересчёт только G6.
