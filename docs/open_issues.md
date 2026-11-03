# CryptoWatcher — delivery plan

## Current Goal и утверждённая последовательность

**INTERFACE-ACCEPTANCE — Goal 2 ACTIVE: разрешены ограниченный prototype global detection + bounded analytics pool, публикация в Draft PR94 и один Linux-прогон31Oct2026; после результата остановиться**, явно поручена владельцем в последовательности30Oct2026; встроенная Goal2 активирована после выполненной Goal1. CAPACITY25 отменена и не возобновляется. Новая параллельная optimization chain, cron/automations и Codex Security запрещены.

Baseline origin/main12cf5d48e8d4b13aa77dbbe84ee9d161f81a4619. Branch codex/interface-acceptance-20261030; существующий checkout .cache-g1/git-recovery-20261024. Fresh Git/remotes/worktrees/protections/openPR проверены: strict ci-required15368/admins, approvals0; чужой draftPR57 сохранён. Goal1 task branch безопасно удалена после merge/CD и exact-tree proof; preexisting checkout/audit/two-writer материалы сохранены.

### Результат, scope и DoD Goal2

Довести согласованный остаточный scope: финальная AC-13-06; регрессия17AC, завершённых Goal1, и прежних инвариантов; F22/F34/F38/F42, F24/F31/F32, UX-001–UX-041 и применимость всех связанных engineering/security IDs ниже. F26/F49–F56 реализованы и прошли required CI/CD Goal1; применимые пользовательские сценарии повторно проверяются визуально. Каждый оставшийся finding получает подтверждённый итог FIX/REMOVE/CONSOLIDATE/сохранён как используемый/обоснованно неприменим; новый DEFER не закрывает scope. Проценты проекта/эпиков не пересчитывать; audit snapshot ниже исторический.

Результат: свежий визуальный аудит доступного Telegram клиента и bot-controlled исправления, объяснимый unified forecast для фактически работающей модели, измеренный ресурсный бюджет CW с запасом соседям, representative multiGB backup/restore и полный physical peak, ограниченное хранение серверных артефактов,30min/24h приёмка после последней поставки на неизменных source/settings/history generation. Один основной PR, required CI/review, merge/CD; необходимые hotfix в том же scope. DONE только по фактическому результату всех этих gates.

Non-goals: новые функции/прибыльные thresholds, receiver/TA/Paper изменения, двух-writer реализация, увеличение VPS, изменения соседних проектов, самостоятельный полный no-CD regression run. Валидную light history5bb5712dd7144032941f5d7f72766522 повторно не сбрасывать. Working set со служебными файлами≤35GB; physical backup/temp/VPS reserve считаются отдельно. Никакого общего Docker prune. Старые backups/unique experiments/archives — только после нового точного согласованного списка и проверенной замены.

### Текущий checkpoint — разрешённый bounded-pool prototype31Oct2026

Владелец отдельно разрешил ограниченный этап плана b692fb2: минимальную реализацию,
публикацию в существующий Draft PR94 и один Linux-прогон с прежними лимитами, затем
остановку с результатом. Full integration/merge/CD/VPS/рабочая БД не разрешены.
Выбор направления/прежняя плановая блокировка сняты этим поручением; новая Goal
не создаётся. Встроенный tool пока возвращает прежний BLOCKED и не имеет resume
API; явное поручение разрешает текущий этап, завершение полной Goal не заявляется.

Реализовано только в isolated tests: durable pool64/shared manual16/warming4,
rate8/min (4/min/market), waitlist128/128KiB; admission перед OutcomeWorker.admit,
глобальные5detectors/4TF/events/episode updates и обе доставки не отключаются.
SQLite transaction связывает selection/reservation и настоящие outcome jobs.
Reference immutable; auto-selection не пишет owner settings/watchlist. Existing
jobs сохраняются и блокируют новый admission до drain; closure не снимает слот
с незавершёнными outcomes. TTL selection30d, finite source/queues/maintenance прежние.
Минимальное изолированное publication storage из предыдущего prototype переиспользуется,
production activation отсутствует; полноценный typed core не переносится.

Независимый ungated engine/oracle сравнивает count/точные market values/types,
metrics/strength/episode reference и source-data hashes, без разных scheduling
receipt/server timestamps; SHA каждого реально записанного input window проверяется
отдельно. Original full seed/offered market hashes сохраняются; outcome population
намеренно ограничена новым scope. Один настоящий global metadata/ticker refresh
добавлен в measured phase и явно отличается от прежних0calls. Подробный mature
forecast и давление заполненного пула не объявляются PASS без нужных данных.

Local18focused PASS25.33s: bounded_pool5 + existing processing_qualification6 +
sqlite_repository7; Windows3.12 fallback, не Linux/native proof. Включены real
SQLite/outcome/delivery small8s, saturation64 без eviction, close→draining→реальный
COMPLETE→release, immutable reference/reopen/rollback/private unchanged и legacy
jobs unchanged/block admission. Canonical quality PASS: docs/Ruff/format349files/
mypy133source. Native/capped result PENDING; existing required CI не ослаблены.

Следующий шаг: один grouped publication этого полного isolated scope в PR94,
один candidate job в существующем PR workflow (заменяет старый prototype job,
не добавляет ещё один). Existing mandatory CI остаётся; результаты/расходы
раздельны. Сохранить evidence внеGit, проверить cleanup и остановиться независимо
от результата. Rerun/расширение/new optimization chain автоматически не разрешены.

### Действующее уточнение scope и ближайший gate31Oct2026

Основание: владелец принял ограничение подробной аналитики при сохранении всех пяти
детекторов на четырёх TF по всему допустимому рынку, затем поручил обновить план
Goal2, показать одну ограниченную сквозную проверку и критерии приёмки. Первоначально
разрешались только план и локальный commit; последующее отдельное разрешение
на bounded prototype/publication/one Linux run записано в checkpoint выше.
Полная интеграция, поставка и операции VPS по-прежнему не разрешены. Встроенная Goal2 возобновлена владельцем.
Блокер выбора полноты outcomes снят; отдельное явное поручение на единственную
проверку уже получено, его границы записаны в текущем checkpoint. Остаточный scope/F/UX/AC и окончательный DoD выше сохраняются,
кроме прямо согласованного отбора подробной аналитики, описанного здесь.

Новая граница для плана имеет приоритет над прежним предположением этого файла,
что каждый новый глобальный episode обязательно получает outcomes. Product spec,
AC IDs/denominator и historical READY сейчас не переписываются. При разрешённой
интеграции синхронизировать затронутые формулировки spec/AC с этим решением,
сохранив IDs и old→new связи; до проверки нового поведения старый PASS не переносить.

| Уровень | Обязательная работа и данные |
| --- | --- |
| Глобальный | Весь eligible Spot USDT и USDT Perpetual, native1m/3m/5m/15m, пять поддерживаемых detectors по active settings, прежние формулы/пороги/precision, source freshness/gaps/recovery; condition/re-arm, immutable episode origin и все meaningful updates независимо от пула |
| Сохранение вне пула | Компактный signal/episode record, metrics/thresholds/settings/provenance и необходимые использованные inputs для объяснения сигнала; причины/время отбора либо отсутствия сопровождения, независимые delivery statuses. Общие свечи72h, подробности/used inputs30d, агрегаты12calendar months; не полные исследования каждого свечного repeat |
| Доставка вне пула | Telegram по enabled families/mute; Agent по его независимым settings, существующий полный v2/HMAC/idempotency/ACK/retry/freshness. Отсутствие места не подавляет сигнал. Недоставленное/просроченное явно видно в истории; expiry не превращает старый сигнал в свежий |
| Подробный пул | Один набор настроенных outcomes/MFE/MAE от исходного episode reference, дополнительный context и подробные результаты. Повтор, обновление, admission или restart не сбрасывают reference/horizons. Поздний admission явно виден, catch-up тоже расходует бюджет |
| Аналитика | Все обнаруженные уникальные episodes и отобранная outcome population учитываются раздельно; coverage/selection reasons/bias явны. NOT_SELECTED не равно нулевому результату, pending, MISSING_INPUT или COMPLETE; digest/history/export не скрывают сигнал вне пула |

Публичные commands/callbacks и форма Agent v2 сохраняются. Пустой список результатов
не выдавать за рассчитанные нули. Selection fields нельзя незаметно добавлять в
закрытый settings object v2; точную совместимую проекцию проверить до интеграции.
Автоотбор хранит собственное состояние/revision, не изменяет owner settings,
manual watchlist и связанные sensitivity multipliers при каждой ротации.

#### Пул и конечность работы

- Единица бюджета — обязательство сопровождения episode, а не только symbol.
  Market входит в identity. Несколько episodes одной монеты расходуют отдельные
  обязательства; общие данные можно разделять без двойного счёта результатов.
- Для единственной проверки предлагается фиксированный K=64 обязательства,
  включая ACTIVE, WARMING, manual pins и DRAINING с незавершёнными outcomes.
  Это параметр изолированного эксперимента, не обещанная production capacity.
  WARMING≤4, manual≤16, минимум48 мест не занимают manual pins;
  admission≤8 новых обязательств/min без накопления токенов сверх8.
  Значения и алгоритм фиксируются до запуска, не подбираются по результату.
- При admission резервировать все четыре выбранных горизонта и необходимые
  inputs; в fixture5/15/30/60min максимум256 horizon jobs для64 новых obligations.
  Активный episode не вытеснять; после closure слот освобождается только после
  выполнения/конечного terminal результата обязательств. Future не overdue,
  но входит в резерв. Зависимости освобождаются по действующим срокам.
- Ranking детерминированный: strength относительно frozen thresholds, новые
  подтверждения detector/TF, freshness и liquidity; представительство обоих
  рынков, фиксированный tie-break. Минимальное удержание/порог замены исключают
  churn; свободный либо завершённый slot заменяется, активный pin — нет.
- Если бюджет заполнен, сигнал сохраняется и доставляется без outcomes.
  Кандидатный waitlist конечен по count/bytes и freshness, не создаёт jobs заранее;
  истечение ожидания явно NOT_SELECTED, не бесконечное pending. Ручные pins
  не расширяют K и не вытесняют уже принятые обязательства.
- Pending/unknown deliveries имеют собственные конечные count/byte/expiry bounds
  и не скрываются за пустой send queue. Backpressure не даёт тихо потерять
  подтверждённый signal; перегрузка и невозможность доставки — видимый FAIL/gap.
- Existing valid episodes/jobs/deliveries не удалять ради K или PASS. Переходный
  долг учитывается отдельно и в суммарных ресурсах; пока он превышает budget,
  новые admissions закрыты. Такой режим нельзя объявить устойчивым без drain.
- K не ограничивает накопление за30d: отдельно считать число admissions/day,
  их retained inputs/updates/outcomes и глобальный signal rate при ротации.

#### Одна ограниченная сквозная проверка — только после разрешения реализации

1. Подготовить минимальный изолированный путь на реальной SQLite, используя
   существующие ingestion, формулы и delivery boundaries. Не переносить все
   подсистемы и не менять process/storage core целиком. Проверяемый путь:
   closed candles → глобальные5detectors/4TF → meaningful episodes → durable
   signal и selection → outcomes принятых episodes → Telegram и подписанный
   Agent → retention/checkpoint/recovery. Вне пула обязательно проходит та же
   signal delivery; её стоимость не выносится за пределы измерения.
2. До capped run зафиксировать manifest: revision, seed/offered SHA, K/все
   queue/rate/byte limits, rank/tie-break, исходные обязательства и expected
   global signal sequence. Использовать прежние1648streams/2472closed arrivals
   за130s, оба рынка/всеTF; не растягивать поступление ради PASS. Market inputs
   и необходимые seed candles прежние. Изменённая outcome population — явное
   следствие scope, не требование повторить824старых outcomes/hash всех рынков.
   Для новой одноразовой fixture начальное распределение selected outcomes
   строится по новой policy на тех же market inputs и полностью раскрывается:
   это изменение workload по scope, не ускорение идентичной работы. Global
   signal/delivery population не сокращать. Существующие production jobs не
   переносить/удалять; сохранность legacy obligations отдельно проверить маленькой
   compatibility fixture, включая закрытие admissions при превышении budget.
   Уже созданные обязательства нельзя удалять ради PASS; reference не causal A/B.
3. В тот же ограниченный сценарий включить filled pool, manual/auto competition,
   warming limit, meaningful confirmation, slot release, необходимые future/due
   horizons, retry/unknown ACK и restart идемпотентности, а также maintenance.
   Недостающие переходы проверяются маленькими correctness fixtures до него,
   без второго полного performance run. Ticker/discovery refresh и удержание
   данных не исключать; если добавлены к старому расписанию, отдельно указать
   стоимость и отличие. В прежнем130s case эти refresh имели0calls.
4. Единственный Linux/native capped run:1CPU СУММАРНО,4GiB/no swap,
   450CPU-s/510wall/600outer; idle admission≥35% в течение30s, stop<15%,
   RAM reserve1.5GiB, исходные disk guards. Все scanner/selector/writer/workers/
   delivery/maintenance и drain входят в суммарные ресурсы/время. Окно arrivals130s,
   последующий ограниченный drain внутри того же общего предела; послезапусковый
   drain не стирает опоздания внутри окна. Никаких VPS операций или рабочей БД:
   CI/isolated Linux, одноразовые test credentials, внешние transports mocked,
   настоящие v2 serialization/HMAC/ACK и SQLite. Запуск CI отдельно согласуется;
   подготовка плана не разрешает publication или расходы Actions.
5. Сохранить report/manifests/hashes и сопоставление с run41220369584 внеGit:
   accepted, global signal recall/sequence, source lag max/p95/time series,
   completed/oldest due age, future counts, selection coverage/reasons/churn,
   обе стадии delivery queues и ACK latency, total CPU/RAM/block I/O,
   DB/index/free pages/WAL/SHM/temp peak, maintenance timings/counts.
   Учитывать seed/active/drain отдельно и итог вместе. Прежние source75.792s,
   due98.042s, phase108.240CPU-s, DB+sidecars197220720B — reference, не новый PASS.
6. После единственного результата остановиться. При FAIL/INVALID — measured
   причины/ограничения и варианты владельцу, без нового prototype или автоматического
   rerun. При PASS — показать результат и минимальный состав интеграции для
   согласования; не начинать большую переработку/merge/CD автоматически.

#### Критерии приёмки ближайшего gate

| Gate / связь | Обязательное подтверждение |
| --- | --- |
| V1 Global correctness / F22,AC-04/05 | Accepted2472/2472; все ожидаемые global signals/meaningful revisions и immutable origins совпадают с frozen full-market reference, включая сигналы вне пула; нет пропусков, double episodes или повторных исследований свечей |
| V2 Bounded selection / F22,F42 | K и все warm/manual/admission/job/queue bounds соблюдены во времени, не только на endpoint. Есть реальный admission, заполнение, отказ новым candidates, release/replacement и рассчитанные due outcomes выбранных episodes: пустой пул или отказ всем не PASS. Нет скрытого retiring хвоста; полное заполнение не гасит global detection/delivery. Existing obligations сохранены/учтены; переходный долг не маскируется |
| V3 Outcomes / F49–F56 regression | У каждого принятого episode ровно один набор; значение/reference/float precision/provenance совпадают с reference для этого же episode и inputs. Не выбранные не запускают outcome jobs. Future отдельно, неполные inputs/expiry/terminal явно; restart/retry не меняет origin |
| V4 Timeliness / F22,F42,AC-13-06 | Source max≤30s; каждый наступивший расчёт при доступных inputs≤60s, включая завершённые с опозданием. Нет растущего overdue хвоста. Нулевой endpoint backlog и средние значения не заменяют эти условия |
| V5 Delivery / F31,AC-07/09 | В нормальном fixture все разрешённые свежие global signals, включая вне пула, доходят до mockTelegram/signedAgent в действующем freshness, без нормального истечения из-за очереди. Failure cases имеют корректные retry/unknown/terminal states. Все intents/outboxes учтены, каналы независимы; это не live Telegram/receiver proof |
| V6 Resources / F31,F34 | Полный поток соблюдает1CPU/4GiB/450CPU-s/510wall/600outer и guards; есть aggregate CPU/RAM/I/O/physical peak, а не только цена метода. Reporting не скрывает setup/drain/maintenance. Отказ начального допуска — NO_RUN; guard stop после старта — INTERRUPTED/FAIL, не PASS |
| V7 Storage / F34,F38,F42 | Единый расчёт: general72h + global details/used inputs30d + selected details30d + global/selected aggregates12mo + indexes/free pages/journals/legacy debt; ротация/admissions/day и signal bursts явны. Shared inputs/страницы учитывать один раз, без двойного суммирования global и selected. Рабочая сумма≤35GB при заявленных assumptions и отдельный backup/temp/shared-VPS physical reserve; неизвестные rates или short DB size не дают mature capacity PASS |
| V8 Cleanup/evidence | Actual FK/integrity, rollback/restart/retention и immutable public/numeric checks подходят к затронутому риску; report сериализован/полон, own temporary DB/container удалены с readback, стоимость CI и ограничения отчёта раскрыты |

Успешный первый gate означает только жизнеспособность ограниченного сценария.
Если workload/обязательные показатели не покрыты либо forecast требует неизвестных
rates, результат PARTIAL/UNPROVEN, а не разрешение полной интеграции. MultiGB restore,
полный physical peak зрелой базы, shared-VPS влияние и неизменные30min/24h остаются
обязательными поздними gates Goal2, не нагружают первую проверку и не считаются PASS.

#### Экономия и последующие шаги

Глобальные4TF feeds/candles, пять detectors, condition/re-arm, значимые signal
records/used inputs и обе доставки остаются. Вне пула исчезают только новые
outcome/MFE/MAE jobs, дополнительный context и соответствующие результаты/maintenance;
новый selection accounting добавляет работу. Прежние76% относились к другому
1m scout варианту и здесь неприменимы; observation69.067CPU-s из108.240CPU-s нельзя
целиком объявить устраняемыми. Проценты CPU/I/O/GB до результата не обещаются.

