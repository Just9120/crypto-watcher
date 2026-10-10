# CryptoWatcher — delivery plan

Canonical plan: этот файл. Canonical spec: [product_scope.md](product_scope.md). Прежние follow-up темы сохранены в F09/F22/F24/F25/F32; номерные разделы старого плана не служили AC IDs. Этот документ хранит статусы, evidence и полный реестр findings; подробный отчёт/скрипты находятся вне repository.

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

## Roadmap и зависимости

| Этап / предложенная Goal | Результат | Приоритет / зависимости |
| --- | --- | --- |
| G1 — Безопасная owner foundation и проверяемая поставка | Fail-closed owner access, validated persisted owner config/delivery separation, безопасная legacy migration/recovery, базовая suite, минимальный CI и подтверждённая Docker/VPS delivery; существующий Spot path продолжает работать | P1; B01/Q03; сначала закрыть риск потери state и owner scope |
| G2 — Рабочий radar обоих рынков | Широкие eligibility universe, shared WS ingestion/REST recovery, concurrent TF, versioned базовые detectors/events/episodes/freshness, правдивые Telegram alerts/diagnostics | P1; G1, Q01; первый продуктовый этап S01 |
| G3 — История, outcomes и digest | Immutable event/revision history с автоматическими horizons, MFE/MAE/cohorts/export, digest из общей history | P1/P2; базовая history начинается в G2; развивается параллельно G4, Q04 |
| G4 — Advisory integration | Authorized schema/outbox/idempotency/replay и independent diagnostics; joint consumer contract | P1; G2, B02; запускать после стабилизации radar, не ждать calibration |
| G5 — Derivatives enrichment / calibration | Optional Spot derivatives context и ручная статистическая калибровка profiles/detectors | P2; G2–G3; нет automatic production tuning |
| Cleanup/consolidation | Убрать superseded CMC/reset UX, подтвердить helpers, объединить docs и commands | По риску вместе с выбранной Goal; F02–F04/F23–F24; preserve migration/state |

## Current Goal

**Implementation Goal не выбрана и не активирована.** Основание текущей работы — явный запрос AUDIT; audit docs PR не поручает исправления findings или pipeline.

Текущий scope: обновить только `docs/product_scope.md` и `docs/open_issues.md`, полный audit quality review, один docs PR до merge по приложенному AGENTS. Non-goals: код, Docker/config, workflows/settings, README/state_contract/router, внешний Doc, trading agent, Telegram сообщения, production state/deploy.

Audit DoD: все обязательные разделы S01 покрыты AC либо SPEC gap; statuses/denominator просчитаны на проверенном main; findings имеют evidence/action/priority/confidence/dependencies; documentation validation/self-review PASS; PR с ровно двумя canonical файлами, актуальные gates проверены, merge подтверждён. CD N/A: этот PR не меняет runtime/artifact/config.

Branch: `codex/audit-2026-10-10`. Base SHA: `8ace3638b9193444948d0beb88e0c49e9f2f068e`. Worktree: исходный repository, без чужих изменений. PR number/head/merge revision: PENDING до primary records; восстанавливать по этой branch, title audit docs и GitHub PR records, не создавать metadata-only follow-up commit ради собственного SHA.

### Предлагаемый scope G1 — требуется выбор владельца

Результат: бот продолжает legacy Spot мониторинг, domain settings безопасно принадлежат единственному owner и сохраняются при restart; damaged/forward state не теряется; changes проверяются CI и поставляются на существующий VPS с идентифицируемой revision.

Полные AC: AC-01-04, AC-08-01, AC-08-02, AC-10-01, AC-10-03, AC-12-01, AC-12-02. Для AC-08-01 future settings schema создаётся без включения нереализованных detectors/outcomes; UI явно отмечает доступные функции. Safety integration: один runtime остаётся допустимым, existing Spot behavior сохранён, owner migration на копиях с rollback; новые market features ещё не включены. Findings closure: F03–F07/F16/F21/F25–F28 и F02/F04/F26/F30 по затронутому scope; F21 закрывается и для legacy poll/status. Один основной PR; необходимость двух пока не установлена.

