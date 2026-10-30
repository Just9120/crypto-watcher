# CryptoWatcher — project spec

Canonical spec: этот файл. Canonical plan: [open_issues.md](open_issues.md). Статусы и Evidence — в плане, не в spec.

## Действующая основа 30 октября 2026

Текущий нормативный источник — S12. D29 ниже заменяет несовместимые положения D01–D28; они сохранены как история решений. Старые 25/30GB,35d general candles, постоянный N20 и требование сохранять/конвертировать прежнюю тестовую историю больше не являются текущим scope. Цель аудита не включает реализацию этих изменений.

## Источники и revisions

| ID | Источник / revision | Охват |
| --- | --- | --- |
| S12 | Требования CryptoWatcher30Oct; Drive ID 1wNt8hpuZTrvMLmSbfTU5H4hQTzngNDQW3DG3Lm7APyE; modified 2026-10-30T10:44:02.254Z; revision AHj4eMQCNFivRFEs8Aae0L-D0fGqRdt6JNkzADv9l29tod8fah4CeFOxxY4vIgkqyHU6XbRPo2vmA12_ifiQ37J7NGun9F0micdBDrT-QwY | Полный t.0, все разделы; project/template отличаются только repository/requirements bindings |
| S13 | Проектный промпт 30Oct; Drive ID 1pVdUNs0q45cidAgp5WoxqESvWquk7W8RUQQHXTwrZpg; modified 2026-10-30T08:35:36.571Z; revision AHj4eMTc1WHQL7mRJ-whPZn3RvXX2YDlXYfWkHln91I46xWHHpT82uVobXOV08VOPuG3yP937Ga47DlmueLfm7ISu-1lTA4ZeCEnruJyRZg | Полный t.0, все разделы; project/template отличаются только repository/requirements bindings |
| S14 | Общий шаблон 30Oct; Drive ID 1QW21f6KpxNJmvf5UBMBXbHhiC9cTxd4nMVv3keZJfls; modified 2026-10-30T08:35:05.587Z; revision AHj4eMSJG1kB8gox_AT-nj7hRCnDm3kgKvyku0Y_Q2JPpsjVQ8EbIxKsgNMBH0yOLc0cGfsJR4rxtw8VkKzE7d2obIUeET42NIoZqywv1DE | Полный t.0, все разделы; project/template отличаются только repository/requirements bindings |
| S15 | Переданные 29Oct2026 [AGENTS.md](../AGENTS.md) SHA256 d5339b8fb9b0d0e7be2d4007613d2c916ce33c6cdb9a1c678281720b865049df; [ci-cd-rules.md](../ci-cd-rules.md) SHA256 41236d79440871b65692de5f3c1a13760009ae25c6948b6b43f5407953086b34 | Универсальные repository/CI правила, без прежнего project-specific tail |
| S16 | Явно утверждённый план полного аудита в текущем чате 30Oct2026 | Функциональность Telegram включена; visual/layout исключены. Только root instructions/spec/plan/local commit, без push/PR/merge/app fixes/production changes/нагрузки/schedules |
| S01–S11 | Прежние источники и решения 10–24Oct2026 в истории этого файла и Git | Исторические; применяются только если совместимы с S12/D29. Не создают полномочий на новую production операцию |

## D29 — Облегчённый scope и приоритет источников

S12 требует отдельной активации detector, meaningful episode updates, одного immutable episode origin/outcome set, unique-episode statistics, исключения постоянного N20, general72h/details30d/aggregates12m, finite waiting и working≤35GB. Детализация/точность формул и provenance сохраняются для действительно сохраняемых объектов; это не обязательство хранить каждый свечной repeat. Глубокое исследование вне текущего рабочего цикла.

Прежний D10 не задаёт обязательное число writer для новой версии. Существующая экспериментальная ветка с двумя writer не является частью main и не становится новым требованием. Выбор архитектуры обосновывается полным процессом и ресурсами общего VPS; локальный speedup/CI сам по себе не приёмка.

Будущий разовый переход удаляет только прежнюю тестовую market history CW/research queues без архива/полной конвертации. Private state/owner settings/watchlist/credentials/infra сохраняются, TA/Paper не затрагиваются, обычный restart/deploy историю не сбрасывает. Источник задаёт будущую процедуру, текущий аудит её не выполняет.

35GB — допустимый верхний предел рабочего набора, не цель заполнить диск. Backups/experiments/temp учитываются отдельно; физический admission и резерв общего VPS обязательны. Доказательство устойчивости новой версии: сначала 30min, затем 24h при неизменной версии/settings, с различением due и future horizons.

## Стабильные AC и связи заменённых критериев

Все 65 прежних AC IDs сохранены. Смысл изменён только по S12; точная карта old→new находится в локальном audit-20261030/AC-changes.json. Добавлены AC-04-06 (activation), AC-08-04 (72h compatibility) и технический AC-12-05 (S15, полный профиль без CD). Итог 68 равноправных продуктовых/технических AC. Findings не становятся AC автоматически.

READY означает реализацию по коду с подходящей автоматикой. Ожидание live/ad-hoc smoke не скрывает явный defect и само по себе не понижает code readiness. Визуальная часть этого аудита не проверялась; функциональные и runtime ограничения перечислены отдельно.

## Интерфейсы и инварианты

Identity: Exchange+Instrument+Market Type, Spot/Perpetual не смешиваются. Events/episodes/reference/settings/provenance versioned и immutable по методике; экспорт/consumer должны отличать поздние результаты от decision context. Owner settings/private и Telegram chat delivery не взаимозаменяемы. Изменение public commands/callbacks/export/agent contract проверяется как compatibility boundary.

Radar не размещает ордера и не вызывает LLM от получения/отправки context. HMAC/strict ack/idempotency/retry/freshness сохраняются; интеграцию receiving-project выполняет основной Trading Agent по D23. Полезность ручного radar не требует его подключения.

## Эпики и критерии приёмки

### E01 — Назначение, границы и владелец

Requirement REQ-E01; текущая трассировка S12/S15, AC IDs сохранены.

Личный deterministic Python radar Bybit без LLM/news/trading credentials, ордеров, risk management и Web SPA. Trading Agent — самостоятельный advisory consumer, execution authority остаётся вне CryptoWatcher.

| AC | Критерий и способ проверки |
| --- | --- |
| AC-01-01 — Детерминированный Python runtime | При одинаковых market data и settings повторный расчёт даёт одинаковые metrics без LLM. Проверка: fixtures и анализ imports/вызовов. |
| AC-01-02 — Нет торговых полномочий | Radar/manual сценарий работает без trading credentials; код и structured context не создают BUY/SELL, position size, order type и ордера. Проверка: анализ endpoints и contract tests на запрещённые поля. |
| AC-01-03 — Bybit-only ingestion | Все активные market-data пути используют Bybit; новости, macro и другие биржи не подключаются. Проверка: inventory источников, handlers и сетевых adapters. |
| AC-01-04 — Доступ только владельцу | Чужой Telegram user/chat и пустая/невалидная owner configuration не получают domain state и не меняют его; каждый command/callback соблюдает один access policy. Проверка: positive/negative tests, в том числе /terms и group chat. |
| AC-01-05 — Отдельный сервис без собственного Web UI | CryptoWatcher имеет отдельные repository/runtime, полезен без trading agent и не требует SPA/PWA. Проверка: entrypoint/dependency inventory и offline domain сценарий. |

