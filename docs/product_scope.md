# CryptoWatcher — project spec

Canonical spec: этот файл. Canonical delivery plan: [open_issues.md](open_issues.md). Существующие пути сохранены вместо создания второго spec/plan. Текущие статусы AC находятся только в плане.

## Назначение и границы

CryptoWatcher — личный deterministic market radar владельца для широкого мониторинга Bybit Spot USDT и USDT Perpetual, поиска краткосрочной необычной активности, ручного просмотра рынка и advisory context основного trading agent. Radar описывает наблюдаемые события и не принимает торговых решений.

Первая версия использует только Bybit. LLM, news/macro ingestion, торговый аккаунт/portfolio, execution/risk management, multi-exchange, собственный SPA/PWA и доступ других пользователей исключены. Trading agent остаётся независимым приложением; здесь готовятся standalone manual radar и handoff. Подключение, credentials/binding/receiver activation и приемочная проверка живой интеграции принадлежат принимающему проекту по S06/D23.

## Источники и приоритет

| ID | Источник / revision | Назначение |
| --- | --- | --- |
| S01 | Согласованный Google Doc «Требования к проекту CryptoWatcher», file ID `1wNt8hpuZTrvMLmSbfTU5H4hQTzngNDQW3DG3Lm7APyE`, modified 2026-10-16 08:48:31.536 UTC; полностью прочитаны 170 paragraphs единственной вкладки t.0 2026-10-17; native revision AHj4eMRw_XfnmGfEiPc-g2cZt8XKzpQmka1tOt14e2Y0FytXypDSNNiUF5qWYpO2CeaAAOes_O9xLrDPguxGW4WgfUarojYn4NdO_coDyx8 | Актуальный intent всего проекта; заголовки источника сохранены в трассировке эпиков |
| S02 | Запрос владельца AUDIT от 2026-10-10 и приложенный комплект AGENTS.md / ci-cd-rules.md | Scope audit PR: только canonical spec/plan; правила AC/Evidence/Goal/Git/delivery; pipeline не меняется аудитом |
| S03 | Предыдущий product scope, [state_contract.md](state_contract.md), README и код на `8ace3638b9193444948d0beb88e0c49e9f2f068e` | Legacy behavior и compatibility evidence; не отменяют S01 |
| S05 | Явное поручение владельца 2026-10-16: исправить новое накопление, bounded retention и контроль 20 GB, затем разово сбросить старую тестовую рыночную историю | Отменяет сохранение/полную конвертацию этой истории; сохраняет owner config, credentials, инфраструктуру, все markets/TF/detectors и аналитику |
| S06 | Явное решение владельца16Oct2026: CryptoWatcher готовится для alerts под ручную торговлю и handoff; подключение — ответственность trading agent проекта | Меняет project boundary/DoD E09: producer readiness/contract handoff здесь; credentials/binding/receiver activation/live acceptance снаружи |
| S07 | Явное поручение владельца16Oct2026: реализовать исправления роста и поставить через CD, сохранив полезную history/analytics/coverage/retention и900s backup/CD | Scope GROW/D24; source epoch не сбрасывается; показать measured post-report относительно20GB |
| S08 | Поручение владельца16Oct2026 после handoff: сначала реальные источники/cardinality/revisions/comparators/services, затем подтвержденные fixes; controlled retention и автоматический24h замер после стабилизации | Уточнение evidence AC-13, без нового reset, сокращения scope/точности/retention или равенства CI/CD и capacity PASS |
| S04 | Уточнение владельца 2026-10-10: бот работает на VPS Contabo; Telegram owner — владелец | Deployment context; assertion владельца не доказывает deployed revision или gates |
| S09 | Уточнение владельца23Oct2026: устойчивый CryptoWatcher на общем Contabo VPS важнее отдельной экономии БД; измерить ресурсы всего host/вклад проекта и normal/recovery/retention/compression/repack, определить бюджет с запасом | Расширяет evidence/DoD текущей CAPACITY25: общая CPU/RAM/I/O нагрузка, DB+sidecars:25GB desired/30GB hard limit по уточнению24Oct, freshness, только просроченные расчёты и Telegram; сохранить функционал/точность/retention, другие проекты и ресурсы VPS не менять; после стабилизации неизменной версии30min/24h, при объективной нехватке представить измерения/варианты |
| S10 | Уточнение владельца24Oct2026 в текущем чате: «25 желаемо,30 приемлемо»,30GB как запас | Для той же Goal CAPACITY25 желаемый объём25,000,000,000bytes; допустимый жёсткий предел30,000,000,000bytes включает DB/indexes/free pages/WAL/SHM/journal. Предварительное допущение25.34GB заменено. Функционал/retention/точность/полнота, CPU/RAM/I/O и стабильность/24h gates сохранены |
| S11 | Ответ владельца24Oct2026 в текущем чате: «Да, увеличить только длительность проверки» | Только конечная полная Linux/native qualification:450CPU seconds/510wall seconds/600outer seconds вместо180/210/270. Один CPU/4GiB/no swap/no network/без production mounts, полный dual-fixture oracle, preflight резервов и стабильность/backlog/24h обязательны; RAM/VPS allocation и900s ordinary delivery gate не изменены |

S01 и последующие явные решения владельца задают intent. Этот файл фиксирует его декомпозицию. State contract описывает current physical schema2 и сохраненную legacy chat-based/import compatibility после консолидации PR41; при противоречии актуальным требованиям приоритет у этого spec. Код сам по себе не создаёт требования. Внешние hyperlinks в repository documentation не добавляются; source IDs и названия позволяют восстановить evidence.

## Принятые решения и изменения прежнего scope

- D01: Spot Top 100 / один выбранный TF / один composite — legacy baseline, а не весь согласованный результат. Fixed Top 100 заменён dynamic eligible universe обоих рынков.
- D02: «futures вне MVP» заменено поддержкой USDT Perpetual. Это не разрешает execution/account integration.
- D03: per-chat market domain заменяется persisted owner configuration; chat ID/preferences принадлежат только delivery. Migration не должна молча потерять legacy list/mutes/overrides.
- D04: baseline-driven monitor и CMC не возвращаются в основной путь. Существующие CMC/reset handlers исследуются перед deprecation; совместимость state отделяется от сохранения устаревшего UX.
- D05: история и outcome analytics развиваются параллельно интеграции. Накопление статистики и калибровка не блокируют базовую доставку events.
- D06: enrichment Spot derivatives — после стабилизации radar обоих рынков. 30m/1h context и stablecoin exclusions допустимы; обязательных новых realtime alerts/фиксированных exclusions источник не задаёт.
- D07: исходные implementation unknowns сохранены как история решения; D09–D22 задают согласованные database/transport/Environment/detector/episode/TTL defaults. Непорученные SLO не вводятся; измеряемая цель storage capacity определена D22.
- D08: история прежних требований сохранена в Git. Старых стабильных AC IDs не было; новые AC имеют IDs `AC-<epic>-<number>`. Связи отменённых контрактов записаны в D01–D04, остальные полезные правила миграции/closed candles/UX сохранены.

## Интерфейсы, данные и зависимости

