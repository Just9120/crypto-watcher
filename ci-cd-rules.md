# Настройка и исправление CI/CD

Читай этот документ при первоначальной настройке CI/CD, изменении pipeline и исправлении его проблем. Он задаёт требования к workflows, triggers, checks, runners, permissions, artifacts, CD и связанным настройкам. Повседневный flow агента и Goal определяет корневой [AGENTS.md](AGENTS.md); команды и процедуры проекта находятся по его routing.

Адаптируй правила к фактическому стеку и согласованному scope. Перечень возможностей не требует внедрять все инструменты. Workflows/scripts/runbooks можно исправлять внутри разрешённой Goal; изменение общей политики, модели доступа и safety exceptions требует соответствующего решения пользователя.

## 1. Границы CI, CD и окружений

- Базовый flow: CI → merge → CD на VPS. CI подтверждает готовность актуальной revision PR к merge через обязательные checks/tests. После каждого merge, требующего deployment, CD доставляет нужную merge revision/artifact и подтверждает работающую версию обязательными post-checks. Настроенные review/protections также обязательны.
- Разделяй проверку кода и изменение окружения по jobs, triggers, credentials и permissions. Отдельные workflow-файлы необязательны. Проверка PR не должна незаметно запускать production deploy.
- Различай runtime environment, GitHub Environment, вариант продукта и операцию delivery. Для каждого GitHub Environment установи назначение, jobs, targets, configuration и access/protection rules; укажи связь с runtime environment, если она есть. Не вводи обязательный набор dev/staging/production. Вариант продукта требует отдельного окружения только при самостоятельной цели deployment.
- Для каждого изменения определи применимость deployment. Изменение документации без влияния на runtime не требует искусственного CD. Неизвестный target или отсутствующий доступ не означают N/A.

## 2. Validation и merge gates

Минимальный CI входит в первую implementation Goal нового проекта: настрой его в первом PR с кодом, до merge обеспечь успешные применимые build/static checks и необходимые tests реализованного поведения. Развивай проверки вместе с функциональностью. Отсутствие pipeline не разрешает merge первого PR с кодом без CI. Аудит/bootstrap сами по себе не поручают его настройку; CD на VPS внедряй в согласованном delivery scope.

Используй Validation Plan проекта и canonical scripts без второй копии команд/AC. Основной прогон перед merge выполняет CI; минимальный локальный набор определяется AGENTS.md и явными gates проекта. Сначала используй существующие tests; новые нужны для существенного непокрытого поведения, важных рисков и содержательных регрессий. Один сценарий может подтверждать несколько AC; test на каждый AC/метод и обязательный TDD не требуются. Выбирай unit/integration/E2E по затронутым требованиям и риску. Coverage выявляет gaps, но не доказывает AC; CI обнаруживает только проверяемые нарушения.

Обычный PR проверяет затронутые компоненты и зависимости: применимые статические проверки, build и релевантные поведенческие tests, плюс критичное ядро, если оно задано проектом. Выбирай проверки по назначению, риску и стоимости, а не только по метке `non-PG`, `postgres` или `E2E`. Простое изменение текста требует применимой проверки документации, а не искусственной E2E suite.

Дополнительные некритичные tests и расширенную проверку бизнес-контура через integration/API/browser tests можно отложить до отдельного этапа по необходимости, локально либо в Actions. Не вводи обязательные nightly или полную suite на каждом этапе CI/CD. Проверки, необходимые для текущих AC, безопасного merge и существенных рисков (права доступа, деньги, миграции, целостность данных), не откладывай. Сохраняй действующие обязательные tests/gates; отключение suite требует проектного исключения по разделу 9.

Для affected selection начни с понятных групп по компонентам и зависимостям; усложняй selector только при измеримой пользе. Учитывай общие config/lockfiles, migrations, test infrastructure и динамические связи. Неизвестный impact, ошибка selector или неполный diff требуют более широкого подходящего набора; пустая выборка должна иметь объяснимое основание.