### E02 — Universe и watchlist

Requirement REQ-E02; текущая трассировка S12/S15, AC IDs сохранены.

Широкие динамические Spot USDT и USDT Perpetual universe раздельны. Metadata/pagination/eligibility fail closed для неизвестных специальных типов; USDT сам по себе не делает xStocks/TradFi/RWA/Event Contracts допустимыми. Liquidity настраивается; custom watchlist по instrument+market, без автоматического seed.

| AC | Критерий и способ проверки |
| --- | --- |
| AC-02-01 — Два широких universe | После refresh доступны все подходящие Spot USDT и USDT Perpetual, без fixed Top 100; данные и events двух рынков не смешиваются. Проверка: paginated metadata fixtures, >100 instruments, одинаковый symbol в обоих рынках. |
| AC-02-02 — Eligibility инструментов | Не-USDT, non-Trading, запрещённые и неизвестные специальные типы исключены; eligibility основана на metadata, а не суффиксе symbol. Проверка: positive/negative fixtures и pagination. |
| AC-02-03 — Настраиваемая liquidity | Изменение минимальной liquidity меняет eligibility universe; liquidity не навязана формуле каждого detector. Проверка: boundary values, refresh и фактические active settings. |
| AC-02-04 — Watchlist по рынкам | Владелец добавляет/удаляет Instrument + Market Type, получает проверку допустимости, сохраняет список и отдельную чувствительность после restart. Пустой список не подменяется seed. Проверка: Telegram/domain и persistence tests. |

### E03 — Realtime ingestion и timeframes

Requirement REQ-E03; текущая трассировка S12/S15, AC IDs сохранены.

Основные 1m/3m/5m/15m одновременно, только закрытые свечи. WebSocket приоритетен, REST — bootstrap/recovery/outcome. Gaps/неполнота ограничивают расчёт, backfill не создаёт исторический live flood.30m/1h context допустим, не обязателен.

| AC | Критерий и способ проверки |
| --- | --- |
| AC-03-01 — WebSocket обоих рынков | Runtime подписывается на public streams обоих рынков, выполняет REST bootstrap и восстанавливает subscriptions после disconnect/restart. Проверка: adapter tests с reconnect и безопасный live smoke. |
| AC-03-02 — Одновременные timeframes | Один market runtime обрабатывает 1m/3m/5m/15m одновременно; объединение сохраняет исходные TF. Проверка: interleaved fixtures и event provenance. |
| AC-03-03 — Закрытые свечи | Незакрытая candle не участвует в подтверждённом kline event; на границе закрытия eligible только closed candle. Проверка: open/closed/boundary fixtures. |
| AC-03-04 — Gaps и recovery | Потеря/неполнота данных ограничивает detectors/outcomes; recovery восстанавливает историю без исторических live alerts и помечает неполные horizons. Проверка: gaps, duplicate/out-of-order/reconnect replay. |
| AC-03-05 — Разделение cadence и TF | Изменение частоты получения данных не меняет размер свечи и наоборот; UI и расчёт используют разные параметры. Проверка: settings/domain fixtures. |

### E04 — Detectors и metrics

Requirement REQ-E04; текущая трассировка S12/S15, AC IDs сохранены.

Поддерживаются price impulse, turnover spike, range breakout, volatility expansion и composite. Формулы/units/provenance детерминированы; liquidity прежде всего eligibility. Активность domain detector отличается от выбора Telegram families; disabled detector не создаёт историю/расчёты. Автоматической production calibration нет.

| AC | Критерий и способ проверки |
| --- | --- |
| AC-04-01 — Composite formula | Для полных закрытых свечей composite использует abs(close/prev_close−1) ×100, turnover/latest против среднего предыдущих candles и active thresholds; equality проходит, любой failed filter запрещает signal. Проверка: deterministic boundary/zero-history fixtures. |
| AC-04-02 — Набор detectors на двух рынках | Price impulse, volume/turnover spike, range breakout, volatility expansion и composite impulse дают воспроизводимые events отдельно для Spot/Perpetual. Проверка: positive/negative/boundary fixtures утверждённых формул и versions. |
| AC-04-03 — Provenance и direction | Каждый event сохраняет detector/version, settings snapshot, исходные данные, metrics/units и обоснованный direction/strength. Проверка: schema и replay fixtures, neutral case. |
| AC-04-04 — Изоляция ошибок | Ошибка одного detector или optional context видна в diagnostics и не останавливает исправные detectors/рынки. Проверка: fault injection и наблюдаемые degraded flags. |
| AC-04-05 — Без auto calibration | Analytics или outcome не меняют production thresholds автоматически; изменяет только явная owner операция. Проверка: анализ всех settings writers и negative test при ingestion/outcome. |
| AC-04-06 — Активация detector отдельно от уведомлений | Owner включает detectors независимо от Telegram families; отключённый detector не создаёт history, episodes и расчёты, активный продолжает работу при mute. Проверка: persisted selection, live apply/CAS, positive/negative admission и два независимых consumer. |

### E05 — Episodes и freshness

Requirement REQ-E05; текущая трассировка S12/S15, AC IDs сохранены.

Единица продолжающегося движения — episode. Сохраняются начало, новое detector/TF подтверждение, существенное усиление и завершение; однотипные свечные повторы не отдельная история/исследование. Max lifetime/re-arm, market/detection/receipt timestamps и fresh/stale/expired обязательны.

| AC | Критерий и способ проверки |
| --- | --- |
| AC-05-01 — Episode aggregation | Связанные events формируют episode с началом, обновлением, strength и исходными TF/detectors; Spot/Perpetual раздельны. Проверка: interleaved event fixtures и history readback. |
| AC-05-02 — Значимые обновления episode | В одном движении сохраняются начало, новое подтверждение detector/TF, существенное усиление и завершение. Однотипная следующая свеча не создаёт отдельную историю/исследование; Telegram и agent получают значимые обновления. Проверка: последовательные свечи, новые TF/family, усиление, closure/restart. |
| AC-05-03 — Завершение и re-arm | Episode закрывается по согласованным условиям или max lifetime, новое независимое движение создаёт новый episode; restart сохраняет незавершённое. Проверка: time-controlled lifecycle/restart tests. |
| AC-05-04 — Freshness и timestamps | Market/detected/received timestamps не взаимозаменяются; fresh/stale/expired зависят от event type/TF. Expired не выдаётся за новый fresh в Telegram/consumer. Проверка: clock-controlled expiry/replay tests. |