Non-goals G1: широкий universe, WS, новые detectors, episodes, history/outcomes/digest, derivatives enrichment, agent integration, произвольный cleanup. Эти части остаются в G2–G5. Dependencies: B01 delivery access/procedure, Q03 owner mapping на безопасной копии. Criteria закрытия: 7 AC READY с meaningful tests, актуальные required CI/review успешны, PR merged, expected artifact/revision на VPS, state/owner/status smoke и обязательные post-checks PASS; неизвестный target/gate не считается N/A.

### Validation Plan

| AC / риск | Проверка / expected result | Команда/tool, cwd | Environment / этап | Обязательность / условия |
| --- | --- | --- | --- | --- |
| Audit docs: scope/источники/AC | 58 unique AC в spec и плане, все statuses/evidence, непротиворечивая арифметика, ссылки существуют, нет внешних links | Markdown/AC checker вне repo; `git diff --check`; root | Local audit / до PR | REQUIRED; PASS до initial push |
| Audit docs: origin/main и gates | remote SHA/base/divergence/PR inventory; ровно 2 docs files; required checks/reviews latest head | Git fetch + GitHub primary APIs | GitHub / до merge | REQUIRED; отсутствие checks установлено, не имитация PASS CI |
| Audit code evidence | Compile in memory; targeted synthetic composite/auth/state/delivery/race/migration/render scenarios | Внешний `diagnostics.py` с PTB20.8; root | Windows Python3.12 / audit | REQUIRED для READY/подтверждённых findings; scripts не добавляются в repo |
| Audit dependencies | Reinstall compatible manifest, pip check, PyPI advisories всей runtime closure | Изолированное environment вне repo; external `dependencies.py` | Windows Python3.12 / audit | RECOMMENDED; target Linux3.11/VPS inventory не проверен |
| G1: access/settings/migration/race | Owner/unauthorized/group/empty config; corrupt/forward/legacy/explicit-empty/invalid settings; concurrent apply; restore на копиях | Будущая canonical suite/command UNSET до implementation; закрепить в README/scripts | Local targeted + CI / G1 | REQUIRED; не выдумывать существующий test command |
| G1: install/config/build | Reproducible dependency resolution, compile/import, config validation, image build/start/restart with persisted state | `python -m pip install -r requirements.txt` сейчас manifest install; Docker build/Compose; root | Python3.11 Linux container / CI | REQUIRED в G1; Docker локально unavailable, это не N/A |
| G1: CI/review | Minimal suite/build на предусмотренной revision; triggers/runners/permissions/required checks зафиксированы | GitHub Actions, будущие canonical scripts | GitHub / pre-merge | REQUIRED; настройка по ci-cd-rules.md только после выбора Goal |
| G1: CD/version/recovery | Exact artifact/revision, no conflicting deploy, backup/migration preconditions, owner status/state smoke, live version/health, rollback procedure | Project delivery procedure UNSET; VPS deployment unit UNSET | VPS Contabo / post-merge | REQUIRED для runtime Goal; B01 пока BLOCKED |
| Telegram live rendering | No parse/length errors, current settings и controls, правильные market links | Безопасный owner smoke/test bot по согласованной процедуре | Test/owner Telegram / соответствующая Goal | RECOMMENDED пока нет отдельного production smoke gate; audit не отправляет сообщения |
| Performance/recovery расширенные | Реальный universe stream/subscription/rate/cadence/backlog и restart/outage replay | Tools/limits UNSET до G2 architecture | Controlled non-production / G2 | По риску REQUIRED для correctness и RECOMMENDED для capacity; SLO не придуманы |

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

No automated regression suite имеется в repository. Format/lint/typecheck/test commands UNSET; это finding, а не повод объявить непроверенные команды работающими. Deployment candidate command из README: `docker compose up -d --build` (root, .env и persistent data), но его наличие не доказывает live procedure или successful deploy.