После PASS и отдельного решения: только минимальная интеграция глобального пути,
selector/budgets, outcome admission и совместимых history/analytics/digest/export/
Agent projections, preserving valid history/private state. Затем остальные прежние
F/UX, required CI/review, один основной PR94/merge/CD, representative backup/restore,
physical capacity и30min/24h. При FAIL дальнейшая достройка прекращается. Старый план
полного typed core A–D и оценка8–15дней ниже — архивная отвергнутая к автоматическому
исполнению альтернатива, не текущий roadmap и не разрешение миграции.

Плановая проверка31Oct: main12cf5d4 совпадает с origin/main; PR94 OPEN/DRAFT,
remote head12ac4b8, local pre-plan7ba6d89, divergence0behind/8ahead; unrelated changes
нет. Свежий protection read403 (integration), protected main подтверждён branch API;
прежние strict ci-required15368/admins/approvals0 — historical snapshot, не новый PASS.
Документ остаётся локально; перед будущей публикацией повторно проверить gates.
Последующее поручение на реализацию только bounded gate получено; исполнение и ограничения — в checkpoint выше.
Локальная проверка только документа: `python scripts/checks.py docs` и
`git diff --check` PASS. Application tests/performance/CI/VPS не запускались;
self-review ограничен изменением плана, локальный commit без публикации.

### Последовательность и Validation Plan Goal2

| Блок / finding / риск | Проверка и ожидаемый результат | Команда/среда/этап | Gate |
| --- | --- | --- | --- |
| Прогрев и full path / F22,F42 | Разделить source bootstrap, уже наступившие расчёты, referenced recovery и future horizons; проверить динамику/inputs/terminal expiry. Source grace30s, due calculations60s при доступных inputs | Bounded readonly diagnostics/queue snapshots через contabo-vps, существующий observer; до baseline и тяжёлых операций | REQUIRED |
| Forecast / F38,F42 | Actual episode/origin/membership/source usage rates и physical pages/journals, без startup churn или N20; единая арифметика с явными limits/reserves | Existing storage_meter/storage_model/capacity; targeted tests, representative isolated writer/retention scenario; steady VPS interval | REQUIRED |
| Telegram / F32,UX | product-design:audit: fresh captured/screenshotted actual flow, navigation/empty/error/confirmation/cancel; desktop и доступная узкая компоновка. Mobile app не заявлять | Native Telegram available; capture evidence внеGit. Bot-only formatting/copy fixes; existing UI/UTF16/control tests | REQUIRED |
| Cleanup / F24 и связанные IDs | Caller/script/contract/recovery inventory; доказанный REMOVE, полезное сохранить с основанием, пересечения CONSOLIDATE. Source security regressions без provider scan | rg/current source/tests/primary records; scoped local checks, current CI | REQUIRED |
| Hardening/resources / F31 | UID/caps/readonly roots и фактический суммарный CPU/RAM/I/O, ordinary против recovery/repack/backup; бюджет с запасом другим проектам | Bounded Contabo/Grafana snapshots;30min/24h, attributable cgroups и whole-host metrics | REQUIRED |
| Backup/capacity / F34 | Representative multiGB full integrity/independent restore/exact byte/numeric/private equality, actual time/resources и полный disk peak до старта | Canonical backup/qualification procedures; owned isolated data only.1CPU/4GiB/450CPUs/510wall/600outer;35%idle admit/15%stop, existing RAM reserve | REQUIRED |
| Artifact retention | Latest2verified backups, current+previousgood release; rehearsal after saved proofs/repro, failed capture until investigated/replaced; exact lists for old unique data | Fresh metadata inventory/replacement/recovery requirements; owner decision only where needed, late with concrete list | REQUIRED |
| Code/release | quality и нужные профильные checks; Linux/native/image required CI на готовом PR; actual merge artifact/source/private/health/smoke | Canonical checks.py quality, existing PR CI/CD. Standalone full workflow не запускать автоматически | REQUIRED |
| Финальная приёмка | Закончить тестовые действия, принять baseline;30min затем24h unchanged version/settings/generation, сохранение interruptions, source/due/delivery/resource/reserve | Existing observer вручную по процедуре, без scheduled tasks/Actions monitoring | REQUIRED |

Сначала выявить системный риск и проверить полный эффект, затем достроить подтверждённые интерфейсные/остаточные исправления и единственный основной PR. После новой модели и одного обоснованного исправления измеренного ограничения сохраняющееся общее отставание требует готовой ветки/сопоставимых measurements/вариантов владельцу; бесконечные локальные micro-optimizations не продолжать. Ordinary failures исправлять автономно в текущем scope. Независимую работу продолжать при недоступном UI/внешнем gate; конкретное согласование выносить поздно, с готовыми доказательствами и действием.

### Предыдущий checkpoint — ограниченный SQLite prototype31Oct2026, FAIL

Последний результат: **FAIL, развитие prototype остановлено по условию владельца**.
После reporter-only fix и отдельно разрешённого одного повтора: head12ac4b8006a632d4ef8d673c857ce7da298cd189,
test merge264bab07da6e8a069aa303e045fbe4f402370003, run41220369584/job124593507395,
artifact13049339407/SHA cf763de71825e17962e6eda6562e74f243fa8c4a3e6002fba1593d690f67e9c7.
1648streams/2472accepted/130s, same seed+offered SHA,824complete outcomes с одинаковым
numeric SHA. Native bothmarkets/4TF/5families; errors0/FK violations0. Source max75.792s
(p9570.803) >30s; completed due max98.042s >60s. Due endpoint0 не отменяет late completion;
future10352 не backlog. Telegram468mock attempts, agent196signed mock ACKs. Pending TG285
(age40.608s), agent intent2958(age428.015s, включая seeded T−5min), send0. Live delivery
не проверялась; фактические очереди доставки не объявлять устойчивыми.

Сохранённое сравнение run41196967614: source92.373s/due118.044s, phase119.576CPU-s;
теперь108.240CPU-s. Working after join97.276MB→197.221MB; sampled cgroup memory.peak
197.595MB→293.716MB; sampled block writes4.079GB→4.535GB по одному device каждого host.
Разные CI hosts, не causal VPS A/B. Observation69.067CPU-s, source5.164,repack0.611,
retention2.418; внутренний SQL/validation/encoding split нового adapter не измерялся.
Рабочий набор35GB и зрелый прогноз не подтверждены коротким DB размером.
Actual1CPU/4GiB/no swap/networknone; OOMfalse/resource stop отсутствует,
controller187.102s/cgroup lastsample131.821CPU-s (lower bound), hostidlemin74.12%.
Owned synthetic DB/container cleanupCOMPLETE, локальные TEMP fixtures удалены.
Reports/manifest/ZIP/hash внеGit. VPS/private state/valid history/другие проекты не менялись.

PR94 остаётся единственным Draft/unmerged. Полную интеграцию/migration/merge/CD,
baseline и30min/24h не начинать; третьего prototype run нет. F22/F42/AC-13-06 и весь
scope Goal2 не закрыты. Новое направление — только по отдельному решению владельца.
Итоговый plan commit сохраняется локально: новый push вызвал бы ещё один неразрешённый
prototype job. Required CI run41220369584 completedFAIL: docs/select/storage-boundaryPASS;
validation2624PASS/1existing broadFAIL905.83s (accepted1222/2472), ci-requiredFAIL.
Новые targeted guards проходят native CI; SLO остаются нарушены. Billing не получен,
runner duration обоих разрешённых runs сохранён внеGit. Последующий push не выполняется.


Goal2 возобновлена явным поручением владельца31Oct: сначала один ограниченный
сквозной prototype нового способа SQLite, не полная переделка. При PASS показать
результат и минимальную интеграцию для решения; при FAIL остановить работу над
подходом с измеренным выводом. Не переносить все подсистемы, не мигрировать историю,
не менять VPS/рабочую БД. Большой план ниже остаётся PROPOSED; его сроки не обещаны.
Отдельное разрешение на публикацию именно этого plan в public repository сохраняется.

Baseline main12cf5d48e8d4b13aa77dbbe84ee9d161f81a4619; PR94 Draft/unmerged,
remote head8fb8db9902aeebd7cc7ef611d6eff81c7aed4c6c, предыдущий applicationf38cff0.
Local494f617 — дополнительный nested-head corruption guard, включается в текущую
проверку. Checkout/branch прежние; новые ветки/PR и production hooks не создаются.

Изолированный адаптер только tests/sqlite_repository.py: прямые неизменяемые SQLite
revisions и shared exact inputs, small compatibility heads, foreign keys/checksums,
те же source/detectors/episode/outcome/retry/retention и обе доставки. Process layout
не меняется. Заменяется только хранение immutable публикаций; generic mutable queues,
source и annual storage остаются реальными текущими компонентами. Это не полное
выполнение предложенного ниже ядра и не доказательство зрелой ёмкости35GB.

Validation REQUIRED: focused golden canonical equality обеих реализаций, rollback,
immutable origin, повтор/повреждение fresh head/inputs, actual signed retry/restart,
TTL/annual facts; local quality. Один дополнительный Linux/native capped CI job
в PR94: unchanged1648streams/2472closed arrivals/130real seconds/seed+offered SHA,
1CPU aggregate/4GiB/no swap/450CPUs/510wall/600outer,35%idle admission/15%stop,
source≤30s/due≤60s, future отдельно; real maintenance/FK checks и полный resource
report. Existing production qualification и required CI не ослабляются и не
заменяются prototype. Windows small PASS не substitute. External transports mocked,
реальные Telegram/TA/VPS и30min/24h здесь не проверяются. После единственного full
result остановиться; merge/CD/baseline автоматически не начинать. Temporary DBs
только новые локальные/CI; учёт и cleanup, маленькие proofs внеGit.

Native direct-SQLite attempt31Oct закончилась REPORT_ERROR, не performance PASS/FAIL:
PR94 head2c99c45a97568bea20e019012d6ba2f10838a75d, tested merge5872e13c15fb25095fc2f918e015ce03c5e1ba9a,
run41219276241/job124590274170. App завершила130s arrival window; JSON report не
сохранился из-за sqlite3.Row в prototype stats. Latency/accepted/queues/numeric hash
утрачены, не восстанавливаются по CPU или отсутствию traceback application.
Artifact13048639740/SHA cfe2b311bf643d5c40d7cbe24ac3ca604a26c9692f44a1a680605bd4a60c0e05:
1CPU/4GiB/networknone/OOMfalse, cgroup lastsample136.14971CPU-s (lower bound), peakRAM308314112B,
owned files peak201178288B, sampled block write4537409536B на одном8:0 device,
hostidlemin73.85%, cleanupCOMPLETE. Resource admission/limits не причина завершения.
До конца окна processCPU133.76149s; exact phaseCPU не сохранён. Сохранённый reference
run41196967614: source92.373s/due118.044s; сравнение source/due нового case невозможно.
Иные runners не causal VPS A/B. Старый runtime в том же run, job124590274319:
accepted1496/2472/source135.060s/due135.186s/pending766 — отдельный FAIL, не результат prototype.

Локальный narrow fix только JSON output: stats rows преобразованы в plain lists;
малый реальный pipeline теперь проверяет json roundtrip всего результата.
PASS1test12.72s Windows, native rerun НЕ выполнен. Первое разрешение на один полный
CI prototype использовано. Владелец31Oct отдельно разрешил один повтор той же
проверки после исправления ТОЛЬКО отчёта; source/queues/algorithms/лимиты прежние.
Перед grouped push дождаться текущего run41219276241; после повторного результата
остановиться независимо от PASS/FAIL, не начинать full integration. Большая
интеграция, migration, merge/CD и VPS изменения не начинаются. Reports/hashes/logs
сохранены внеGit, synthetic DB очищена. Обычные CI gates не ослабляются.

Local validation31Oct:13focused tests PASS14.64s (test_sqlite_repository и
 test_processing_qualification), quality PASS: docs/Ruff/format/mypy133source files.
Windows3.12 fallback; native и capped result PENDING. Readonly calculation threads
явно завершаются перед снятием adapters; snapshot cancellation не считается join.
Canonical extra case: `scripts/qualify_processing.py run IMAGE REVISION NEW_REPORT_DIR --direct-sqlite`.
Он создаёт только owned isolated Docker/SQLite с networknone, сохраняет JSON/log,
проверяет native/fixture SHA/actual direct tables/FK/maintenance и удаляет fixture.

Предыдущий sole prototype остаётся FAIL и служит основным сохранённым сравнением;
последующий docs-only run41206428542 не новый candidate: requiredFAIL, capped accepted1287,
source133425ms/due805 на другом runner. Новая попытка меняет изолированный storage
adapter; numerical/transaction correctness отдельно от performance.

| Проверка | Факт, evidence и ограничения |
| --- | --- |
| Sole structural prototype | Run41196967614/job124525014452 FAIL:2472/2472accepted, maxsource92373ms/p9584582ms, due pending0, maxcompleted due118044ms. Limits30/60s не пройдены; zero backlog на endpoint не доказывает своевременность |
| Сопоставимый предыдущий case | Run41190740672:1784/2472accepted, maxsource119706ms/p95112028ms, due312/maxcompleted126368ms. Phase131.077CPU-s против119.576CPU-s prototype; одинаковые offered/seed SHA, разные CI runner instances, не causal VPS A/B |
| Полнота/ресурсы prototype | Native, both markets/4TF/5families/1648streams; producer/worker/receiver errors0. Future10200 не overdue. Actual1CPU/4GiB/no swap/networknone; OOMfalse; peakRAM197595136B, temporary work97144760B; host idlemin72.93%. Cgroup133.779CPU-s — last sampled lower bound, не exact total |
| CPU decomposition | Observation80.483CPU-s/2472calls, source6.378,repack2.234,retention0.073,diagnostics0.260. Предшествующий instrumented profile: observe96.218, version53.771, tick closure38.599, detector0.526; nested cumulative не суммировать. SQL995246calls в profile не количество SQL каждого обычного run |
| Ordinary required CI |2615PASS/1broadFAIL679.19s:accepted1672/2472 на другом runner без capped report; поздние assertions traceback не выполнял. docs/select/storage-boundary PASS, ci-required FAIL; subsequent image/package не запускались |
| Proof | Artifact13040847857/11807B/SHA93eafcc2c9ac9d6d2cf85e2e2408e6662de0abc83bc3aecee7b879dba46404d9 проверен, reports внеGit, synthetic cleanupCOMPLETE. MockTelegram348/signedagent101 — не live delivery acceptance |

Prototype убрал повторную materialization из lifecycle/head checks, сгруппировал
closure re-arm и общие candle inputs. Immutable records, reference, CAS и generic
callbacks сохранены; этого оказалось недостаточно. Не начинать следующую цепочку
операционных micro-optimizations, не увеличивать ресурсы и не уменьшать universe.

### Архивная альтернатива — предметное ядро одной SQLite, не текущий план

Статус PROPOSED, НЕ IMPLEMENTED. Заменяется generic live publication path; согласованные
business rules, private settings, внешние interfaces и numerical methods не меняются.
Одно приложение остаётся одним deployment; PostgreSQL/Redis и второй writer не нужны.

1. Network/Telegram loop обслуживает I/O и bounded input commands; единственный
   writer process владеет SQLite writes и доменным порядком. Read-only calculations
   и UI reads имеют отдельные snapshots. Process split сам по себе не ускорение:
   все процессы вместе укладываются в прежние1CPU/4GiB, а очередь входит в lag.
   Accepted/confirmed только после durable commit; backpressure/crash ведут к явному
   gap/recovery, не к потере свечи или переносу timestamps для маскировки задержки.
2. Одна closed observation — один prepared input frame и одна предметная transaction:
   source revision, все meaningful events/episode revisions, origin jobs и независимые
   consumer intents. Прямые bound SQL против typed tables вместо recursive generic
   runtime envelopes, head re-reading и service-reference expansion при записи.
   Сохраняется КАЖДАЯ meaningful immutable revision; delta storage восстанавливает
   exact public snapshot, не объединяет события и не заменяет origin текущей ценой.
3. Явные tables/constraints для streams/source revisions/windows, episodes/updates/
   confirmations/conditions, finite jobs, outboxes/attempts, settings/provenance.
   Ready/deadline indexes ограничивают lifecycle и due selection; один owner/CAS,
   immutable uniqueness и транзакционные dependencies остаются DB invariants.
   Во время network send writer transaction не удерживается; frozen body/signature,
   HMAC/idempotency/retry/unknown ACK/expiry и независимость каналов сохраняются.
4. Annual facts хранят один компактный набор выбранных horizons на episode, а не
   повторяющиеся JSON schemas/UUID keys в каждом horizon row. Dimensions/provenance
   dictionary и exact memberships отдельны. Binary UUID/digest и существующие pure
   lossless codecs допустимы; float bits/types, MFE/MAE, completeness, arbitrary
   поддерживаемые owner horizons и provenance не округляются и не сокращаются.
   Indexed/streaming analytics и export не разворачивают полные source windows.
5. Public History/commands/callbacks/export/agent v2 получают compatibility adapters.
   Legacy readers/converters остаются для реально retained объектов; новый live
   writer не использует старую catalogue/repack цепочку как обязательный путь.
   F24 REMOVE только после proof callers/recovery/retention, не по названию модуля.
6. Переход без reset валидной light history: additive typed layout и resumable
   ownership handoff в той же SQLite. Private state, generation, IDs/revisions,
   reference/settings, leases/jobs и pending/unknown deliveries сохраняются.
   Каждая adopted identity имеет единственного authoritative owner; union reads
   исключают double N. До switch квалифицируются migration и reverse projection
   новых writes либо другая доказанная recovery strategy. Старый image сам по себе
   не считается rollback нового layout. Legacy debt входит в physical forecast.

### Архивные этапы и оценка typed core — не разрешены и заменены bounded gate

