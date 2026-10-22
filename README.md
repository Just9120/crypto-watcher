# CryptoWatcher

Deterministic radar Bybit для владельца в Telegram. Согласованный результат — Spot USDT и USDT Perpetual, четыре одновременных TF, пять detectors, episodes/history/outcomes и advisory context для отдельного trading agent. Radar не исполняет ордера и не вызывает LLM. Цель этого проекта — alerts для ручной торговли и готовый handoff; credentials/binding/receiver activation выполняются в принимающем проекте и не требуются для manual radar.

Radar обрабатывает широкий metadata/liquidity universe обоих рынков и native `1m/3m/5m/15m` одновременно через public WebSocket. Пять versioned detectors создают persistent events и episodes; Telegram delivery имеет независимые attempts/retry. Custom thresholds сохранены. Telegram управляет typed watchlist, profiles/manual settings без restart, detectors, market-specific mutes, alerts/digest toggles, историей и outcomes. Статусы поставки и следующих подсистем — в [плане](docs/open_issues.md).

## Документация

- [Product scope и стабильные AC](docs/product_scope.md)
- [Current Goal, Validation Plan, findings и checkpoint](docs/open_issues.md)
- [State ownership, JSON compatibility и SQLite migration](docs/state_contract.md)
- [Runtime boundaries](docs/architecture.md)
- [Handoff в trading agent: contract, example, границы и checklist подключения](docs/runbooks/agent-delivery.md#handoff-принимающему-проекту)
- [CI/CD, VPS target, первая поставка и recovery](docs/runbooks/delivery.md)
- [Хранение, routine disk backup и разовый reset](docs/runbooks/bounded-storage-reset.md)
- [Исторический tooling: конвертация qualified history backup](docs/runbooks/delivery.md#изолированная-конвертация-legacy-history-backup)
- [Исторический tooling: compact snapshot](docs/runbooks/delivery.md#read-only-измерение-compact-current-dataset)
- [Repository procedures](AGENTS.md) и [CI/CD configuration contract](ci-cd-rules.md)

Хранение обновляется по [D22/E13](docs/product_scope.md#e13--компактное-хранение-сроки-и-новый-период-истории): подробности30d, свечи35d, quality summaries12mo, измеряемый storage target25GB. Разовый сброс прежней тестовой market history выполняется отдельно после проверок; полная конвертация этой истории исключена из текущей задачи. Статус реализации — в плане.

## Stack и установка

Target runtime: Python 3.11/Linux; PTB20.8 с job queue, HTTPx async, websockets17.1 и стандартный SQLite. APSW3.53.4.0 используется для bounded full validation private snapshots; Zstandard0.25.0 — для pre-deploy archives и lossless shared-body decoding. Lockfiles с hashes генерируются uv для Python3.11; `requirements.in` / `requirements-dev.in` — inputs, соответствующие `.txt` — canonical installation locks. Нужны только Telegram Bot API и public Bybit endpoints; Redis/DB server не требуются. Tests используют disposable fixtures и mocked adapters, без API tokens и сети.

Isolated history converter дополнительно использует source-pinned yyjson0.12.0
helper, собранный отдельной стадией Docker с fixed GCC image digest. Radar runtime
его не выбирает. Linux dev/CI перед native tests выполняет из root:

```bash
python scripts/build_native.py
```

Команда требует GCC/C99 и проверяет upstream hashes; runtime image уже содержит
готовый helper и license. `history_rebuild --backend native` требует этот artifact,
`--backend python` выбирает прежний путь; default `auto` указывает фактический
backend в результате. Windows local checks используют Python fallback, required
Linux/image checks проверяют native artifact. Budget и data/recovery gates одинаковы.

Та же команда собирает frozen-image VFS с pinned SQLite3.53.4 headers и
Zstd1.5.7 decoder. Он выполняет прежний full `integrity_check`/CHECK через
SQLite extension API, физически запрещает запись source и сохраняет общий
deadline/CPU1/RAM4GiB. Mutable capture использует Python VFS; frozen source и
recovery выбирают native helper при наличии Linux artifact. Отсутствующий helper
оставляет portable Python path; повреждённый или несовместимый установленный
helper прекращает validation. Frozen-image extension отделён от runtime БД. Native
corruption/bounds/lifetime/deadline fixtures входят в required Linux suite.

Эта же сборка создаёт `numeric_frame.so` ABI3 для bounded BP1/BR1 flat layout
и read-only NF/NR/NS2 proof. Transpose только копирует байты без allocations или
float arithmetic. Python проверяет BF1/checksum/Zstd bounds и OW mask, native
flatten сохраняет прежние BR1/BP1 shape/identity/length checks, а native proof
читает до64 current parent/ref rows на frame, проверяет прежние NR/NS2 preimages через
OpenSSL из Python и NR2 header через pinned Zstd. Extension не пишет данные,
проверяет до8frames за вызов с временным blob не более1153737B и не сохраняет
connection/statement/cache; загрузка разрешена только на время
установки exact artifact. Прежний shared pure cache остаётся1MiB. При отсутствии
artifact работает portable path; повреждённый installed artifact прекращает
чтение. Linux CI и image checks требуют корректный artifact, byte-exact parity
и полный finite structural read в прежнем50s budget.

Конвертер также читает сохранённые gzip/Zstandard archives через bounded compressed
image без дополнительной raw source copy. Explicit encoding/byte cap и полный набор
source/candidate проверок описаны в [процедуре конвертации](docs/runbooks/delivery.md#изолированная-конвертация-legacy-history-backup).

Команды выполняются из root repository. Создать virtual environment:

```bash
python -m venv .venv
```

Активировать `.venv/bin/activate` на Linux либо `.venv\Scripts\Activate.ps1` в PowerShell. Container installer versions фиксируются отдельно в `requirements-bootstrap.in` / `requirements-bootstrap.txt`; hashes проверяются до установки runtime dependencies. Установить runtime или dev lock:

```bash
python -m pip install --require-hashes -r requirements.txt
# Для разработки вместо runtime-only install:
python -m pip install --require-hashes -r requirements-dev.txt
uv pip check
```

Обновлять locks только при намеренном изменении dependencies:

```bash
uv pip compile requirements.in --python-version 3.11 --universal --generate-hashes --output-file requirements.txt
uv pip compile requirements-dev.in --python-version 3.11 --universal --generate-hashes --output-file requirements-dev.txt
```

## Local run

Скопировать `.env.example` в ignored `.env`, указать существующий token владельца и локальные `STATE_FILE` / `DATABASE_FILE`. Paths в example предназначены контейнеру; для local run использовать абсолютные пути внутри собственного `data/`. Env процесса имеет приоритет над `.env`. Обычные сохранённые owner settings имеют приоритет над initial defaults.

```bash
python -m cryptowatcher check-config
python -m cryptowatcher run
python -m cryptowatcher --version
python -m cryptowatcher diagnostics
python -m cryptowatcher storage
python -m cryptowatcher storage-indexes
python -m cryptowatcher capacity
python -m cryptowatcher capacity --capacity-ledger --capacity-cohorts
python -m cryptowatcher shared-storage-status
python -m cryptowatcher state-check
python -m cryptowatcher health
```

Существующий JSON требует explicit migration из [state contract](docs/state_contract.md); обычный startup его не перезаписывает. Владелец `233382575`, bot `7523865123`; sender и delivery chat должны соответствовать private chat владельца. Пустая либо неверная конфигурация закрывает доступ. Старые `ALLOWED_CHAT_IDS`, CMC и reset-baseline настройки не активируют дополнительные chats.

Явная offline активация shared storage с exact identity token, backup и recovery
описана в [storage процедуре](docs/runbooks/bounded-storage-reset.md#явное-включение-shared-storage-без-сброса-истории).

## Checks

```bash
python scripts/checks.py quality
python scripts/checks.py test
python scripts/checks.py all
python -m ruff format .
```

`quality` проверяет canonical doc links/fences, Ruff lint/format и mypy. `test` запускает meaningful regression suite; `all` объединяет оба этапа. PR CI исполняет оба с конечной numeric qualification между ними; trusted release проверяет `all`. Во время разработки допустим точечный pytest по затронутому риску. Coverage target не задан. Основной merge gate — GitHub `ci-required` на актуальной PR test merge revision; local PASS его не заменяет.

После native build и quality Linux PR CI выполняет `python scripts/qualify_numeric_boundary.py`:
конечная временная SQLite с129572frames/8292608owners/313564240metrics,
50000shared groups и1050000DS2 descriptors проверяется новым текущим runtime reader
за прежние общие50s на одном CPU при address-space limit4GiB. Pinned seeds повторяют
actual numeric bytes и самый крупный SB2 group из owned N20 component; количество
groups/descriptors превышает текущий30day scenario, но не является будущей верхней
границей. При materialization guards временно сняты только в этой owned fixture
и точно восстановлены до full integrity/FK и чтения. Временная БД удаляется.
Один итоговый full integrity/FK охватывает обе группы таблиц; прежний числовой
standalone builder сохраняет собственную проверку, а совместный не повторяет
ее перед добавлением shared rows. CLI не позволяет пропустить итоговую проверку.
Проверка не содержит полный public domain graph и не подтверждает независимую
рыночную entropy, зрелую ёмкость всей БД или live SLO; OS file cache не очищается.

`capacity` читает counters/files и статус стабилизации без полного обхода данных.
Explicit flags выполняют ограниченные разовые ledger/cohort/index измерения;
logical bytes не складываются с physical pages. Автоматический finite24h observer
и controlled-clock rehearsal описаны в [процедуре хранения](docs/runbooks/bounded-storage-reset.md#измерение-ёмкости-и-конечное24h-наблюдение).

## Telegram controls

Главное меню содержит шесть разделов: Статус, Радар, История, Аналитика, Настройки и Помощь. `/start` только открывает меню; сохраненный выбор уведомлений не меняется. Старые кнопки/commands и явные on/off actions совместимы. В обычном сценарии не требуется запоминать команды или вводить Event ID. Native command menu показывает основные входы, полный reference с аргументами доступен в Помощи.

Настройки показывают фактические пороги каждого TF. Профиль сначала показывает все заменяемые пороги и окна; явное применение сохраняет текущий custom набор для возврата. Individual parameter input имеет cancel и CAS revision, устаревший ввод не применяется. `/set price_3m 2.5` меняет только3m; `/set price_move_min 2.5` явно устанавливает все4TF. Карточка/рынок ввода без префикса отделены от одновременной работы4TF/обоих рынков. Owner scope применяется отдельной source task, независимо от ожидания REST refresh; diagnostics связывает applied source scope с configuration revision.

`/detectors` управляет только Telegram families; detection/history всех пяти остаются независимыми от delivery. History показывает небольшие страницы с исходным временем, типом наблюдения и доступным Telegram decision/receipt; keyset navigation открывает более ранние retained events. Нет квитанции — не значит «не отправлено». Event details открывают полный сохраненный контекст/точные исходные данные файлом и Outcome с immutable reference/horizons. `/horizons 5,15,30,60` меняет только новые события. Статистика/сравнение предлагают разрез, затем страницы до20групп с N/ограничениями и файл точного снимка; полная выборка остается в CLI export. Диагностика по умолчанию краткая, raw JSON скачивается отдельным файлом. Display rounding не меняет stored/export values. Время карточек явно МСК; исходные UTC timestamps сохраняются в данных.

`Derivatives` в меню или `/derivatives spot/BTC` показывает optional OI/funding/basis с source/units/timestamps и состояниями каждого показателя. Context первоначально OFF; `/enrichment on` и `/enrichment off` сохраняют owner setting без restart. Exact metadata base identity связывает eligible Spot с единственным USDT Perpetual; отсутствие/неоднозначность не угадывается. Ticker cadence1min, OI change5min из двух точек; shared REST budget и current recovery имеют приоритет. Missing/stale context не меняет trigger и не блокирует Spot. Perpetual detectors самостоятельны. Alert сохраняет только доступный до observation cutoff context; текущая карточка отдельно показывает latest snapshot.

`Сравнение` или `/compare` группирует сохранённые outcomes по profile/version/settings/market/TF/horizon с actual N/distribution/noise/comparator limitations и показывает текущие параметры. `/analytics profile,version,settings` задаёт grouping. `/recompare EVENT_ID` явно ставит prior no-signal сравнение в очередь на новых доступных исходных данных; frozen event/settings/reference, прежние results и availability сохраняются. Автоматической калибровки/изменения thresholds нет; отсутствующий profile в history не симулируется.

`/setlist spot/BTC,linear/ETH`, `/mute linear/BTC 60`, `/unmute linear/BTC`, `/unmute all` сохраняются. Bare symbol выбирает рынок карточки. Неизвестный/запрещённый или недостаточно ликвидный instrument отклоняется без изменения списка. Watchlist multiplier меняется через `/set watchlist_multiplier 0.75`; он применяется к detector trigger thresholds, liquidity eligibility сохраняется. Invalid effective ratio≤1 даёт diagnostic; значения не подменяются. `/alerts on|off` управляет доставкой; `/alerts on` также возобновляет target после постоянной Telegram error. No-op buttons не создают ошибку. `/sample_alert` показывает демонстрационный composite. Старые CMC/reset callbacks возвращают сообщение об изменении.

Composite использует `100 × (close / previous_close − 1)`, оборот закрытой свечи / среднее предыдущих SMA periods и ticker turnover24h. Все три threshold comparisons включают equality. Price, turnover, breakout и volatility имеют отдельные formulas/directions/strength; `/terms` объясняет их, точные policy — в [spec](docs/product_scope.md). REST использует Bybit response clock, WebSocket — confirm=true и candle boundaries. Invalid/gapped windows ограничивают affected detector до восстановления.

Episodes разделены по instrument/market/direction. Новый detector/TF либо strength≥1.5 предыдущего уведомлённого значения создаёт update; подтверждения объединяются2s. Inactivity2×longestTF или maxlife60min закрывают episode; продолжающееся условие re-arm после2 полных non-signal candles. Events сохраняются независимо от mute, alerts off и Telegram failure. Receipt/retry не освежают event: обычно fresh1TF/expire2TF, breakout2TF/4TF. Expired/historical sources не отправляются как live. Successful notification state меняется только после ack; при потере ack возможен повтор того же delivery identity, это отражено в diagnostics.

## Outcomes, digest и export

`/digest` показывает preference и interval; `/digest on 60` включает, `/digest off` выключает независимо от `/alerts` и mutes. Default OFF/60min; первый digest — следующий закрытый UTC interval. Digest использует оба рынка/все detector events, strongest period episodes и ongoing episodes на момент сборки. Restart отправляет последний завершённый период, фиксирует skipped intervals и сохраняет delivery identity/frozen text. Unknown acknowledgement допускает повтор того же сообщения; подтверждённый interval не отправляется повторно.

Outcomes используют полный subsequent1m диапазон от immutable observation/reference close. Gaps/invalid sources означают INCOMPLETE/INVALID_SOURCE без нулевого return; recovery работает и для inactive instruments в shared REST budget ниже current ingestion. Historical composite comparator требует действительно доступного prior ticker snapshot: будущая liquidity не подставляется. Малые N и partial prior28days history явно ограничивают comparisons; результаты замораживаются, более поздняя source history не переписывает их автоматически. Это оценка информативности detector, не Strategy backtest или доходность.

Read-only CLI из root, существующая owner-bound DB и `OWNER_USER_ID`/`DATABASE_FILE` из environment. Production используется этот же CLI внутри текущего container; export не требует Telegram/trading credentials и не включает transport/infrastructure state. `--destination` создаёт только новый файл; существующий не перезаписывается.

```bash
python -m cryptowatcher export --format ndjson --market spot --detector price --detector-version 1 --destination events.ndjson
python -m cryptowatcher export --format csv --destination summary.csv
python -m cryptowatcher analytics --groups detector,profile,version,settings,market,timeframe,direction
python -m cryptowatcher context --event-id EVENT_ID --cutoff-ms UNIX_MILLISECONDS
```

Export поддерживает `[--start-ms --end-ms)` в UTC UNIX milliseconds, detector/version/market filters. NDJSON schema v1 содержит manifest, все выбранные event revisions, связанные episode revisions, outcomes/comparisons, исторические settings/methodology и frozen source provenance, затем completion counts. Related episode snapshots могут ссылаться на events вне выбранного фильтра; IDs сохранены, фильтр относится к event selection. Pagination100/read-only WAL snapshot обеспечивает полноту и согласованность; отсутствие completion означает частичный/неуспешный export. CSV streamed cohorts сохраняет actual N, distribution/noise/comparator counts. CLI analytics/Telegram ограничивают число возвращаемых cohorts с явным truncated flag; export не ограничивает число групп.

## Docker и delivery

Production использует проверенный immutable image, host `.env` и bind data. Compose требует `CW_IMAGE`, `CW_SOURCE_REVISION`, абсолютные `CW_ENV_FILE` / `CW_DATA_DIR`; пути и существующий project зафиксированы в [runbook](docs/runbooks/delivery.md). Не использовать build на production вместо validated artifact.

Для disposable image checks на Linux с Docker:

```bash
docker build --build-arg SOURCE_REVISION=0000000000000000000000000000000000000000 -t cryptowatcher:local .
python scripts/image_checks.py cryptowatcher:local 0000000000000000000000000000000000000000
```

Production secrets не входят в image/context/logs. SQLite migration и SSH credential bootstrap выполняются по concrete gated procedure. [Одноразовое подключение CD](docs/runbooks/delivery.md#одноразовое-подключение-cd) скачивает проверенные scripts непосредственно на VPS и сохраняет существующий SSH access. Последующие eligible merges доставляются автоматически без ручного переноса файлов; unit trading agent не меняется.

## Ingestion и диагностика

Metadata refresh10min, liquidity refresh5min, REST budget5req/s/concurrency4, heartbeat20s, reconnect1–60s с jitter. Spot discovery не имеет pagination; linear проходит весь cursor. Eligibility учитывает Trading/USDT/settlement/perpetual/prelisting, известные crypto types и versioned tokenized-gold exclusions; unknown special types quarantined. Ни fixed Top100, ни symbol suffix не определяют eligible universe.

Native closed candles сохраняются в общей SQLite, memory window≤256 на active stream. Schema2 bootstrap получает нужные detector lookbacks (default22), максимум256 closed bars; gaps приоритетно восстанавливают текущий window, затем older missing history постранично без live alerts. Cursor переживает restart; optional historical work не опережает current recovery. Initial bootstrap progressive: status показывает actual available/requested и причины ограничений. CLI diagnostics включает metadata counts/quarantine/acks/TF/buffers/pending/gaps. Новые subscribers проверяются только по validated metadata/liquidity.

Подробные events/revisions/episodes/outcomes хранятся30d, small-TF candles35d плюс ограниченные зависимости сохраняемых расчетов. До удаления подробностей сохраняются quality summaries12mo с provenance/N/limitations; служебные зависимости имеют конечный срок. Истекшие подробности недоступны и не подменяются агрегатом. Coarse context90/180d применим только при его фактическом использовании; текущие detector/comparator/enrichment paths используют small-TF inputs и snapshots, отдельного coarse source нет.

## Компактный market layout v2

Новая generation использует нормализованные immutable candle revisions и отдельные context values; history windows содержат координаты, а не копии перекрывающихся OHLC arrays. Settings/context используют shared bodies, outcome heads не дублируют metrics; finished jobs удаляются после сохранения результатов, успешная доставка оставляет компактный receipt. Binary JSON records используют frozen dictionary/checksum/SHA256 без изменения public values. Schema1 читается для совместимости; обычный open/restart не конвертирует старую историю и не включает reset.

`storage` возвращает actual working files/counts/encoded category bytes, durable repack progress и явно помеченный forecast. Retained inserts считаются отдельно от net queue occupancy; эффект repack не выдается за изменение потока событий. Quality baseline и сценарии density/comparators измеряют текущий codec/SQLite table+indexes на isolated synthetic данных; они не production measurement и не upper bound. Initial small-file index measurement ограничен; `storage-indexes` запрашивает отдельный bounded exact snapshot. Автоматический control не сканирует постоянно все страницы, samples ограничены26 записями;25GB fit не считается доказанным по fixture или короткому post-deploy окну. Windows без dbstat показывает UNAVAILABLE, required Linux verification остается обязательной.

Backfill и resumable repack обслуживаются отдельными jobs каждую секунду, независимо
от долгих outcome/comparator calculations. Repack сохраняет конечные границы прохода
и cursor: fixed physical high water/next ID, исходный logical key floor и
schema_version guard. Новые записи не продлевают interval; обычный restart сохраняет
позицию, DDL/VACUUM консервативно перевычитывает оставшийся logical scope. Проверяет
целевые typed records без повторного сжатия. Первые600s процесса
получает один40ms slice за tick. Затем при fresh ingestion/analytics, coverage>=95%
и idle live recovery — до4 таких slices с отдельными checkpoint/yield и повторной
проверкой приоритета между ними; общий intended wall cap200ms. При потере условий
остается один slice, реальные live queues сохраняют приоритет. Backfill обрабатывает до32 due jobs за ограниченный проход
с прежним общим REST budget5req/s/concurrency4 и приоритетом live recovery. Retry,
restart, source conflicts и устаревшие pending bindings не изменяют immutable history.
Состояния `repack_worker`/`analytics_recovery_worker` диагностируют ошибки независимо
от analytics; их failure не допускает stable capacity baseline.

Воспроизводимое измерение на **новом** disposable fixture path из root:

```bash
python -m scripts.measure_market_storage --database /absolute/new/fixture.sqlite3 --instruments 5
```

Сценарий охватывает оба рынка/4TF/5families, outcomes и служебные записи, controlled expiry и сравнение analytics. Он не измеряет частоту production signals; artifact сохранять отдельно от working DB. Разовый production reset/cutover выполняется по [отдельной процедуре](docs/runbooks/bounded-storage-reset.md); пока ее post-checks не пройдены, новая generation не считается поставленной. Старые backup/conversion tools выше остаются историческими, не текущей задачей.