Каноническая identity — Exchange + Instrument + Market Type (Spot / USDT Perpetual), без смешивания одинаковых symbols разных рынков. Bybit adapter нормализует metadata, closed candles, tickers и optional derivatives context. Internal event model по возможности не привязан к raw Bybit JSON.

Event содержит ID/revision, Episode ID, instrument/market, detector/version/settings snapshot, direction, исходные timeframes, reproducible metrics/units/data provenance, market/detection/receipt timestamps и freshness. Episode хранит начало, последнее обновление, strength и связь с исходными events/revisions. Outcome содержит immutable observation time/reference price, horizon, dataset completeness и calculated metrics. Context первоначального решения не дополняется будущими outcomes/max strength.

Telegram и trading agent — независимые consumers общей event history. Mute меняет Telegram delivery, не domain detection/history/agent delivery. Agent delivery требует authorization, durable pending state, retry/replay и idempotency по Event ID/revision; owner diagnostics доступны независимо от Telegram. API/webhook contract согласован в D18. По S06/D23 доступность production receiver и его credentials не являются prerequisite завершения manual radar/producer handoff; это receiving-project integration scope.

Owner configuration отделяется от secrets/infrastructure (.env), chat delivery state, runtime cursors и analytics. Persistent store — SQLite WAL по D10; versioned migration сохраняет существующие данные по state contract. Текущий runtime — Python 3.11 Docker, dependency manifest requirements.txt, Compose service crypto_watcher; фактическая VPS version отдельно требует evidence. Настройка Actions выполняется только в выбранной Goal по ci-cd-rules.md.

## Эпики, business rules и AC

Каждый AC задаёт условие/действие, наблюдаемый результат и проверку. READY требует кода и подходящих автоматических проверок; текущие статусы и ограничения evidence см. план. Состав ниже покрывает все обязательные разделы S01; разрешённые опции отмечены в rules, не превращены в новые обязательства.

### E01 — Назначение, границы и владелец

Requirement `REQ-E01`; трассировка: S01 / ПРОДУКТ; ГРАНИЦЫ ПРОЕКТА; ТЕХНИЧЕСКИЙ КОНТУР.

Личный deterministic radar на Python. Bybit — единственная биржа v1. Нет LLM, news/macro ingestion, trading account, portfolio, execution, risk management или собственного SPA/PWA. Основной trading agent самостоятельно принимает решения; получение radar event не запускает сделку или LLM.

| AC | Критерий и способ проверки |
| --- | --- |
| AC-01-01 — Детерминированный Python runtime | При одинаковых market data и settings повторный расчёт даёт одинаковые metrics без LLM. Проверка: fixtures и анализ imports/вызовов. |
| AC-01-02 — Нет торговых полномочий | Radar/manual сценарий работает без trading credentials; код и structured context не создают BUY/SELL, position size, order type и ордера. Проверка: анализ endpoints и contract tests на запрещённые поля. |
| AC-01-03 — Bybit-only ingestion | Все активные market-data пути используют Bybit; новости, macro и другие биржи не подключаются. Проверка: inventory источников, handlers и сетевых adapters. |
| AC-01-04 — Доступ только владельцу | Чужой Telegram user/chat и пустая/невалидная owner configuration не получают domain state и не меняют его; каждый command/callback соблюдает один access policy. Проверка: positive/negative tests, в том числе /terms и group chat. |
| AC-01-05 — Отдельный сервис без собственного Web UI | CryptoWatcher имеет отдельные repository/runtime, полезен без trading agent и не требует SPA/PWA. Проверка: entrypoint/dependency inventory и offline domain сценарий. |

### E02 — Universe и watchlist

Requirement `REQ-E02`; трассировка: S01 / MARKET DATA / ALERT UNIVERSE.

Широкие динамические universe Spot USDT и USDT Perpetual раздельны. Eligibility учитывает crypto instrument type, Trading Status, quote/settle currency и настраиваемую liquidity. xStocks/TradFi/Event Contracts, tokenized stocks/gold и неизвестные специальные типы автоматически не включаются. Обычные crypto tokens сектора RWA допускаются по metadata/liquidity (D09). Stablecoin/stablecoin допускается исключать отдельным объяснимым правилом. Custom watchlist содержит Instrument + Market Type и допускает более чувствительные условия.

| AC | Критерий и способ проверки |
| --- | --- |
| AC-02-01 — Два широких universe | После refresh доступны все подходящие Spot USDT и USDT Perpetual, без fixed Top 100; данные и events двух рынков не смешиваются. Проверка: paginated metadata fixtures, >100 instruments, одинаковый symbol в обоих рынках. |
| AC-02-02 — Eligibility инструментов | Не-USDT, non-Trading, запрещённые и неизвестные специальные типы исключены; eligibility основана на metadata, а не суффиксе symbol. Проверка: positive/negative fixtures и pagination. |
| AC-02-03 — Настраиваемая liquidity | Изменение минимальной liquidity меняет eligibility universe; liquidity не навязана формуле каждого detector. Проверка: boundary values, refresh и фактические active settings. |
| AC-02-04 — Watchlist по рынкам | Владелец добавляет/удаляет Instrument + Market Type, получает проверку допустимости, сохраняет список и отдельную чувствительность после restart. Пустой список не подменяется seed. Проверка: Telegram/domain и persistence tests. |

### E03 — Realtime ingestion и timeframes

Requirement `REQ-E03`; трассировка: S01 / TIMEFRAMES; MARKET DATA SOURCES; DIAGNOSTICS; DEPLOYMENT.

Приоритет realtime — public Bybit WebSocket. REST служит bootstrap, historical/backfill, gap recovery и outcome. 1m/3m/5m/15m работают одновременно; 30m/1h разрешены как дополнительный context без обязательных realtime alerts. Глубокий order book не добавляется без подтверждённой пользы. Market event timeframe и cadence ingestion — разные сущности.

| AC | Критерий и способ проверки |
| --- | --- |
| AC-03-01 — WebSocket обоих рынков | Runtime подписывается на public streams обоих рынков, выполняет REST bootstrap и восстанавливает subscriptions после disconnect/restart. Проверка: adapter tests с reconnect и безопасный live smoke. |
| AC-03-02 — Одновременные timeframes | Один market runtime обрабатывает 1m/3m/5m/15m одновременно; объединение сохраняет исходные TF. Проверка: interleaved fixtures и event provenance. |
| AC-03-03 — Закрытые свечи | Незакрытая candle не участвует в подтверждённом kline event; на границе закрытия eligible только closed candle. Проверка: open/closed/boundary fixtures. |
| AC-03-04 — Gaps и recovery | Потеря/неполнота данных ограничивает detectors/outcomes; recovery восстанавливает историю без исторических live alerts и помечает неполные horizons. Проверка: gaps, duplicate/out-of-order/reconnect replay. |
| AC-03-05 — Разделение cadence и TF | Изменение частоты получения данных не меняет размер свечи и наоборот; UI и расчёт используют разные параметры. Проверка: settings/domain fixtures. |

### E04 — Detectors и metrics