| Этап | Результат и gate | Оценка effort, не обещанный срок |
| --- | --- | --- |
| A — минимальное новое ядро | Schema/transaction API + один настоящий end-to-end adapter: immutable inputs,5detectors,episodes,one outcome set, обе доставки, expiry/restart/CAS. Same frozen130s replay и ранний physical archive sample. Не макет, не mock persistence. При FAIL остановиться и вернуть вариант/измерения |2–3 рабочих дня |
| B — функциональная интеграция | Полные adapters для history/outcome/analytics/digest/export/settings/legacy callbacks; active detectors отдельно от alerts; finite queues/source recovery и exact annual storage; текущие golden/negative tests |3–5 рабочих дней |
| C — переход и эксплуатационные риски | Непрерывная valid history migration/recovery, representative multiGB restore, measured capacity/physical peak и retained artifact policy; shared-VPS CPU/RAM/I/O admission |2–4 рабочих дня |
| D — закрытие Goal2 | Оставшиеся F/UX/F24/F31/F32, visual после owner resume, required CI/review/merge/CD, accepted30min затем24h unchanged observation |1–3 рабочих дня + минимум24h |

Итого8–15 рабочих дней плюс минимум сутки наблюдения, confidence LOW. Отсчёт от
одобрения реализации, не от накопленного времени отменённой CAPACITY25. Оценки effort
не задают непрерывное время автономной сессии; главный неизвестный — schema/adapters/
migration и legacy debt, не только строки кода. После этапа A оценку пересмотреть
по actual full-path evidence. Эти2–3дня — первый решающий результат, не вся поставка.

Validation этапа A REQUIRED: неизменные offered/seed input SHA, оба рынка/4TF/
5families, accepted2472/2472, source≤30s, due≤60s при доступных inputs, future отдельно,
никакого нового N20. Real current formulas и persistence/outbox/receiver boundaries;
совпадение всех canonical fields/float bits/state transitions с fixtures, meaningful
revision sequence, rollback после failure и restart в каждом handoff boundary.
Очередь IPC учитывается в end-to-end, accepted после commit. Замер ресурсов суммарный
по всем процессам;1CPU/4GiB/450CPU-s/510wall/600outer,35%idle admission/15%stop и
RAM reserve неизменны. CI Linux/native — основной proof, Windows не substitute.

Capacity gate REQUIRED уже в A: actual writer/index pages, source-used dedup и
membership/provenance sensitivity, detail30d/general72h/annual12calendar months,
fragmentation/free pages/WAL/SHM/checkpoint/repack/legacy debt. Forecast имеет
явные episode/day, updates/members/horizons, no-sharing и sharing scenarios; число
35GB не выводится из короткого DB размера. Representative2–4GB sample и calendar
boundaries проверяются в существующем qualification budget; при недоступном gate
не ослаблять limits. Измерение costs не является proof live incidence rates.
Численные16/20/25GB targets заранее не обещаются. Финальная сумма≤35GB И физически
вмещает backup/temp/shared-VPS reserve. Annual query latency/resources проверяются
наряду с записью, без скрытого переноса издержек на чтение.

Первый code milestone не переносит весь menu/UX и старые experimental branches.
Он проверяет новую единицу обработки на прежнем полном сценарии; после одного
отрицательного полноценного результата не достраивать его бессрочно. Успех A даёт
основание продолжить B–D, но не закрывает AC-13-06 и не заменяет VPS30min/24h.
Новое implementation поручение должно утвердить этот подход и bounded early gate;
сейчас выполняется только plan publication, без нового prototype/поставки/перезапуска.

### Сохраняемые findings и физический reserve

- По коду17/18 shared AC из Goal1; AC-13-06/F22/F34/F38/F42 не закрыты. Все stable
  F/UX/CAP25/CWSEC IDs и прежние dispositions остаются в реестре ниже; новый design
  не закрывает их автоматически и не меняет readiness denominator/проценты.
- F26/F49–F56 поставлены Goal1; regressions READY сохраняют подходящие evidence,
  но future adapters потребуют affected checks. F31 deployed hardening сохранён,
  sustainable ordinary/maintenance budget не подтверждён. F32/UX POSTPONED_OWNER;
  интерфейс приложения Telegram не открывать до отдельного resume visual.
- F38: прежний cached442–446GB не принят, startup/repack flow не steady. Sensitivity
  current writer512EP:3328B/EP при1membership и4712при4; условные91452EP/day дают
 111.4/157.7GB/year только archive. Это не forecast нового typed layout и не lower
  bound. Новая модель обязана объяснить stored bytes без удобного выбора26–27GB.
- Fresh readonly31Oct11:45UTC: workingDB+sidecars0.864GB, free27.961GB, backups3.280GB
  (7files с sidecars), rehearsals15.233GB/109dirs, releases0.585GB/20dirs (3heavy).
  Рост working до35GB превышает current free на6.175GB ещё до backup/temp reserve.
  Снимок1092streams/285late/unknown0, bootstrap/blocked/recovery0; heartbeat healthy
  не делает source своевременным. Generation5bb5712 сохранилась, source12cf5d4.
- F34 source pre-97592ec15d0716ba7e7072a7c951e0008fb77788-1792737395.sqlite3,
 2962976768B нужен multiGB qualification, не удалять. Readonly stat не full integrity.
  Его conservative3x source+7GiB дополнительный envelope16.405GB арифметически
  помещается с current11.556GB остатком; это не CPU admission или restore PASS.
  На полном35GB такой backup envelope физически не помещается в этот filesystem.
- F24/artifact review:64old heavy directories/15.039GB directory upper bound,
  no open FD в полном bounded scan, но58mounts stopped containers. Verified replacement
  уникальных inputs отсутствует; deletion не разрешена. Small reports/scenarios/
  manifests/hashes сохранить. Latest2ACTUALLY verified backups и current+previous good
  release — политика, не доказательство существования двух проверенных новых копий.
- Новый valid-history reset, old artifact deletion, other project changes, увеличение
  ресурсов, schedules/automations и Codex Security не разрешены. Canonical tooling
  и подробные evidence/checkpoint сохраняются внеGit; public plan разрешён владельцем.

### Goal1 — DONE по primary records,30Oct2026

PR93: reviewed head5ccfefabb874a5b6591e6ccb71b01a0c1ee96e25, merge12cf5d48e8d4b13aa77dbbe84ee9d161f81a4619. Required CI41097848708:2587Linux/native tests, quality/docs,13.45GB joint structural boundary, image/security/package PASS. Trusted release41100389366:2587tests PASS583.07s, source-pinned image/package, production PASS18:00:55UTC. API metadata запаздывала; full logs, independent artifact/primary VPS receipt и затем final run success сохранены. No standalone full workflow run.

Artifact12997713871/64769770bytes/SHA493539be508d6cb2e8727338a96e2ba7c2295164fe41efe545a5a2b05a65f838; host image4288c29670e684fde336ba8672f1078ba971bc9230a8ace15c277c5ec88b9286. Primary release12cf5d4-493539be508d6cb2/deployment.json SHA72cb850946d0c89c271c98ad6dc94a587c8c650abe6476c4850b6e9b1d63ea8e. Exact-generation transition/private independent restore/verify PASS, config5→6 only candle_days35→3 и absent activation→all5; .env/watchlist/private/notification selection/TA unchanged. Old working DB/wal/shm removed3 only after post-checks; no backups/experiments/other projects touched. PermitCONSUMED; new valid light generation защищена от повторного reset.

По коду READY17/18 shared AC:04-06,05-02,06-01/02/05,07-04/06,08-01/04,09-02/06,11-01,12-04/05,13-02/03/04. Evidence T-EP01/02,T-DG01,T-AN01,T-ME02,T-AD02,T-N20,T-WN01,T-GC01,T-AG02,T-FULL01,T-LEG01,T-CI01 и primary CI/CD; прежний audit% не пересчитывался. AC-13-06 code control реализован, final operating acceptance намеренно Goal2. Functional regression и visual подтверждения продолжаются.

Finite identical104inputs fullpath:188→76observations,752→144outcomes,156→0N20,36episodes,16initial numeric facts hash unchanged; due0/late0/Telegram2/signedagent136. Windows finite timings не выдаются за sharedVPS capacity; Linux structural boundary не live SLO.62week lifecycle/used30d/general72h/calendar12month/exact numeric export/finiteGC и v1 frozen formula compatibility проходят tests. Public receiver schema qualification42accepted/126negative; actual agent OFF/unconfigured отдельно, receiver не менялся. F26 actual image certifi2026.10.19/setuptools84.1.0, compromised emSign ECC G3 absent; build uv0.13.4 locked, no exhaustive scan claim.

Scope closure не означает устойчивую широкую нагрузку, окончательный forecast, multiGB backup, visual или24h PASS. Эти явно согласованные остатки остаются Goal2, не скрыто перенесены вDEFER. Goal1 учёт5h55m; обе её task branches удалены по merge/tree/CAS proof; preexisting checkout и экспериментальные ветки сохранены.

## Последний полный аудит 30Oct2026