### E06 — History и outcome analytics

Requirement REQ-E06; текущая трассировка S12/S15, AC IDs сохранены.

Для episode один набор 5/15/30/60min outcomes и MFE/MAE от первого immutable observation/reference. Описательная статистика считает уникальные episodes с явными groups/completeness, не вклад каждого detector и не доходность стратегии. Постоянного N20/control-period research в рабочем цикле нет. Export/causal context не подменяет старые решения поздними данными.

| AC | Критерий и способ проверки |
| --- | --- |
| AC-06-01 — История значимых episodes | Значимые события и revisions episode сохраняют ID, instrument/market, detector/version/settings, direction/TF, metrics/timestamps/context независимо от Telegram. Повторные свечные подтверждения не становятся отдельными исследованиями. Проверка: replay/transaction/restart/readback и неизменный episode ID. |
| AC-06-02 — Один набор outcome от начала episode | Для episode один раз создаются горизонты 5/15/30/60 минут от первого наблюдения и immutable reference time/price; новые подтверждения, задержка расчёта и Telegram не меняют точку отсчёта. Owner horizons совместимы с 72 часами. Проверка: много подтверждений, поздний worker, restart, точные границы и уникальность jobs/results. |
| AC-06-03 — Returns и развитие | Outcome показывает price return, движение в сторону/против direction, continuation/reversal; neutral events используют ненаправленные metrics. Неполные данные помечены. Проверка: directed/neutral/gapped series. |
| AC-06-04 — MFE/MAE | MFE/MAE и другие выбранные market metrics воспроизводимо вычисляются на обозначенном диапазоне исходных данных. Проверка: known OHLC/time series и gaps. |
| AC-06-05 — Описательная статистика уникальных episodes | N считает уникальные episodes, а не свечи/подтверждения. Видны последующее движение, continuation/reversal и полнота по versions/settings/markets/TF с явно определённой группировкой; нет постоянных N20/control-period исследований или заявления прибыльности и отдельного вклада каждого detector. Проверка: controlled multi-confirmation cohorts до/после expiry. |
| AC-06-06 — Export и неизменность | Export выбранного периода/detector/version/market содержит историю и outcomes для coding agent; старые результаты/settings не пересчитываются незаметно. Для подробностей период точный; после их expiry доступная UTC-day детализация, фактический охват и ограничения периода должны быть явны и согласованы между форматами. Проверка: filters/pagination/versioned export readback до/после expiry, включая границы внутри дня; policy Q07. |
| AC-06-07 — Нет future leakage | Исторический decision context содержит только данные, доступные на момент события; поздние outcomes/max episode strength доступны analytics отдельно. Проверка: time-travel/revision fixtures. |

### E07 — Telegram alerts, control deck и manual use

Requirement REQ-E07; текущая трассировка S12/S15, AC IDs сохранены.

Telegram функционально обеспечивает alerts, status/settings/watchlist/mutes, history/outcome/analytics/digest/download/help/diagnostics без обязательного запоминания slash-команд. Старые команды/callbacks — проверяемый compatibility contract. Mute/delivery не управляют domain detection/agent. Внешний вид/вёрстка исключены из текущего аудита, а не из исходных возможностей продукта.

| AC | Критерий и способ проверки |
| --- | --- |
| AC-07-01 — Полный realtime alert | При meaningful live event владелец видит compact instrument/market/type/TF/direction/metrics/strength/freshness; optional context помечен. Уточнение владельца 24Oct: ключевые значения движения, пробоя, отношений и интенсивности выделяются жирным; исходные числа/единицы и сохранённый текст повторной отправки не меняются. Проверка: render fixtures, UTF16 entity bounds/length/parse tests и owner Bot API smoke. |
| AC-07-02 — Ссылка на правильный рынок | Spot и Perpetual alerts открывают соответствующий Instrument/Market Type Bybit; открытие ссылки не создаёт ордер. Проверка: URL mapping fixtures и безопасный browser/manual smoke. |
| AC-07-03 — Control deck | Владелец открывает status/settings/list/add/remove/mute/terms через Telegram controls без запоминания команд; no-op settings не вызывает noisy error. Проверка: keyboard/handler fixtures и no-op exception scenario. |
| AC-07-04 — Правдивый active status | Status/settings различают возможности, активные detectors и уведомления, показывают реальные рынки/universe/TF/thresholds и состояние данных. Старое решение не применяется после concurrent revision. Проверка: persisted/readback, отключённый detector, settings race; внешний вид в текущем аудите исключён. |
| AC-07-05 — Управление доставкой | Realtime alerts включаются/выключаются отдельно от digest; mute действует на Telegram delivery и не останавливает history/agent delivery. Проверка: two-consumer isolation/failure tests. |
| AC-07-06 — Просмотр episode outcome | Из episode доступен один последующий outcome от первого immutable reference; после 30 дней явно недоступны отдельные подробности, сводка их не подменяет. Проверка: несколько confirmations→один результат, navigation/expiry; без торговой рекомендации. |
| AC-07-07 — Объяснение manual | Для каждого detector доступно почему он сработал и значения metrics/terms; manual use не требует account credentials или trading agent. Проверка: detector explanation fixtures и disconnected consumer сценарий. |

### E08 — Owner settings и presets

Requirement REQ-E08; текущая трассировка S12/S15, AC IDs сохранены.

Owner domain settings persistent и отделены от secrets/infra (.env) и chat delivery. Markets/universe/active detectors/thresholds/liquidity/TF/enrichment/watchlist/policy/horizons управляются без обычного restart, с validation/CAS и profiles/manual override. Lookback/horizons укладываются в 72h.

| AC | Критерий и способ проверки |
| --- | --- |
| AC-08-01 — Owner configuration | Persisted owner configuration содержит markets/universe, активные detectors, thresholds/liquidity/TF, enrichment/watchlist и policy/horizons; delivery families/chat ID отделены. Проверка: roundtrip, отдельное включение detection/delivery, приватные настройки при reset/restart. |
| AC-08-02 — Изменение без restart | Owner меняет обычные settings, validated revision применяется в runtime без .env editing/restart; invalid/NaN/negative значения не ломают state. Проверка: concurrent update и validation tests. |
| AC-08-03 — Profiles и manual parameters | Несколько готовых sensitivity profiles показывают изменяемые thresholds; owner может отдельно настроить параметры и увидеть фактические активные значения. Проверка: apply/edit/reset profile fixtures. |
| AC-08-04 — Настройки совместимы с 72 часами | Lookback и outcome horizons укладываются в 72 часа; несовместимые значения отклоняются с объяснением без изменения revision/state. Параметры detector windows валидируются во всех путях ввода и миграции. Проверка: 72h boundary, >72h, NaN/negative/oversized и прежние настройки. |