Requirement `REQ-E04`; трассировка: S01 / MARKET EVENTS / DETECTORS; DIAGNOSTICS; ЭФФЕКТИВНОСТЬ DETECTORS.

Composite price move + turnover spike + liquidity сохраняется наряду с price impulse, volume/turnover spike, range breakout, volatility expansion и composite impulse на собственных данных обоих рынков. Metrics воспроизводимы, единицы/источники явны; положительное/отрицательное движение различается, без основания direction neutral. Калибровка по статистике допускается вручную; production settings автоматически не меняются.

| AC | Критерий и способ проверки |
| --- | --- |
| AC-04-01 — Composite formula | Для полных закрытых свечей composite использует abs(close/prev_close−1) ×100, turnover/latest против среднего предыдущих candles и active thresholds; equality проходит, любой failed filter запрещает signal. Проверка: deterministic boundary/zero-history fixtures. |
| AC-04-02 — Набор detectors на двух рынках | Price impulse, volume/turnover spike, range breakout, volatility expansion и composite impulse дают воспроизводимые events отдельно для Spot/Perpetual. Проверка: positive/negative/boundary fixtures утверждённых формул и versions. |
| AC-04-03 — Provenance и direction | Каждый event сохраняет detector/version, settings snapshot, исходные данные, metrics/units и обоснованный direction/strength. Проверка: schema и replay fixtures, neutral case. |
| AC-04-04 — Изоляция ошибок | Ошибка одного detector или optional context видна в diagnostics и не останавливает исправные detectors/рынки. Проверка: fault injection и наблюдаемые degraded flags. |
| AC-04-05 — Без auto calibration | Analytics или outcome не меняют production thresholds автоматически; изменяет только явная owner операция. Проверка: анализ всех settings writers и negative test при ingestion/outcome. |

### E05 — Episodes и freshness

Requirement `REQ-E05`; трассировка: S01 / EVENT EPISODES / ОБЪЕДИНЕНИЕ СОБЫТИЙ; FRESHNESS.

Один Instrument/Market Type/direction объединяет связанные detectors/timeframes, сохраняя исходные events. Episode хранит начало, last update и strength; meaningful strengthening/new confirming detector/значимое обновление разрешают уведомление. После окончания detector re-arm; maximum lifetime закрывает зависшие episodes. Event имеет market/detected/received timestamps где доступны и type/TF-specific freshness. Expired не передаётся consumer как новый fresh event.

| AC | Критерий и способ проверки |
| --- | --- |
| AC-05-01 — Episode aggregation | Связанные events формируют episode с началом, обновлением, strength и исходными TF/detectors; Spot/Perpetual раздельны. Проверка: interleaved event fixtures и history readback. |
| AC-05-02 — Meaningful notifications | Повтор detector/candle не создаёт идентичный alert; усиление/новое подтверждение создаёт revision и уведомление согласно policy. Проверка: duplicate replay и escalation fixtures. |
| AC-05-03 — Завершение и re-arm | Episode закрывается по согласованным условиям или max lifetime, новое независимое движение создаёт новый episode; restart сохраняет незавершённое. Проверка: time-controlled lifecycle/restart tests. |
| AC-05-04 — Freshness и timestamps | Market/detected/received timestamps не взаимозаменяются; fresh/stale/expired зависят от event type/TF. Expired не выдаётся за новый fresh в Telegram/consumer. Проверка: clock-controlled expiry/replay tests. |

### E06 — History и outcome analytics

Requirement `REQ-E06`; трассировка: S01 / OUTCOME ANALYTICS / ИСТОРИЯ СОБЫТИЙ; ПОСЛЕДУЮЩИЙ OUTCOME; ЭФФЕКТИВНОСТЬ DETECTORS.

Все events/episodes сохраняются в пределах подробной истории, независимо от Telegram, отдельно от trades trading agent; после ее истечения доступны сводные результаты с явным ограничением детализации. Outcome автоматически рассчитывается от зафиксированного observation time/reference price на настраиваемых horizons (базово 5/15/30/60 min). Оценивается информативность detector, не прибыльность Strategy и не trading backtest.

| AC | Критерий и способ проверки |
| --- | --- |
| AC-06-01 — Полная история | Каждый detected event/revision и episode persistently содержит ID, instrument/market, detector/version/settings, direction/TF, metrics/timestamps/context, даже при mute или сбое Telegram. Проверка: transaction/restart/readback tests. |
| AC-06-02 — Настраиваемые horizons | Outcome worker автоматически считает выбранные 5/15/30/60 min или owner horizons от immutable observation time/reference price. Проверка: delayed worker/backfill и exact horizon fixtures. |
| AC-06-03 — Returns и развитие | Outcome показывает price return, движение в сторону/против direction, continuation/reversal; neutral events используют ненаправленные metrics. Неполные данные помечены. Проверка: directed/neutral/gapped series. |
| AC-06-04 — MFE/MAE | MFE/MAE и другие выбранные market metrics воспроизводимо вычисляются на обозначенном диапазоне исходных данных. Проверка: known OHLC/time series и gaps. |
| AC-06-05 — Статистика качества | Выборка группируется по version/settings/market/TF/instrument или universe/direction/strength; видны sample size, сопоставимые no-signal periods, noise и ограничения, без заявления Strategy profitability. Проверка: controlled cohort dataset. |
| AC-06-06 — Export и неизменность | Export выбранного периода/detector/version/market содержит историю и outcomes для coding agent; старые результаты/settings не пересчитываются незаметно. Для подробностей период точный; после их expiry доступная UTC-day детализация, фактический охват и ограничения периода должны быть явны и согласованы между форматами. Проверка: filters/pagination/versioned export readback до/после expiry, включая границы внутри дня; policy Q07. |
| AC-06-07 — Нет future leakage | Исторический decision context содержит только данные, доступные на момент события; поздние outcomes/max episode strength доступны analytics отдельно. Проверка: time-travel/revision fixtures. |

### E07 — Telegram alerts, control deck и manual use

Requirement `REQ-E07`; трассировка: S01 / TELEGRAM / REALTIME ALERTS; УПРАВЛЕНИЕ; MANUAL TRADING.

Telegram — основной интерфейс владельца. Компактный event показывает instrument/market, detector/TF/direction/metrics/strength/freshness; optional derivatives context только полезный и не перегружает сообщение. Alert открывает корректный рынок Bybit. Control deck доступен без запоминания slash-команд; ручной сценарий полезен независимо от trading agent.


Уточнение ручного интерфейса17Oct2026 по выбранной TELEGRAM-UX Goal: основные разделы компактно сгруппированы по задачам; открытие deck/status/settings/delivery panels не меняет сохраненное состояние уведомлений. Включение/выключение и применение параметров — явные действия владельца. Настройка отдельных TF отличается от применения ко всем; manual input имеет отмену и проверку актуальной revision. Active source coverage, выбранный owner scope и разрешенные Telegram detectors показаны раздельно. Краткая карточка сохраняет обязательные metrics/freshness и переход к полным сохраненным данным; существующие command identifiers/old callbacks сохраняют совместимость. Это уточнение AC-07/08, не новые AC и не изменение formulas/retention/торговых границ.

