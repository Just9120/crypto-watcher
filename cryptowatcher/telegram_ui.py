"""Owner control deck: navigation is read-only, mutations are explicit and revision-safe."""

from __future__ import annotations

import asyncio
import io
import json
import re
import time
import uuid
from typing import TYPE_CHECKING, Any

from telegram import Update

from .analytics_worker import read_snapshot
from .config import TIMEFRAMES, ConfigError, instrument_key
from .detector_config import PARAMETERS, effective_parameters
from .diagnostics import snapshot as diagnostic_snapshot
from .ingestion import BybitRadar
from .storage import RevisionConflict, json_payload
from .telegram_views import (
    FAMILY,
    FIELDS,
    HELP,
    PROFILE,
    TERMS_SHORT,
    amount,
    diagnostic_summary,
    keyboard,
    market,
    observation_kind,
    parameters_text,
    profile_text,
    source_summary,
    when,
)

if TYPE_CHECKING:
    from .bot import Controller

INPUT = "ui:v1:input"
CUSTOM = "ui:v1:custom_parameters"
PAGE_SIZE = 6


class ControlDeck:
    def __init__(self, controller: Controller) -> None:
        self.c = controller

    def cancel(self) -> None:
        if self.c.store.get_runtime(INPUT):
            self.c.store.put_runtime(INPUT, {})
        if self.c.store.get_runtime("pending_action"):
            self.c.store.put_runtime("pending_action", "")

    def remember_custom(self) -> None:
        s = self.c.store.snapshot().settings
        if s.profile == "custom":
            self.c.store.put_runtime(CUSTOM, effective_parameters(s))

    async def data(self) -> dict[str, Any]:
        return await asyncio.to_thread(
            read_snapshot, self.c.store.path, self.c.store.owner_id, diagnostic_snapshot
        )

    async def document(self, update: Update, value: Any, name: str) -> None:
        if update.effective_message:
            content = io.BytesIO(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode())
            await update.effective_message.reply_document(document=content, filename=name)

    async def prompt(self, update: Update, kind: str, field: str = "", selected: str = "") -> None:
        snapshot = self.c.store.snapshot()
        self.cancel()
        self.c.store.put_runtime(
            INPUT,
            {
                "kind": kind,
                "field": field,
                "market": selected or snapshot.settings.bybit_category,
                "revision": snapshot.revision,
                "at": time.time(),
            },
        )
        if kind in ("add", "remove"):
            text = ("Добавление" if kind == "add" else "Удаление") + " монет\n"
            text += f"Рынок ввода: {market(selected or snapshot.settings.bybit_category)}.\n"
            text += "Отправьте BTC,ETH или явный рынок: spot/BTC,linear/ETH."
            rows = [[("Spot USDT", f"ui:input:{kind}:spot"), ("USDT Perpetual", f"ui:input:{kind}:linear")]]
        else:
            p = effective_parameters(snapshot.settings)
            current = p.get(field, getattr(snapshot.settings, field, None))
            text = f"{FIELDS[field]}\nСейчас: " + (
                ", ".join(map(str, current)) if isinstance(current, tuple) else amount(current)
            )
            text += (
                "\nОтправьте новое значение. Пороги положительные; окна — целые свечи; отношения — больше1."
            )
            if field == "price_move_min":
                text += "\nЗаменятся пороги цены всех четырёх TF. Для отдельного TF выберите его параметр."
            if field == "outcome_horizons":
                text += "\nМинуты через запятую, например5,15,30,60. Только для новых событий."
            rows = []
        await self.c.send(update, text, keyboard(rows + [[("Отменить ввод", "ui:cancel")]], back=None))

    async def input(self, update: Update, text: str) -> bool:
        value = self.c.store.get_runtime(INPUT, {})
        if not value:
            return False
        if text.casefold() in ("отмена", "назад", "/cancel"):
            self.cancel()
            await self.c.send(update, "Ввод отменён. Настройки сохранены.", keyboard([]))
            return True
        snapshot = self.c.store.snapshot()
        if snapshot.revision != value["revision"] or time.time() - value.get("at", 0) > 900:
            self.cancel()
            raise RevisionConflict("Настройки изменились или время ввода истекло. Откройте параметр заново")
        if value["kind"] in ("add", "remove"):
            pairs = tuple(
                dict.fromkeys(
                    instrument_key(p, value["market"]) for p in re.split(r"[,\s]+", text.strip()) if p
                )
            )
            if not pairs:
                raise ConfigError("Нужна хотя бы одна монета")
            if value["kind"] == "add" and isinstance(self.c.market, BybitRadar):
                await self.c.market.validate_instruments(pairs)
            # Validation may await network; never replace a newer owner revision.
            if self.c.store.snapshot().revision != snapshot.revision:
                self.cancel()
                raise RevisionConflict("Список изменился во время проверки. Откройте действие заново")
            current = snapshot.settings.instruments()
            selected = (
                tuple(dict.fromkeys((*current, *pairs)))
                if value["kind"] == "add"
                else tuple(p for p in current if p not in pairs)
            )
            settings = snapshot.settings.changed(
                watchlist=selected,
                custom_pairs=tuple(
                    p.split("/")[1] for p in selected if p.startswith(snapshot.settings.bybit_category + "/")
                ),
            )
        else:
            field = value["field"]
            if field == "outcome_horizons":
                parts = text.replace(",", " ").split()
                if not parts or any(not part.isdecimal() for part in parts):
                    raise ConfigError("Нужны целые положительные минуты через запятую")
                settings = snapshot.settings.changed(outcome_horizons=tuple(map(int, parts)))
            elif field in PARAMETERS:
                settings = snapshot.settings.with_parameter(field, text)
            else:
                settings = snapshot.settings.changed(**{field: text})
        after = self.c.store.update(snapshot.revision, settings)
        self.c.store.put_runtime("last_poll_ts", 0)
        self.cancel()
        await self.c.send(
            update, "Значение сохранено." if after != snapshot else "Значение уже установлено.", keyboard([])
        )
        return True

    async def history(self, update: Update, cursor: str = "") -> None:
        from .events import PREFIX

        if cursor and not re.fullmatch(r"\d{16}:[0-9a-f-]{36}", cursor):
            raise ConfigError("Некорректная страница истории")
        start = PREFIX + "history:"
        upper = start + (cursor or "~")
        rows = self.c.store.db.execute(
            "SELECT key,payload_json FROM runtime WHERE key>=? AND key<? ORDER BY key DESC LIMIT ?",
            (start, upper, PAGE_SIZE + 1),
        ).fetchall()
        entries = []
        for row in rows[:PAGE_SIZE]:
            binding = json_payload(row[1])
            event = self.c.history.get("event-head:" + binding["event_id"])
            if event:
                entries.append(event)
        lines = [
            "История · последние наблюдения" if not cursor else "История · более ранние наблюдения",
            self.c.history_notice().strip(),
        ]
        controls: list[list[tuple[str, str]]] = []
        for index, event in enumerate(entries, 1):
            lines.append(
                f"{index}. {when(event['market_observation_ms'])} · {event['symbol']} · {market(event['market_type'])}\n"
                f"{FAMILY[event['detector_id']]} · {event['timeframe']} · {event['direction']} · ×{amount(event['strength'])}\n"
                + observation_kind(event)
                + "; статус Telegram — в подробностях"
            )
            controls.append(
                [(f"{index}. {event['symbol']} · {event['timeframe']}", "event:" + event["event_id"])]
            )
        if not entries:
            lines.append("История пока пуста в этом доступном периоде.")
        lines.append(
            "Сохранённое наблюдение не означает отправленное уведомление. Полная доступная выборка — через export."
        )
        if len(rows) > PAGE_SIZE:
            controls.append([("Ранее →", "ui:history:" + rows[PAGE_SIZE - 1][0][len(start) :])])
        if cursor:
            controls.append([("К последним", "ui:history")])
        await self.c.send(update, "\n".join(filter(None, lines)), keyboard(controls))

    async def command(self, update: Update, command: str, raw: str) -> bool:
        from .render import detectors_keyboard, menu, settings_keyboard, settings_text

        snapshot = self.c.store.snapshot()
        s = snapshot.settings
        if command in ("start", "home"):
            await self.c.send(
                update, "Радар Bybit\nВыберите раздел. Открытие меню не меняет уведомления.", menu()
            )
        elif command in ("help", "help_home"):
            await self.c.send(
                update,
                HELP,
                keyboard(
                    [
                        [("Как читать сигналы", "ui:terms"), ("Пример", "ui:sample_alert")],
                        [("Диагностика", "ui:diagnostics"), ("Все команды", "ui:commands")],
                    ]
                ),
            )
        elif command == "terms":
            await self.c.send(
                update, TERMS_SHORT, keyboard([[("Формулы и правила", "ui:formulas")]], back="help")
            )
        elif command == "settings":
            await self.c.send(update, settings_text(snapshot), settings_keyboard(snapshot))
        elif command in ("radar", "watchlist"):
            current = s.instruments()
            text = source_summary(snapshot, self.c.store.get_runtime("ingestion", {}), time.time())
            text += "\n\nМой список:\n" + (
                "\n".join(f"{market(key.split('/')[0])}: {key.split('/')[1]}" for key in current)
                or "Список пуст"
            )
            text += f"\nПороги монет списка умножаются на {amount(s.watchlist_multiplier)}; история и доставка — разные подсистемы."
            await self.c.send(
                update,
                text,
                keyboard(
                    [
                        [
                            ("Использовать весь рынок", "ui:radar_top"),
                            ("Использовать мой список", "ui:radar_custom"),
                        ],
                        [("Добавить", "ui:addcoin"), ("Удалить", "ui:removecoin")],
                        [("Очистить список", "ui:clear"), ("Уведомления", "ui:alerts")],
                        [("Заглушённые монеты", "ui:mutes")],
                    ]
                ),
            )
        elif command in ("radar_top", "radar_custom"):
            mode = "top" if command == "radar_top" else "custom"
            self.c.change(alert_universe_mode=mode)
            changed = self.c.store.snapshot().revision != snapshot.revision
            await self.c.send(
                update,
                ("Режим изменён: " if changed else "Уже выбран режим: ")
                + ("весь подходящий рынок." if mode == "top" else "мой список.")
                + (
                    " Список пуст; выбранных инструментов нет."
                    if mode == "custom" and not s.instruments()
                    else ""
                ),
                keyboard([[("Охват и список", "ui:radar")]]),
            )
        elif command in ("addcoin", "removecoin"):
            if command == "removecoin" and not s.instruments():
                await self.c.send(
                    update,
                    "Список пуст — удалять нечего.",
                    keyboard([[("Добавить монету", "ui:addcoin")]], back="radar"),
                )
            else:
                await self.prompt(update, "add" if command == "addcoin" else "remove")
        elif command == "clearlist":
            if not s.instruments():
                text = "Список уже пуст. Настройки не изменены."
            else:
                self.c.change(watchlist=(), custom_pairs=())
                text = "Список очищен." + (
                    " Режим «Мой список» теперь не выбирает инструменты."
                    if s.alert_universe_mode == "custom"
                    else " Широкий охват продолжает работать."
                )
            self.cancel()
            await self.c.send(update, text, keyboard([[("Охват и список", "ui:radar")]]))
        elif command == "profiles":
            await self.c.send(
                update,
                profile_text(snapshot),
                keyboard(
                    [
                        [(PROFILE[name], "ui:profile:" + name)]
                        for name in ("normal", "conservative", "sensitive")
                    ]
                    + [[("Вернуть свои пороги", "ui:restore:" + str(snapshot.revision))]],
                    back="settings",
                ),
            )
        elif command == "detectors":
            selected = ", ".join(FAMILY[f] for f in s.alert_detectors) or "выключены"
            await self.c.send(
                update,
                "Уведомления по детекторам\nВсе пять семейств рассчитываются для выбранного охвата и сохраняют историю.\n"
                f"В Telegram разрешены: {selected}.\nКнопки меняют только доставку уведомлений.",
                detectors_keyboard(snapshot),
            )
        elif command == "alerts" and not raw:
            enabled = self.c.store.enabled()
            await self.c.send(
                update,
                f"Мгновенные уведомления: {'включены' if enabled else 'выключены'}\n"
                "Переключение не меняет расчёты, историю, дайджест и доставку агенту.",
                keyboard(
                    [
                        [
                            (
                                "Выключить" if enabled else "Включить",
                                "ui:alerts:" + ("off" if enabled else "on"),
                            )
                        ],
                        [("По детекторам", "ui:detectors"), ("Заглушённые монеты", "ui:mutes")],
                    ],
                    back="settings",
                ),
            )
        elif command == "history":
            await self.history(update)
        elif command == "outcomes" and not raw:
            await self.c.send(
                update,
                "Результаты событий\nВыберите событие, затем нажмите «Результат». Сроки и исходная цена взяты из сохранённого события.",
            )
            await self.history(update)
        elif command == "analytics_home":
            await self.c.send(
                update,
                "Аналитика\nРезультаты описывают движение после событий, не доходность стратегии. "
                "Малое число наблюдений и неполные данные ограничивают выводы.",
                keyboard(
                    [
                        [("Статистика", "ui:analytics"), ("Сравнение", "ui:compare")],
                        [("Результаты событий", "ui:outcomes"), ("Дайджест", "ui:digest")],
                    ]
                ),
            )
        elif command in ("analytics", "compare") and not raw:
            await self.c.send(
                update,
                ("Сравнение сохранённых профилей/версий" if command == "compare" else "Статистика детекторов")
                + "\nВыберите разрез. Сроки результатов считаются отдельно; исходные пороги не меняются.",
                keyboard(
                    [
                        [("По детекторам", "ui:groups:" + command + ":detector")],
                        [
                            ("По профилям", "ui:groups:" + command + ":profile"),
                            ("По версиям", "ui:groups:" + command + ":version"),
                        ],
                        [("По рынку и TF", "ui:groups:" + command + ":market,timeframe")],
                    ],
                    back="analytics_home",
                ),
            )
        elif command == "digest" and not raw:
            enabled = self.c.digest.preference()["enabled"]
            await self.c.send(
                update,
                f"Дайджест: {'включён' if enabled else 'выключен'}\n"
                f"Интервал: {s.digest_interval_min} минут. Независим от мгновенных уведомлений и заглушений. "
                "После включения — следующий полный интервал UTC.",
                keyboard(
                    [
                        [("Выключить" if enabled else "Включить", "digest:" + ("off" if enabled else "on"))],
                        [("Изменить интервал", "ui:field:digest_interval_min")],
                    ],
                    back="analytics_home",
                ),
            )
        elif command == "diagnostics":
            data = await self.data()
            await self.c.send(
                update,
                diagnostic_summary(data),
                keyboard(
                    [
                        [("Источники", "ui:diag:ingestion"), ("Очереди", "ui:diag:queues")],
                        [("Хранение", "ui:diag:storage"), ("Полный JSON файлом", "ui:diag:file")],
                    ],
                    back="help",
                ),
            )
        elif command == "agent" and raw.strip() in ("", "status"):
            d = (await self.data())["agent_delivery"]
            state = (
                "не подключена"
                if d.get("configuration") != "READY"
                else "включена"
                if d.get("enabled")
                else "выключена"
            )
            await self.c.send(
                update,
                f"Доставка контекста агенту: {state}\nОжидают: {amount(d.get('pending'))}; доставлены: {amount(d.get('delivered'))}.\n"
                "Ручной радар работает независимо. Подключение и credentials настраиваются в принимающем проекте. "
                "Просмотр не включает доставку, retry или replay.",
                keyboard([[("Подробности в отчёте", "ui:diag:file")]], back="settings"),
            )
        elif command == "mutes":
            rows = self.c.store.db.execute(
                "SELECT symbol,mute_until FROM mutes WHERE mute_until>? ORDER BY symbol LIMIT 30",
                (time.time(),),
            ).fetchall()
            controls = [[("Снять: " + row[0], "ui:unmute:" + row[0])] for row in rows]
            text = "Заглушённые монеты\n" + (
                "\n".join(f"{row[0]} до {when(row[1] * 1000)}" for row in rows) or "Нет активных заглушений."
            )
            text += (
                "\nЗаглушение влияет только на Telegram. Расчёт и история продолжаются. Показано до30монет."
            )
            await self.c.send(update, text, keyboard(controls, back="alerts"))
        elif command in ("enrichment", "derivatives") and not raw:
            from .render import enrichment_keyboard

            await self.c.send(
                update,
                f"Дополнительный контекст деривативов: {'включён' if s.enrichment_enabled else 'выключен'}\n"
                "Добавляет доступные OI (открытый интерес), funding и basis для соответствующей Spot-монеты. "
                "Это не переключатель основного Perpetual радара. Отсутствие context не блокирует детекторы.",
                enrichment_keyboard(snapshot),
            )
        else:
            return False
        return True

    async def callback(self, update: Update, parts: list[str]) -> None:
        from .render import TERMS

        action = parts[0] if parts else ""
        snapshot = self.c.store.snapshot()
        if action in ("cancel", "home"):
            self.cancel()
            await self.c.command(update, "home")
        elif action == "parameters":
            await self.c.send(
                update,
                "Пороги детекторов\nВыберите группу. Значения каждого TF настраиваются отдельно; применение ко всем — отдельное действие.",
                keyboard(
                    [
                        [
                            ("Цена по TF", "ui:parameter_group:price"),
                            ("Оборот", "ui:parameter_group:turnover"),
                        ],
                        [
                            ("Пробой", "ui:parameter_group:breakout"),
                            ("Диапазон", "ui:parameter_group:volatility"),
                        ],
                        [
                            ("Общие параметры", "ui:parameter_group:common"),
                            ("Все значения", "ui:parameter_group:all"),
                        ],
                    ],
                    back="settings",
                ),
            )
        elif action == "parameter_group" and len(parts) == 2:
            groups = {
                "price": [*("price_" + tf for tf in TIMEFRAMES), "price_move_min"],
                "turnover": ["turnover_ratio", "turnover_lookback"],
                "breakout": ["breakout_lookback", "breakout_buffer"],
                "volatility": ["volatility_ratio", "volatility_lookback"],
                "common": ["liquidity_floor_24h", "watchlist_multiplier", "radar_poll_sec"],
                "all": [],
            }
            if parts[1] not in groups:
                raise ConfigError("Неизвестная группа параметров")
            values = effective_parameters(snapshot.settings)
            fields = groups[parts[1]]
            lines = ["Текущие значения"]
            for key in fields:
                if key == "price_move_min":
                    continue
                current = values.get(key, getattr(snapshot.settings, key, None))
                lines.append(f"{FIELDS[key]}: {amount(current)}")
            if parts[1] == "all":
                lines = [parameters_text(snapshot)]
            await self.c.send(
                update,
                "\n".join(lines),
                keyboard([[(FIELDS[key], "ui:field:" + key)] for key in fields], back="parameters"),
            )
        elif action == "card":
            s = snapshot.settings
            await self.c.send(
                update,
                f"Карточка и ввод\nСейчас: {market(s.bybit_category)}, {s.signal_timeframe}.\n"
                "Это пример/карточка и рынок ввода монет без префикса. Основной радар обрабатывает оба рынка и все4TF.",
                keyboard(
                    [
                        [
                            (
                                ("✓ " if s.bybit_category == value else "") + market(value),
                                f"ui:cardset:bybit_category:{value}:{snapshot.revision}",
                            )
                            for value in ("spot", "linear")
                        ],
                        [
                            (
                                ("✓ " if s.signal_timeframe == value else "") + value,
                                f"ui:cardset:signal_timeframe:{value}:{snapshot.revision}",
                            )
                            for value in TIMEFRAMES
                        ],
                    ],
                    back="settings",
                ),
            )
        elif action == "cardset" and len(parts) == 4:
            if parts[1] not in ("bybit_category", "signal_timeframe") or parts[3] != str(snapshot.revision):
                raise RevisionConflict("Карточка настроек устарела")
            self.c.store.update(snapshot.revision, snapshot.settings.changed(**{parts[1]: parts[2]}))
            await self.callback(update, ["card"])
        elif action == "field" and len(parts) == 2 and parts[1] in FIELDS:
            await self.prompt(update, "field", parts[1])
        elif (
            action == "input"
            and len(parts) == 3
            and parts[1] in ("add", "remove")
            and parts[2] in ("spot", "linear")
        ):
            await self.prompt(update, parts[1], selected=parts[2])
        elif action == "profile" and len(parts) == 2 and parts[1] in ("normal", "conservative", "sensitive"):
            await self.c.send(
                update,
                profile_text(snapshot, parts[1]),
                keyboard(
                    [
                        [("Применить профиль", f"ui:apply:{parts[1]}:{snapshot.revision}")],
                    ],
                    back="profiles",
                ),
            )
        elif action in ("apply", "restore"):
            revision = parts[-1] if len(parts) > 1 else ""
            if not revision.isdecimal() or int(revision) != snapshot.revision:
                raise RevisionConflict("Панель устарела. Откройте профили заново")
            if action == "apply" and len(parts) == 3:
                self.remember_custom()
                settings = snapshot.settings.with_profile(parts[1])
            elif action == "restore" and len(parts) == 2:
                saved = self.c.store.get_runtime(CUSTOM)
                if not saved:
                    await self.c.send(
                        update,
                        "Сохранённых своих порогов пока нет. Они сохраняются перед применением профиля.",
                        keyboard([], back="profiles"),
                    )
                    return
                settings = snapshot.settings.changed(
                    profile="custom", detector_parameters=tuple(saved.items())
                )
            else:
                raise ConfigError("Некорректное действие профиля")
            self.c.store.update(snapshot.revision, settings)
            self.c.store.put_runtime("last_poll_ts", 0)
            await self.c.send(
                update, "Профиль применён. " + PROFILE[settings.profile], keyboard([], back="settings")
            )
        elif action == "history":
            await self.history(update, ":".join(parts[1:]))
        elif action == "clear":
            if len(parts) == 1 and snapshot.settings.instruments():
                await self.c.send(
                    update,
                    f"Очистить все {len(snapshot.settings.instruments())} инструментов моего списка?",
                    keyboard(
                        [
                            [("Очистить", f"ui:clear:{snapshot.revision}")],
                        ],
                        back="radar",
                    ),
                )
            elif len(parts) == 1 or (len(parts) == 2 and parts[1] == str(snapshot.revision)):
                await self.c.command(update, "clearlist")
            else:
                raise RevisionConflict("Список изменился. Откройте очистку заново")
        elif action == "alerts" and len(parts) == 2 and parts[1] in ("on", "off"):
            await self.c.command(update, "alerts", parts[1])
        elif action == "groups" and len(parts) == 3 and parts[1] in ("analytics", "compare"):
            await self.c.command(update, parts[1], parts[2])
        elif action == "unmute" and len(parts) == 2:
            await self.c.command(update, "unmute", parts[1])
        elif action == "eventfile" and len(parts) == 2:
            event_id = str(uuid.UUID(parts[1]))
            event = self.c.history.get("event-head:" + event_id)
            if event is None:
                await self.c.send(
                    update, "Подробности события уже недоступны в текущем сроке хранения.", keyboard([])
                )
            else:
                await self.document(update, event, "event-" + event_id + ".json")
        elif action == "diag" and len(parts) == 2:
            data = await self.data()
            if parts[1] == "file":
                await self.document(update, data, "cryptowatcher-diagnostics.json")
            elif parts[1] == "queues":
                await self.c.send(
                    update,
                    "Очереди — снимок\n"
                    + json.dumps(
                        {key: data.get(key) for key in ("outcomes", "analytics", "analytics_recovery")},
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\nPending включает будущие сроки; возраст/просрочка здесь не измерены, нулём их не считаем.",
                    keyboard([], back="diagnostics"),
                )
            elif parts[1] in ("ingestion", "storage"):
                await self.c.send(
                    update,
                    json.dumps(data[parts[1]], ensure_ascii=False, indent=2),
                    keyboard([[("Полный JSON файлом", "ui:diag:file")]], back="diagnostics"),
                )
            else:
                raise ConfigError("Неизвестный раздел диагностики")
        elif action == "commands":
            from .bot import COMMANDS

            await self.c.send(
                update,
                "Дополнительные команды\n"
                + "\n".join(
                    f"/{name} — {label}" for name, label in COMMANDS.items() if not name.startswith("reset")
                )
                + "\nКоманды изменения требуют аргументов. Обычные действия доступны кнопками; /help показывает примеры ввода.",
                keyboard([], back="help"),
            )
        elif action == "formulas":
            await self.c.send(update, TERMS, keyboard([], back="terms"))
        elif len(parts) == 1 and action in {
            "settings",
            "radar",
            "watchlist",
            "profiles",
            "detectors",
            "alerts",
            "mutes",
            "history",
            "analytics_home",
            "analytics",
            "compare",
            "outcomes",
            "digest",
            "agent",
            "diagnostics",
            "help",
            "terms",
            "sample_alert",
            "addcoin",
            "removecoin",
            "radar_top",
            "radar_custom",
            "enrichment",
            "derivatives",
            "status",
        }:
            self.cancel()
            await self.c.command(update, action)
        else:
            raise ConfigError("Панель устарела. Откройте главное меню")
