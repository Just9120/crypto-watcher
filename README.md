# CryptoWatcher

Deterministic radar Bybit для владельца в Telegram. Согласованный результат — Spot USDT и USDT Perpetual, четыре одновременных TF, пять detectors, episodes/history/outcomes и advisory context для отдельного trading agent. Radar не исполняет ордера и не вызывает LLM.

Текущая G1 сохраняет composite и owner controls: Top100/custom, один selected `1m/3m/5m/15m`, настройки без restart, mutes и alerts toggle. Первая production migration в SQLite и запуск G1 подтверждены; оставшиеся delivery gates и подсистемы указаны в [плане](docs/open_issues.md).

## Документация

- [Product scope и стабильные AC](docs/product_scope.md)
- [Current Goal, Validation Plan, findings и checkpoint](docs/open_issues.md)
- [State ownership, JSON compatibility и SQLite migration](docs/state_contract.md)
- [Runtime boundaries](docs/architecture.md)
- [CI/CD, VPS target, первая поставка и recovery](docs/runbooks/delivery.md)
- [Repository procedures](AGENTS.md) и [CI/CD configuration contract](ci-cd-rules.md)

## Stack и установка

Target runtime: Python 3.11/Linux; PTB20.8 с job queue, HTTPx async и стандартный SQLite. Lockfiles с hashes генерируются uv для Python3.11; `requirements.in` / `requirements-dev.in` — inputs, соответствующие `.txt` — canonical installation locks. Нужны только Telegram Bot API и public Bybit endpoints; Redis/DB server не требуются. Tests используют disposable fixtures и mocked adapters, без API tokens и сети.

Команды выполняются из root repository. Создать virtual environment:

```bash
python -m venv .venv
```

Активировать `.venv/bin/activate` на Linux либо `.venv\Scripts\Activate.ps1` в PowerShell. Установить runtime или dev lock:

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
python -m cryptowatcher state-check
python -m cryptowatcher health
```

Существующий JSON требует explicit migration из [state contract](docs/state_contract.md); обычный startup его не перезаписывает. Владелец `233382575`, bot `7523865123`; sender и delivery chat должны соответствовать private chat владельца. Пустая либо неверная конфигурация закрывает доступ. Старые `ALLOWED_CHAT_IDS`, CMC и reset-baseline настройки не активируют дополнительные chats.

## Checks

```bash
python scripts/checks.py quality
python scripts/checks.py test
python scripts/checks.py all
python -m ruff format .
```

`quality` проверяет canonical doc links/fences, Ruff lint/format и mypy. `test` запускает meaningful regression suite; `all` используется в CI. Во время разработки допустим точечный pytest по затронутому риску. Coverage target не задан. Основной merge gate — GitHub `ci-required` на актуальной PR test merge revision; local PASS его не заменяет.

## Telegram controls

Постоянная клавиатура: status/settings, Top100/custom, список/add/remove/clear, alerts, mutes, terms и diagnostics. Settings показывают actual thresholds/TF/revision; значения меняются presets либо `/set price_move_min 2.5`. `/setlist BTC,ETH`, `/mute BTC 60`, `/unmute BTC`, `/unmute all` сохраняются. `/alerts on|off` управляет доставкой; расчёт рынка продолжается. No-op buttons не создают ошибку. `/sample_alert` показывает пример. Старые CMC/reset callbacks возвращают сообщение об изменении.

Composite использует `100 × (close / previous_close − 1)`, оборот закрытой свечи / среднее предыдущих SMA periods и ticker turnover24h. Все три threshold comparisons включают equality. Bybit response clock определяет closure; invalid/gapped windows показываются как неполные. TF и polling cadence различны. Широкий eligible universe, concurrent TF и episode freshness остаются G2A/G2B.

## Docker и delivery

Production использует проверенный immutable image, host `.env` и bind data. Compose требует `CW_IMAGE`, `CW_SOURCE_REVISION`, абсолютные `CW_ENV_FILE` / `CW_DATA_DIR`; пути и существующий project зафиксированы в [runbook](docs/runbooks/delivery.md). Не использовать build на production вместо validated artifact.

Для disposable image checks на Linux с Docker:

```bash
docker build --build-arg SOURCE_REVISION=0000000000000000000000000000000000000000 -t cryptowatcher:local .
python scripts/image_checks.py cryptowatcher:local 0000000000000000000000000000000000000000
```

Production secrets не входят в image/context/logs. SQLite migration, host permission setup и SSH credential bootstrap первой поставки выполняются только по concrete gated procedure. Последующие eligible merges доставляются автоматически после включения проверенного target; не меняют unit trading agent.