Настрой точную проверяемую revision, изолированный workspace и воспроизводимую установку dependencies с lockfiles, когда применимо. Required command должна завершаться ошибкой при failure; test discovery должен находить ожидаемые tests. Silent fallback, безусловный success и `continue-on-error` не должны скрывать обязательную ошибку.

Required checks и их источник устанавливай по фактическим rulesets/branch protection. Уточни revision model: PR head, test merge commit или merge group. После изменения revision требуется соответствующая validation; reuse после merge допустим только по условиям раздела 3. При merge queue настрой `merge_group` trigger. Self-review не заменяет обязательное approval, а зелёный YAML job не доказывает выполнение repository gate.

Required workflow не должен зависать в ожидании из-за path/branch filters. Для выборочных jobs настрой итоговый required gate, который запускается после dependencies и проверяет результат каждого применимого required job, в том числе при failures/cancellations зависимостей.

GitHub допускает успешное завершение отдельных checks со статусом `skipped` или `neutral`. По этому контракту skip допустим при заранее определённой неприменимости либо подтверждённом reuse по разделу 3. Reuse оформляй отдельным основанием с исходным run, не как неприменимость самих tests. Ошибочный filter, dependency failure или недоступность сервиса не являются N/A. Разбирай `failure`, `cancelled`, `timed_out`, отсутствие результата и неожиданный `action_required`, сохраняя обязательные gates.

## 3. Время CI и GitHub Actions minutes

Избегай дублирующих запусков одной suite по `push` и `pull_request` без отдельной цели. Используй безопасные caches и обоснованную параллельность; не дроби быстрые команды на отдельные jobs без пользы. Отменяй устаревшие CI runs в пределах того же PR/workflow, сохраняя stateful операции и независимые проверки. Timeouts задавай по реальным операциям и поведению зависаний.

Определи selection/reuse до дорогого setup. Поднимай PostgreSQL, браузеры и другие сервисы и устанавливай специальные dependencies только в jobs, которым они нужны для оставшихся checks/build. Пропуск test step после безусловного setup не устраняет его стоимость.

После merge допускается reuse успешной PR validation вместо повторной suite, если автоматически подтверждены merged PR, связь head/base/tested revision с текущим main, совпадение проверенного Git tree и влияющих inputs: dependencies, test/workflow/config и environment. Проверь trusted repository/workflow и результаты всех required jobs последнего релевантного run/attempt для этой PR revision. Старый PASS не перекрывает более новый неуспешный, отменённый или незавершённый релевантный запуск.

Неизвестные или изменившиеся inputs, недоступные records либо неподтверждённые условия reuse требуют выполнения необходимых checks; не подменяй их success. Для текущей revision сохраняй проверяемое решение reuse и ID исходного run. Reuse tests не отменяет проверку нового artifact и применимый CD; cache не заменяет эту процедуру.

Измеряй длительность CI и суммарный runner time отдельно от billing. Сравнивай сопоставимые изменения до/после оптимизации: выбранные tests, причины fallback, setup/build и повторы. Параллельность сама по себе не означает экономии minutes. Проектные бюджеты и цели устанавливай по измерениям; универсальный лимит времени или coverage target не вводи. Стоимость не разрешает ослаблять обязательные проверки.

Длительный monitoring/observation и speculative reruns на GitHub-hosted Actions требуют отдельного owner approval и предварительной проверки остатка included minutes. Неизвестный остаток сообщи. Ограниченные обязательные post-checks и обоснованный retry подтверждённой transient ошибки отличаются от наблюдения без конкретной проверяемой причины.

Обычный CI проверяет repository. Auto-fix commits/push и self-modifying workflows допускаются только как отдельно согласованная узкая automation с trusted trigger, минимальными permissions, allowlist изменений и защитой от циклов.

## 4. Trust boundaries и supply chain