## Полный реестр findings

Priority: P1 — блокирует correctness/security/основной scope; P2 — существенный gap/долг; P3 — отложенная hygiene/кандидат. Confidence относится к доказанности finding, не к вероятности эксплуатации или масштабам production impact. High не означает production reproduce; условия evidence указаны отдельно. Action/включение в план не разрешают implementation.

| ID / область | Суть, path / AC | Evidence | Влияние / priority | Action и основание | Dependencies | Confidence / status |
| --- | --- | --- | --- | --- | --- | --- |
| F01 DOC/SPEC | S01 полностью меняет прежний Spot MVP; отсутствовали AC и трассировка; product_scope.md / все эпики | Сверка полного S01 с прежним scope: Top100, futures исключены, per-chat domain | P1 | CONSOLIDATE: Восстановить весь S01 и AC в существующем spec, не создавать второй документ | Этот audit docs PR | HIGH / RESOLVED_IN_PR |
| F02 DOC | README/state_contract остаются на старом Spot/chat-based контракте; README.md, docs/state_contract.md; D01–D04 | Тексты fixed Top100/futures outside/per-chat domain противоречат S01; план раньше содержал follow-up вместо delivery tracking | P1 | CONSOLIDATE: Сохранить полезный raw migration/closed candle контракт, объединить ownership/schema с актуальным spec; legacy часть явно обозначить | Следующая выбранная Goal; ссылки/упоминания README, state_contract и header кода | HIGH / OPEN |
| F03 DOC/PROCEDURE | AGENTS.md и ci-cd-rules.md отсутствуют в repository; root; AC-12-02 | EV01 tracked inventory; комплект предоставлен владельцем отдельно во время аудита | P1 | DOCUMENT: Адаптировать router к реальным canonical paths, зафиксировать применимые delivery gates; не копировать шаблон со ссылками на несуществующие файлы | Implementation Goal; приложенный комплект | HIGH / OPEN |
| F04 DOC/COMMANDS | Quickstart обрывается, нет canonical install/validation/environment процедуры; README.md: конец незакрытого bash fence; requirements/Dockerfile | README заканчивается docker compose up -d --build; install/config/test/lint/typecheck команды отсутствуют | P2 | DOCUMENT: Закрыть существующий quickstart, описать .env/data/secrets, runtime и реальные проверочные команды без второго списка | Goal docs consolidation | HIGH / OPEN |
| F05 SECURITY | Access policy fail-open и неполный guard; код 261–277, 1408–1415; AC-01-04 | EV02 owner-access-default и terms-auth FAIL; auth по chat_id не проверяет group sender | P1 | FIX: Fail-closed owner validation и единые guards commands/callbacks/poll; проверить group/user identity и пустую/invalid config | G1; actual VPS whitelist UNSET | HIGH / OPEN |
| F06 DATA LOSS | load_state перезаписывает corrupt/unknown-version state; код 421–450; AC-10-03 | EV02 state-corruption-forward-version FAIL на копиях: corrupt JSON/version99 заменены пустым v2 | P1 | FIX: Сохранить исходный файл, fail safely/quarantine с явной diagnostics; versioned migration и backup/restore | G1; копия production state и owner mapping | HIGH / OPEN |
| F07 CONFIG/STARTUP | Невалидные persisted settings могут прервать весь startup; код 344–399; AC-08-02/10-03 | EV02 invalid-state-settings FAIL; float/int без schema/range/isfinite; env числа также парсятся на import | P1 | FIX: Validate schema/ranges/finite values; локализовать повреждённый record и не маскировать его пустым state | G1; safe fallback policy | HIGH / OPEN |
| F08 DATA INTEGRITY | Нет проверки непрерывности и полноты candles; код 918–961; AC-03-04 | EV02 gaps FAIL: часовой gap допустим для 1m composite | P1 | FIX: Gap tracker/ordered candle validation; блокировать affected detector, REST backfill и пометка completeness | G2 ingestion | HIGH / OPEN |
| F09 FRESHNESS | Нет event timestamps/TTL; closed определяется только local clock; код 919–945; AC-05-04 | EV02 freshness FAIL: суточные candles дают сигнал; legacy follow-up #1 server time sync | P1 | IMPLEMENT: Type/TF freshness, market/detected/received time; clock drift diagnostics/WS confirm; проверить VPS time sync | G2; B03 TTL policy | HIGH / OPEN |
| F10 UNIVERSE | Fixed100/одна category, eligibility без metadata; код 899–913, 987–1009; AC-02-01..03 | EV02 universe-eligibility FAIL: ETHBTC/unknownUSDT попали в scan; нет instrument status/type фильтров | P1 | IMPLEMENT: Широкие раздельные Spot USDT/USDT Perpetual universe по metadata/liquidity и полной pagination; safe unknown type exclusion | G2; Bybit metadata contract | HIGH / OPEN |
| F11 INGESTION | Нет WS и одновременно работающих TF; код 963–1027, requirements; AC-03-01/02 | Только REST kline, одно signal_timeframe per-chat | P1 | IMPLEMENT: Shared owner ingestion, subscriptions обоих рынков и 1m/3m/5m/15m, REST bootstrap/recovery | G2 после G1 | HIGH / OPEN |
| F12 DETECTORS | Реализован только legacy composite; код 918–961; AC-04-02/03 | Отсутствуют impulse/spike-alone/breakout/volatility/versioned event contracts | P1 | IMPLEMENT: Versioned deterministic detectors с metrics/units/direction/provenance и fixtures | G2; B03 formulas/defaults | HIGH / OPEN |
| F13 EPISODES | Cooldown без event identity не заменяет episodes; код 2090–2105; AC-05-01..03 | EV02 repeat-same-candle FAIL; baselines содержит price/ts/last_alert, нет candle/event/revision ID | P1 | IMPLEMENT: Episode lifecycle/correlation/re-arm/max lifetime и meaningful notification policy, исходные events сохранить | G2; B03 | HIGH / OPEN |
| F14 DELIVERY | Failed Telegram send расходует cooldown без retry; код 2101–2120; AC-07-01/05 | EV02 failed-delivery-cooldown FAIL: last_alert обновлён до send, следующий poll не повторяет | P1 | FIX: Отдельное delivery состояние/pending retry; detector/history не зависят от Telegram успеха | G2 delivery; B03 retry policy | HIGH / OPEN |
| F15 ANALYTICS | Нет event history, outcome worker, analytics/export; state 331–450, весь tracked inventory; E06 | Отсутствуют persisted events/revisions/episodes/outcomes, cohorts/MFE/MAE/export | P1 | IMPLEMENT: История с settings snapshots и immutable observation/reference; horizons/outcomes/export с completeness и no future leakage | G3 параллельно G4; B03 methodology | HIGH / OPEN |
| F16 OWNER SETTINGS | Market domain зависит от chat; profiles/manual settings не реализованы; код 302–399, 1084–1133, 1759–1795; E08/AC-02-04 | Settings вложены в chats; один market/TF, кнопки отдельных числовых presets | P1 | IMPLEMENT: Persisted owner domain config и отдельные delivery preferences; profiles плюс manual validated parameters | G1; B03 legacy owner mapping | HIGH / OPEN |
| F17 INTEGRATION | Нет advisory API/автоматической передачи trading agent; все entrypoints/dependencies; E09 | Нет HTTP server/webhook/outbox/consumer config, IDs/auth/replay/delivery diagnostics | P1 | IMPLEMENT: Согласовать contract и authorized idempotent delivery/replay; joint consumer tests; не ждать calibration | G4 после G2; B02 | HIGH / OPEN |
| F18 DIGEST | Нет periodic digest и независимых delivery toggles; код 2171 и handlers; AC-11-01/02/07-05 | JobQueue только poll_engine_job; digest state/aggregation отсутствуют | P2 | IMPLEMENT: Digest из общей history/episodes обоих рынков; interval и separate alerts/digest controls | G3 после history | HIGH / OPEN |
| F19 ENRICHMENT | Нет derivatives context для Spot; весь fetch layer; AC-11-03/04 | Нет OI/funding/basis adapters или надёжной market/asset mapping | P2 | IMPLEMENT: Optional enrichment с source/market/units/time, независимым cadence и nonblocking failure | G5 после G2 | HIGH / OPEN |
| F20 OBSERVABILITY | Symbol errors/timeouts скрываются как отсутствующие данные; код 963–1023; AC-04-04/12-03 | except Exception→None; timeout cancels pending без error summary; status показывает нет данных, без gap/source reason | P1 | FIX: Reasoned degraded diagnostics по market/detector/universe/gaps и доставкам; не показывать no-signal при missing data | G2; secret-safe error handling tests | HIGH / OPEN |
| F21 CONCURRENCY | Settings могут смениться во время fetch, old result доставляется как active; код 2068–2112, status_cmd; AC-07-04/08-02 | EV02 settings-revision-race FAIL: threshold вырос до99, old signal отправлен; UI заново читает current settings | P1 | FIX: Owner config revisions и snapshot provenance; согласованная apply/discard policy перед notification/status | G1/G2; не переписывать concurrent updates | HIGH / OPEN |
| F22 ARCHITECTURE/PERFORMANCE | Monolith и per-chat REST fanout мешают расширению; telegram_crypto_watcher.py 2177 строк; AC-12-03 | Два pools 4/12; ~1 ticker+100 klines на scan/chat, без shared snapshot; awaits Telegram при state lock; legacy follow-up #2/#5 | P2 | REFACTOR: Разделить ingestion/domain/state/render/delivery, общий owner dataset, измерять backlog/cadence/rate budget; load tests по реальной нагрузке | G1/G2; текущая performance/VPS limits UNSET | HIGH / OPEN |
| F23 LEGACY | CMC/source/core/reset действия остаются достижимыми вне v1; код 536–874, 1583–1643, 1798–1881, 1967–2041, 2163–2168; AC-01-03 | Commands resetbase/resetallbase зарегистрированы; старые inline callbacks src/core/cmctop поддерживаются. CMC main poll отключён, но пути живы | P2 | DEPRECATE: После state compatibility review убрать superseded UX/network paths; baselines.last_alert не удалять как якобы полностью dead | Goal cleanup после G1; D04/Q06 | HIGH / OPEN |
| F24 ORPHAN CANDIDATES | Невызванные legacy helpers/caches; внешние consumers неизвестны; код _fetch_async/_prepare_core_context/tracked_desc/price_unit/_mode_label/_rank_prefix/_sub_metrics/_directional_marker | AST+rg и регистрация entrypoints: прямых callers нет; executor/handler functions исключены из кандидатов; legacy follow-up #3 | P3 | DEFER: Проверить историю/внешние imports/старые callbacks; затем REMOVE подтверждённых helpers либо DOCUMENT compatibility; imports alone недостаточно | Q06; external Python consumers UNSET | MEDIUM / OPEN |
| F25 TEST GAP | Нет tracked воспроизводимой regression suite; EV01 tracked inventory; E01/E03/E08/E10/E12 | Есть только assertions import token и исторические ad-hoc PR18 проверки; tests/config/fixtures отсутствуют; legacy #4 | P1 | IMPLEMENT: Минимальные auth/migration/config/composite/recovery tests в существующем Python stack; CI запускает meaningful suite | G1; external audit scripts не заменяют suite | HIGH / OPEN |
| F26 DEPENDENCIES | Нет lock/hash constraints; разрешены уязвимые requests releases; requirements.txt:2–3; Dockerfile:1,9 | requests>=2.31.0/python-dotenv>=1.0.0, unpinned base/pip; EV04 current18 packages: advisories=[]; 2.31.0 имеет GHSA-9wx4-h78v-vm56/GHSA-9hjg-9r4m-mvj7 | P2 | FIX: Выбрать проверенный reproducible manifest/lock и safe bounds; audit точного Docker artifact. Не утверждать vulnerable package на VPS без inventory | G1; exploitability по fixed Bybit URLs не доказана | HIGH / OPEN |
| F27 CI/CD | Нет Actions workflows/checks/Environments/deployments; .github отсутствует; GitHub primary records; AC-12-02 | EV03 workflows/runs/environments/check_runs/deployments=0, rulesets=[]; main protected=false; branch protection details 403 | P1 | IMPLEMENT: В выбранной Goal минимальный CI в первом code PR; определить artifact, gates, CD и recovery по приложенным правилам | G1; B01; audit pipeline не меняет | HIGH / OPEN |
| F28 OPS EVIDENCE | Работа VPS заявлена владельцем, runtime revision/gates не установлены; Contabo target по S04; AC-12-01/02/10-04 | Host известен; SSH account/deployment unit/path/backup/running version/health/time sync UNSET; GitHub deployment records отсутствуют | P1 | DOCUMENT: Read-only инвентаризация через согласованный доступ, зафиксировать target/unit/config owners/version/queue/recovery/post-checks | B01; deployment не запускать ради статуса | HIGH / OPEN |
| F29 CONFIG/UX | Env category/runtime knobs могут противоречить Spot UI и raw input; BYBIT_CATEGORY, settings_text/status_text/alert_keyboard, .env.example | Env category unrestricted, UI/link всегда Spot; .env example не описывает many legacy knobs. env default SIGNAL_TIMEFRAME и state_contract фиксированный5m могут расходиться | P2 | CONSOLIDATE: Явная market identity и validated current config; убрать конфликтующие legacy settings после compatibility review, синхронизировать UI/contract | G1/G2 и F02; actual VPS env UNSET | HIGH / OPEN |
| F30 BUILD HYGIENE | Docker context не исключает data/ и .git;  .dockerignore, Dockerfile | Ignore содержит .env/state file, но не data/ и .git; Dockerfile копирует только manifest/code, поэтому inclusion в image не доказан | P3 | FIX: Исключить runtime data/Git из build context для уменьшения context и риска передачи builder; verify context/image artifact | G1; deployment build location UNSET | MEDIUM / OPEN |
| F31 HARDENING | Docker runtime использует root default; resource/health policy UNSET; Dockerfile/Compose; AC-12-01 | Нет USER/healthcheck/resource settings; фактические VPS user/resource limits не проверены | P3 | DEFER: После runtime inventory выбрать минимальные privileges/health/resources по необходимости; отсутствие инструмента само по себе не defect | B01; не превращать hardening в новый AC автоматически | MEDIUM / OPEN |
| F32 UX CANDIDATE | Legacy polish/preset strategy без конкретного нового дефекта; прежний open_issues #6/#7; E07/E08 | Следить за едиными русскими terms/threshold explanations; visual quality/чувствительность требуют user feedback/statistics, не подтверждены как bug | P3 | DEFER: Сохранить полезные terms/presets; улучшать объяснения и выбор thresholds по согласованным detector profiles и outcome statistics | G3 calibration; Q01, не откладывает agent integration | MEDIUM / OPEN |