| AC | Критерий и способ проверки |
| --- | --- |
| AC-07-01 — Полный realtime alert | При meaningful live event владелец видит compact instrument/market/type/TF/direction/metrics/strength/freshness; optional context помечен. Уточнение владельца24Oct: ключевые значения движения, пробоя, отношений и интенсивности выделяются жирным; исходные числа/единицы и сохранённый текст повторной отправки не меняются. Проверка: render fixtures, UTF16 entity bounds/length/parse tests и owner Bot API smoke. |
| AC-07-02 — Ссылка на правильный рынок | Spot и Perpetual alerts открывают соответствующий Instrument/Market Type Bybit; открытие ссылки не создаёт ордер. Проверка: URL mapping fixtures и безопасный browser/manual smoke. |
| AC-07-03 — Control deck | Владелец открывает status/settings/list/add/remove/mute/terms через Telegram controls без запоминания команд; no-op settings не вызывает noisy error. Проверка: keyboard/handler fixtures и no-op exception scenario. |
| AC-07-04 — Правдивый active status | Status/settings отражают фактические рынки/universe/detectors/TF/thresholds и состояние данных; concurrent settings change не применяет старое решение как новое. Проверка: settings revision race и render/domain tests. |
| AC-07-05 — Управление доставкой | Realtime alerts включаются/выключаются отдельно от digest; mute действует на Telegram delivery и не останавливает history/agent delivery. Проверка: two-consumer isolation/failure tests. |
| AC-07-06 — Просмотр outcome | Из event в Telegram доступно последующее движение и outcome, сопоставимое с исходным signal, без торговой рекомендации. Проверка: event→outcome navigation fixtures. |
| AC-07-07 — Объяснение manual | Для каждого detector доступно почему он сработал и значения metrics/terms; manual use не требует account credentials или trading agent. Проверка: detector explanation fixtures и disconnected consumer сценарий. |

### E08 — Owner settings и presets

Requirement `REQ-E08`; трассировка: S01 / НАСТРОЙКИ RADAR / OWNER CONFIGURATION; PRESETS; TELEGRAM / УПРАВЛЕНИЕ.

Market domain принадлежит owner persisted configuration. Telegram chat ID/delivery preferences хранятся отдельно и не определяют universe/detector logic. .env — secrets/infrastructure/safe defaults. Обычные settings меняются без ручного .env/restart; готовые sensitivity profiles меняют понятный набор thresholds, individual parameters также доступны вручную.

| AC | Критерий и способ проверки |
| --- | --- |
| AC-08-01 — Owner configuration | Markets/universe/detectors/thresholds/liquidity/TF/enrichment/watchlist/alert policy/outcome horizons persistently принадлежат owner, отделены от chat delivery. Проверка: multi-chat-independent domain/persistence tests. |
| AC-08-02 — Изменение без restart | Owner меняет обычные settings, validated revision применяется в runtime без .env editing/restart; invalid/NaN/negative значения не ломают state. Проверка: concurrent update и validation tests. |
| AC-08-03 — Profiles и manual parameters | Несколько готовых sensitivity profiles показывают изменяемые thresholds; owner может отдельно настроить параметры и увидеть фактические активные значения. Проверка: apply/edit/reset profile fixtures. |

### E09 — Producer и handoff для trading agent

Requirement `REQ-E09`; трассировка: S01 / ИНТЕГРАЦИЯ С TRADING AGENT / STRUCTURED MARKET CONTEXT; INTEGRATION BOUNDARY; ДОСТАВКА EVENTS; ownership уточнен S06/D23.

Advisory source в отдельном runtime. Здесь реализованы независимый от Telegram signed producer и handoff; activation/приемка внешнего consumer выполняется в принимающем проекте. HTTP API/webhook transport не меняет contract. Consumer сам выбирает использование; его автономность, auth rejection и отсутствие model/trade side effects — обязательства принимающей стороны, переданные в checklist, а не prerequisites manual radar.

| AC | Критерий и способ проверки |
| --- | --- |
| AC-09-01 — Structured contract | Event/update содержит Event ID/revision, Episode ID, Instrument/Market Type, detector/version/settings/direction/TF, timestamps/freshness и reproducible metrics/units; нет execution fields. Проверка: schema/consumer contract tests. |
| AC-09-02 — Готовность авторизованного producer | При явной валидной transport configuration producer автоматически подписывает и отправляет events/meaningful updates независимо от Telegram mute/outage. Missing/invalid config ограничивает agent delivery и сохраняет standalone radar. Проверка: controlled receiver HMAC/ack/fault/retry/restart fixtures. Credential/binding/отклонение unauthorized на production receiver и live acceptance переданы принимающему проекту по D23. |
| AC-09-03 — Outbox и recovery | Restart/consumer outage сохраняет undelivered и позволяет replay последних актуальных events; retries разрешены, stale/expired помечены и не выдаются за fresh. Проверка: outage/restart/expiry tests. |
| AC-09-04 — Identity/revisions для consumer | Producer сохраняет Event ID, source/transport revisions и неизменные bytes при повторных попытках одной transport revision; новые revisions сохраняют event/episode identity и source history. Проверка: replay/reorder/ack/contract fixtures; consumer обязан реализовать dedup/conflict/history по переданному контракту, его live приемка вне этого проекта. |
| AC-09-05 — Delivery diagnostics | Owner видит pending/delivered/error состояние agent delivery независимо от Telegram. Проверка: diagnostics/readback и fault injection. |
| AC-09-06 — Handoff и causal analytics | Переданы versioned contract, synthetic payload, config/authorization/retry/freshness/retention boundaries, evidence/limitations и receiving-project checklist. Producer предоставляет history/episodes/outcomes/export без future leakage; manual radar полезен без consumer. Проверка: artifact/source alignment, example/producer fixtures и causal export/context tests. Consumer autonomy/no model/order side effects/live delivery проверяет принимающий проект по checklist D23. |

Уточнение accepted interface v2: `cryptowatcher.market_event.v2`, backward-compatible приём v1;
source event revision отделён от transport revision. Полный raw settings hash/snapshot и effective
profile parameters, source window identity/completeness, explicit fresh_until/expires_at,
episode revision/confirmations и frozen optional enrichment сохраняют causal provenance.
Late outcomes — отдельный historical update; receipt/retry не обновляют market observation.
Raw-body HMAC, immutable bytes, independent durable queues и qualified HTTPS/machine access
определены в [contract/procedure](runbooks/agent-delivery.md). Consumer freshness policy ограничивает
producer policy; owner transport preference не меняет detector settings revision.

### E10 — Persistence, migration и recoverability

Requirement `REQ-E10`; трассировка: S01 / DATA И DIAGNOSTICS / PERSISTENCE; DEPLOYMENT; S03 legacy state.

Owner settings/watchlist, history/revisions/episodes/detector metadata/outcomes персистентны. Runtime state, user settings, analytics и infrastructure config не смешиваются. Новые detectors/context добавляются без destructive migrations истории. При переносе chat domain в owner configuration legacy lists/mutes и overrides сохраняются по явной mapping policy; production state вручную для изменений detectors не редактируется.