Задавай `permissions` явно: read-only/none по умолчанию, повышение только для нужного job/target. Используй short-lived credentials/OIDC, когда доступны, с ограниченной identity. Reusable workflows передавай только необходимые inputs/secrets.

Untrusted PR/fork code не должен получать production secrets, write-capable token, privileged persistent runner или доступ к внутренним ресурсам. Для PR используй подходящую изоляцию; deploy runners отделяй от общего PR execution. На self-hosted runners учитывай очистку и возможность сохранения чужого состояния.

`pull_request_target` и privileged `workflow_run` не должны исполнять untrusted PR code или без проверки принимать его artifacts. PR metadata обрабатывай как данные. Не вставляй untrusted expressions в shell source; безопасно передавай аргументы/environment variables и проверяй формат.

Внешние Actions/reusable workflows фиксируй полным commit SHA из проверенного repository. Проверяй источник и требуемые полномочия зависимости, lock integrity, registries и install scripts. Vulnerability/license scans, SBOM, signing и attestations применяй по требованиям и риску, без автоматического внедрения всей инфраструктуры.

Cache — оптимизация, а не доверенный artifact. Учитывай OS/runtime/lockfile и trust context в ключах; privileged job не должен потреблять cache, который может отравить untrusted job. Не помещай secrets в repository, artifacts/cache/logs. При изменении runner, trigger или credential model перепроверь trust boundaries.

## 5. Build и identity поставки

Свяжи image/package/archive с source revision, build run и immutable digest/version. `latest` или branch tag без неизменяемой identity недостаточны. Для передачи между workflows проверяй repository, источник и provenance artifact по модели проекта.

Production artifact должен пройти required validation в допустимом trust context. Успешный untrusted PR run сам по себе не делает artifact доверенным. Для release build после merge свяжи фактический merge SHA, применимую validation и созданный artifact. CD не должен зависеть от существования удаляемой feature/fix branch.

По возможности строй один раз и продвигай тот же artifact. Если модель проекта требует пересборки, зафиксируй её, обеспечь точную source revision, воспроизводимость inputs и проверку нового artifact; не выдавай его за ранее проверенный бинарный результат. Publishing — явный разрешённый этап, а не побочный эффект tests.

## 6. CD на VPS и recovery

Проверь trusted trigger, expected repository/ref, exact revision/artifact, target host/account, directory, service/deployment unit и config/credential owners. Настрой обязательные preconditions, environment protections, allowed branches/tags, approvals и stop criteria. Не выбирай неизвестный target по догадке и не обходи protections.

Сериализуй поставки в один target. Перед изменением окружения повторно проверяй допустимость candidate по release policy: отложенный job не должен затереть более новую поставленную версию. Одна очередь не гарантирует правильный порядок версий. Намеренный rollback выполняй по recovery procedure. Не отменяй migration/deploy, если это может оставить неконсистентный state; retry требует известной idempotency или безопасной точки продолжения.

До переключения версии проверь config и stateful preconditions, изменяй только intended deployment unit. Успех CD должен зависеть от проверки фактически запущенной версии, health/readiness и необходимых прикладных smoke checks. Статус процесса или доступный endpoint не доказывают успешный бизнес-сценарий. Failed post-check останавливает дальнейшее продвижение; сохраняй Evidence и применяй согласованную recovery strategy.

### VPS, Docker Compose и IaC по применимости

Проверь SSH host identity, deploy directory, remote/ref/exact commit, worktree, Compose project, allowlisted services и persistent volumes. Не отключай host verification ради исправления доступа. Git-based deploy использует безопасный fast-forward либо exact revision в чистой release directory, сохраняя неизвестные изменения на host.

`reset --hard`, broad `clean`, `docker compose down`, volume removal и system-wide prune не являются стандартной стратегией CD. Bootstrap host, users/SSH/firewall, массовые permission changes и перенос данных требуют согласованного setup/maintenance scope. Разделяй credential чтения repository/artifact с VPS и credential подключения CI к VPS.