### План консолидации документов/кода

- Сохранить existing product_scope/open_issues как canonical spec/plan; scope этого docs PR только эти два файла. Исправление F01 подтверждается diff/readback после merge.
- README сохранить для назначения/quickstart/navigation; при выбранной Goal заменить устаревший продуктовый scope кратким актуальным описанием и относительными ссылками, сохранить полезные commands. F04 — конкретная поломка quickstart, не косметическая переформулировка.
- State contract сохранить как техническое уточнение до implementation owner schema, затем перенести ownership/schema/migration в актуальный contract с index из spec; legacy часть при необходимости архивировать. Не удалять полезные empty-list/raw-state/backward-compatibility правила.
- Router адаптировать из комплекта к existing filenames; architecture/runbooks создавать при соответствующем реальном содержимом, не пустые placeholders.
- В прежнем open_issues старые deployment checks сохранены как risks/Validation Plan: migration/status/no-op/parse/startup/poll/TF/thresholds/truncation. Их прежнее наличие не доказывает PASS.
- Superseded Top100/single-TF/Spot-only/CMC/reset surfaces связаны с D01–D04. CMC/reset доступны, поэтому DEPRECATE, а не «dead code». Невызванные helpers остаются DEFER кандидатами до external consumer review. Operational baselines.last_alert используется и не подлежит слепому REMOVE.
- Затронутые ссылки/упоминания: README documentation list/source hierarchy/project structure/state ownership; state_contract sections2–12; module docstring/boot log/help/settings/status/command registry. Исправления за пределами двух canonical docs ожидают выбранной Goal.