### E09 — Producer и handoff для trading agent

Requirement REQ-E09; текущая трассировка S12/S15, AC IDs сохранены.

Существующий versioned HMAC/idempotency transport сохраняется. Producer передаёт воспроизводимые advisory metrics и meaningful updates, без execution/model side effects. CryptoWatcher не отвечает за реализацию receiver, Paper/risk/strategy/переход Demo/Production. Receiver activation — после стабилизации radar и отдельного решения владельца.

| AC | Критерий и способ проверки |
| --- | --- |
| AC-09-01 — Structured contract | Event/update содержит Event ID/revision, Episode ID, Instrument/Market Type, detector/version/settings/direction/TF, timestamps/freshness и reproducible metrics/units; нет execution fields. Проверка: schema/consumer contract tests. |
| AC-09-02 — Авторизованная доставка значимых updates | Совместимый producer подписывает и автоматически отправляет активные episodes и значимые updates независимо от Telegram mute/outage. Missing/invalid transport configuration не ломает standalone radar. Повтор каждой свечи не является новой доставкой исследования. Проверка: HMAC/ack/fault/retry/restart и coalescing; receiver live acceptance остаётся вне CryptoWatcher по D23. |
| AC-09-03 — Outbox и recovery | Restart/consumer outage сохраняет undelivered и позволяет replay последних актуальных events; retries разрешены, stale/expired помечены и не выдаются за fresh. Проверка: outage/restart/expiry tests. |
| AC-09-04 — Identity/revisions для consumer | Producer сохраняет Event ID, source/transport revisions и неизменные bytes при повторных попытках одной transport revision; новые revisions сохраняют event/episode identity и source history. Проверка: replay/reorder/ack/contract fixtures; consumer обязан реализовать dedup/conflict/history по переданному контракту, его live приемка вне этого проекта. |
| AC-09-05 — Delivery diagnostics | Owner видит pending/delivered/error состояние agent delivery независимо от Telegram. Проверка: diagnostics/readback и fault injection. |
| AC-09-06 — Handoff и causal analytics | Versioned contract и synthetic example согласованы с одной reference point episode, details30d/агрегатами 12 м, auth/retry/freshness/idempotency и receiver checklist. Решение использует только доступный тогда context, поздние outcomes не подставляются. Проверка: artifact/source alignment, producer/causal fixtures; receiver execution/Paper/Demo/Production принимает основной проект. |

### E10 — Persistence, migration и recoverability

Requirement REQ-E10; текущая трассировка S12/S15, AC IDs сохранены.

Owner/private/chat/runtime/history schemas разграничены. Corrupt/unknown state fail closed, private migration/restart/backups проверяются. Плановая retention допустима; неизвестные/чужие данные не перезаписываются и не удаляются.

| AC | Критерий и способ проверки |
| --- | --- |
| AC-10-01 — Разделение хранения | Owner settings, delivery chat state, runtime и history имеют явное владение и совместимые persistent schemas. Проверка: schema/persistence roundtrip и ownership tests. |
| AC-10-02 — Расширяемая история | Новый detector/version/context добавляется без destructive migration старых events/outcomes; previous revisions доступны в пределах срока хранения. Плановая retention не считается потерей данных при update. Проверка: upgrade/compatibility и retention fixtures. |
| AC-10-03 — Безопасная миграция/recovery | Legacy custom/seed/mutes/overrides сохраняются при согласованной owner migration; corrupt/unknown-version state не перезаписывается, backup/restore проверяемы. Проверка: old payloads, empty list, corruption/forward-version и restore tests. |
| AC-10-04 — Restart continuity | Restart восстанавливает settings/watchlist, episodes, subscriptions и delivery/outcome pending work без ручного production state edit. Проверка: crash/restart/duplicate-replay integration tests. |

### E11 — Digest и derivatives context

Requirement REQ-E11; текущая трассировка S12/S15, AC IDs сохранены.

Digest строится из общей episode history, считает уникальные значимые episodes/активность, strongest/ongoing и detectors обоих рынков; interval/toggle независимы от realtime. Optional Spot derivatives OI/changeOI/funding/basis имеют надёжный asset mapping/provenance и не блокируют core radar.

| AC | Критерий и способ проверки |
| --- | --- |
| AC-11-01 — Digest уникальных episodes | Digest общей истории за период показывает число значимых уникальных episodes, наиболее активные инструменты, сильные/продолжающиеся движения и detectors обоих рынков. Повторные свечные подтверждения не увеличивают независимый N. Проверка: fixed-period dataset с несколькими confirmations в одном episode. |
| AC-11-02 — Digest schedule/toggles | Owner меняет interval и включает digest независимо от realtime; restart сохраняет настройки и не дублирует один digest interval. Проверка: controlled scheduler/restart tests. |
| AC-11-03 — Безопасный derivatives enrichment | Надёжно сопоставленный Asset получает доступные OI/change OI/funding/basis; отсутствие perpetual/metric не блокирует Spot. Проверка: mapping/optional failure fixtures. |
| AC-11-04 — Provenance enrichment | Каждый derivatives показатель имеет source/market/units/timestamps, собственный cadence; enrichment не заменяет самостоятельный Perpetual detector. Проверка: schema/freshness и separate market events. |

### E12 — Технические границы, CI/CD и эксплуатация

Requirement REQ-E12; текущая трассировка S12/S15, AC IDs сохранены.

Python/SQLite — исходная основа, логические границы ingestion/domain/persistence/analytics/delivery обязательны; один/два writer или структура модулей новыми требованиями не фиксированы. Docker/GitHub Actions/известный VPS, source/artifact/recovery gates, secrets и воспроизводимая проверка по 29Oct instructions. Health/liveness не равно timely data/24h acceptance.

| AC | Критерий и способ проверки |
| --- | --- |
| AC-12-01 — Docker/VPS runtime | Собираемый Docker artifact запускает сервис с persistent data volume/config; после restart данные сохраняются. Проверка: target-runtime image build/start/restart и readback без production mutation. |
| AC-12-02 — GitHub Actions delivery | Изменения проверяются Actions перед обновлением приложения; CI→merge→CD доставляет идентифицируемый artifact/revision на известный VPS с required gates/post-checks. Проверка: первичные CI/CD records и live version/smoke. |
| AC-12-03 — Domain boundaries и diagnostics | Ingestion/domain/persistence/analytics/delivery логически разделены; diagnostics показывает streams/markets/detectors/universe/gaps/recovery и обе доставки, отличая отсутствие events от отсутствия данных. Проверка: architecture review и degraded-state tests. |
| AC-12-04 — Secrets и проверяемые изменения | Secrets не попадают в tracked artifacts, alerts/logs/context; критичные auth/migration/data paths имеют воспроизводимые regression checks и dependency/config review. Проверка: negative leakage fixtures, regression suite и security inventory. |
| AC-12-05 — Полный профиль проверки без deployment | Существует canonical полный поддерживаемый применимый профиль для exact main SHA без запуска CD, с inventory/prerequisites и итогами suites/skips, включая Linux/native/recovery и применимые image/qualification gates. Его запуск — отдельное поручение, не обязательная часть каждого аудита. Проверка: scripts/README/workflow review и первичные records при отдельном запуске; инструкция 29Oct. |