Для IaC apply подтверди target account/workspace, plan diff, revision, полномочия и изменения persistent resources. Нужны state locking и recovery procedure; защити state/plan artifacts, которые могут содержать secrets. Пересоздание или удаление ресурсов не маскируй под routine deploy.

## 7. Runtime configuration и stateful changes

Установи canonical config owner: platform settings, secret manager, environment secrets или host files. Schema/examples содержат безопасные значения. CD сохраняет existing runtime values и не заменяет production `.env` шаблоном. Missing required value блокирует соответствующий deploy.

Проверяй наличие и формат config без печати secret values, resolved secret-bearing config и authorization headers. Rotation и recovery выполняются в разрешённом scope, без скрытой смены секретов при обычной поставке.

Для DB, очередей, object/file storage, volumes и других persistent systems определи класс изменения:

- `NONE` — persistent schema/data не меняются.
- `BACKWARD_COMPATIBLE_AUTOMATED` — versioned migration совместима в rollout window; известны retry, locking, duration/failure behavior, необходимые backup/recovery preconditions и post-check.
- `EXPLICITLY_GATED` — destructive, несовместимое, необратимое или privileged изменение; нужны явные scope/authorization, target, preconditions, recovery/forward-fix и stop criteria.

Обязательный operation gate оставляет соответствующий delivery stage незавершённым до выполнения. Его не подменяет отложенное Ad-hoc тестирование. Если migration требует backup, проверь пригодность восстановления по принятой процедуре: наличия файла недостаточно. Restore, удаление volumes, перенос данных и broad cleanup не становятся разрешёнными автоматически.

Automatic rollback допустим только при проверенной совместимости artifact, config и уже изменённой schema/state. Откат приложения не равен откату данных. Если безопасный rollback не определён, используй согласованный forward-fix или внешний gate; destructive recovery не импровизируй.

## 8. Records и проверка конфигурации

Сохраняй первичные records независимо от сессии агента: CI run/check ID и проверяемую revision с результатами required jobs; build run и artifact identity; CD run/deployment ID, target environment, фактически запущенную revision/artifact, время, migration result и обязательные post-checks. Настрой retention/access по потребностям восстановления контекста и recovery, без secrets в artifacts.

Успешный CD подтверждает выполненные проверки на момент поставки. Наличие конфигурации не доказывает запуск; raw log без target/revision не заменяет Evidence. Недоступный результат остаётся неподтверждённым. Pre-merge CI не подтверждает будущую поставку.

Отдельные статусы DEPLOY/LIVE, обязательная post-merge запись delivery metadata в main и metadata-only follow-up PR не требуются. Источник результата поставки — первичные records. Не вводи automation для переписывания metadata без отдельного поручения.

После настройки проверь синтаксис и затронутые trigger/revision/selection/reuse/trust/failure paths, required checks и protections доступным безопасным способом. Для selector/reuse проверь неизвестный impact, неполные records, изменённые inputs и более новый неуспешный run; они не должны давать ложный green. Сохрани результат и ограничения. Непроверенная гарантия не становится PASS из-за наличия YAML; production/destructive сценарий не запускай только ради проверки конфигурации.

## 9. Проектная адаптация

Фактические workflows/settings/scripts и runbooks — canonical источники команд и процедур. В README/AGENTS.md оставь routing, без второго Project profile в этом файле. Обеспечь восстановление обычного delivery/recovery по проектным процедурам без повторного чтения универсальных правил настройки.

Указывай источники и время/revision проверки; неизвестное — UNSET, неприменимое — N/A с основанием. При изменении stack, settings или окружений обновляй затронутые процедуры. Сохраняй доступную версию шаблона и основания проектных отличий, не выдумывая provenance.

Исключение из safety contract требует решения пользователя/уполномоченного владельца: правило, причина, scope/срок, риск, compensating checks и stop/recovery criteria. Уже разрешённое исключение действует в своих границах и не меняет универсальный шаблон. Стоимость, flaky test или ожидание доступа не разрешают обходить обязательный gate.