## Blockers / ограничения доступа

| ID | Что неизвестно / блокирует | Что нужно для снятия |
| --- | --- | --- |
| B01 | E12 runtime delivery; только host Contabo известен. SSH account/procedure, target unit/path/config owners/artifact/queue/protections/recovery/version/post-checks UNSET. Docker локально отсутствует | Read-only доступ и подтверждённая project delivery процедура; runtime Goal не DONE без required build/CI/CD evidence |
| B02 | E09 contract и joint tests: main trading agent repository/endpoint/auth/schema/consumer scope UNSET | Назвать repository/contract и разрешённую границу совместной implementation/validation |
| B03 | SPEC Q01/Q03/Q04: detector/episode/freshness/outcome policies и legacy owner mapping | Существенные решения владельца или согласование конкретного reviewable design в соответствующей Goal; без выдуманных чисел |
| B04 | Детали GitHub branch-protection admin settings не читаются интеграцией; gh token invalid | Для privileged настройки нужны admin access/CLI authorization; audit использует visible main protected=false и empty rulesets/checks. Не ослаблять/обходить неизвестные gates |

## Audit quality review

Полный S01 прочитан, все его обязательные разделы покрыты 12 эпиками/58 AC; дополнительно проверена обратная трассировка reachable legacy CMC/reset и старого UX. Scope расширен требованиями владельца, не новыми findings. Generated/vendor области отсутствуют в tracked inventory. Runtime data/cache/logs/backups отдельно: initial untracked/ignored inventory пустой; временные audit artifacts вне repository.