Code SHA: b254e6e1851dc5662cfcd1156ef13aec31d2e2a8. Источники и их полные revisions — [product_scope.md](product_scope.md#источники-и-revisions). Основной источник содержит 130 пунктов; все 130 имеют code/test/boundary либо конкретное ограничение в source-coverage.json.68AC=65 сохранённых+3 явно обоснованных дополнения;50READY,15IN_PROGRESS,3BACKLOG,0BLOCKED. **50/68=73.5%**. Product/technical AC равноправны, проценты эпиков не усреднялись. Незамерженные изменения readiness не повышают.

Оценка относится к коду main по новым требованиям, не к работоспособности production. Текущий runtime не прошёл устойчивость:1535→1568late streams из 1648 и oldest due comparison77.8h. Future horizons и источник-ограниченный INCOMPLETE outcome не названы просрочкой без измерения.

### Предыдущий полный snapshot

17Oct2026, main affdd7bc71e1c8eb362bcf08f0e898b52095d9cd:61/65=93.8%. Изменение−20.3 п.п. объясняется новой нормой episode/detector activation/uniqueN/72h/35GB и самостоятельного full profile, повторной проверкой известных runtime/capacity gaps и новыми advisoryF26. Это не утверждение, что 20% прежнего работающего кода сломались. F47/F48, наоборот, исправлены. Более старые snapshots сохранены в Git и не выдаются за текущую готовность.

### Готовность по эпикам

| Эпик | READY / все AC | Готовность |
| --- | --- | --- |
| E01 — Назначение, границы и владелец | 5/5 | 100.0% |
| E02 — Universe и watchlist | 4/4 | 100.0% |
| E03 — Realtime ingestion и timeframes | 5/5 | 100.0% |
| E04 — Detectors и metrics | 5/6 | 83.3% |
| E05 — Episodes и freshness | 3/4 | 75.0% |
| E06 — History и outcome analytics | 4/7 | 57.1% |
| E07 — Telegram alerts, control deck и manual use | 5/7 | 71.4% |
| E08 — Owner settings и presets | 2/4 | 50.0% |
| E09 — Producer и handoff для trading agent | 4/6 | 66.7% |
| E10 — Persistence, migration и recoverability | 4/4 | 100.0% |
| E11 — Digest и derivatives context | 3/4 | 75.0% |
| E12 — Технические границы, CI/CD и эксплуатация | 3/5 | 60.0% |
| E13 — Хранение, сроки и новый период истории | 3/7 | 42.9% |

## Все 68AC: снимок аудита main b254e6e, не статусы implementation branch

Матрица ниже — текущий snapshot, а не обещание будущего PASS. T1/T2/T3 — локальный Windows/fallback, G1 — повторно прочитанные exact-main Linux records25Oct. READY не означает live Telegram/receiver или принятую capacity. Подробная requirement→scenario→code→evidence→result→finding матрица 68AC/130source bullets сохранена вне Git.

| AC | Статус / результат | Code/test evidence, ограничения / finding |
| --- | --- | --- |
| AC-01-01 | READY / PASS | test_config,test_bot,test_agent_delivery,test_release_security; Автоматические fixtures, без нового live взаимодействия или нагрузочного теста. |
| AC-01-02 | READY / PASS | test_config,test_bot,test_agent_delivery,test_release_security; Автоматические fixtures, без нового live взаимодействия или нагрузочного теста. |
| AC-01-03 | READY / PASS | test_config,test_bot,test_agent_delivery,test_release_security; Автоматические fixtures, без нового live взаимодействия или нагрузочного теста. |
| AC-01-04 | READY / PASS | test_config,test_bot,test_agent_delivery,test_release_security; Private-chat owner AND user identity fail closed; callbacks проходят общий access gate. |
| AC-01-05 | READY / PASS | test_config,test_bot,test_agent_delivery,test_release_security; Автоматические fixtures, без нового live взаимодействия или нагрузочного теста. |
| AC-02-01 | READY / PASS | test_universe,test_bot,test_sources; Автоматические fixtures, без нового live взаимодействия или нагрузочного теста. |
| AC-02-02 | READY / PASS | test_universe,test_bot,test_sources; Автоматические fixtures, без нового live взаимодействия или нагрузочного теста. |
| AC-02-03 | READY / PASS | test_universe,test_bot,test_sources; Автоматические fixtures, без нового live взаимодействия или нагрузочного теста. |
| AC-02-04 | READY / PASS | test_universe,test_bot,test_sources; Автоматические fixtures, без нового live взаимодействия или нагрузочного теста. |
| AC-03-01 | READY / PASS | test_sources,test_async_runtime,test_recovery_window; Адаптеры/повторная подписка реализованы. Живая своевременность сейчас FAIL отдельно F22/AC-13-06; READY не означает здоровые потоки. |
| AC-03-02 | READY / PASS | test_sources,test_async_runtime,test_recovery_window; Автоматические fixtures, без нового live взаимодействия или нагрузочного теста. |
| AC-03-03 | READY / PASS | test_sources,test_async_runtime,test_recovery_window; Автоматические fixtures, без нового live взаимодействия или нагрузочного теста. |
| AC-03-04 | READY / PASS | test_sources,test_async_runtime,test_recovery_window; Gaps ограничивают calculations и видимы; наличие recovery не доказывает, что производительность восстановления достаточна. |
| AC-03-05 | READY / PASS | test_sources,test_async_runtime,test_recovery_window; Автоматические fixtures, без нового live взаимодействия или нагрузочного теста. |
| AC-04-01 | READY / PASS | test_detectors,test_events,test_config; Автоматические fixtures, без нового live взаимодействия или нагрузочного теста. |
| AC-04-02 | READY / PASS | test_detectors,test_events,test_config; Автоматические fixtures, без нового live взаимодействия или нагрузочного теста. |
| AC-04-03 | READY / PASS | test_detectors,test_events,test_config; Event metrics/settings/provenance immutable, direction neutral без основания; не утверждается правильность новой единицы статистики. |
| AC-04-04 | READY / PASS | test_detectors,test_events,test_config; Автоматические fixtures, без нового live взаимодействия или нагрузочного теста. |
| AC-04-05 | READY / PASS | test_detectors,test_events,test_config; Автоматические fixtures, без нового live взаимодействия или нагрузочного теста. |
| AC-04-06 | BACKLOG / FAIL | test_detectors,test_events,test_config; Новая норма не полностью реализована; факт и отрицательный сценарий — F49 Findings: F49. |
| AC-05-01 | READY / PASS | test_events,test_delivery_recovery; Автоматические fixtures, без нового live взаимодействия или нагрузочного теста. |
| AC-05-02 | IN_PROGRESS / PARTIAL | test_events,test_delivery_recovery; Новая норма не полностью реализована; факт и отрицательный сценарий — F50 Findings: F50. |
| AC-05-03 | READY / PASS | test_events,test_delivery_recovery; Closure/re-arm/max age подтверждены автоматикой; current live sample не является lifecycle test. |
| AC-05-04 | READY / PASS | test_events,test_delivery_recovery; Clock-controlled freshness/expiry checks PASS; old delivered timestamp не доказывает текущую Telegram latency. |
| AC-06-01 | IN_PROGRESS / PARTIAL | test_events,test_outcomes,test_analytics,test_export,test_analytics_recovery; Новая норма не полностью реализована; факт и отрицательный сценарий — F50 Findings: F50. |
| AC-06-02 | IN_PROGRESS / PARTIAL | test_events,test_outcomes,test_analytics,test_export,test_analytics_recovery; Новая норма не полностью реализована; факт и отрицательный сценарий — F51 Findings: F51. |
| AC-06-03 | READY / PASS | test_events,test_outcomes,test_analytics,test_export,test_analytics_recovery; READY относится к математике return/direction/continuation и явной неполноте на сохранённой reference; выбор episode reference не готов по 06-02. |
| AC-06-04 | READY / PASS | test_events,test_outcomes,test_analytics,test_export,test_analytics_recovery; READY относится к deterministic MFE/MAE на указанном диапазоне; per-episode admission отдельно 06-02. |
| AC-06-05 | IN_PROGRESS / PARTIAL | test_events,test_outcomes,test_analytics,test_export,test_analytics_recovery; Новая норма не полностью реализована; факт и отрицательный сценарий — F52,F53 Findings: F52,F53. |
| AC-06-06 | READY / PASS | test_events,test_outcomes,test_analytics,test_export,test_analytics_recovery; F48: единая period_metadata, CSV/NDJSON effective bounds; source filters/version immutability PASS. Новая модель episodes отдельно 06-01/02/05. |
| AC-06-07 | READY / PASS | test_events,test_outcomes,test_analytics,test_export,test_analytics_recovery; Decision snapshot не подменяется поздними outcomes или max strength episode. |
| AC-07-01 | READY / PASS | test_bot,test_telegram_ui,test_outcome_ui,test_telegram_reply; Функциональные поля, emphasis entities и UTF16 границы PASS. Визуальная компоновка/мобильный Telegram исключены. |
| AC-07-02 | READY / PASS | test_bot,test_telegram_ui,test_outcome_ui,test_telegram_reply; Точные Spot/linear URL mapping проверены без открытия сделки; live browser не проверялся. |
| AC-07-03 | READY / PASS | test_bot,test_telegram_ui,test_outcome_ui,test_telegram_reply; Функциональные routing/aliases/CAS/input/cancel/pagination/download fixtures PASS; текущая end-to-end задержка owner не измерялась. |
| AC-07-04 | IN_PROGRESS / PARTIAL | test_bot,test_telegram_ui,test_outcome_ui,test_telegram_reply; Новая норма не полностью реализована; факт и отрицательный сценарий — F49 Findings: F49. |
| AC-07-05 | READY / PASS | test_bot,test_telegram_ui,test_outcome_ui,test_telegram_reply; Realtime/digest/preferences/mutes не управляют history и agent; activation отдельного detector отсутствует по 04-06. |
| AC-07-06 | IN_PROGRESS / PARTIAL | test_bot,test_telegram_ui,test_outcome_ui,test_telegram_reply; Новая норма не полностью реализована; факт и отрицательный сценарий — F51 Findings: F51. |
| AC-07-07 | READY / PASS | test_bot,test_telegram_ui,test_outcome_ui,test_telegram_reply; Автоматические fixtures, без нового live взаимодействия или нагрузочного теста. |
| AC-08-01 | IN_PROGRESS / PARTIAL | test_config,test_bot,test_telegram_ui; Новая норма не полностью реализована; факт и отрицательный сценарий — F49 Findings: F49. |
| AC-08-02 | READY / PASS | test_config,test_bot,test_telegram_ui; Обычные допустимые значения применяются по revision без restart; новая 72h policy отдельно 08-04. |
| AC-08-03 | READY / PASS | test_config,test_bot,test_telegram_ui; Preview/CAS apply/custom restore и реальные параметры в fixtures PASS. |
| AC-08-04 | BACKLOG / FAIL | test_config,test_bot,test_telegram_ui; Новая норма не полностью реализована; факт и отрицательный сценарий — F54 Findings: F54. |
| AC-09-01 | READY / PASS | test_agent_delivery,test_delivery_recovery; Форма advisory v2 без execution fields сохранена; meaningful admission отдельно 09-02. |
| AC-09-02 | IN_PROGRESS / PARTIAL | test_agent_delivery,test_delivery_recovery; Новая норма не полностью реализована; факт и отрицательный сценарий — F50 Findings: F50. |
| AC-09-03 | READY / PASS | test_agent_delivery,test_delivery_recovery; Durable outbox/expiry/replay реализованы; live receiver и доставка нового scope не проверены. |
| AC-09-04 | READY / PASS | test_agent_delivery,test_delivery_recovery; Одни bytes на transport revision, строгий ack/event identity; provider receiver не изменялся. |
| AC-09-05 | READY / PASS | test_agent_delivery,test_delivery_recovery; UNSET/BLOCKED/READY и errors отделены от Telegram; наличие diagnostics не доказывает принятие получателем. |
| AC-09-06 | IN_PROGRESS / PARTIAL | test_agent_delivery,test_delivery_recovery; Новая норма не полностью реализована; факт и отрицательный сценарий — F55 Findings: F55. |
| AC-10-01 | READY / PASS | test_storage,test_market_reset_delivery,test_recovery_bytes,test_async_runtime; Автоматические fixtures, без нового live взаимодействия или нагрузочного теста. |
| AC-10-02 | READY / PASS | test_storage,test_market_reset_delivery,test_recovery_bytes,test_async_runtime; Versioned readable history в доступном TTL и fail-closed unknown schemas; плановая expiry допустима. |
| AC-10-03 | READY / PASS | test_storage,test_market_reset_delivery,test_recovery_bytes,test_async_runtime; Малые backup/restore и private migration PASS. Не выдаётся за large-backup performance или разрешение менять данные. |
| AC-10-04 | READY / PASS | test_storage,test_market_reset_delivery,test_recovery_bytes,test_async_runtime; Очереди/episodes/subscriptions переживают restart по тестам; audit фактический restart не выполнял. |
| AC-11-01 | IN_PROGRESS / PARTIAL | test_digest,test_enrichment,test_enrichment_runtime; Новая норма не полностью реализована; факт и отрицательный сценарий — F53 Findings: F53. |
| AC-11-02 | READY / PASS | test_digest,test_enrichment,test_enrichment_runtime; Независимые toggles/interval/restart идемпотентны; текущая owner-visible доставка digest не проверялась. |
| AC-11-03 | READY / PASS | test_digest,test_enrichment,test_enrichment_runtime; Optional mapping/failure изолированы; production enrichment OFF не считается failure. |
| AC-11-04 | READY / PASS | test_digest,test_enrichment,test_enrichment_runtime; Source/market/unit/time context заморожены; native Perpetual detectors независимы. |
| AC-12-01 | READY / PASS | test_release_security,test_receiver_update,test_vendor_source_pins; G1; G1 exact-main image/restart/backup fixtures и R1 same-source persistent volume; чужие проекты не менялись. |
| AC-12-02 | READY / PASS | test_release_security,test_receiver_update,test_vendor_source_pins; G1; G1 PR92/ci-required и trusted release40162581287, delivery record source/image/digest bound; это не 24h или большой backup. |
| AC-12-03 | READY / PASS | test_release_security,test_receiver_update,test_vendor_source_pins; G1; Логические границы и bounded diagnostics есть; healthy heartbeat обозначает живость компонентов, не своевременность. R1 late_streams явны. |
| AC-12-04 | IN_PROGRESS / PARTIAL | test_release_security,test_receiver_update,test_vendor_source_pins; G1; Новая норма не полностью реализована; факт и отрицательный сценарий — F26 Findings: F26. |
| AC-12-05 | BACKLOG / FAIL | test_release_security,test_receiver_update,test_vendor_source_pins; G1; Новая норма не полностью реализована; факт и отрицательный сценарий — F56 Findings: F56. |
| AC-13-01 | READY / PASS | test_capacity25,test_catalogue_lifecycle,test_market_reset_delivery,test_export; G1/R1/P1; Shared immutable windows/settings/context реализованы; не требуется переписывать codecs лишь потому, что старый scope изменён. |
| AC-13-02 | IN_PROGRESS / PARTIAL | test_capacity25,test_catalogue_lifecycle,test_market_reset_delivery,test_export; G1/R1/P1; Новая норма не полностью реализована; факт и отрицательный сценарий — F52,F54 Findings: F52,F54. |
| AC-13-03 | IN_PROGRESS / PARTIAL | test_capacity25,test_catalogue_lifecycle,test_market_reset_delivery,test_export; G1/R1/P1; Новая норма не полностью реализована; факт и отрицательный сценарий — F53 Findings: F53. |
| AC-13-04 | IN_PROGRESS / PARTIAL | test_capacity25,test_catalogue_lifecycle,test_market_reset_delivery,test_export; G1/R1/P1; Новая норма не полностью реализована; факт и отрицательный сценарий — F54 Findings: F54. |
| AC-13-05 | READY / PASS | test_capacity25,test_catalogue_lifecycle,test_market_reset_delivery,test_export; G1/R1/P1; Parent/detail expiry и terminal receipts конечны, GC/retry/rearm tests PASS; точный новый timeout источник не задаёт, бессрочность не доказана. |
| AC-13-06 | IN_PROGRESS / PARTIAL | test_capacity25,test_catalogue_lifecycle,test_market_reset_delivery,test_export; G1/R1/P1; Новая норма не полностью реализована; факт и отрицательный сценарий — F22,F34,F38,F42 Findings: F22,F34,F38,F42. |
| AC-13-07 | READY / PASS | test_capacity25,test_catalogue_lifecycle,test_market_reset_delivery,test_export; G1/R1/P1; discard_test_history уже поддержан exact-target/private restore/no-old-source tests; будущий переход не выполнен и аудит его не разрешает. |

## Evidence и baseline

| Record | Revision / среда / время / результат | Ограничения |
| --- | --- | --- |
| T0 quality | Audit branch с runtime source b254e6e, Windows Python3.12; canonical scripts/checks.py quality: docs/Ruff/format/mypy PASS,132source files | Не Linux build; final canonical quality PASS30Oct |
| T1 core | b254e6e,30Oct,14existing suites,273PASS/10.70s; core.xml/core.log | Temporary local DB/test credentials/mock external services, не live Telegram |
| T2 boundaries | b254e6e,30Oct,12existing suites,185PASS/2SKIP/14.87s; boundaries.xml/log | Skips: Linux native converter и procfs; не Linux/native PASS |
| T3 supplementary | b254e6e,30Oct,telegram_reply/vendor_source_pins,6PASS/1.10s | Entities/ambiguous timeout/source hashes; не новый security scan |
| P1 semantic probes | b254e6e,30Oct,owned temporary bounded DB:3events/1episode→12outcome+3comparison jobs, digestN3; 3dTTL rejected,4321min horizon accepted | Synthetic negative evidence новой нормы; не production frequency/performance |
| G1 trusted Linux | PR92, ci-required run40161389747 на PR test-merge697549…; release40162581287 exactmain b254e6e, build121077067983 log:2485PASS/753.74s, quality/native/image/archive checks PASS | Не запускается заново; прежние tests не проверяют новые episode rules. Main release повторяет tests, reuse не заявлен |
| D1 delivery | release40162581287 production121079843868, deployment artifact12587347410, PASS/COMPLETE25Oct04:53:46UTC; source b254e6e/image54fab5bd/bundlea986dc87/ZIP0783f567 | DATA DEGRADED record, source/image/health/persistence/market PASS не значит stable24h. Backup22.05MB/0.419s не large qualification |
| R1 VPS |30Oct11:01–11:11UTC, Contabo vmi3224957/6vCPU; live b254e6e/image54fab5bd,26containers; runtime/config5/generation8a0e8841 unchanged в двух точках, queries total_changes0 | Read-only snapshots, no profile/workload/restart. Healthy heartbeat — liveness, не своевременность |
| R2 resources |60.14s proc/cgroup:host idle33.50%,iowait2.35%,minAvailable6.628GB; CW0.830CPU/anon0.662GB/cache-inclusive1.918GB/read0.255MBps/write3.219MBps | Не 24h/peak budget; hostkernel/clock accounting и cgroup rows не суммировать как benchmark причин. Probe Python0.306CPU без childCLI |
| R3 Grafana |30Oct11:11UTC, Prometheus instant5m:normalized idle35.03%, CW0.875CPU/write2.593MBps, availableRAM6.779GB | Datasource/source timestamps сохранены, короткое соседнее окно; соседние SLA не измерены |
| R4 disk | working8.514GB snapshot, free20.015GB; поздний df free19.987GB, проект 17.047GB/backups3.280/rehearsals4.833/releases0.390 | Sidecars/indexes already included; backup/temp отдельны; metadata не доказывает ненужность/restore integrity |
| S1 advisory |30Oct OSV35unique pinned packages,3distinct reviewed GHSA; certifi/uv/setuptools first-party advisory readback | Aliases не отдельные findings, exploitability условна; no Codex Security, no base-image scanner |
| G2 CI history |102runs17–30Oct,2pages:61PR(43success/15failure/3cancel),41release(29success/12failure);8selected runs all job attempts reviewed | Selected-job durations не весь billing; usage/remaining paid minutes неизвестны, новых runs не создавали |

Первичные local record IDs перечислены для восстановления; полный материал вне Git: audit-20261030/report.md, ac-matrix.json, source-coverage.json, findings.json, previous-findings.json, legacy-engineering-ids.json и evidence/. Без raw logs/подробного исследовательского отчёта в repository.

## Validation Plan аудита

| Этап / риск | Проверка / expected | Canonical команда/tool / среда | Обязательность / результат |
| --- | --- | --- | --- |
| Источники/границы | Full source/revisions, root instructions,130source/68AC | project-workflow + Drive reads; Git/GitHub | REQUIRED / PASS |
| Код main | Two-way requirements/code, stable finding reconciliation | code/module inventory, current canonical docs | REQUIRED / PASS с findings/ограничениями |
| Local quality | Docs links/fences,Ruff format/lint,mypy, без rewrites | Python scripts/checks.py quality из root, owned caches | REQUIRED / PASS initial/final |
| Существенные сценарии | Existing positive/negative tests + targeted probes | isolated pytest T1/T2/T3, temporaryDB/mock credentials | REQUIRED /464PASS+2LinuxSKIP; probes FAIL новой нормы ожидаемо, не исправлять |
| Linux/native/main | Exact source job log/steps/source image | G1 existing Actions records, no new workflows | REQUIRED /PASS существующих 2485tests; новые требования не покрыты |
| Runtime | Version/freshness/due/resource/space | contabo-vps readonly/Grafana bounded snapshot | REQUIRED /PARTIAL, timely service FAIL; no destructive/live workload |
| Visual Telegram | Layout/mobile/scroll/client rendering | исключено явным scope | N/A /не проводилось |
| Полная свежая регрессия / нагрузка / live sends | Не входят в audit authorization | Новый run/production operation не выполняется | N/A по scope; приёмку новой версии не заменяет |
| Документы / результат | Crosscheck IDs/READY/evidence, own temp cleanup, local commit | canonical quality, Git diff/readback | REQUIRED / PASS quality/IDs/cleanup; commit/readback в конце |

## Открытые обязательные findings

13 без дублей:8 новых F49–F56 и 5 актуализированных F22/F26/F34/F38/F42. F24/F31 — отдельные DEFER, F32/visual части UX — вне текущей visual проверки; они не выданы за исправленные. Каждая строка: место/факт/последствие/action/закрытие; полный разбор с expected/evidence/confidence/dependencies — локальный findings.json.

| ID / приоритет / confidence | Суть, область / evidence | Действие, критерий закрытия, зависимости |
| --- | --- | --- |
| F22 / P1 / HIGH факт; MEDIUM распределение причин | Общий поток не успевает за работой; AC-03-01,AC-12-03,AC-13-06; ingestion.py / events.py / analytics_worker.py / bot.py. R1: late_streams1535→1568/1648 за 32.08s при той же source/config5/runtime/generation; P1 oldest due comparison280088152ms=77.80h. Счётчик завершённых outcome содержит задержку до 363839531ms, но это возраст завершённого задания, не текущая глубина очереди. Evidence: vps-runtime.json11:05UTC, vps-queues.json11:10UTC; resource-summary.json60.14s; semantic-probes.json | REFACTOR: Сначала сменить единицу работы на значимый episode/один outcome и убрать постоянный N20. По оставшемуся измеренному полному пути выбрать минимально достаточную архитектуру; два writer не считать обязательными новыми требованиями. Закрытие: Воспроизводимый offered→saved→due→delivery ledger без растущих overdue и потерь; shared-host30min, затем 24h без изменения версии/config; соседние проекты не ухудшаются. Зависимости: F49–F54,F42 |
| F26 / P1 / HIGH affected pins; MEDIUM эксплуатационная применимость | Новые advisory зафиксированных зависимостей; AC-12-04; requirements.txt / requirements-dev.txt / requirements-bootstrap.txt. certifi2026.7.22 входит в affected диапазон GHSA-6q48-6c7w-9gw2; uv0.13.0 — GHSA-hq85-5cpj-mr62. Setuptools84.0.0 сопоставлен GHSA-xmj3-9gf7-h52w только для legacy Windows wheel installation. PYSEC aliases не отдельные findings. Evidence: dependencies-osv.json35packages; advisory-details.json; официальные certifi/uv/setuptools advisories20–26Oct; httpx использует certifi trust store по умолчанию. | FIX: Обновить применимые locks/trust store и перепроверить совместимость: certifi≥2026.10.19, uv≥0.13.4; setuptools≥84.1.0 при сохранении применимого Windows-tooling. Пересобрать image, не менять версии вслепую. Закрытие: Нет соответствующих affected pins; dependency/runtime/native/transport tests и required CI проходят; отсутствие утечки secrets и host trust сохраняются. Зависимости: обычный implementation PR |
| F34 / P1 / HIGH снимок; MEDIUM будущий peak | Физическая вместимость и backup peak не квалифицированы; AC-10-03,AC-13-06; /opt/crypto_watcher и native_disk_backup.py. R1 working8.514GB/free20.015GB; поздний df free19.987GB. Проект 17.047GB: backups3.280GB, rehearsals4.833GB, releases0.390GB. Рост working до 35GB требует ещё 26.486GB, уже больше свободного примерно на 6.5GB до backup/reserve. Evidence: vps-resource.json,vps-disk.txt; native_disk_backup.py reserve6GiB+1GiB; deployment.json25Oct backup22.05MB/0.419s | IMPLEMENT: Сделать physical peak ledger для новой измеренной модели с ограниченной политикой backup/rehearsals/releases и проверенным recovery-набором. Владельцу согласовывать точные cleanup candidates, не общий prune. Закрытие: До тяжёлой операции есть достаточный reserve и измеренный large-backup/restore в согласованном бюджете; прогноз учитывает concurrent growth/sidecars, без изменения других проектов. Зависимости: F38,F42,F54 |
| F38 / P1 / HIGH | Модель хранения остаётся моделью прежнего scope; AC-13-06; storage_model.py / capacity.py / сохранённый storage_report. Штатный cached forecast95.872GB, observed103940events/day и page overhead2.330; candles61d/context58d в dependency estimate из 28d comparator и 30d details. В тексте budget25/30GB. Это не прогноз нового episode/72h/35GB scope. Evidence: vps-queues.json; quality_frame_layout IF1_OR_QC1_MIN, source columns OFF; storage-model/current-layout code; limitations прямо сохранены в report. | FIX: Перестроить одну модель после новых admission/TTL/aggregate rules, сверить физические bytes и включённые writers/layouts. Старые модели оставить как явно исторические evidence. Закрытие: Fixture+наблюдаемые category/physical totals согласуются; указаны episode-rate, used-input leases, indexes/free/WAL/SHM, peaks и диапазон неопределённости; estimate≤physical admission envelope. Зависимости: F49–F54,F34 |
| F42 / P1 / HIGH | Нет приёмки облегчённой версии 30min/24h; AC-13-06; capacity observer / прошлые qualification records. Отменённая CAPACITY25 не завершила устойчивость. Текущий main не реализует новый scope, а R1 уже показывает массовую задержку. Старые 15problem checkpoints и micro/native улучшения не являются чистым замером новой версии. Evidence: G1 current-main2485Linux tests; R1 backlog; сохранённые исторические CAP25/observer records, все ограничения сохранены. | IMPLEMENT: После позитивного полного локального/Linux сценария и safe delivery выполнить предусмотренную 30min/24h проверку с due-only queue health, freshness/delivery и суммарными host budgets. Закрытие: Непрерывный измеренный период без растущего backlog/потерь/неизвестных baseline flags, working≤35GB и physical reserve, воспроизводимый final/result. Зависимости: все runtime fixes,F34,F38 |
| F49 / P1 / HIGH | Нет независимой активации detectors; AC-04-06,AC-08-01,AC-07-04; config.py:OwnerSettings / events.py:EventEngine / Telegram settings. Есть alert_detectors (Telegram families), но нет persisted active domain detectors; EventEngine перебирает все 5FAMILIES. Probe alert_detectors=() оставляет history/jobs и выключает только Telegram. Evidence: semantic-probes.json, config.py/telegram_ui.py/events.py; T1 existing isolation tests подтверждают прежнюю notification-only policy. | IMPLEMENT: Ввести отдельный persisted activation с validation/CAS/live apply и правдивой functional панелью; не переиспользовать alert_detectors как domain setting. Закрытие: Disabled family0events/0new jobs; enabled family сохраняет provenance при mute; reload/CAS/consumer isolation на обоих рынках/4TF. Зависимости: F50,F54 |
| F50 / P1 / HIGH | Свечные повторы продолжают отдельную историю; AC-05-02,AC-06-01,AC-09-02; events.py:observe / agent_delivery.py:admit. Три однотипные закрытые свечи создают 3event heads в одном episode. Event ID зависит от candle.start. Notify suppression не прекращает запись event/revision и downstream admission; agent intent принимает каждый liveevent. Evidence: semantic-probes.json3events/1episode; events.py source and T1 lifecycle; agent intents keyed episode coalesce лишь близкие 2s updates, не всю длительность. | REFACTOR: Сделать episode update admission общей границей history, analytics и consumers, сохранить значимые revisions/provenance и максимальный lifetime/rearm. Закрытие: Повтор свечи без значимых изменений не добавляет event research/jobs; новый TF/family/strength/closure добавляет ровно ожидаемый update, restart idempotent. Зависимости: F49,F51,F53 |
| F51 / P1 / HIGH | Outcome закреплён за каждым event, а не началом episode; AC-06-02,AC-07-06; events.py reference_time_ms / outcomes.py:admit. Для 3events одного episode probe создаёт 12outcomejobs; reference times отличаются на 60s. Новое подтверждение получает отдельную собственную reference price/time и результаты. Evidence: semantic-probes.json exactreference times; outcomes.py event_id/horizon identity; T1 math/gap tests проходят на старой event модели. | REFACTOR: Вынести origin в immutable episode snapshot, job/result identity episode+horizon+method; сохранить causal decision context и compatible Event ID/revision envelopes. Закрытие: Много confirmations/retries/restart дают 4results на episode и неизменную firstreference; directed/neutral/gaps exact math не меняется. Зависимости: F50,F55 |
| F52 / P1 / HIGH | Постоянный N20 остаётся в рабочем цикле; AC-06-05,AC-13-02; outcomes.py:admit / analytics.py:ComparisonWorker. Каждый event автоматически создаёт comparison job и leases на prior28days плюс input window. Probe3events→3comparison jobs; production oldest due comparison77.8h. Evidence: semantic-probes.json, outcomes.py admission/holds, analytics.py COMPARATOR_LOOKBACK_DAYS28/limit20; vps-queues.json. | REMOVE: Убрать automatic N20 admissions/scheduler/hold dependencies в новой версии; inventory callers/contracts/tests до удаления helpers. Совместимые старые entrypoints должны объяснять доступную описательную аналитику. Закрытие: Обычный episode не создаёт comparator jobs/28d leases; нет скрытого запуска через digest/commands/recovery; outcomes/causal exports сохраняются. Зависимости: F50,F54,F55 |
| F53 / P1 / HIGH | Статистика и digest считают observations; AC-06-05,AC-11-01,AC-13-03; analytics.py / quality_summary.py / digest.py. Probe1episode/3events даёт digest event_count3 и activity metric SAVED_EVENT_OBSERVATIONS. Когорты и долгие quality facts опираются на event_id, не dedup uniqueepisode. F47 most-active instruments уже исправлен. Evidence: semantic-probes.json; digest.aggregate/activity; analytics.cohort queries/quality_summary; T1/T2 controlled summaries. | REFACTOR: Один episode fact с фиксированным origin/outcomes, явными dimensions и dedup; описательные 12m aggregates и единая UTC-day export policy сохраняются. Закрытие: Один episode с 5confirmations даёт N1 в общей выборке, корректные N по объявленным groups до/после 30d expiry, digest/ranking/export согласованы. Зависимости: F50,F51,F52 |
| F54 / P1 / HIGH | 72h окно и validation не реализованы; AC-08-04,AC-13-02,AC-13-04; config.py / market_storage.py / storage_lifecycle.py. OwnerSettings candle_retention_days=3 отвергается прежним minimum29d; default/production35d. Horizon4321min принимается при 35d. Общие ticker/instrument contexts тоже получают 35d базовый purge плюс leases, даже если не используются. Evidence: semantic-probes.json; config.py validation/default35; market_storage.save_context expiry; vps-queues.json productionsettings35d. | FIX: Связать admission/lookback limits с 72h, включить used-input ownership/finite lease GC для 30d details; private migration должна сохранить значения и явно объяснить несовместимость. Закрытие: 72h boundary accepted, >72h отвергнут без смены revision; controlled clock очищает unused>72h и сохраняет только доказанные references до 30d, eventual GC/aggregates correct. Зависимости: F49–F53 |
| F55 / P2 / HIGH | Операционные документы и handoff описывают прежний scope; AC-09-06,AC-13-06; README.md / docs/runbooks/bounded-storage-reset.md / docs/contracts / architecture.md. Main README описывает detection всех 5families, per-event outcomes/compare; runbook содержит 25/30GB и старые qualification/conversion правила. Этот audit обновляет spec/plan, а as-built runtime документы не выдаёт за готовую новую реализацию. Evidence: README159, bounded-storage-reset115/395; existing v2 contract/handoff inventory; new source30Oct/D29. | DOCUMENT: Одним implementation PR актуализировать as-built+normative ссылки, сохранить valid receiver boundary и совместимость public commands/callbacks. Полную старую конвертацию не продолжать. Закрытие: Все active operational paths и handoff отражают shipped code/new policy; no obsolete autoN20/limits/indefinite study promises; command/callback compatibility fixtures. Зависимости: F49–F54 |
| F56 / P2 / HIGH | Не выделен полный профиль exact-main без CD; AC-12-05; scripts/checks.py / README124–145 / ci.yml / release.yml. checks.py all=quality+pytest; native build/numeric qualification/image/recovery packaging разнесены. PR CI имеет полный набор, но единственный manual workflow на main — release с CD; canonical standalone full-main profile/inventory не оформлен. Evidence: ci-cd-rules29Oct строки 27–30, workflows/source review; G1 existing exact-main full2485 suite+image не покрывает новый способ запуска. | IMPLEMENT: Определить canonical Linux full profile без CD и запись revision/состав/skip reasons; reuse текущие scripts, без автоматических расписаний. Закрытие: Exactmain profile собирает весь поддерживаемый применимый набор и не имеет production write permissions/side effects; dry plan и existing unit checks; реальный run только отдельным поручением. Зависимости: без нового CIrun в аудите |

## Сверка прежних findings F01–F48 и UX-001–UX-041

Все 89IDs сохранены. Прежняя closed state ниже подтверждает только исходный дефект, а не готовность изменившегося AC. Исторические исходные формулировки/Evidence доступны в Git b254e6e и baseline-open_issues.md вне Git; новый re-audit не удаляет старые решения. UX visual части не получили нового visual PASS.

| ID | Прежняя суть / область / приоритет / действие / Evidence | Актуальное состояние и ограничения |
| --- | --- | --- |
| UX-001 | Главные metrics теряются в alert; P2 / REFACTOR; AC-07-01; `event_summary`/`notification_text`: причина, метрики/единицы и интенсивность в компактном alert; full context по кнопкам; event/render regressions PASS; HIGH / READY по коду; local PASS, CI/CD PENDING | FUNCTIONAL PASS / прежнее исправление сохранено. T1 UI/bot/outcome + T3 reply entities и G1 current-main; commands/callbacks/доступ/пагинация/typed input/download проверены изолированно, не live Telegram. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| UX-002 | Смешанный язык и raw names; P2 / REFACTOR; AC-07-01/03/07; Русские family/profile/state/metric labels в обычных panels; stable identifiers остаются в advanced commands/JSON; UI crawl PASS; HIGH / READY по коду; local PASS, CI/CD PENDING | FUNCTIONAL PASS / прежнее исправление сохранено. T1 UI/bot/outcome + T3 reply entities и G1 current-main; commands/callbacks/доступ/пагинация/typed input/download проверены изолированно, не live Telegram. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| UX-003 | Нечитаемые числа и избыточная precision в UI; P2 / REFACTOR; AC-07-01/04; `amount`: группировка/читаемые units, маленькое ненулевое значение не превращается в 0; display rounding отдельно от exact event/statistics JSON; regressions PASS; HIGH / READY по коду; local PASS, CI/CD PENDING | FUNCTIONAL PASS; VISUAL N/A. T1 UI/bot/outcome + T3 reply entities и G1 current-main; commands/callbacks/доступ/пагинация/typed input/download проверены изолированно, не live Telegram. Визуальные свойства не перепроверены и не заявлены закрытыми новым visual evidence. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| UX-004 | Служебные IDs/revisions на первом экране; P2 / REFACTOR; AC-07-01; AC-04-03; IDs/revision/provenance перенесены в event details/advanced параметры, полный event JSON неизменён; controlled event flow PASS; HIGH / READY по коду; local PASS, CI/CD PENDING | FUNCTIONAL PASS; VISUAL N/A. T1 UI/bot/outcome + T3 reply entities и G1 current-main; commands/callbacks/доступ/пагинация/typed input/download проверены изолированно, не live Telegram. Визуальные свойства не перепроверены и не заявлены закрытыми новым visual evidence. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| UX-005 | Fresh/expired confirmations не объяснены; P2 / REFACTOR; AC-05-04; AC-07-01; `notification_text`: только FRESH той же settings revision в актуальных; устаревшие и другие настройки считаются отдельно; mixed-confirmation regression PASS; MEDIUM / READY по коду; local PASS, CI/CD PENDING | FUNCTIONAL PASS / прежнее исправление сохранено. T1 UI/bot/outcome + T3 reply entities и G1 current-main; commands/callbacks/доступ/пагинация/typed input/download проверены изолированно, не live Telegram. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| UX-006 | Причина обновления episode не выделена; P2 / REFACTOR; AC-05-02; AC-07-01; Причина первого/нового detector/TF/усиления — первая строка уведомления; frozen episode не меняется; event regressions PASS; HIGH / READY по коду; local PASS, CI/CD PENDING | FUNCTIONAL PASS / прежнее исправление сохранено. T1 UI/bot/outcome + T3 reply entities и G1 current-main; commands/callbacks/доступ/пагинация/typed input/download проверены изолированно, не live Telegram. Новая episode/history/outcome семантика не готова отдельно F50/F51/F53. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| UX-007 | Неудобные timestamps/timezone; P2 / REFACTOR; AC-05-04; AC-07-01; `when`: дата с годом/секундами и МСК во всех новых panels; UTCms исходных payload не меняются; time/history regressions PASS; HIGH / READY по коду; local PASS, CI/CD PENDING | FUNCTIONAL PASS; VISUAL N/A. T1 UI/bot/outcome + T3 reply entities и G1 current-main; commands/callbacks/доступ/пагинация/typed input/download проверены изолированно, не live Telegram. Визуальные свойства не перепроверены и не заявлены закрытыми новым visual evidence. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| UX-008 | Длинные первые экраны; P2 / REFACTOR; AC-07-01/03; Краткие settings/status/help/diagnostics, history по 4 и lists по 10, stats по 5; UI crawl/UTF16/page regressions PASS; HIGH / READY по коду; local PASS, CI/CD PENDING | FUNCTIONAL PASS; VISUAL N/A. T1 UI/bot/outcome + T3 reply entities и G1 current-main; commands/callbacks/доступ/пагинация/typed input/download проверены изолированно, не live Telegram. Визуальные свойства не перепроверены и не заявлены закрытыми новым visual evidence. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| UX-009 | Status смешан с settings и деталями; P2 / REFACTOR; AC-07-04; Status разделён с settings/advanced/composite, отдельные кнопки перехода; readonly routing PASS; HIGH / READY по коду; local PASS, CI/CD PENDING | FUNCTIONAL PASS / прежнее исправление сохранено. T1 UI/bot/outcome + T3 reply entities и G1 current-main; commands/callbacks/доступ/пагинация/typed input/download проверены изолированно, не live Telegram. Текущая runtime freshness/physical capacity и новый active detector scope отдельно F22/F38/F49. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| UX-010 | Splitter разрывает слова/JSON; P1 / FIX; AC-07-03 (общий renderer); `split_message`: UTF16 limit3500, слова/идентификаторы сохраняются; character fallback только для одного oversized token; boundary regression PASS; HIGH / READY по коду; local PASS, CI/CD PENDING | FUNCTIONAL PASS / прежнее исправление сохранено. T1 UI/bot/outcome + T3 reply entities и G1 current-main; commands/callbacks/доступ/пагинация/typed input/download проверены изолированно, не live Telegram. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| UX-011 | Composite-only status выглядит общим; P1 / REFACTOR; AC-07-04; E04; Основной status описывает 5families, composite snapshot явно отдельный; `/status` и panel crawl PASS; HIGH / READY по коду; local PASS, CI/CD PENDING | FUNCTIONAL PASS / прежнее исправление сохранено. T1 UI/bot/outcome + T3 reply entities и G1 current-main; commands/callbacks/доступ/пагинация/typed input/download проверены изолированно, не live Telegram. Текущая runtime freshness/physical capacity и новый active detector scope отдельно F22/F38/F49. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| UX-012 | Health/freshness/completeness смешаны; P2 / REFACTOR; AC-07-04; AC-12-03; Source age/applied revision/discovery/completeness/recovery отдельно от composite/delivery; неизвестное не считается healthy; status/diagnostics regressions PASS; MEDIUM / READY по коду; local PASS, CI/CD PENDING | FUNCTIONAL PASS / прежнее исправление сохранено. T1 UI/bot/outcome + T3 reply entities и G1 current-main; commands/callbacks/доступ/пагинация/typed input/download проверены изолированно, не live Telegram. Текущая runtime freshness/physical capacity и новый active detector scope отдельно F22/F38/F49. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| UX-013 | Snapshot cadence названа проверкой сигналов; P2 / REFACTOR; AC-03-05; AC-07-04; Cadence названа обновлением служебной сводки, не проверкой сигналов/WebSocket/TF; advanced parameters и UI crawl PASS; HIGH / READY по коду; local PASS, CI/CD PENDING | FUNCTIONAL PASS / прежнее исправление сохранено. T1 UI/bot/outcome + T3 reply entities и G1 current-main; commands/callbacks/доступ/пагинация/typed input/download проверены изолированно, не live Telegram. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| UX-014 | Selected card market/TF похожи на global scope; P2 / REFACTOR; AC-03-02; AC-07-04; Карточка/рынок ввода — отдельная панель; оба core markets/4TF явно независимы; old callbacks и perTF regressions PASS; HIGH / READY по коду; local PASS, CI/CD PENDING | FUNCTIONAL PASS / прежнее исправление сохранено. T1 UI/bot/outcome + T3 reply entities и G1 current-main; commands/callbacks/доступ/пагинация/typed input/download проверены изолированно, не live Telegram. Текущая runtime freshness/physical capacity и новый active detector scope отдельно F22/F38/F49. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| UX-015 | Failed threshold выглядит системной ошибкой; P2 / REFACTOR; AC-07-04; Composite details используют нейтральное описание достижения порога; отсутствие сигнала не красная системная ошибка; rendering/UI PASS; HIGH / READY по коду; local PASS, CI/CD PENDING | FUNCTIONAL PASS; VISUAL N/A. T1 UI/bot/outcome + T3 reply entities и G1 current-main; commands/callbacks/доступ/пагинация/typed input/download проверены изолированно, не live Telegram. Визуальные свойства не перепроверены и не заявлены закрытыми новым visual evidence. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| UX-016 | У счетчика coverage не указана единица; P2 / REFACTOR; AC-07-04; AC-12-03; Source counter обозначен потоками инструмент/рынок/TF, metadata counts отдельно; 1104/empty fixture PASS; HIGH / READY по коду; local PASS, CI/CD PENDING | FUNCTIONAL PASS / прежнее исправление сохранено. T1 UI/bot/outcome + T3 reply entities и G1 current-main; commands/callbacks/доступ/пагинация/typed input/download проверены изолированно, не live Telegram. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| UX-017 | Dump вместо результата изменения режима; P2 / REFACTOR; AC-07-03; AC-08-02; Mode/set/clear дают короткий результат/нет изменения, переход к нужной панели отдельно; mutation/readback regressions PASS; HIGH / READY по коду; local PASS, CI/CD PENDING | FUNCTIONAL PASS / прежнее исправление сохранено. T1 UI/bot/outcome + T3 reply entities и G1 current-main; commands/callbacks/доступ/пагинация/typed input/download проверены изолированно, не live Telegram. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| UX-018 | Пустой custom list и actual source scope не объяснены; P1 / FIX; AC-02-01/04; AC-08-01; Bounded owner/runtime readback подтвердил config4/custom0 при 1104source streams. Независимый reconcile loop5s+lock, guard текущей eligibility перед observation и защита старого REST refresh; empty-scope/race/observation regressions PASS; runtime fix до CD PENDING; HIGH / READY по коду; local PASS, CI/CD PENDING | FUNCTIONAL PASS / прежнее исправление сохранено. T1 UI/bot/outcome + T3 reply entities и G1 current-main; commands/callbacks/доступ/пагинация/typed input/download проверены изолированно, не live Telegram. Текущая runtime freshness/physical capacity и новый active detector scope отдельно F22/F38/F49. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| UX-019 | Неявный рынок технического ввода списка; P2 / REFACTOR; AC-02-04; AC-07-03; Add/remove показывают выбранный рынок, явные market prefixes и кнопки смены рынка; typed list/CAS regressions PASS; HIGH / READY по коду; local PASS, CI/CD PENDING | FUNCTIONAL PASS / прежнее исправление сохранено. T1 UI/bot/outcome + T3 reply entities и G1 current-main; commands/callbacks/доступ/пагинация/typed input/download проверены изолированно, не live Telegram. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| UX-020 | Add/remove prompts и отмена ввода; P2 / REFACTOR; AC-02-04; AC-07-03; Input context15min, cancel/назад/navigation, invalid input без смены owner revision, stale CAS отказ; empty remove объяснён; regressions PASS; HIGH / READY по коду; local PASS, CI/CD PENDING | FUNCTIONAL PASS / прежнее исправление сохранено. T1 UI/bot/outcome + T3 reply entities и G1 current-main; commands/callbacks/доступ/пагинация/typed input/download проверены изолированно, не live Telegram. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| UX-021 | Empty clear дает ложный результат; P2 / REFACTOR; AC-02-04; AC-07-03; Очистка уже пустого списка сообщает no-op; непустой список требует явного подтверждения с revision; owner state regressions PASS; HIGH / READY по коду; local PASS, CI/CD PENDING | FUNCTIONAL PASS / прежнее исправление сохранено. T1 UI/bot/outcome + T3 reply entities и G1 current-main; commands/callbacks/доступ/пагинация/typed input/download проверены изолированно, не live Telegram. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| UX-022 | Alerts reply entry отклоняет label; P1 / FIX; AC-07-03/05; Reply alias очищает raw label; bare alerts только panel, on/off явно и независимо от digest; routing/persistence PASS; HIGH / READY по коду; local PASS, CI/CD PENDING | FUNCTIONAL PASS / прежнее исправление сохранено. T1 UI/bot/outcome + T3 reply entities и G1 current-main; commands/callbacks/доступ/пагинация/typed input/download проверены изолированно, не live Telegram. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| UX-023 | Термины без понятных примеров; P2 / REFACTOR; AC-07-07; Короткие terms с примерами ×3/интенсивности/пробоя/свежести и отдельными формулами; help/UI crawl PASS; HIGH / READY по коду; local PASS, CI/CD PENDING | FUNCTIONAL PASS; VISUAL N/A. T1 UI/bot/outcome + T3 reply entities и G1 current-main; commands/callbacks/доступ/пагинация/typed input/download проверены изолированно, не live Telegram. Визуальные свойства не перепроверены и не заявлены закрытыми новым visual evidence. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| UX-024 | Diagnostics flood raw JSON; P1 / REFACTOR; AC-12-03; E07; Diagnostics summary/разделы; exact raw JSON отдельным файлом, readonly off-thread snapshot; file/access regressions PASS; HIGH / READY по коду; local PASS, CI/CD PENDING | FUNCTIONAL PASS / прежнее исправление сохранено. T1 UI/bot/outcome + T3 reply entities и G1 current-main; commands/callbacks/доступ/пагинация/typed input/download проверены изолированно, не live Telegram. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| UX-025 | Actual/forecast/logical/physical storage не различимы; P2 / REFACTOR; AC-13-06; AC-12-03; Physical DB+WAL/SHM факт отдельно от модели filled-window и target; indexes не прибавляются второй раз, stale snapshot отмечен; fixture PASS; HIGH / READY по коду; local PASS, CI/CD PENDING | FUNCTIONAL PASS; VISUAL N/A. T1 UI/bot/outcome + T3 reply entities и G1 current-main; commands/callbacks/доступ/пагинация/typed input/download проверены изолированно, не live Telegram. Визуальные свойства не перепроверены и не заявлены закрытыми новым visual evidence. Текущая runtime freshness/physical capacity и новый active detector scope отдельно F22/F38/F49. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| UX-026 | Optional OFF/UNSET выглядит failure/PENDING; P2 / REFACTOR; AC-09-05; AC-11-03; AC-12-03; Optional OFF/UNSET/unknown различаются; READY integration требуется для agent toggle, optional state не влияет на core; negative/unknown regressions PASS; HIGH / READY по коду; local PASS, CI/CD PENDING | FUNCTIONAL PASS / прежнее исправление сохранено. T1 UI/bot/outcome + T3 reply entities и G1 current-main; commands/callbacks/доступ/пагинация/typed input/download проверены изолированно, не live Telegram. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| UX-027 | Pending counts не объясняют due/overdue; P2 / REFACTOR; AC-06-02; AC-12-03; AC-13-05; Pending включает будущие сроки; overdue/oldest явно не измерены, PENDING не 0; queue diagnostics regression PASS; MEDIUM / READY по коду; local PASS, CI/CD PENDING | FUNCTIONAL PASS / прежнее исправление сохранено. T1 UI/bot/outcome + T3 reply entities и G1 current-main; commands/callbacks/доступ/пагинация/typed input/download проверены изолированно, не live Telegram. Текущая runtime freshness/physical capacity и новый active detector scope отдельно F22/F38/F49. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| UX-028 | Profiles не объясняют изменения/возврат custom; P2 / REFACTOR; AC-08-03; Preset preview показывает все пороги/окна до apply/CAS; один custom backup переживает reopen, explicit restore; profile regressions PASS; MEDIUM / READY по коду; local PASS, CI/CD PENDING | FUNCTIONAL PASS / прежнее исправление сохранено. T1 UI/bot/outcome + T3 reply entities и G1 current-main; commands/callbacks/доступ/пагинация/typed input/download проверены изолированно, не live Telegram. Preset preview/apply/CAS/custom restore PASS, не визуальная оценка панели. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| UX-029 | В history rows нет времени; P2 / REFACTOR; AC-06-01; E07; Каждая history row содержит исходную полную дату/МСК; keyset page regression PASS; HIGH / READY по коду; local PASS, CI/CD PENDING | FUNCTIONAL PASS / прежнее исправление сохранено. T1 UI/bot/outcome + T3 reply entities и G1 current-main; commands/callbacks/доступ/пагинация/typed input/download проверены изолированно, не live Telegram. Новая episode/history/outcome семантика не готова отдельно F50/F51/F53. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| UX-030 | History/delivery/suppressed flags не различимы; P2 / REFACTOR; AC-06-01; AC-07-05; Observation type и сохранённый Telegram decision/receipt раздельно; отсутствие receipt не приравнено к неотправке; muted/event JSON regression PASS; MEDIUM / READY по коду; local PASS, CI/CD PENDING | FUNCTIONAL PASS / прежнее исправление сохранено. T1 UI/bot/outcome + T3 reply entities и G1 current-main; commands/callbacks/доступ/пагинация/typed input/download проверены изолированно, не live Telegram. Новая episode/history/outcome семантика не готова отдельно F50/F51/F53. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| UX-031 | History entrypoint ограничен 12 без навигации; P2 / IMPLEMENT; E06/E07: уточнение навигации, не новый AC; History keyset cursor открывает более ранние retained events без fixed12/OFFSET/full scan; distinct-page regression PASS; MEDIUM / READY по коду; local PASS, CI/CD PENDING | FUNCTIONAL PASS / прежнее исправление сохранено. T1 UI/bot/outcome + T3 reply entities и G1 current-main; commands/callbacks/доступ/пагинация/typed input/download проверены изолированно, не live Telegram. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| UX-032 | Outcomes reply entry разбирает label как UUID; P1 / FIX; E07; AC-07-06: event→outcome отдельно; Bare Outcomes открывает выбор события; UUID нужен только explicit path, frozen reference/horizons сохранены; routing/outcome PASS; HIGH / READY по коду; local PASS, CI/CD PENDING | FUNCTIONAL PASS / прежнее исправление сохранено. T1 UI/bot/outcome + T3 reply entities и G1 current-main; commands/callbacks/доступ/пагинация/typed input/download проверены изолированно, не live Telegram. Новая episode/history/outcome семантика не готова отдельно F50/F51/F53. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| UX-033 | Analytics/compare reply entries разбирают labels как groups; P1 / FIX; AC-06-05; E07; Bare analytics/compare открывает выбор разреза; страницы frozen20-group snapshot, exact download; completed-result/expiry regressions PASS; HIGH / READY по коду; local PASS, CI/CD PENDING | FUNCTIONAL PASS / прежнее исправление сохранено. T1 UI/bot/outcome + T3 reply entities и G1 current-main; commands/callbacks/доступ/пагинация/typed input/download проверены изолированно, не live Telegram. Новая episode/history/outcome семантика не готова отдельно F50/F51/F53. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| UX-034 | Digest reply entry требует command syntax; P1 / FIX; AC-11-02; AC-07-05; Bare Digest открывает readonly panel; toggle/interval явные, preference независима; routing/state PASS; HIGH / READY по коду; local PASS, CI/CD PENDING | FUNCTIONAL PASS / прежнее исправление сохранено. T1 UI/bot/outcome + T3 reply entities и G1 current-main; commands/callbacks/доступ/пагинация/typed input/download проверены изолированно, не live Telegram. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| UX-035 | Derivatives context путается с core Perpetual; P2 / DOCUMENT; AC-11-03/04; Optional Spot OI/funding/basis назван отдельно от core Perpetual detection; current vs frozen context объяснён; UI/enrichment regressions PASS; MEDIUM / READY по коду; local PASS, CI/CD PENDING | FUNCTIONAL PASS / прежнее исправление сохранено. T1 UI/bot/outcome + T3 reply entities и G1 current-main; commands/callbacks/доступ/пагинация/typed input/download проверены изолированно, не live Telegram. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| UX-036 | Agent reply entry требует command syntax; P1 / FIX; AC-09-05; D23; Bare agent/status показывает подключение/очередь; explicit on/off/retry/replay сохранены, просмотр не запускает delivery; routing/guard PASS; HIGH / READY по коду; local PASS, CI/CD PENDING | FUNCTIONAL PASS / прежнее исправление сохранено. T1 UI/bot/outcome + T3 reply entities и G1 current-main; commands/callbacks/доступ/пагинация/typed input/download проверены изолированно, не live Telegram. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| UX-037 | Главное меню 21buttons/11rows; P2 / REFACTOR; AC-07-03; Root6buttons/3rows; вторичные действия внутри 6 разделов, inline panels редактируют существующее сообщение; crawl/quiet-fallback PASS; HIGH / READY по коду; local PASS, CI/CD PENDING | FUNCTIONAL PASS; VISUAL N/A. T1 UI/bot/outcome + T3 reply entities и G1 current-main; commands/callbacks/доступ/пагинация/typed input/download проверены изолированно, не live Telegram. Визуальные свойства не перепроверены и не заявлены закрытыми новым visual evidence. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| UX-038 | Плоский каталог 32commands; P2 / REFACTOR; AC-07-03; Published9core commands, все старые handler identifiers сохранены; полный reference/аргументы/manual fields доступны в Help; routing/crawl PASS; HIGH / READY по коду; local PASS, CI/CD PENDING | FUNCTIONAL PASS; VISUAL N/A. T1 UI/bot/outcome + T3 reply entities и G1 current-main; commands/callbacks/доступ/пагинация/typed input/download проверены изолированно, не live Telegram. Визуальные свойства не перепроверены и не заявлены закрытыми новым visual evidence. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| UX-039 | /start включает Alerts вместо простого открытия; P2 / FIX; AC-07-05; согласованный UX-контракт read-only entry; `/start` только открывает меню, повторный вход сохраняет Alerts OFF и независимые preferences; readonly/persistence regression PASS; MEDIUM / READY по коду; local PASS, CI/CD PENDING | FUNCTIONAL PASS / прежнее исправление сохранено. T1 UI/bot/outcome + T3 reply entities и G1 current-main; commands/callbacks/доступ/пагинация/typed input/download проверены изолированно, не live Telegram. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| UX-040 | Manual button ведет в help; P2 / IMPLEMENT; AC-08-03; AC-07-03; Legacy `st:manual` ведёт к группам параметров, ввод/cancel доступны; compatibility regression PASS; HIGH / READY по коду; local PASS, CI/CD PENDING | FUNCTIONAL PASS / прежнее исправление сохранено. T1 UI/bot/outcome + T3 reply entities и G1 current-main; commands/callbacks/доступ/пагинация/typed input/download проверены изолированно, не live Telegram. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| UX-041 | Price check selected TF при записи всем TF; P2 / FIX; AC-08-03; AC-07-04; Per-TF fields сохраняют остальные TF; all-TF действие явно подписано и показывает 4 текущих значения, старый st callback совместим; heterogeneous TF regression PASS; HIGH / READY по коду; local PASS, CI/CD PENDING | FUNCTIONAL PASS / прежнее исправление сохранено. T1 UI/bot/outcome + T3 reply entities и G1 current-main; commands/callbacks/доступ/пагинация/typed input/download проверены изолированно, не live Telegram. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F01 | S01 полностью меняет прежний Spot MVP; отсутствовали AC и трассировка; product_scope.md / все эпики; P1; CONSOLIDATE: Scope источника, без новых features.; S01/S05 полностью сверены; D22/E13,65 stable AC; PR19 и PR37/39.; HIGH / CLOSED | CLOSED / сохраняется исправление. Прежний дефект не воспроизводится по current code и профильным tests; историческое исправление сохранено. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F02 | Legacy schema1/storage/recovery формулировки противоречили current schema2; README/state_contract/architecture/agent-delivery; P2: риск выбора неверного backup/rollback path; CONSOLIDATE: current contract поднят перед legacy, logical v1 отделен от physical2, routine disk backup имеет один canonical runbook; useful historical anchors/tooling сохранены; G1/PR37 адаптация + PR41/main2e242abf. Сверка Store/Sources/History/receiver dispatch, relative links и local docs PASS; CI38901694618 PASS, merged41, postmerge38901939152 docs-only PASS; HIGH / CLOSED | CLOSED / сохраняется исправление. Прежний дефект не воспроизводится по current code и профильным tests; историческое исправление сохранено. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F03 | AGENTS.md и ci-cd-rules.md отсутствуют в repository; root; AC-12-02; P1; DOCUMENT: Процедуры существуют и используются.; Root AGENTS/ci-cd-rules/router tracked; strict ci-required и production Environment подтверждены 16Oct.; HIGH / CLOSED | CLOSED / сохраняется исправление. Прежний дефект не воспроизводится по current code и профильным tests; историческое исправление сохранено. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F04 | Quickstart обрывается, нет canonical install/validation/environment процедуры; README.md: конец незакрытого bash fence; requirements/Dockerfile; P2; DOCUMENT: Format/links/fences проверены; команды не дублируются в плане.; README canonical Python3.11/hash-lock/CLI/Docker commands; validate_docs и image checks CI38897051479 PASS.; HIGH / CLOSED | CLOSED / сохраняется исправление. Прежний дефект не воспроизводится по current code и профильным tests; историческое исправление сохранено. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F05 | Access policy fail-open и неполный guard; код 261–277, 1408–1415; AC-01-04; P1; FIX: Owner private233382575; доступ не открывается при missing config.; config.authorized + единый Controller.handle guard; stranger/group/empty/invalid/bot identity fixtures, full CI731 PASS.; HIGH / CLOSED | CLOSED / сохраняется исправление. Прежний дефект не воспроизводится по current code и профильным tests; историческое исправление сохранено. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F06 | load_state перезаписывает corrupt/unknown-version state; код 421–450; AC-10-03; P1; FIX: Не заменять corrupt state пустой DB.; Store.initialize/migrate/restore отказывают unknown/corrupt state; tests_storage/market_reset_delivery и реальный reset private equality PASS.; HIGH / CLOSED | CLOSED / сохраняется исправление. Прежний дефект не воспроизводится по current code и профильным tests; историческое исправление сохранено. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F07 | Невалидные persisted settings могут прервать весь startup; код 344–399; AC-08-02/10-03; P1; FIX: Startup не подменяет сохраненные настройки defaults.; OwnerSettings валидирует finite/ranges/lookbacks/horizons/retention; CAS update сохраняет прошлую config при отказе; config/bot tests PASS.; HIGH / CLOSED | CLOSED / сохраняется исправление. Прежний дефект не воспроизводится по current code и профильным tests; историческое исправление сохранено. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F08 | Нет проверки непрерывности и полноты candles; код 918–961; AC-03-04; P1; FIX: Отсутствие полных данных дает INCOMPLETE/diagnostic, не zero return.; Sources/BybitRadar/AnalyticsRecovery: continuity, duplicate/out-of-order/gaps/backfill tests; runtime gap/recovery0 в bounded readback.; HIGH / CLOSED | CLOSED / сохраняется исправление. Прежний дефект не воспроизводится по current code и профильным tests; историческое исправление сохранено. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F09 | Нет event timestamps/TTL; closed определяется только local clock; код 919–945; AC-05-04; P1; IMPLEMENT: Freshness независима от worker/outcome времени.; Bybit response clock/WS confirm, type/TF TTL и replay/future-clock tests PASS; receipt/retry не освежает market time.; HIGH / CLOSED | CLOSED / сохраняется исправление. Прежний дефект не воспроизводится по current code и профильным tests; историческое исправление сохранено. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F10 | Fixed100/одна category, eligibility без metadata; код 899–913, 987–1009; AC-02-01..03; P1; IMPLEMENT: Нет fixed Top100; watchlist typed.; Metadata Spot без pagination/linear cursor, type/status/USDT/quarantine/liquidity; test_universe/ingestion, runtime оба рынка 1248 streams.; HIGH / CLOSED | CLOSED / сохраняется исправление. Прежний дефект не воспроизводится по current code и профильным tests; историческое исправление сохранено. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F11 | Нет WS и одновременно работающих TF; код 963–1027, requirements; AC-03-01/02; P1; IMPLEMENT: REST recovery имеет shared budget, historical data не дает live flood.; Concurrent1/3/5/15m, ACK/read loop separation PR35; CI ingestion/reconnect tests и normal CD39 PASS.; HIGH / CLOSED | CLOSED / сохраняется исправление. Прежний дефект не воспроизводится по current code и профильным tests; историческое исправление сохранено. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F12 | Реализован только legacy composite; код 918–961; AC-04-02/03; P1; IMPLEMENT: Composite развивается как одно семейство.; Five pure versioned detector families/effective parameters; fixtures boundaries/negative/neutral/zero/gaps и live event sample обоих markets/TF.; HIGH / CLOSED | CLOSED / сохраняется исправление. Прежний дефект не воспроизводится по current code и профильным tests; историческое исправление сохранено. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F13 | Cooldown без event identity не заменяет episodes; код 2090–2105; AC-05-01..03; P1; IMPLEMENT: Episode aggregation не удаляет source events.; EventEngine episode/re-arm/maxlife/strength/coalescing/restart fixtures, durable source revisions.; HIGH / CLOSED | CLOSED / сохраняется исправление. Прежний дефект не воспроизводится по current code и профильным tests; историческое исправление сохранено. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F14 | Failed Telegram send расходует cooldown без retry; код 2101–2120; AC-07-01/05; P1; FIX: Failed send не расходует successful notification state.; TelegramDelivery durable retries/retry_after/ambiguous ACK/permanent target и success-baseline fixtures; runtime DELIVERED.; HIGH / CLOSED | CLOSED / сохраняется исправление. Прежний дефект не воспроизводится по current code и профильным tests; историческое исправление сохранено. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F15 | Исторически отсутствовали history/outcome/analytics/export; E06; P1; IMPLEMENT: подсистемы поставлены; обнаруженный archived-period defect выделен F48, не закрывать весь 06-06 прежним PASS; G3 + bounded format/PR39/F35; T1 numeric/cohort/causal/full-period equality; R2 sampled128COMPLETE; HIGH / исходная подсистема CLOSED; F48 OPEN | CLOSED / сохраняется исправление. Прежний дефект не воспроизводится по current code и профильным tests; историческое исправление сохранено. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F16 | Market domain зависит от chat; profiles/manual settings не реализованы; код 302–399, 1084–1133, 1759–1795; E08/AC-02-04; P1; IMPLEMENT: CAS/no-op/invalid settings tests и hash readback PASS.; Owner revision3 private preservation; profiles/manual/typed watchlist/enrichment/horizons/retention отдельно от delivery preferences/secrets.; HIGH / CLOSED | CLOSED / сохраняется исправление. Прежний дефект не воспроизводится по current code и профильным tests; историческое исправление сохранено. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F17 | Исторически отсутствовал producer/handoff; E09; P1; IMPLEMENT/DOCUMENT: producer готов; live credentials/binding/activation receiving-project scope S06/D23; T1/H1 controlled HMAC/ack/expiry/restart/no-authority; R2 UNSET/OFF не мешает manual radar; HIGH / PRODUCER_READY_HANDOFF; activation transferred | CLOSED / сохраняется исправление. Прежний дефект не воспроизводится по current code и профильным tests; историческое исправление сохранено. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F18 | Исторически нет digest и независимых toggles; AC-11-01/02; P2; IMPLEMENT: scheduler/toggles/retries поставлены; содержимое не полностью соответствует source, F47; test_digest four scheduler/retry/race cases PASS; T2 active-coin ranking отсутствует; HIGH / schedule CLOSED; aggregation remainder F47 OPEN | CLOSED / сохраняется исправление. Прежний дефект не воспроизводится по current code и профильным tests; историческое исправление сохранено. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F19 | Нет derivatives context для Spot; весь fetch layer; AC-11-03/04; P2; IMPLEMENT: Optional feature может быть OFF; new market source не добавлен.; G5 exact asset mapping/OI/funding/basis/source timestamps/cadence/fault isolation; CI/CD27 PASS, current suite includes enrichment regressions.; HIGH / CLOSED | CLOSED / сохраняется исправление. Прежний дефект не воспроизводится по current code и профильным tests; историческое исправление сохранено. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F20 | Ошибки источника маскировались как no-signal; AC-04-04/12-03; P1; FIX: bounded independent source/worker/delivery state; query budget дает PENDING; T1 async/ingestion/detector fault tests; R1 allACK/zero source blockers, analytics/recovery/repackOK; HIGH / CLOSED | CLOSED / сохраняется исправление. Прежний дефект не воспроизводится по current code и профильным tests; историческое исправление сохранено. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F21 | Settings могут смениться во время fetch, old result доставляется как active; код 2068–2112, status_cmd; AC-07-04/08-02; P1; FIX: Нет silent применения старого результата к новой config.; Settings revision CAS/frozen notification and worker snapshots, old-result discard/send-time race fixtures PASS.; HIGH / CLOSED | CLOSED / сохраняется исправление. Прежний дефект не воспроизводится по current code и профильным tests; историческое исправление сохранено. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F22 | Legacy monolith/per-chat fanout устранены; дальнейшие hot-path costs требуют измерения; AC-12-03; P2; REFACTOR/DOCUMENT/DEFER: сохранять shared data/короткие transactions; profile новых подтвержденных узких мест. Digest aggregation внутри writer transaction — кандидат без измеренного production impact; Module/source review, T1 yielding/CAS; R1 1104streams. F46 local benchmark отдельно, не actual production effect. Sustained load/SLO не измерялись; HIGH по former fix / MEDIUM по further tuning; DEFER | OPEN / актуализировано. Общий поток не успевает за работой T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F23 | CMC/source/core/reset действия остаются достижимыми вне v1; код 536–874, 1583–1643, 1798–1881, 1967–2041, 2163–2168; AC-01-03; P2; DEPRECATE: Основание удаления UX — D01-D04, не отсутствие imports.; Активных CMC/reset network handlers нет; old callbacks объясняют изменение. Startup wrapper и legacy import compatibility сохранены.; HIGH / CLOSED | CLOSED / сохраняется исправление. Прежний дефект не воспроизводится по current code и профильным tests; историческое исправление сохранено. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F24 | Невызванные legacy helpers/caches; внешние consumers неизвестны; код _fetch_async/_prepare_core_context/tracked_desc/price_unit/_mode_label/_rank_prefix/_sub_metrics/_directional_marker; P3; DEFER: Новое удаление без необходимости не выполнено. Consolidate duplicate helper при затрагивании diagnostics; source-pinned tooling сохраняется.; Original private helpers отсутствуют в текущем wrapper/module inventory после G1. Historical converter/native helpers используются CLI/tests/retained archives, не dead. Controller.diagnostics старый дублирующий helper не вызывается production CLI; внешние users UNSET.; MEDIUM / DOCUMENT / DEFER | DEFER / кандидаты. Нет доказанного отсутствия scripts/CLI/native/compatibility callers. Одна статическая import-map не основание REMOVE. Новые codecs/two-writer artifacts исследовать по актуальной пользе. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F25 | Изначально нет regression suite; E01/E03/E08/E10/E12; P1; IMPLEMENT: содержательные auth/data/restart/contracts/retention tests; отсутствие regression на новые края отражено F47/F48; 46modules/441test functions на affdd7b; T1 574PASS/3platform skips; G1 full Linux/native/image PASS; HIGH / suite CLOSED; новые содержательные gaps F47/F48 | CLOSED / сохраняется исправление. Прежний дефект не воспроизводится по current code и профильным tests; историческое исправление сохранено. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F26 | Historical installer advisory versions; AC-12-04; P2; FIX/READY: pinned bootstrap pip26.2.1/setuptools84.0.0 and Docker context allowlist delivered; current build/image/runtime versions verified24Oct. This closes the selected version remediation, not an exhaustive current vulnerability claim.; Prior D2 advisory snapshot remains historical. Exact-source PR85 CI40061968452 and trusted release40062953706/build120780191578:2294PASS697.97s, image/archive PASS; production120782967668 and primary c47a15be PASS. Read-only runtime container a4893b22/image6d4c3c0d confirms pip26.2.1/setuptools84.0.0, APSW3.53.4.0/Zstandard0.25.0 in `pr85-installer-versions-v1.json`; no installs, fresh OSV/OS scan or exploitability claim.; HIGH source/build/runtime version mapping; READY selected fix | OPEN / актуализировано. Новые advisory зафиксированных зависимостей T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F27 | Исторически отсутствовали pipeline/gates и qualified ordinary backup; AC-12-02; P1; FIX: main protection/CI/artifact/ordinary full integrity+backup восстановлены, <=900s; failures сохраняются в primary records; G1/D1 exact main56 CI38999539232/release38999831287 PASS; production210s/full disk validation52.526s, no gate exception; HIGH / CLOSED | CLOSED / сохраняется исправление. Прежний дефект не воспроизводится по current code и профильным tests; историческое исправление сохранено. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F28 | Target/unit/config/version первоначально UNSET; AC-12-01/02/10-04; P1; DOCUMENT: actual source/image/target/host identity известны; deploy ради статуса не повторять; R1/D1 Contabo /opt/crypto_watcher, Compose crypto_watcher, image25ee4ca2…, sourceaffdd7b, healthy; owner config equality R2; HIGH / CLOSED | CLOSED / сохраняется исправление. Прежний дефект не воспроизводится по current code и профильным tests; историческое исправление сохранено. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F29 | Env category/runtime knobs могут противоречить Spot UI и raw input; BYBIT_CATEGORY, settings_text/status_text/alert_keyboard, .env.example; P2; CONSOLIDATE: Legacy selected market не ограничивает shared ingestion.; Typed spot/linear identity, Bybit URLs, current universe/TF/effective settings и env precedence fixtures; source config revision3 readback.; HIGH / CLOSED | CLOSED / сохраняется исправление. Прежний дефект не воспроизводится по current code и профильным tests; историческое исправление сохранено. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F30 | Docker context не исключает data/ и .git;  .dockerignore, Dockerfile; P3; FIX: Data не передаются builder через широкий COPY.; Docker whitelist исключает .git/.env/data/cache; native source-only включен явно. CI build/image checks PASS.; MEDIUM / CLOSED | CLOSED / сохраняется исправление. Прежний дефект не воспроизводится по current code и профильным tests; историческое исправление сохранено. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F31 | Legacy root default; resource policy candidate; AC-12-01; P3; DOCUMENT/DEFER: hard app CPU/RAM caps без requirement/измеренного основания не добавлять; R1 UID10001:10001/read-only root/cap-dropALL/no-new-privileges/health PASS; app limits0. Maintenance4GiB/CPU1 и observer256MiB/CPU10% прежние; MEDIUM / hardening implemented; further caps DEFER | DEFER / дальнейшее hardening. Nonroot UID10001, pinned build/процедуры реализованы. Дополнительные resource/capability policies не менять по аудиту. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F32 | Legacy polish/preset strategy без конкретного нового дефекта; прежний open_issues #6/#7; E07/E08; P3; DEFER для неподтвержденного visual polish/подбора прибыльных thresholds. Конкретные 41owner findings имеют стабильные UX IDs и реализуются отдельно в TELEGRAM-UX; F32 не закрывает их автоматически.; Five-family terms/explanations/profiles/history→outcomes/comparison views и no-auto-calibration fixtures PASS.; MEDIUM / DOCUMENT / DEFER | N/A / визуальная часть исключена. Исторические visual рекомендации сохранены; без visual audit нельзя ни подтвердить, ни закрыть по вкусу. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F33 | `/sample_alert` потерял baseline Bybit/mute/coin controls и не учитывает active TF; cryptowatcher/bot.py/render.py; G1 preservation / AC-07-03; P2: неверный sample и потеря manual controls; FIX: Ограничение: owner assertion не заменяет все browser scenarios.; PR22/CD38087285142 + owner screenshot/«вроде работает»; current sample/menu/no-op tests PASS.; HIGH / CLOSED | CLOSED / сохраняется исправление. Прежний дефект не воспроизводится по current code и профильным tests; историческое исправление сохранено. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F34 | Payload/head/window/service duplication и 20GB target; AC13/E06/E10; P1; REFACTOR: shared/typed records, exact numeric summaries, finite dependencies/repack внедрены. Текущая задача — подтвердить filled-window cost и причины/model, без нового reset/старой conversion; R1 actual repack COMPLETE; R2 working2.803GB. Current point162.8GB и isolated scenarios100.9–164.8GB — model, не steady day/upper bound; F42/F46; HIGH / fixes delivered; capacity fit UNPROVEN | OPEN / актуализировано. Физическая вместимость и backup peak не квалифицированы T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F35 | AC-06-05/06-06/10-02/13-03; quality_summary.archive_event выбирал global method/latest event revision; P1; FIX: все methods/comparator versions, frozen revisions и metadata-only expiry reader; Repros before expiry методы 1/2→после только 1 и strength1→101. PR39/main130225d2, CI38897051479 731 PASS/image equality; normal CD38897380425 PASS. Source/candidate quality0 еще не достиг 30d; переписывать summaries не требовалось; HIGH / CLOSED | CLOSED / сохраняется исправление. Прежний дефект не воспроизводится по current code и профильным tests; историческое исправление сохранено. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F36 | AC-09-01/09-02; actual consumer PublicSettings отвергает новые retention metadata; P1; FIX: backward-compatible optional positive ints, полный snapshot/hash без fabricated defaults; unknown/secret fields остаются forbidden; Actual cacdfda parser40 fixtures FAIL только detail_retention_days/candle_retention_days. Consumer PR949 merged8c207edf; CI38898629578/38899151685 и CD38899376113 PASS. Actual parser40/40 payloads и 40/40 authority rejects, snapshot/hash preservation/legacy/invalid metadata fixtures PASS; no events sent; HIGH / CLOSED | CLOSED / сохраняется исправление. Прежний дефект не воспроизводится по current code и профильным tests; историческое исправление сохранено. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F37 | Локально заполненные/ownerless ranges оставались PENDING; AC-03-04/13-05; P1; FIX: local completeness/reuse, owner-first terminal retirement/re-arm, fairness и bounded duty; T1 analytics_recovery/capacity/maintenance; G1/D1 main56. R1 historical backlog0; R2 current8jobs с fresh worker, их наличие не новый старый backlog; HIGH / CLOSED | CLOSED / сохраняется исправление. Прежний дефект не воспроизводится по current code и профильным tests; историческое исправление сохранено. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F38 | Gross queue turnover умножался на 30d и общий page-factor на годовые facts; AC-13-06; P1; FIX: separate lifetimes/queue occupancy/retained slope и explicit measured isolated scenarios; текущая корректность steady forecast остается F42; T1 forecast/no-double-index/count tests; R2 negative pending net не умножается как retained detail, synthetic models помечены; G1/D1 поставлено; HIGH / mechanism CLOSED; capacity evidence F42 OPEN | OPEN / актуализировано. Модель хранения остаётся моделью прежнего scope T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F39 | Cold windows/starved first market/TF buckets; AC-03-01; P1; FIX: local restore/yield/seed fairness/affected gaps; прежние deadlines/budgets; G1/D1 PR56 normalCD PASS210s; R1 both markets/4TF,1104streams, all12shardsACK; old failed CD46/48/50 retained; HIGH / CLOSED | CLOSED / сохраняется исправление. Прежний дефект не воспроизводится по current code и профильным tests; историческое исправление сохранено. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F40 | Oversized linear shard и continuously-ready receive обходили ACK deadline; AC-03-01/02; P1; FIX: <=100topics/shard и independent strict15s ACK before candle handling; T1 continuous update timeout/fake WS/allACK tests; R1 Spot336/linear772topics в 12shards/allCONNECTED; no coverage cuts; HIGH / CLOSED | CLOSED / сохраняется исправление. Прежний дефект не воспроизводится по current code и профильным tests; историческое исправление сохранено. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F41 | Decision head/revision дублировали полный payload; AC-13-01; P2; FIX: compact checked pointer/durable finite repack, no public value changes; T1 pointer/corruption/rollback/restart tests; R1 decisions pass COMPLETE,60352heads compacted; public64fixed hashes/private preservation D1; HIGH / CLOSED | CLOSED / сохраняется исправление. Прежний дефект не воспроизводится по current code и профильным tests; историческое исправление сохранено. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F42 | Полная real cohort cardinality/24h deltas/filled-window estimate не подтверждены; AC-13-06; P1; IMPLEMENT: дождаться существующего finite observer без ожидания агента; учесть partial boundary и interruption flags. F46 подготовлен отдельно, до resume/конца замера не deploy; R3 baseline runtime ledger2259582rows MEASURED, prospective114956event revisions/37736cohort-days PARTIAL_BUDGET. R2 sample26/5queue interruptions; final/result PENDING; HIGH / IN_PROGRESS; target UNPROVEN | OPEN / актуализировано. Нет приёмки облегчённой версии 30min/24h T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F43 | Малый fixture не покрывал rolling population/реальные 20comparator arrays/methods; AC-13-02/03/05; P1; IMPLEMENT: controlled62week plateau/rolling cleanup/GC/restart + richer comparator/method boundaries; T1/G1 main56 exact numeric/group/method/settings/comparator multiset preserved; fully expired GC empty. Synthetic mix, не annual production capacity. Period query gap отдельно F48; HIGH / lifecycle proof CLOSED; F48 separate | CLOSED / сохраняется исправление. Linux current-main2485tests/retention tests подтверждают реализованный старый lossless proof. Обязательный N20/mass-research scope заменён D29; полезные integrity/GC tests не удалять. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F44 | Ownerless source scan и terminal receipt мешали новым valid owners; AC-13-05; P1; FIX: owner-first retirement/no source mutation и terminal re-arm при valid retained owner; T1 ownerless scan forbidden/arrived-data/noREST/re-arm tests; G1/D1 main56/current worker R1/R2; HIGH / CLOSED | CLOSED / сохраняется исправление. Прежний дефект не воспроизводится по current code и профильным tests; историческое исправление сохранено. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F45 | Backfill/repack starvation/transient current queue мешали baseline; E06/E13; P1; FIX: independent duty/fair scheduling/finite physical boundary; bounded prebaseline settling, interruptions OBSERVING не скрывать; R1 repackCOMPLETE/historical backlog0. PR56 CI/CD PASS; R3 actual baseline17Oct07:02:41MSK/observerOBSERVING. Суточный вывод отдельно F42; HIGH / CLOSED; observation in progress | CLOSED / сохраняется исправление. Исторический starvation fix сохранён. Новый/текущий общий backlog отдельно F22/F42, старый PASS не переносится на устойчивость. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F46 | Validated typed records previously passed decode→JSON→parse; varint byte slicing; diagnostics/F42; P2; REFACTOR/READY code,24Oct source reconciliation: original PR57 compact_json.unpack, record_codec._typed_value and Store.json_payload are exact AST in delivered638bd23; original typed dispatch retained alongside later frozen-head dispatch. All6original test functions exact. No duplicate code merge required; PR57 remains OPEN DRAFT and its pre-existing branch is preserved. Whole-service backlog remains W02, not closed by this finding.; Primary GitHub PR57 head5be499da/4source-test blob IDs mapped in `f46-pr57-current-source-mapping-v2.json`; current PR84 CI40043326332 and trusted build40044686290 run canonical full2248test suite on matched tree2502bfac; exact-source CD120727335184 PASS638bd23. Historical12005event/156133row median1.832→1.364s is the old component snapshot, not measured current VPS speedup.; HIGH source/test/delivery mapping; current live performance effect UNPROVEN | CLOSED / сохраняется исправление. F46 current-source.json: _typed_value и json_payload AST совпадают с 638bd23; unpack дополнен native dispatch, прежний portable fallback сохранён (diff checked). G1 current-main native/portable tests PASS. Прежнее полное AST-совпадение относилось к 638bd23. PR57 DRAFT, повторный merge не нужен; нынешний whole-service performance effect не доказан. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F47 | digest.aggregate/text не показывают наиболее активные instruments; AC-11-01/source S01 digest; P2; FIX: отдельный activity список из общей history; strongest list/count не заменяет его. Ranking metric уточнить при реализации; T2 on affdd7b:25events/6instruments, BUSYUSDT20events отсутствует, top5 сильных singleton episodes вместо activity. Existing test_digest не проверяет этот край; live digestOFF; HIGH / OPEN; IN_PROGRESS AC-11-01 | CLOSED / сохраняется исправление. digest.aggregate/text теперь содержит activity ranking и наиболее активные instruments; T1 test_digest PASS. Новая единица uniqueepisode не готова отдельно F53, это не повтор F47. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |
| F48 | quality_summary.union_sql ceil(start/DAY), export.ndjson floor(start/DAY); CSV без effective bounds; AC-06-06/13-03; P1; FIX/CONSOLIDATE: единая явная UTC-day selection policy Q07 и consistent CSV/NDJSON/cohorts; не восстанавливать выдуманную intraday точность; T2 isolated before/after expiry intraday period: CSV5→0, unfiltered5, NDJSON20blocks/5completefacts. Numeric facts сохранены; production epoch еще<30d, runtime impact пока prospective; HIGH / OPEN; IN_PROGRESS AC-06-06/13-03 | CLOSED / сохраняется исправление. quality_summary.period_metadata унифицирована для cohort union/export CSV/NDJSON, effective UTC-day bounds явны; T2 test_export и analytics tests PASS. Новая episode statistics отдельно F53. T1/T2/T3/G1 + source b254; соответствующий AC matrix; confidence HIGH code/test; live ограничения. |

## Связанные engineering и security IDs отменённой Goal

84IDs сохранены как связанные findings/evidence, а не 84 новых независимых defects. Частные оптимизации не закрывают общий F22/F42. Provider Codex Security не запрашивался, карточки не объявлены закрытыми. Исторический exact record каждого ID сохранён в локальном legacy-engineering-ids.json и Git b254e6e.

| ID | Актуальное решение / связь / основание |
| --- | --- |
| CAP25-RETRY-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-CD-IDENTITY-001 | FIX PRESERVED; AC-12-02/04. Текущий receiver/security tests и primary deployment record сохраняют separate ZIP/inner bundle/image/source identity. |
| CAP25-BACKUP-STABILITY-001 | PRESERVED / REASSESS; F34/F42. Малые successful current backup не закрывают large cost/time/resource qualification. Сохраняется как размерно-зависимый gap, без повторной конвертации старой истории. |
| CWSEC-001 | SOURCE FIXES PRESERVED / PROVIDER NOT QUERIED; AC-12-04/F26. Source patches/security tests/CI текущего main сохранены. Точный provider status не читался, закрытие карточек не заявляется; Codex Security не вызывался. |
| CWSEC-004 | SOURCE FIXES PRESERVED / PROVIDER NOT QUERIED; AC-12-04/F26. Source patches/security tests/CI текущего main сохранены. Точный provider status не читался, закрытие карточек не заявляется; Codex Security не вызывался. |
| CWSEC-007 | SOURCE FIXES PRESERVED / PROVIDER NOT QUERIED; AC-12-04/F26. Source patches/security tests/CI текущего main сохранены. Точный provider status не читался, закрытие карточек не заявляется; Codex Security не вызывался. |
| CWSEC-002 | SOURCE FIXES PRESERVED / PROVIDER NOT QUERIED; AC-12-04/F26. Source patches/security tests/CI текущего main сохранены. Точный provider status не читался, закрытие карточек не заявляется; Codex Security не вызывался. |
| CWSEC-003 | SOURCE FIXES PRESERVED / PROVIDER NOT QUERIED; AC-12-04/F26. Source patches/security tests/CI текущего main сохранены. Точный provider status не читался, закрытие карточек не заявляется; Codex Security не вызывался. |
| CWSEC-005 | SOURCE FIXES PRESERVED / PROVIDER NOT QUERIED; AC-12-04/F26. Source patches/security tests/CI текущего main сохранены. Точный provider status не читался, закрытие карточек не заявляется; Codex Security не вызывался. |
| CWSEC-006 | SOURCE FIXES PRESERVED / PROVIDER NOT QUERIED; AC-12-04/F26. Source patches/security tests/CI текущего main сохранены. Точный provider status не читался, закрытие карточек не заявляется; Codex Security не вызывался. |
| CWSEC-008 | SOURCE FIXES PRESERVED / PROVIDER NOT QUERIED; AC-12-04/F26. Source patches/security tests/CI текущего main сохранены. Точный provider status не читался, закрытие карточек не заявляется; Codex Security не вызывался. |
| UX-CD-01 | PRESERVED / REASSESS; F34/F42. Малые successful current backup не закрывают large cost/time/resource qualification. Сохраняется как размерно-зависимый gap, без повторной конвертации старой истории. |
| CAP25-NATIVE-CHECK-001 | CORRECT PRODUCT PATH / OLD EXPERIMENT LIMIT; AC-10-03/F42. disk_validation уже logicalRW+query_only+ignore_check_constraintsOFF; G1 native/disk negative CHECK tests PASS. Старый qualifier не доказывал CHECK, его исторический PASS не расширен. |
| CAP25-PASSIVE-DURABILITY-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-QUEUE-FAIRNESS-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-CONTEXT-ADMISSION-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-SNAPSHOT-SETTINGS-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-ATTRIBUTION-GAP-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-NUMERIC-LEASE-COMMON-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-DIRECT-SHARED-READ-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-SOURCE-WIRE-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-NATIVE-READ-STAGES-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-DIRECT-PUBLICATION-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-DIRECT-ACTUAL-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-DIRECT-MIXED-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-DIRECT-PRODFORMAT-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-DIRECT-SETTLED-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-SHAREDHOST-24OCT | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-DUE-ATTRIBUTION-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-SHAREDHOST-PR82 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-TRANSIENT-PR82 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-CONDITION-WRITE-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-OBSPIPE-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-SHAREDHOST-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-EXPIRYUPSERT-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-PARSE-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-PIPELINE-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-READINIT-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-POSTCHECK-DIAG-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-SOURCE-VALIDATION-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-WRITER-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-READ-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-CTXINDEX-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-BOOST-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-MISFIRE-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-OBSCPU-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-SERVICE-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-SHAREDVIEW-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-SHAREDBACKUP-001 | PRESERVED / REASSESS; F34/F42. Малые successful current backup не закрывают large cost/time/resource qualification. Сохраняется как размерно-зависимый gap, без повторной конвертации старой истории. |
| CAP25-LIVEPIPE-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-NUMFRAME-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-DELIVERY-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-NFBOUND-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-DICTCPU-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-CHURN-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-REFCPU-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-OWNCPU-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-DENSITY-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-CONTEXT-BOUND-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-PRIORITY-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-CONTEXTCPU-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-CONTEXTCPU-002 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-POPULATION-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-PROBE-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-CI-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-LEASE-001 | PRESERVED / REASSESS; F52/F54/AC-13-05. Сохранность/finite TTL/integrity полезны; прежние 35d/N20/90–180d объёмы superseded D29. Конкретные active callers нужны перед REMOVE; наличие старого finding не требует старый scope. |
| CAP25-QUEUE-001 | PRESERVED / REASSESS; F52/F54/AC-13-05. Сохранность/finite TTL/integrity полезны; прежние 35d/N20/90–180d объёмы superseded D29. Конкретные active callers нужны перед REMOVE; наличие старого finding не требует старый scope. |
| CAP25-PACK-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-BOUND-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-COMPAT-002 | PRESERVED / REASSESS; F52/F54/AC-13-05. Сохранность/finite TTL/integrity полезны; прежние 35d/N20/90–180d объёмы superseded D29. Конкретные active callers нужны перед REMOVE; наличие старого finding не требует старый scope. |
| CAP25-RET-001 | PRESERVED / REASSESS; F52/F54/AC-13-05. Сохранность/finite TTL/integrity полезны; прежние 35d/N20/90–180d объёмы superseded D29. Конкретные active callers нужны перед REMOVE; наличие старого finding не требует старый scope. |
| CAP25-CODEC-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-COMPAT-001 | PRESERVED / REASSESS; F52/F54/AC-13-05. Сохранность/finite TTL/integrity полезны; прежние 35d/N20/90–180d объёмы superseded D29. Конкретные active callers нужны перед REMOVE; наличие старого finding не требует старый scope. |
| CAP25-RET-002 | PRESERVED / REASSESS; F52/F54/AC-13-05. Сохранность/finite TTL/integrity полезны; прежние 35d/N20/90–180d объёмы superseded D29. Конкретные active callers нужны перед REMOVE; наличие старого finding не требует старый scope. |
| CAP25-METER-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-RECLAIM-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| CAP25-RECOVERY-001 | PRESERVED / REASSESS; F22/F38/F42. Частный experiment/evidence прежней отменённой Goal; не считать whole-system fix или новой обязательной задачей. Полезный реализованный механизм сохраняется до caller/нового full-path proof. |
| O01 | PRESERVED / REASSESS; F42. Старые observer/checkpoint records сохранены историческими. Baseline/new24h не принят по их существованию; после отмены старая Goal не возобновлена. |
| O02 | PRESERVED / REASSESS; F42. Старые observer/checkpoint records сохранены историческими. Baseline/new24h не принят по их существованию; после отмены старая Goal не возобновлена. |
| O03 | PRESERVED / REASSESS; F42. Старые observer/checkpoint records сохранены историческими. Baseline/new24h не принят по их существованию; после отмены старая Goal не возобновлена. |
| O04 | PRESERVED / REASSESS; F42. Старые observer/checkpoint records сохранены историческими. Baseline/new24h не принят по их существованию; после отмены старая Goal не возобновлена. |
| O02-CURRENT | PRESERVED / REASSESS; F42. Старые observer/checkpoint records сохранены историческими. Baseline/new24h не принят по их существованию; после отмены старая Goal не возобновлена. |
| O03-SPACE | PRESERVED / REASSESS; F42. Старые observer/checkpoint records сохранены историческими. Baseline/new24h не принят по их существованию; после отмены старая Goal не возобновлена. |
| O03-OWNER | PRESERVED / REASSESS; F42. Старые observer/checkpoint records сохранены историческими. Baseline/new24h не принят по их существованию; после отмены старая Goal не возобновлена. |

## Исторический task checkpoint — не действующая Goal

CAPACITY25 W01–W10 CANCELLED; не заменять это на COMPLETE и не продолжать по прежним scope/gates. Полезный код/evidence сохранён. COHORTS остаётся прежним отложенным направлением, новые постоянные N20 задачи из него не продолжаются по D29. TELEGRAM-UX ранее COMPLETE по primary records; новый audit не означает visual acceptance. OBSERVER/O01–O04 — историческая работа, не текущий чистый 24h.

| Старый task | Сохранённая связь / статус |
| --- | --- |
| W01 scope/evidence | CANCELLED как часть CAPACITY25. Исходный task/Evidence сохранены в Git b254e6e и baseline-open_issues.md; актуальные defects распределены F22/F24/F26/F34/F38/F42/F49–F56. |
| W02 F22/F46 backlog | CANCELLED как часть CAPACITY25. Исходный task/Evidence сохранены в Git b254e6e и baseline-open_issues.md; актуальные defects распределены F22/F24/F26/F34/F38/F42/F49–F56. |
| W03 F42 observer | CANCELLED как часть CAPACITY25. Исходный task/Evidence сохранены в Git b254e6e и baseline-open_issues.md; актуальные defects распределены F22/F24/F26/F34/F38/F42/F49–F56. |
| W04 F34 storage | CANCELLED как часть CAPACITY25. Исходный task/Evidence сохранены в Git b254e6e и baseline-open_issues.md; актуальные defects распределены F22/F24/F26/F34/F38/F42/F49–F56. |
| W05 preservation | CANCELLED как часть CAPACITY25. Исходный task/Evidence сохранены в Git b254e6e и baseline-open_issues.md; актуальные defects распределены F22/F24/F26/F34/F38/F42/F49–F56. |
| W06 previous functional/UX | CANCELLED как часть CAPACITY25. Исходный task/Evidence сохранены в Git b254e6e и baseline-open_issues.md; актуальные defects распределены F22/F24/F26/F34/F38/F42/F49–F56. |
| W07 deferred findings | CANCELLED как часть CAPACITY25. Исходный task/Evidence сохранены в Git b254e6e и baseline-open_issues.md; актуальные defects распределены F22/F24/F26/F34/F38/F42/F49–F56. |
| W08 validation/PR | CANCELLED как часть CAPACITY25. Исходный task/Evidence сохранены в Git b254e6e и baseline-open_issues.md; актуальные defects распределены F22/F24/F26/F34/F38/F42/F49–F56. |
| W09 delivery/stability | CANCELLED как часть CAPACITY25. Исходный task/Evidence сохранены в Git b254e6e и baseline-open_issues.md; актуальные defects распределены F22/F24/F26/F34/F38/F42/F49–F56. |
| W10 clean24h | CANCELLED как часть CAPACITY25. Исходный task/Evidence сохранены в Git b254e6e и baseline-open_issues.md; актуальные defects распределены F22/F24/F26/F34/F38/F42/F49–F56. |

## Рудименты и cleanup decisions

| Объект | Решение / основание |
| --- | --- |
| Automatic N20 admission/worker/28d holds | REMOVE из рабочего цикла по явному S12/F52, после caller/contract inventory; это не удаление всех полезных analytics helpers/tests |
| Старые converters/сложные lossless layouts/native vendors | DEFER/REFACTOR по F24: часть уже используется production/backup/validation; нельзя объявить ненужной по отсутствию Python import. Старую test history полную conversion не продолжать |
| Two-writer branch ae270373 | DEFER:110files/+19317lines, вне main. Не продолжать автоматически, не удалять уникальный код/proof. Сначала новая episode модель и минимальный full-path тест |
| Legacy helpers/new_write_check/history_snapshot/conversion_qualification | DOCUMENT: статических внутренних callers нет для части entrypoints, но есть scripts/CLI/qualification; отсутствие import не доказывает dead code |
| Backups/rehearsals/releases/VPS archives | DEFER: принадлежность/содержимое/замена/recovery-набор перед любым удалением. Audit только metadata. Никакого Docker prune |
| Большой старый дневник плана | CONSOLIDATE выполнено: current checkpoint+все finding/task IDs, исходные records в Git/local baseline. Поведение приложения не меняется |

## Предложение следующей implementation Goal — не активировано

Результат: облегчённый episode radar по S12,13 обязательных findings закрыты с evidence, на общем VPS нет растущего due backlog/ухудшения соседних проектов и рабочий набор укладывается в 35GB и более строгую физическую вместимость. Реализация 18 неготовых AC плюс regression важных READY инвариантов; не новый scope и не обязательная схема двух writer.

Один основной PR: независимая detector activation → meaningful episode update/origin/outcome → uniqueN/digest/aggregates → удаление automaticN20 и 72h retention/validation → единая модель/finite queues/операционные документы → применимые dependency fixes/full no-CD profile. До тяжёлого теста — полный diskpeak и ограниченный resource envelope по измерениям. Код/архитектура выбираются после полного воспроизводимого offered/saved/due/delivery сценария; не новая цепочка micro-оптимизаций.

Затем только в согласованной новой Goal: required local/CI/native/negative integrity/contracts, safe merge/CD, exact-target discard прежней тестовой history без архива/полной конвертации с private equality/TA isolation. Прогрев/корректный baseline,30min, затем 24h неизменных source/settings/generation, due-only queues/sourcefreshness/Telegram/agent/hostCPU/RAM/I/O/working+temp+backup/physicalreserve. При растущей просрочке короткого теста суточный не запускать; при объективном resource infeasibility представить измерения/варианты владельцу.

Non-goals: visual Telegram, execution/LLM/news/multi-exchange, новый transport, receiver strategy/Paper/Production activation, непрерывный deep research, полная конвертация старых тестовых данных, другие проекты/VPS expansion/schedules.

## Blockers и следующий шаг

Прежний аудит — historical snapshot. Текущая Goal2: scope global detection + bounded detailed pool принят, разрешены минимальный prototype/publication/один Linux gate. Прежний direct-SQLite gate FAIL. Full integration/merge/CD/VPS не разрешены; после единственного результата остановиться. F22/F34/F38/F42/AC-13-06 и остальные findings не закрыты; отменённую CAPACITY25 не возобновлять.

## Checkpoint аудита

Сделано: источники/revisions/root instructions, baseline/main/runtime distinction,68AC/130source matrix, existing464localPASS/2SKIP, primary2485LinuxPASS/CI-CD/Git protections, boundedVPS/Grafana и disk snapshot, новые и прежние findings. Runtime код/конфиг/данные не менялись; push/PR/merge/workflow/schedule/Codex Security не выполнялись. Финальная canonical quality PASS (docs/Ruff/format/mypy132), coverage/IDs/READY/false-positive review PASS. Собственные5temp-каталогов и semantic-probe DB удалены, всего83702033.0bytes; readback отсутствия PASS, cleanup.json внеGit. Документы готовы к локальному commit; его фактический SHA/readback сохраняется в completion.json вне Git. Публикация не разрешена. Подробный audit report вне Git; это не новый Goal/поставка/24hPASS.