| AC | Критерий и способ проверки |
| --- | --- |
| AC-10-01 — Разделение хранения | Owner settings, delivery chat state, runtime и history имеют явное владение и совместимые persistent schemas. Проверка: schema/persistence roundtrip и ownership tests. |
| AC-10-02 — Расширяемая история | Новый detector/version/context добавляется без destructive migration старых events/outcomes; previous revisions доступны в пределах срока хранения. Плановая retention не считается потерей данных при update. Проверка: upgrade/compatibility и retention fixtures. |
| AC-10-03 — Безопасная миграция/recovery | Legacy custom/seed/mutes/overrides сохраняются при согласованной owner migration; corrupt/unknown-version state не перезаписывается, backup/restore проверяемы. Проверка: old payloads, empty list, corruption/forward-version и restore tests. |
| AC-10-04 — Restart continuity | Restart восстанавливает settings/watchlist, episodes, subscriptions и delivery/outcome pending work без ручного production state edit. Проверка: crash/restart/duplicate-replay integration tests. |

### E11 — Digest и derivatives context

Requirement `REQ-E11`; трассировка: S01 / TELEGRAM / DIGEST; MARKET EVENTS / DERIVATIVES CONTEXT.

Digest агрегирует существующие events/episodes обоих рынков, не отдельную аналитику. Настраиваемая периодичность (например час) и независимые alerts/digest toggles. Spot enrichment только при надёжном сопоставлении Asset с USDT Perpetual; OI/change OI/funding/basis optional, самостоятельные Perpetual events обязательны независимо от enrichment.

| AC | Критерий и способ проверки |
| --- | --- |
| AC-11-01 — Digest aggregation | За выбранный период digest показывает counts, наиболее активные инструменты (отдельно от числа instruments), strongest/ongoing episodes и detectors обоих рынков из общей history. Проверка: fixed-period aggregation fixtures. |
| AC-11-02 — Digest schedule/toggles | Owner меняет interval и включает digest независимо от realtime; restart сохраняет настройки и не дублирует один digest interval. Проверка: controlled scheduler/restart tests. |
| AC-11-03 — Безопасный derivatives enrichment | Надёжно сопоставленный Asset получает доступные OI/change OI/funding/basis; отсутствие perpetual/metric не блокирует Spot. Проверка: mapping/optional failure fixtures. |
| AC-11-04 — Provenance enrichment | Каждый derivatives показатель имеет source/market/units/timestamps, собственный cadence; enrichment не заменяет самостоятельный Perpetual detector. Проверка: schema/freshness и separate market events. |

### E12 — Технические границы, CI/CD и эксплуатация

Requirement `REQ-E12`; трассировка: S01 / ТЕХНИЧЕСКИЙ КОНТУР; DEPLOYMENT; DIAGNOSTICS; S02 AGENTS; S09 shared Contabo resource evidence.

Один deployable Python сервис допустим при подходящей нагрузке; ingestion/detectors/episodes/persistence/analytics/Telegram логически разделены. Docker/VPS и GitHub Actions CI→merge→CD. Никаких придуманных SLO, числа environments или горизонтальных replicas. Перед runtime delivery устанавливаются target/config/artifact/preconditions/recovery/version/health/smoke.

По S09 подходящая нагрузка подтверждается сопоставимыми измерениями всего VPS и CryptoWatcher: CPU, resident/cache memory, physical I/O/pressure и запас соседним проектам. Установившаяся работа отделяется от конечных затрат recovery, штатного expiry, compression/repack и backup; одни lossless disk savings или CI этого не доказывают. Ресурсный бюджет выводится из измерений в текущей Goal, не подменяется слепой CPU quota. Приёмка совместно проверяет объём/рост БД со служебными файлами, свежесть источников, возраст due-only backlog и Telegram freshness/timeliness; future horizons не считаются отставанием. После recovery/repack нужны неизменная revision/config/generation, существующий30min stable gate и полный24h result. Новых AC IDs/процентов или выдуманного абсолютного Telegram SLO это уточнение не создаёт.

| AC | Критерий и способ проверки |
| --- | --- |
| AC-12-01 — Docker/VPS runtime | Собираемый Docker artifact запускает сервис с persistent data volume/config; после restart данные сохраняются. Проверка: target-runtime image build/start/restart и readback без production mutation. |
| AC-12-02 — GitHub Actions delivery | Изменения проверяются Actions перед обновлением приложения; CI→merge→CD доставляет идентифицируемый artifact/revision на известный VPS с required gates/post-checks. Проверка: первичные CI/CD records и live version/smoke. |
| AC-12-03 — Domain boundaries и diagnostics | Ingestion/domain/persistence/analytics/delivery логически разделены; diagnostics показывает streams/markets/detectors/universe/gaps/recovery и обе доставки, отличая отсутствие events от отсутствия данных. Проверка: architecture review и degraded-state tests. |
| AC-12-04 — Secrets и проверяемые изменения | Secrets не попадают в tracked artifacts, alerts/logs/context; критичные auth/migration/data paths имеют воспроизводимые regression checks и dependency/config review. Проверка: negative leakage fixtures, regression suite и security inventory. |

### E13 — Компактное хранение, сроки и новый период истории

Requirement `REQ-E13`; трассировка: S01 / DATA И DIAGNOSTICS / ХРАНЕНИЕ И КОНТРОЛЬ ОБЪЁМА; РАЗОВЫЙ ПЕРЕЗАПУСК НАКОПЛЕНИЯ; S05. Дополняет E06/E10/E12, сохраняет их ID; D22 заменяет прежний D12.