### E13 — Компактное хранение, сроки и новый период истории

Requirement REQ-E13; текущая трассировка S12/S15, AC IDs сохранены.

Details значимых episodes/updates/outcomes30d; general candles72h, дальше только использованные inputs доступных details. Описательные агрегаты 12 месяцев без всех raw arrays; отдельного 90–180d context archive нет. Queues/leases имеют finite wait и terminal result. Working DB/index/freepages/WAL/SHM/journal≤35,000,000,000B — потолок; backup/temp/physical reserve отдельно. Экономия не ухудшает общий VPS. Новый переход discarded старой тестовой history/queues без архива/полной conversion сохраняет private/infra и не затрагивает TA/Paper.

| AC | Критерий и способ проверки |
| --- | --- |
| AC-13-01 — Общие данные без полных дублей | При повторной обработке и пересекающихся source windows актуальные heads ссылаются на immutable revisions; общие candles/settings/context не копируются полностью в каждый event/outcome. Формулы, результаты, provenance, все events/markets/detectors/TF сохраняются. Проверка: equality с исходными аналитическими fixtures, replay/restart и измерение bytes/event с outcomes/services. |
| AC-13-02 — Bounded retention и рабочее окно | Подробности значимых episodes/updates/outcomes30d, общие свечи — скользящие 72h. Дольше остаются только действительно использованные inputs доступных подробностей; автоматическая bounded cleanup сначала сохраняет агрегаты, затем освобождает зависимости. Проверка: controlled-clock 72h/30d, перекрывающиеся ссылки, restart, jobs и GC без полного архива рынка. |
| AC-13-03 — Описательные агрегаты 12 месяцев | 12 календарных месяцев сохраняются описательные результаты по уникальным episodes с N/completeness/methodology/version/settings/market/TF. Старые подробности и все исходные массивы не обязательны; ограничения UTC-day granularity/периода одинаковы в export и Telegram. Никакого скрытого пересчёта по новым settings. Проверка: cohorts/expiry/period/export с multi-confirmation episodes. |
| AC-13-04 — Только использованный context | Отдельный 90–180d архив укрупнённого market context не нужен. Фактически использованный context сохраняет provenance в пределах 30d details, далее доступны описательные агрегаты без всех исходных массивов. Optional30m/1h context не превращается в обязательный источник/alert. Проверка: input ownership, TTL/GC и отсутствие неиспользуемого общего архива. |
| AC-13-05 — Конечные служебные зависимости | Completed jobs/queues сокращаются до необходимого состояния; retry/restart/dedup сохранены. Pending расчет/доставка при истечении допустимого ожидания получает явный terminal status и освобождает зависимости; service cursors не удерживают архив бессрочно. Проверка: fault/retry/restart/expiry и replay fixtures. |
| AC-13-06 — Рабочий набор≤35GB и устойчивость общего VPS | Diagnostics bounded показывает категории/indexes/journals, net growth и единую объяснимую оценку фактически включённых механизмов. Рабочая DB+indexes+free pages+WAL/SHM/journal≤35,000,000,000bytes — потолок, не цель; backups/temp и физический резерв отдельно. Экономия не создаёт растущие просроченные расчёты, stale market data/Telegram и ущерб соседям. Проверка: воспроизводимый полный путь и CPU/RAM/I/O/peak-space, затем 30min/24h на неизменной версии/settings; future horizons не backlog. |
| AC-13-07 — Разовый переход с тестовой истории | Отдельная exact-target процедура начинает новую историю облегчённой версии, удаляя старую тестовую market history CW и research queues без архива/конвертации. Owner settings/watchlist/credentials/private/infra сохраняются; TA/Paper не затрагиваются. Обычный restart/deploy не сбрасывает историю. Проверка: private equality/restore, неверный target, generation/restart/no-old-alerts и bounded backfill; во время аудита reset запрещён. |

## История источников S01–S11 и решений D01–D08

Исторические привязки сохранены; приоритет у S12/D29.

| ID | Источник | Историческое основание |
| --- | --- | --- |
| S01 | Согласованный Google Doc «Требования к проекту CryptoWatcher», file ID `1wNt8hpuZTrvMLmSbfTU5H4hQTzngNDQW3DG3Lm7APyE`, modified 2026-10-16 08:48:31.536 UTC; полностью прочитаны 170 paragraphs единственной вкладки t.0 2026-10-17; native revision AHj4eMRw_XfnmGfEiPc-g2cZt8XKzpQmka1tOt14e2Y0FytXypDSNNiUF5qWYpO2CeaAAOes_O9xLrDPguxGW4WgfUarojYn4NdO_coDyx8 | Актуальный intent всего проекта; заголовки источника сохранены в трассировке эпиков |
| S02 | Запрос владельца AUDIT от 2026-10-10 и приложенный комплект AGENTS.md / ci-cd-rules.md | Scope audit PR: только canonical spec/plan; правила AC/Evidence/Goal/Git/delivery; pipeline не меняется аудитом |
| S03 | Предыдущий product scope, [state_contract.md](state_contract.md), README и код на `8ace3638b9193444948d0beb88e0c49e9f2f068e` | Legacy behavior и compatibility evidence; не отменяют S01 |
| S05 | Явное поручение владельца 2026-10-16: исправить новое накопление, bounded retention и контроль 20 GB, затем разово сбросить старую тестовую рыночную историю | Отменяет сохранение/полную конвертацию этой истории; сохраняет owner config, credentials, инфраструктуру, все markets/TF/detectors и аналитику |
| S06 | Явное решение владельца 16Oct2026: CryptoWatcher готовится для alerts под ручную торговлю и handoff; подключение — ответственность trading agent проекта | Меняет project boundary/DoD E09: producer readiness/contract handoff здесь; credentials/binding/receiver activation/live acceptance снаружи |
| S07 | Явное поручение владельца 16Oct2026: реализовать исправления роста и поставить через CD, сохранив полезную history/analytics/coverage/retention и 900s backup/CD | Scope GROW/D24; source epoch не сбрасывается; показать measured post-report относительно 20GB |
| S08 | Поручение владельца 16Oct2026 после handoff: сначала реальные источники/cardinality/revisions/comparators/services, затем подтвержденные fixes; controlled retention и автоматический 24h замер после стабилизации | Уточнение evidence AC-13, без нового reset, сокращения scope/точности/retention или равенства CI/CD и capacity PASS |
| S04 | Уточнение владельца 2026-10-10: бот работает на VPS Contabo; Telegram owner — владелец | Deployment context; assertion владельца не доказывает deployed revision или gates |
| S09 | Уточнение владельца 23Oct2026: устойчивый CryptoWatcher на общем Contabo VPS важнее отдельной экономии БД; измерить ресурсы всего host/вклад проекта и normal/recovery/retention/compression/repack, определить бюджет с запасом | Расширяет evidence/DoD текущей CAPACITY25: общая CPU/RAM/I/O нагрузка, DB+sidecars:25GB desired/30GB hard limit по уточнению 24Oct, freshness, только просроченные расчёты и Telegram; сохранить функционал/точность/retention, другие проекты и ресурсы VPS не менять; после стабилизации неизменной версии 30min/24h, при объективной нехватке представить измерения/варианты |
| S10 | Уточнение владельца 24Oct2026 в текущем чате: «25 желаемо,30 приемлемо»,30GB как запас | Для той же Goal CAPACITY25 желаемый объём 25,000,000,000bytes; допустимый жёсткий предел 30,000,000,000bytes включает DB/indexes/free pages/WAL/SHM/journal. Предварительное допущение 25.34GB заменено. Функционал/retention/точность/полнота, CPU/RAM/I/O и стабильность/24h gates сохранены |
| S11 | Ответ владельца 24Oct2026 в текущем чате: «Да, увеличить только длительность проверки» | Только конечная полная Linux/native qualification:450CPU seconds/510wall seconds/600outer seconds вместо 180/210/270. Один CPU/4GiB/no swap/no network/без production mounts, полный dual-fixture oracle, preflight резервов и стабильность/backlog/24h обязательны; RAM/VPS allocation и 900s ordinary delivery gate не изменены |