Проверены false positives: settings/no-op/legacy raw migration работают в обозначенных fixtures; CMC не назван активным main poll; отсутствующий Docker/tool сам по себе не баг; context data/ не доказано включённым в image; requests advisories не означают vulnerable installed VPS; timezone Compose не признан дефектом без требования; callbacks/executor functions не названы orphan только из-за отсутствия прямого вызова.

Confidence: HIGH по code/fixture-backed gaps и inventory; MEDIUM у orphan external usage/hygiene/optional hardening/visual polish; LOW production correctness не оценивается без records. Возможные false negatives: реальные stream/rate/Telegram parsing/time sync/concurrent process behavior, credentials in third-party exception logs, Docker3.11 startup, disk/backup/actual installed dependencies/VPS resource pressure не проверены. Весь READY ограничен baseline и описанными checks; missing live smoke не замаскирован PASS.

## Checkpoint и следующий шаг

На момент последнего содержательного commit: spec/plan подготовлены; baseline7/58, все findings сохранены. Local docs validation/self-review и exact diff/remote recheck должны завершиться до initial push. GitHub gates latest head и merge остаются PENDING до primary records. После docs merge: fetch main, safe fast-forward, сверить ровно два docs changes, удалить только собственные branches после safe deletion; CD N/A для docs. Итоговые PR/head/merge/gates восстанавливаются из GitHub/Git, без нового commit ради metadata.

Следующий шаг после audit delivery — владелец выбирает G1 либо иной набор AC/findings. Implementation, pipeline и production changes пока не поручены. Процент повторно пересчитывается только новым полным AUDIT; во время будущей Goal показывать AC progress и обновлять Evidence без нового процента.