| AC | Критерий и способ проверки |
| --- | --- |
| AC-13-01 — Общие данные без полных дублей | При повторной обработке и пересекающихся source windows актуальные heads ссылаются на immutable revisions; общие candles/settings/context не копируются полностью в каждый event/outcome. Формулы, результаты, provenance, все events/markets/detectors/TF сохраняются. Проверка: equality с исходными аналитическими fixtures, replay/restart и измерение bytes/event с outcomes/services. |
| AC-13-02 — Автоматическая bounded retention | Defaults: details30d, small-TF candles35d; сроки настраиваются и совместимы с lookback/horizons. Cleanup ограниченными batches сначала сохраняет агрегаты, затем удаляет просроченные revisions/episodes/outcomes и ненужные inputs. Только необходимые зависимости удерживаются до окончания срока подробного расчета. Проверка: полный controlled-clock цикл, pending/restart/boundary и reference integrity. |
| AC-13-03 — Качество detectors 12 месяцев | Сводные результаты сохраняются 12 календарных месяцев от даты events с methodology/settings/version/market/TF и остальными согласованными grouping dimensions, N/completeness/noise/comparator limitations. Старые детали явно недоступны; новые settings не меняют прежние результаты. Проверка: одинаковые cohorts до/после удаления details в доступной гранулярности, границы срока и выбранного периода, export/Telegram; policy Q07. UTC-day summaries не обещают восстановление минутной детализации. |
| AC-13-04 — Укрупненный context | Если такой context используется, хранится90d, для выбранных Spot до180d; минутная история не продлевается до этого срока. Новые источники не добавляются. Проверка: inventory используемого context и controlled-clock fixtures; отсутствие необязательного context явно указано. |
| AC-13-05 — Конечные служебные зависимости | Completed jobs/queues сокращаются до необходимого состояния; retry/restart/dedup сохранены. Pending расчет/доставка при истечении допустимого ожидания получает явный terminal status и освобождает зависимости; service cursors не удерживают архив бессрочно. Проверка: fault/retry/restart/expiry и replay fixtures. |
| AC-13-06 — Контроль объёма БД | Автоматически доступны category counts/bytes, indexes/journals, net growth и прогноз по выбранным срокам. Желаемый объём25,000,000,000bytes; допустимый жёсткий предел30,000,000,000bytes для DB/index/free pages/WAL/SHM/journal проверяется измерением по уточнению владельца24Oct. Превышение желаемого объёма само по себе не блокирует приёмку в пределах30GB; backups/experiments отдельно. Actual/estimate и ограничения видны, превышение объясняется без silent сокращения scope/retention. Routine control bounded, без постоянного full scan. Проверка: счетчики/измерения против fixture, рост/очистка/предупреждение/measurement budget и конечный стабилизированный замер D25. Наличие счетчиков, successful tests/CD или short-window model не заменяет оценку заполненной базы и ее допущений; превышение target требует расчета причин и вариантов без скрытых сокращений. |
| AC-13-07 — Разовый reset | После REQUIRED checks отдельная exact-target процедура сбрасывает только прежнюю тестовую market history CW и ее queues/cursors; проверенный owner settings/watchlist/thresholds/notifications/mutes и credentials/infra сохранены. Новый generation/start виден, restart/deploy не повторяет reset, backfill не отправляет прошлое как live и ограничен потребностями расчетов. Backups/experiments не удаляются. Проверка: private-state equality/restore, отказ при неверном target, reset/restart/no-old-alerts fixtures, actual source/image/health/ingestion/outcome/Telegram и применимые integration post-checks. |

## Решения по исходным вопросам и зависимости

| ID | Действующее правило | Источник / эксплуатационная зависимость |
| --- | --- | --- |
| Q01 | Формулы, profiles, episodes и freshness заданы D11–D15; эффективность defaults оценивается описательной статистикой, без обещания прибыльности. | Согласованный план10Oct; изменение production thresholds — явная owner операция. |
| Q02 | Отдельный claude-bybit-trading-agent принимает backward-compatible v1/v2 по D18; полный settings snapshot/hash сохраняется, включая известные retention metadata. | Producer contract/controlled tests/handoff проверяются здесь; endpoint/binding/current consumer revision/owner credential и signed live delivery — в задаче принимающего проекта. Отсутствие credential не блокирует standalone radar/handoff. |
| Q03 | JSON import относится только к private owner233382575; прочие chats архивируются без объединения. Явный пустой список сохраняется. | D11; actual migration/recovery Evidence находится в плане. Новый reset по D22 не повторяет JSON import. |
| Q04 | Horizons, neutral outcomes, no-signal comparator и export определены D16; сроки хранения изменены D22. | Прежние raw90days/indefinite details отменены; expiry не меняет записанные методики/settings/results. |
| Q05 | Target — Contabo167.86.68.98, /opt/crypto_watcher, Compose project crypto_watcher/service crypto_watcher, один production Environment. | Точный runtime, approvals, artifact/backup/recovery и post-checks — в действующей процедуре delivery и Evidence плана. |
| Q06 | CMC/reset callbacks deprecated с понятным ответом; startup wrapper и legacy migration compatibility сохраняются. | D04; неподтвержденные внешние consumers private helpers не дают основания для нового удаления. F24 в плане. |
| Q07 | При запросе части UTC-дня после expiry details точная intraday выборка из day summaries недоступна. Нужна единая явная policy CSV/NDJSON/cohorts. | Выбрано D27: явно нормализовать архивную часть периода до UTC-дней с requested/effective bounds и фактическим охватом. Это уточнение исходного period/limitations requirement, а не разрешение хранить полные старые events или менять точность numeric facts. F48; решение при выбранной implementation Goal. |

Текущий согласованный scope содержит **65 AC**: исходные58 сохранены, E13 добавляет7 критериев обновленного S01/S05. Статусы и аудиторские оценки находятся только в плане. Новые business rules, сокращение analytics/retention и изменение формул требуют решения владельца; конкретизация проверок понятного требования scope не меняет.

## Утверждённые implementation решения 2026-10-10

Основание: явное поручение владельца реализовать согласованный полный план. Действующие AC IDs сохранены; findings сами по себе новых требований не создают.