- D01: Spot Top 100 / один выбранный TF / один composite — legacy baseline, а не весь согласованный результат. Fixed Top 100 заменён dynamic eligible universe обоих рынков.
- D02: «futures вне MVP» заменено поддержкой USDT Perpetual. Это не разрешает execution/account integration.
- D03: per-chat market domain заменяется persisted owner configuration; chat ID/preferences принадлежат только delivery. Migration не должна молча потерять legacy list/mutes/overrides.
- D04: baseline-driven monitor и CMC не возвращаются в основной путь. Существующие CMC/reset handlers исследуются перед deprecation; совместимость state отделяется от сохранения устаревшего UX.
- D05: история и outcome analytics развиваются параллельно интеграции. Накопление статистики и калибровка не блокируют базовую доставку events.
- D06: enrichment Spot derivatives — после стабилизации radar обоих рынков. 30m/1h context и stablecoin exclusions допустимы; обязательных новых realtime alerts/фиксированных exclusions источник не задаёт.
- D07: исходные implementation unknowns сохранены как история решения; D09–D22 задают согласованные database/transport/Environment/detector/episode/TTL defaults. Непорученные SLO не вводятся; измеряемая цель storage capacity определена D22.
- D08: история прежних требований сохранена в Git. Старых стабильных AC IDs не было; новые AC имеют IDs `AC-<epic>-<number>`. Связи отменённых контрактов записаны в D01–D04, остальные полезные правила миграции/closed candles/UX сохранены.

## История решений D09–D28 — не текущий scope

Сохранена для восстановления оснований и старых Evidence. При несовместимости действует D29/S12. Старые слова «текущая Goal»,ACTIVE,25/30GB,N20,35d,90/180d,full conversion и прежние разрешения ниже описывают прошлую работу, не инструкции продолжать её. Последние действующие runtime процедуры остаются as-built до новой реализации; расхождение учтено F55.

### Решения по исходным вопросам и зависимости

| ID | Действующее правило | Источник / эксплуатационная зависимость |
| --- | --- | --- |
| Q01 | Формулы, profiles, episodes и freshness заданы D11–D15; эффективность defaults оценивается описательной статистикой, без обещания прибыльности. | Согласованный план 10Oct; изменение production thresholds — явная owner операция. |
| Q02 | Отдельный claude-bybit-trading-agent принимает backward-compatible v1/v2 по D18; полный settings snapshot/hash сохраняется, включая известные retention metadata. | Producer contract/controlled tests/handoff проверяются здесь; endpoint/binding/current consumer revision/owner credential и signed live delivery — в задаче принимающего проекта. Отсутствие credential не блокирует standalone radar/handoff. |
| Q03 | JSON import относится только к private owner233382575; прочие chats архивируются без объединения. Явный пустой список сохраняется. | D11; actual migration/recovery Evidence находится в плане. Новый reset по D22 не повторяет JSON import. |
| Q04 | Horizons, neutral outcomes, no-signal comparator и export определены D16; сроки хранения изменены D22. | Прежние raw90days/indefinite details отменены; expiry не меняет записанные методики/settings/results. |
| Q05 | Target — Contabo167.86.68.98, /opt/crypto_watcher, Compose project crypto_watcher/service crypto_watcher, один production Environment. | Точный runtime, approvals, artifact/backup/recovery и post-checks — в действующей процедуре delivery и Evidence плана. |
| Q06 | CMC/reset callbacks deprecated с понятным ответом; startup wrapper и legacy migration compatibility сохраняются. | D04; неподтвержденные внешние consumers private helpers не дают основания для нового удаления. F24 в плане. |
| Q07 | При запросе части UTC-дня после expiry details точная intraday выборка из day summaries недоступна. Нужна единая явная policy CSV/NDJSON/cohorts. | Выбрано D27: явно нормализовать архивную часть периода до UTC-дней с requested/effective bounds и фактическим охватом. Это уточнение исходного period/limitations requirement, а не разрешение хранить полные старые events или менять точность numeric facts. F48; решение при выбранной implementation Goal. |

Текущий согласованный scope содержит **65 AC**: исходные 58 сохранены, E13 добавляет 7 критериев обновленного S01/S05. Статусы и аудиторские оценки находятся только в плане. Новые business rules, сокращение analytics/retention и изменение формул требуют решения владельца; конкретизация проверок понятного требования scope не меняет.

### Утверждённые implementation решения 2026-10-10

Основание: явное поручение владельца реализовать согласованный полный план. Действующие AC IDs сохранены; findings сами по себе новых требований не создают.