- D09: пять семейств detectors; composite impulse развивает существующий composite, отдельного шестого detector нет. Финансовые tokenized assets/TradFi исключены; обычные crypto tokens сектора RWA допустимы по metadata/liquidity.
- D10: Python3.11, один modular deployable; SQLite WAL с одним writer/короткими транзакциями. Telegram SDK20.8 сохраняется. REST/agent async HTTP, public WS websockets; precise dependencies/hash lock.
- D11: owner user233382575, private-chat access fail-closed; bot7523865123. Owner config revision/snapshot отделён от chat delivery/runtime. JSON migration сохраняет original, явный empty list, owner overrides/mutes; чужие chats архивируются, unknown/corrupt schema не перезаписывается. Saved values выше env defaults.
- D12: SUPERSEDED D22 от 2026-10-16. Прежние raw90days и бессрочное хранение details/inputs отменены; исторические Evidence не подтверждают новую policy.
- D13: defaults universe liquidity1M USDT при отсутствии saved value; metadata10min/liquidity5min; native1m/3m/5m/15m concurrently; heartbeat20s; REST5req/s/concurrency4; reconnect1–60s+jitter; bootstrap256 closed candles. Bybit shared-IP budget учитывает trading agent.
- D14: normal price thresholds1/2/2.5/4% для1/3/5/15m; turnover/latest mean previous12 ≥3; breakout previous20 high/low +0.1 previous ATR14; volatility TR/mean previous20TR ≥2; composite price+turnover+liquidity сохраняет legacy formula/equality. ATR14 — simple mean предыдущих14TR; current bar исключён из baseline. Insufficient/zero/gapped windows не считаются signal. Turnover/volatility direction NEUTRAL; price/composite по знаку, breakout по стороне. Conservative thresholds×1.5; sensitive×0.75; watchlist отдельный×0.75. Migrated custom values не заменяются preset автоматически.
- D15: episodes instrument/market/direction, new detector family/TF confirmation либо strength×1.5 разрешают meaningful notification; coalesce2s. Inactivity2×longest episodeTF, maxlife60min; re-arm detector/TF после2 complete non-signal bars. Suppressed observations сохранены. Fresh price/spike/volatility/composite1TF, expire2TF; breakout fresh2TF/expire4TF; receipt/retry/outcome исходный event не освежают.
- D16: immutable outcome reference close/time; horizons5/15/30/60min, complete subsequent1m window; returns/MFE/MAE, neutral без directional label. Continuation/reversal noise=max(2ticks/reference×100,0.1ATR14/reference×100), snapshot versioned. Comparators sameinstrument/market/TF, previous28days matched UTC hour/ATR% quartile, до20 closest nonoverlapping windows, no signal по frozen detector/settings, complete outcome заканчивается до event observation. Insufficient N/completeness явны; statistics не являются Strategy profitability. NDJSON versioned streaming export и CSV aggregates; no silent recomputation/future leakage.
- D17: Telegram alerts/digest независимы, digest60min initiallyOFF. Mute меняет Telegram delivery, history/agent продолжаются. New detectors могут работать silent до owner enable; actual settings/capabilities видны. BotAPI ambiguity не позволяет обещать exactly-once messages.
- D18: contract receiver POST /integrations/cryptowatcher/events; activation после его проверки принадлежит принимающему проекту по D23. Backward-compatible v2 с settings/profile/provenance/freshness/episode/completeness/enrichment и explicit live/historical update kind; v1 поддерживается. HMAC-SHA256 raw immutable body, receiver limit65536bytes, outbox retry1–300s, expiry и cutoff independent of receipt. No execution fields; receiver не запускает model/trade. Full used inputs доступны producer-side для export в пределах сроков хранения. Primary source prepared counterpart branch43c7e88f; counterpart main40a3147172 на момент проверки ещё не содержит receiver.
- D19: optional asset-matched Spot derivatives context после core; ticker1min/OI5min cadence, explicit source/units/timestamps, missing/stale data nonblocking. Separate Perpetual detection сохраняется.
- D19a: enrichment default OFF, strict persisted owner bool; exact eligible Bybit base identity и один USDT Perpetual match, zero/multiple/alias/scaled candidates не угадываются. OI linear BASE/asset (both sides),5min change по двум точкам ровно5min apart, нулевая baseline без pct; funding raw fraction×100%, next funding UTCms. Basis100×(perp_last/spot_last−1)% по двум доступным REST snapshots≤1min с timestamps/skew≤1min; не simultaneous trade prices. Per-metric source age≤cadence FRESH, затем STALE; missing/future/invalid явно помечены. Общий budget/current recovery приоритетны; optional failures не меняют core signals. Event context frozen по availability≤observation cutoff, latest owner view отдельно.
- D16a: explicit owner recompare может добавить comparator revision на новых prior inputs/availability, сохраняя original event/settings/reference и прежние results. Completed comparisons не меняются без действия владельца; methodological change получает новую version. Profile/version/settings comparisons используют реально записанные outcomes с N/limitations; automatic threshold writes отсутствуют.
- D20: Contabo167.86.68.98 SSHroot:22/MobaXterm; unit/path известны по inventory в delivery runbook; complete image source revision/backup fitness требуют evidence. First migration отдельный safety gate; затем automatic CI→merge→CD. Production Environment один; artifact sourceSHA/digest immutable, exact target lock/eligibility/recovery/postchecks. Решения supersede соответствующие UNSET в D07/Q01–Q05; operational факты продолжают требовать evidence.
- D21, explicit owner decision2026-10-11: initial consumer context maximum age900s от market observation time, дополнительно к более короткой type/TF-specific producer freshness и episode state; receipt/retry не продлевают возраст. Older records/outcomes остаются в history, но не считаются fresh context. Cloudflare Zero Trust/Access не добавляется в текущую поставку; сначала установить причину403 и проверить existing HTTPS transport с фактического VPS. Существующие TLS/HMAC/Cloudflare rules сохраняются. Затронуты05-04/06-07/09-01/09-03, denominator и Strategy behavior не меняются. Runtime activation/readback ещё требуется.

### Уточнение проверки outcome/comparator/export v1 (G3)

Декомпозиция D16, без нового scope/AC: numeric targets требуют полного subsequent1m окна и неизменных reference time/close; equality у ±noise относится к neutral movement. Недостающий ATR/tick context помечается UNCLASSIFIED. Actual prior ATR%-population задаёт nearest-rank empirical quartiles, равенство boundary относится к нижнему bin. Reference windows выбираются от наиболее недавнего подходящего времени, до20 неперекрывающихся; outcome end строго раньше event observation. Composite использует доступный тогда ticker, не будущую liquidity; отсутствие нужного prior context уменьшает N и даёт limitation. Frozen completed comparisons не меняются при последующей source history; другой метод получает отдельную version.

Export filter относится к selected event observation time `[start,end)`, detector/version/market. Все revisions выбранных events, related episode revisions и отдельные late outcomes/settings/provenance доступны с availability timestamps; related episodes могут содержать ссылки на события вне filter. Для исторического decision coding agent использует explicit cutoff/revision readout; final max episode strength/outcomes не включаются туда автоматически. Read-only consistent export заканчивается completion counts. CSV/analytics grouping отделяет frozen settings revision, detector/version и outcome methodology; actual N/noise/partial data и зависимости samples явно видны.

### D22 — Новое накопление и bounded retention (2026-10-16)

S01 обновлен и S05 явно заменяют D12: подробные events/revisions/episodes/outcomes30d; raw small-TF35d плюс только использованные inputs еще сохраняемых подробных расчетов. Сроки настраиваются, lookback/horizons должны укладываться в поддерживаемые окна. Quality summaries12 календарных месяцев от event date, без полных raw arrays; старые подробности не подменяются агрегатами. Используемый coarse context90d/selected Spot до180d условен и не требует новых источников или минутной детализации180d. После сохранения агрегатов automatic bounded cleanup освобождает ссылки и service dependencies. Ожидание не бесконечно: истечение подробного срока дает явный terminal incomplete/expired status.

Working DB+indexes+journals:25GB — желаемый объём,30GB — допустимый жёсткий предел по уточнению D27 от24Oct. Факты и extrapolation разделены; причина превышения и варианты решения видны владельцу. Постоянный full scan не является механизмом контроля. Не сокращать события/detectors/markets/TF/аналитику/retention скрытно.

Прежняя тестовая market history подлежит разовому reset после исправлений/tests; ее перенос, сжатие, полная конвертация и qualification исключены из текущей задачи. Owner settings/watchlist/thresholds/notification preferences/mutes/user config и secrets/infra сохраняются и проверяются. Reset имеет exact перечень, generation boundary и совместимые queues/cursors; не выполняется при обычном startup/deploy. Удаление backups/experiments не разрешено этим reset. Обычные backup/CD остаются<=900s, RAM не увеличивается; последующее наблюдение отделено от закрытия исправлений/reset.

## D23 — Разделение проектов и handoff (S06, 2026-10-16)

CryptoWatcher отвечает за manual Telegram radar, свои market data/detectors/episodes/
analytics/storage, готовность подписанного producer и воспроизводимый handoff.
Trading agent отвечает за подключение: credential/recovery/binding, receiver policy,
согласованную producer infrastructure configuration и end-to-end acceptance.
Наличие receiver binary и прежние cross-project fixes сохраняются как Evidence;
они не означают, что live integration уже включена.

Стабильные AC-09-01…06 сохранены. 09-02/04/06 уточнены по новой границе, а прежние
обязательства production auth rejection/idempotency/consumer independence/causal
cutoff не объявлены выполненными и не потеряны: они переданы в TA-CW-01…06
[canonical handoff](runbooks/agent-delivery.md#handoff-принимающему-проекту).
Прежний B02 и требование activation до CW Goal DONE superseded в части границ
этого проекта. D18 transport/authority/freshness semantics и согласованные900s
consumer cap сохраняются как контракт принимающей стороны.

Для CW DoD достаточно validated producer/contract/handoff и проверенного manual
runtime; доступность/активация внешнего приложения не блокирует его поставку.
Новая оценка процентов этим UPDATE не проводится; исторический audit snapshot
остается на своей revision и прежних границах.

## D24 — Lossless storage growth correction (GROW, 2026-10-16)

По явному поручению владельца исправляется layout/упаковка, а не продуктовый scope.
Physical user_version остается2; storage extension использует typed record encoding3,
нормализованные внутренние row IDs для expiry и общие cohort/provenance dictionaries.
Public event/revision IDs, JSON types/float bits, формулы и source links сохраняются.
Old CW2/J/Z records и legacy index/quality tables остаются читаемыми в смешанной базе.

После обычной полной source/backup validation writer устанавливает небольшие атомарные
DDL extensions без копирования market history. Старые rows не пропадают из compatibility
views. Bounded background repack переносит по одной проверенной записи с durable cursor
в той же транзакции; restart продолжает сохраненный прогресс. Нет полного VACUUM,
второй рабочей БД, reset или повышения15min backup/CD budgets/RAM.
Recovery проверяет arrived closed data перед REST, объединяет overlapping requests и
retire satisfied/obsolete work; реальные gaps/conflicts сохраняют retry и диагностику.
Годовые facts с другим provenance получают отдельный chunk, не metadata первого метода.

Forecast различает retained inserts, net queue occupancy и эффект repack. Quality
сценарии измеряют текущий codec/SQLite layout на isolated synthetic данных, отдельно
показывают cohort density/comparator count; это не production measurement или upper bound.
Проверяемый20GB target сохраняется, превышение/неизвестность не скрываются. Сроки D22,
полнота аналитики и охват не сокращаются. Forward-fix с reader обоих formats; старый
binary без encoding3/view support не является допустимым rollback после новых writes.

## D25 — Capacity evidence после GROW (S08, 2026-10-16)

20GB относятся к working DB с её indexes/free pages и рабочими journals. Logical
payload/key sizes и физические таблицы/indexes показываются раздельно; indexes
внутри DB не прибавляются повторно. Backups/experiments — отдельный inventory.
Старый point forecast разбирается по исходным формулам, а allocation текущего mix
не называется измеренным per-category daily flow. Actual quality facts отличаются
от prospective frozen cohorts и synthetic сценариев comparator density.

Проверка retention использует управляемое время, finite dependencies, повторные
запуски/restart и сохранение точных numeric/group/method/settings/comparator results.
После bootstrap/repack организуется конечный24h замер counters/files/cleanup и
boundary ledger/indexes; без ожидания агента/Actions и без частого полного scan.
Короткое post-restart окно не объявляется устойчивым суточным темпом. Наблюдение
имеет явные completion/partial/failure states; tests/CD PASS не означают capacity PASS.
Новые codec/batching выбираются после измерения ожидаемого эффекта. Сокращение
retention, coverage, точности или возможностей аналитики требует решения владельца.

Уточнение по поручению владельца17Oct2026: backfill/repack не должны зависеть от
завершения долгого analytics cycle; обработка ограничена, уступает ingestion и
Telegram, сохраняет retry/cursor/restart и исходные данные. Repack завершается на
зафиксированной границе накопленного; новые compact writes не продлевают его
бесконечно. После фактического завершения bootstrap/backfill/repack automatic finite
observer начинает24h только на стабильной revision/config/generation. RAM и обычные
backup/CD<=15min сохраняются. Полный24h итог отделен от завершения реализации.

## D26 — Observer diagnostics и чистое накопление для замера (2026-10-17)

По явному поручению владельца отклонённые baseline attempts сохраняют полный
измеренный snapshot, phase, blockers и sanitized error metadata. Quick sample и
deep boundary не подменяют друг друга; сброс stable counter не стирает причину.
Успешный baseline требует прежних stability/source/config/generation gates.

Отдельно разрешено начать новый период на чистой working DB. В отличие от
прежнего удаления тестовой истории D22 вся текущая БД/sidecars сохраняется в
проверяемом архиве вне working total, вместе со всеми observation files.
Owner settings/preferences/mutes/legacy identity и credentials/infra сохраняются;
новая generation явно отделяет новое накопление. Это explicit exact-target
операция, не startup/автоматический reset, не повтор consumed permit. Необходимы
full archive integrity/hash/private restore и post-checks. Scheduled tasks не
создаются. Запуск нового24h подтверждается фактическим OBSERVING/baseline;
ожидание завершения суток не входит в исправление и поставку.

## D27 — Единая CAPACITY25 Goal (18 октября 2026)

Владелец утвердил и поручил полную реализацию backlog/observer/lossless storage,
прежних findings, ordinary delivery и чистого конечного24h замера. Жёсткий предел
Первоначальный предел AC-13-06 составлял25,000,000,000bytes. Уточнение владельца24Oct заменяет его:25,000,000,000bytes — желаемый объём,30,000,000,000bytes — допустимый жёсткий предел для DB/indexes/free pages/WAL/SHM/journal;
исторические20GB оценки остаются snapshots прежнего решения. Backups/experiments
учитываются отдельно. Рынки/TF/detectors/retention/точность/полнота аналитики,
existing data/generation/archives/settings сохраняются. Новый reset, scheduled
tasks и повышение RAM/900s gates в эту Goal не входят.

Q07: архивная часть intraday selection явно нормализуется до UTC-дней с requested
и effective bounds/resolution во всех CSV/NDJSON/cohort views; точные retained
details фильтруются точно. Это не восстановление выдуманной intraday точности.
Digest activity — число сохранённых event observations по instrument/market
в запрошенном периоде; strongest/ongoing остаются самостоятельными разделами.

Observer различает future horizons, текущую работу и overdue backlog; state OK
не заменяет измерение возраста outcomes/comparisons и arrival/service flow.
Каждая попытка/error сохраняется. Partial ledger/cohorts/indexes не являются
полным boundary или capacity PASS. Сначала measured mature-population capacity
при прежней policy, затем clean24h на fixed source/config/generation; обе проверки
обязательны. Отклонённые/прерванные прежние замеры и их факты сохраняются.

## D28 — Длительность конечной Linux/native проверки (24 октября 2026)

По S11 владелец разрешил увеличить только длительность полной конечной проверки:
450CPU seconds,510wall seconds,600outer seconds. Сохраняются один CPU/4GiB/no swap,
изолированные public/default fixtures без сети и production mounts, полный исходный
point/scan/CHECK/FK/private/hash/fresh-reopen oracle и preflight резервов общего VPS.
Результаты прежних180CPU failures сохраняются. Это не разрешение повысить ресурсы VPS,
ослабить проверки данных, заменить исправление backlog или чистое суточное наблюдение.
900s ordinary delivery gate и25GB desired/30GB hard working limit неизменны.