- D09: пять семейств detectors; composite impulse развивает существующий composite, отдельного шестого detector нет. Финансовые tokenized assets/TradFi исключены; обычные crypto tokens сектора RWA допустимы по metadata/liquidity.
- D10: Python3.11, один modular deployable; SQLite WAL с одним writer/короткими транзакциями. Telegram SDK20.8 сохраняется. REST/agent async HTTP, public WS websockets; precise dependencies/hash lock.
- D11: owner user233382575, private-chat access fail-closed; bot7523865123. Owner config revision/snapshot отделён от chat delivery/runtime. JSON migration сохраняет original, явный empty list, owner overrides/mutes; чужие chats архивируются, unknown/corrupt schema не перезаписывается. Saved values выше env defaults.
- D12: SUPERSEDED D22 от 2026-10-16. Прежние raw90days и бессрочное хранение details/inputs отменены; исторические Evidence не подтверждают новую policy.
- D13: defaults universe liquidity1M USDT при отсутствии saved value; metadata10min/liquidity5min; native1m/3m/5m/15m concurrently; heartbeat20s; REST5req/s/concurrency4; reconnect1–60s+jitter; bootstrap256 closed candles. Bybit shared-IP budget учитывает trading agent.
- D14: normal price thresholds1/2/2.5/4% для 1/3/5/15m; turnover/latest mean previous12 ≥3; breakout previous20 high/low +0.1 previous ATR14; volatility TR/mean previous20TR ≥2; composite price+turnover+liquidity сохраняет legacy formula/equality. ATR14 — simple mean предыдущих 14TR; current bar исключён из baseline. Insufficient/zero/gapped windows не считаются signal. Turnover/volatility direction NEUTRAL; price/composite по знаку, breakout по стороне. Conservative thresholds×1.5; sensitive×0.75; watchlist отдельный×0.75. Migrated custom values не заменяются preset автоматически.
- D15: episodes instrument/market/direction, new detector family/TF confirmation либо strength×1.5 разрешают meaningful notification; coalesce2s. Inactivity2×longest episodeTF, maxlife60min; re-arm detector/TF после 2 complete non-signal bars. Suppressed observations сохранены. Fresh price/spike/volatility/composite1TF, expire2TF; breakout fresh2TF/expire4TF; receipt/retry/outcome исходный event не освежают.
- D16: immutable outcome reference close/time; horizons5/15/30/60min, complete subsequent1m window; returns/MFE/MAE, neutral без directional label. Continuation/reversal noise=max(2ticks/reference×100,0.1ATR14/reference×100), snapshot versioned. Comparators sameinstrument/market/TF, previous28days matched UTC hour/ATR% quartile, до 20 closest nonoverlapping windows, no signal по frozen detector/settings, complete outcome заканчивается до event observation. Insufficient N/completeness явны; statistics не являются Strategy profitability. NDJSON versioned streaming export и CSV aggregates; no silent recomputation/future leakage.
- D17: Telegram alerts/digest независимы, digest60min initiallyOFF. Mute меняет Telegram delivery, history/agent продолжаются. New detectors могут работать silent до owner enable; actual settings/capabilities видны. BotAPI ambiguity не позволяет обещать exactly-once messages.
- D18: contract receiver POST /integrations/cryptowatcher/events; activation после его проверки принадлежит принимающему проекту по D23. Backward-compatible v2 с settings/profile/provenance/freshness/episode/completeness/enrichment и explicit live/historical update kind; v1 поддерживается. HMAC-SHA256 raw immutable body, receiver limit65536bytes, outbox retry1–300s, expiry и cutoff independent of receipt. No execution fields; receiver не запускает model/trade. Full used inputs доступны producer-side для export в пределах сроков хранения. Primary source prepared counterpart branch43c7e88f; counterpart main40a3147172 на момент проверки ещё не содержит receiver.
- D19: optional asset-matched Spot derivatives context после core; ticker1min/OI5min cadence, explicit source/units/timestamps, missing/stale data nonblocking. Separate Perpetual detection сохраняется.
- D19a: enrichment default OFF, strict persisted owner bool; exact eligible Bybit base identity и один USDT Perpetual match, zero/multiple/alias/scaled candidates не угадываются. OI linear BASE/asset (both sides),5min change по двум точкам ровно 5min apart, нулевая baseline без pct; funding raw fraction×100%, next funding UTCms. Basis100×(perp_last/spot_last−1)% по двум доступным REST snapshots≤1min с timestamps/skew≤1min; не simultaneous trade prices. Per-metric source age≤cadence FRESH, затем STALE; missing/future/invalid явно помечены. Общий budget/current recovery приоритетны; optional failures не меняют core signals. Event context frozen по availability≤observation cutoff, latest owner view отдельно.
- D16a: explicit owner recompare может добавить comparator revision на новых prior inputs/availability, сохраняя original event/settings/reference и прежние results. Completed comparisons не меняются без действия владельца; methodological change получает новую version. Profile/version/settings comparisons используют реально записанные outcomes с N/limitations; automatic threshold writes отсутствуют.
- D20: Contabo167.86.68.98 SSHroot:22/MobaXterm; unit/path известны по inventory в delivery runbook; complete image source revision/backup fitness требуют evidence. First migration отдельный safety gate; затем automatic CI→merge→CD. Production Environment один; artifact sourceSHA/digest immutable, exact target lock/eligibility/recovery/postchecks. Решения supersede соответствующие UNSET в D07/Q01–Q05; operational факты продолжают требовать evidence.
- D21, explicit owner decision2026-10-11: initial consumer context maximum age900s от market observation time, дополнительно к более короткой type/TF-specific producer freshness и episode state; receipt/retry не продлевают возраст. Older records/outcomes остаются в history, но не считаются fresh context. Cloudflare Zero Trust/Access не добавляется в текущую поставку; сначала установить причину 403 и проверить existing HTTPS transport с фактического VPS. Существующие TLS/HMAC/Cloudflare rules сохраняются. Затронуты 05-04/06-07/09-01/09-03, denominator и Strategy behavior не меняются. Runtime activation/readback ещё требуется.

#### Уточнение проверки outcome/comparator/export v1 (G3)

Декомпозиция D16, без нового scope/AC: numeric targets требуют полного subsequent1m окна и неизменных reference time/close; equality у ±noise относится к neutral movement. Недостающий ATR/tick context помечается UNCLASSIFIED. Actual prior ATR%-population задаёт nearest-rank empirical quartiles, равенство boundary относится к нижнему bin. Reference windows выбираются от наиболее недавнего подходящего времени, до 20 неперекрывающихся; outcome end строго раньше event observation. Composite использует доступный тогда ticker, не будущую liquidity; отсутствие нужного prior context уменьшает N и даёт limitation. Frozen completed comparisons не меняются при последующей source history; другой метод получает отдельную version.

Export filter относится к selected event observation time `[start,end)`, detector/version/market. Все revisions выбранных events, related episode revisions и отдельные late outcomes/settings/provenance доступны с availability timestamps; related episodes могут содержать ссылки на события вне filter. Для исторического decision coding agent использует explicit cutoff/revision readout; final max episode strength/outcomes не включаются туда автоматически. Read-only consistent export заканчивается completion counts. CSV/analytics grouping отделяет frozen settings revision, detector/version и outcome methodology; actual N/noise/partial data и зависимости samples явно видны.

#### D22 — Новое накопление и bounded retention (2026-10-16)

S01 обновлен и S05 явно заменяют D12: подробные events/revisions/episodes/outcomes30d; raw small-TF35d плюс только использованные inputs еще сохраняемых подробных расчетов. Сроки настраиваются, lookback/horizons должны укладываться в поддерживаемые окна. Quality summaries12 календарных месяцев от event date, без полных raw arrays; старые подробности не подменяются агрегатами. Используемый coarse context90d/selected Spot до 180d условен и не требует новых источников или минутной детализации 180d. После сохранения агрегатов automatic bounded cleanup освобождает ссылки и service dependencies. Ожидание не бесконечно: истечение подробного срока дает явный terminal incomplete/expired status.

Working DB+indexes+journals:25GB — желаемый объём,30GB — допустимый жёсткий предел по уточнению D27 от 24Oct. Факты и extrapolation разделены; причина превышения и варианты решения видны владельцу. Постоянный full scan не является механизмом контроля. Не сокращать события/detectors/markets/TF/аналитику/retention скрытно.

Прежняя тестовая market history подлежит разовому reset после исправлений/tests; ее перенос, сжатие, полная конвертация и qualification исключены из текущей задачи. Owner settings/watchlist/thresholds/notification preferences/mutes/user config и secrets/infra сохраняются и проверяются. Reset имеет exact перечень, generation boundary и совместимые queues/cursors; не выполняется при обычном startup/deploy. Удаление backups/experiments не разрешено этим reset. Обычные backup/CD остаются<=900s, RAM не увеличивается; последующее наблюдение отделено от закрытия исправлений/reset.

### D23 — Разделение проектов и handoff (S06, 2026-10-16)

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
этого проекта. D18 transport/authority/freshness semantics и согласованные 900s
consumer cap сохраняются как контракт принимающей стороны.

Для CW DoD достаточно validated producer/contract/handoff и проверенного manual
runtime; доступность/активация внешнего приложения не блокирует его поставку.
Новая оценка процентов этим UPDATE не проводится; исторический audit snapshot
остается на своей revision и прежних границах.

### D24 — Lossless storage growth correction (GROW, 2026-10-16)

По явному поручению владельца исправляется layout/упаковка, а не продуктовый scope.
Physical user_version остается 2; storage extension использует typed record encoding3,
нормализованные внутренние row IDs для expiry и общие cohort/provenance dictionaries.
Public event/revision IDs, JSON types/float bits, формулы и source links сохраняются.
Old CW2/J/Z records и legacy index/quality tables остаются читаемыми в смешанной базе.

После обычной полной source/backup validation writer устанавливает небольшие атомарные
DDL extensions без копирования market history. Старые rows не пропадают из compatibility
views. Bounded background repack переносит по одной проверенной записи с durable cursor
в той же транзакции; restart продолжает сохраненный прогресс. Нет полного VACUUM,
второй рабочей БД, reset или повышения 15min backup/CD budgets/RAM.
Recovery проверяет arrived closed data перед REST, объединяет overlapping requests и
retire satisfied/obsolete work; реальные gaps/conflicts сохраняют retry и диагностику.
Годовые facts с другим provenance получают отдельный chunk, не metadata первого метода.

Forecast различает retained inserts, net queue occupancy и эффект repack. Quality
сценарии измеряют текущий codec/SQLite layout на isolated synthetic данных, отдельно
показывают cohort density/comparator count; это не production measurement или upper bound.
Проверяемый 20GB target сохраняется, превышение/неизвестность не скрываются. Сроки D22,
полнота аналитики и охват не сокращаются. Forward-fix с reader обоих formats; старый
binary без encoding3/view support не является допустимым rollback после новых writes.

### D25 — Capacity evidence после GROW (S08, 2026-10-16)

20GB относятся к working DB с её indexes/free pages и рабочими journals. Logical
payload/key sizes и физические таблицы/indexes показываются раздельно; indexes
внутри DB не прибавляются повторно. Backups/experiments — отдельный inventory.
Старый point forecast разбирается по исходным формулам, а allocation текущего mix
не называется измеренным per-category daily flow. Actual quality facts отличаются
от prospective frozen cohorts и synthetic сценариев comparator density.

Проверка retention использует управляемое время, finite dependencies, повторные
запуски/restart и сохранение точных numeric/group/method/settings/comparator results.
После bootstrap/repack организуется конечный 24h замер counters/files/cleanup и
boundary ledger/indexes; без ожидания агента/Actions и без частого полного scan.
Короткое post-restart окно не объявляется устойчивым суточным темпом. Наблюдение
имеет явные completion/partial/failure states; tests/CD PASS не означают capacity PASS.
Новые codec/batching выбираются после измерения ожидаемого эффекта. Сокращение
retention, coverage, точности или возможностей аналитики требует решения владельца.

Уточнение по поручению владельца 17Oct2026: backfill/repack не должны зависеть от
завершения долгого analytics cycle; обработка ограничена, уступает ingestion и
Telegram, сохраняет retry/cursor/restart и исходные данные. Repack завершается на
зафиксированной границе накопленного; новые compact writes не продлевают его
бесконечно. После фактического завершения bootstrap/backfill/repack automatic finite
observer начинает 24h только на стабильной revision/config/generation. RAM и обычные
backup/CD<=15min сохраняются. Полный 24h итог отделен от завершения реализации.

### D26 — Observer diagnostics и чистое накопление для замера (2026-10-17)

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
создаются. Запуск нового 24h подтверждается фактическим OBSERVING/baseline;
ожидание завершения суток не входит в исправление и поставку.

### D27 — Единая CAPACITY25 Goal (18 октября 2026)

Владелец утвердил и поручил полную реализацию backlog/observer/lossless storage,
прежних findings, ordinary delivery и чистого конечного 24h замера. Жёсткий предел
Первоначальный предел AC-13-06 составлял 25,000,000,000bytes. Уточнение владельца 24Oct заменяет его:25,000,000,000bytes — желаемый объём,30,000,000,000bytes — допустимый жёсткий предел для DB/indexes/free pages/WAL/SHM/journal;
исторические 20GB оценки остаются snapshots прежнего решения. Backups/experiments
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

### D28 — Длительность конечной Linux/native проверки (24 октября 2026)

По S11 владелец разрешил увеличить только длительность полной конечной проверки:
450CPU seconds,510wall seconds,600outer seconds. Сохраняются один CPU/4GiB/no swap,
изолированные public/default fixtures без сети и production mounts, полный исходный
point/scan/CHECK/FK/private/hash/fresh-reopen oracle и preflight резервов общего VPS.
Результаты прежних 180CPU failures сохраняются. Это не разрешение повысить ресурсы VPS,
ослабить проверки данных, заменить исправление backlog или чистое суточное наблюдение.
900s ordinary delivery gate и 25GB desired/30GB hard working limit неизменны.
