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

from .analytics import cohorts
from .analytics_worker import read_snapshot
from .config import TIMEFRAMES, ConfigError, instrument_key
from .detector_config import PARAMETERS, effective_parameters
from .diagnostics import snapshot as diagnostic_snapshot
from .ingestion import BybitRadar
from .storage import RevisionConflict
from .telegram_views import (
    DIRECTION,
    FAMILY,
    FIELDS,
    HELP,
    PROFILE,
    TERMS_SHORT,
    agent_state,
    amount,
    diagnostic_section,
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
PAGE_SIZE = 4


class ControlDeck:
    def __init__(self, controller: Controller) -> None:
        self.c = controller
        self.statistics: dict[str, Any] = {}
        self.history_contexts: dict[str, dict[str, Any]] = {}

    @staticmethod
    def input_back(value: dict[str, Any]) -> str:
        field = value.get("field", "")
        if value.get("kind") in ("add", "remove"):
            return "radar"
        for group, fields in {
            "price": ["price_move_min", *["price_" + tf for tf in TIMEFRAMES]],
            "turnover": ["turnover_ratio", "turnover_lookback"],
            "breakout": ["breakout_lookback", "breakout_buffer"],
            "volatility": ["volatility_ratio", "volatility_lookback"],
            "common": ["watchlist_multiplier", "liquidity_floor_24h", "radar_poll_sec"],
        }.items():
            if field in fields:
                return "parameter_group:" + group
        return "settings"

    async def show_event(
        self, update: Update, event_id: str, token: str = "", *, result: bool = False
    ) -> None:
        from .config import OwnerSettings
        from .events import identity
        from .outcomes import METHOD
        from .render import event_keyboard, event_text, outcome_text

        try:
            event_id = str(uuid.UUID(event_id))
        except ValueError as exc:
            raise ConfigError("Некорректная ссылка на событие") from exc
        context = self.history_contexts.get(token, {})
        valid = event_id in context.get("event_ids", []) and time.time() - context.get("at", 0) < 900
        nav = token if valid else None
        event = self.c.history.get("event-head:" + event_id)
        if event is None:
            await self.c.send(
                update,
                "Подробности события уже недоступны в текущем сроке хранения.",
                keyboard([], back="returnhistory:" + token if valid else "history"),
            )
            return
        if result:
            horizons = OwnerSettings.from_payload(event["settings_snapshot"]).outcome_horizons
            outcomes = [
                self.c.history.get("outcome-head:" + identity(f"outcome/{event_id}/{h}/{METHOD}"))
                for h in horizons
            ]
            body = outcome_text(event, outcomes, horizons)
        else:
            body = (
                event_text(event, int(time.time() * 1000))
                + "\nТип: "
                + observation_kind(event)
                + "\nTelegram: "
                + self.delivery_note(event_id)
            )
        await self.c.send(update, body, event_keyboard(event, history_token=nav))

    def cancel(self) -> None:
        if self.c.store.get_runtime(INPUT):
            self.c.store.put_runtime(INPUT, {})
        if self.c.store.get_runtime("pending_action"):
            self.c.store.put_runtime("pending_action", "")

    def remember_custom(self) -> None:
        s = self.c.store.snapshot().settings
        if s.profile == "custom":
            self.c.store.put_runtime(CUSTOM, effective_parameters(s))

    def delivery_note(self, event_id: str) -> str:
        decision = self.c.history.get("decision:telegram:" + event_id, {})
        receipt = (
            self.c.history.get("outbox:telegram:" + decision["delivery_id"], {})
            if decision.get("delivery_id")
            else {}
        )
        reasons = {
            "alerts_disabled": "уведомления были выключены",
            "muted": "монета была заглушена",
            "detector_silent": "семейство не выбрано для уведомлений",
            "not_fresh": "наблюдение уже не было свежим",
            "historical_observation": "историческое наблюдение",
            "awaiting_rearm": "ожидалось новое независимое движение",
        }
        return {
            "DELIVERED": "доставлено по сохранённой квитанции",
            "RETRY": "ожидает повторной попытки",
            "PENDING": "ожидает отправки",
            "EXPIRED": "истёк срок отправки",
            "BLOCKED": "доставка заблокирована",
        }.get(
            str(receipt.get("state")),
            reasons.get(str(decision.get("reason")), "сохранённого статуса доставки нет"),
        )

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
                "\nОтправьте новое значение. Пороги положительные; окна — целые свечи; отношения — больше 1."
            )
            if field == "price_move_min":
                text = (
                    FIELDS[field]
                    + "\nСейчас: "
                    + "; ".join(f"{tf}: {amount(p['price_' + tf])}%" for tf in TIMEFRAMES)
                )
                text += "\nЗаменятся пороги цены всех четырёх TF. Для отдельного TF выберите его параметр."
            if field == "outcome_horizons":
                text += "\nМинуты через запятую, например 5,15,30,60. Только для новых событий."
            rows = []
        await self.c.send(update, text, keyboard(rows + [[("Отменить ввод", "ui:cancel")]], back=None))

    async def input(self, update: Update, text: str) -> bool:
        value = self.c.store.get_runtime(INPUT, {})
        if not value:
            return False
        if text.casefold() in ("отмена", "назад", "/cancel"):
            self.cancel()
            await self.c.send(
                update, "Ввод отменён. Настройки сохранены.", keyboard([], back=self.input_back(value))
            )
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
                settings = snapshot.settings.with_parameter(field, text.replace(",", "."))
            else:
                settings = snapshot.settings.changed(**{field: text.replace(",", ".")})
        after = self.c.store.update(snapshot.revision, settings)
        self.c.store.put_runtime("last_poll_ts", 0)
        self.cancel()
        await self.c.send(
            update,
            "Значение сохранено." if after != snapshot else "Значение уже установлено.",
            keyboard([], back=self.input_back(value)),
        )
        return True

    async def history(self, update: Update, cursor: str = "") -> None:
        from .events import PREFIX

        if cursor and not re.fullmatch(r"\d{16}:[0-9a-f-]{36}", cursor):
            raise ConfigError("Некорректная страница истории")
        start = PREFIX + "history:"
        upper = start + (cursor or "~")
        from .catalogue_sql import read_snapshot

        with read_snapshot(self.c.store):
            rows = list(self.c.store.scan_runtime(start, before=upper, reverse=True, limit=PAGE_SIZE + 1))
            entries = []
            for row in rows[:PAGE_SIZE]:
                binding = row[1]
                event = self.c.history.get("event-head:" + binding["event_id"])
                if event:
                    entries.append(event)
        lines = [
            "🗂 История · последние наблюдения" if not cursor else "🗂 История · более ранние наблюдения",
        ]
        token = uuid.uuid4().hex[:8]
        self.history_contexts = {
            k: v for k, v in self.history_contexts.items() if time.time() - v["at"] < 900
        }
        while len(self.history_contexts) >= 64:
            self.history_contexts.pop(next(iter(self.history_contexts)))
        self.history_contexts[token] = {
            "cursor": cursor,
            "at": time.time(),
            "event_ids": [e["event_id"] for e in entries],
        }
        controls: list[list[tuple[str, str]]] = []
        previous_date = ""
        for index, event in enumerate(entries, 1):
            stamp = when(event["market_observation_ms"])
            date, clock, zone = stamp.split()
            if date != previous_date:
                lines.append(date + " · " + zone)
                previous_date = date
            lines.append(
                f"\n{index}. {clock} · {event['symbol']} · {market(event['market_type'])}\n"
                f"{FAMILY[event['detector_id']]} · {event['timeframe']} · {DIRECTION.get(event['direction'], event['direction'])} · ×{amount(round(event['strength'], 2))}\n"
                + observation_kind(event)
                + "; Telegram: "
                + self.delivery_note(event["event_id"])
            )
            controls.append(
                [
                    (
                        f"{index}. {event['symbol']} · {event['timeframe']}",
                        "ui:open:" + event["event_id"] + ":" + token,
                    )
                ]
            )
        if not entries:
            lines.append("История пока пуста в этом доступном периоде.")
            lines.append(self.c.history_notice())
        lines.append("Наблюдение не означает отправленное уведомление.")
        controls.append([("Период и доступные данные", "ui:historyinfo")])
        if len(rows) > PAGE_SIZE:
            controls.append([("Ранее →", "ui:history:" + rows[PAGE_SIZE - 1][0][len(start) :])])
        if cursor:
            controls.append([("К последним", "ui:history")])
        await self.c.send(update, "\n".join(filter(None, lines)), keyboard(controls))

    async def analytics_page(self, update: Update, token: str, page: int) -> None:
        value = self.statistics
        if value.get("token") != token or time.time() - value.get("at", 0) > 300:
            raise ConfigError("Снимок статистики устарел. Выберите разрез заново")
        data = value["data"]
        rows = data["cohorts"]
        if page < 0 or page * 5 >= max(len(rows), 1):
            raise ConfigError("Страница статистики не найдена")
        labels = {
            "detector": "детектор",
            "profile": "профиль",
            "version": "версия",
            "market": "рынок",
            "timeframe": "TF",
            "direction": "направление",
            "settings": "настройки",
            "instrument": "инструмент",
            "universe": "охват",
            "strength": "интенсивность",
            "horizon_min": "срок, мин",
        }
        lines = [
            "Сравнение" if value["command"] == "compare" else "Статистика детекторов",
            "Снимок: " + when(value["at"] * 1000),
        ]
        if data.get("selection_period"):
            lines.append(
                "Архивная часть выбирается целыми UTC-днями; доступные подробности — по точному времени."
            )
        for row in rows[page * 5 : page * 5 + 5]:
            group = row["group"]
            if isinstance(group, dict):
                description = "; ".join(
                    f"{labels.get(k, k)}: {FAMILY.get(str(v), PROFILE.get(str(v), str(v)))}"
                    for k, v in group.items()
                )
            else:
                description = str(group)
            lines.append(
                f"{description}\nНаблюдений: {row['sample_size']}; среднее {amount(row['mean'])}%; медиана {amount(row['median'])}%\n"
                f"Сравнимых периодов без сигнала: {row['comparator_sample_size']}; среднее {amount(row['comparator_mean'])}%\n"
                f"Шум: {amount(row['noise_min_pct'])}…{amount(row['noise_max_pct'])}%; наблюдений шума {row['noise_sample_size']}."
            )
        if not rows:
            lines.append("Для этого разреза завершённых результатов пока нет.")
        lines.append(
            f"Неполных результатов: {data['incomplete_outcomes']}. Каждый срок оценивается отдельно. События могут быть зависимы; малое N ограничивает выводы. Это не доходность стратегии."
        )
        if data["truncated"]:
            lines.append("Снимок ограничен первыми 20 группами. Полная выборка доступна через CLI export.")
        controls = []
        navigation = []
        if page:
            navigation.append(("← Предыдущие", f"ui:stats:{token}:{page - 1}"))
        if (page + 1) * 5 < len(rows):
            navigation.append(("Следующие →", f"ui:stats:{token}:{page + 1}"))
        if navigation:
            controls.append(navigation)
        controls.append([("Точные данные снимка", "ui:statsfile:" + token)])
        await self.c.send(update, "\n\n".join(lines), keyboard(controls, back="analytics_home"))

    async def command(self, update: Update, command: str, raw: str) -> bool:
        from .render import (
            detectors_keyboard,
            event_keyboard,
            event_text,
            menu,
            settings_keyboard,
            settings_text,
            status_text,
        )

        snapshot = self.c.store.snapshot()
        s = snapshot.settings
        if command == "cancel":
            self.cancel()
            await self.c.send(update, "Ввод закрыт. Настройки не изменены.", keyboard([]))
        elif command in ("start", "home"):
            await self.c.send(
                update, "📡 Радар Bybit\n\nВыберите раздел. Открытие меню не меняет уведомления.", menu()
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
        elif command == "set" and not raw:
            await self.callback(update, ["parameters"])
        elif command == "horizons" and not raw:
            await self.c.send(
                update,
                "Сроки оценки результата: "
                + ", ".join(map(str, s.outcome_horizons))
                + " минут. Изменения применяются только к новым событиям.",
                keyboard([[("Изменить", "ui:field:outcome_horizons")]], back="settings"),
            )
        elif command == "profile" and not raw:
            await self.command(update, "profiles", "")
        elif command == "status":
            result = await self.c.market.snapshot(s)
            if self.c.store.snapshot().revision != snapshot.revision:
                await self.c.send(
                    update,
                    "Настройки изменились во время запроса; прежняя сводка не применена. Откройте статус заново.",
                )
                return True
            await self.c.send(
                update,
                status_text(snapshot, result, self.c.store.enabled())
                + "\n\n"
                + source_summary(snapshot, self.c.store.get_runtime("ingestion", {}), time.time()),
                keyboard(
                    [
                        [("Обновить", "ui:status"), ("Разбор совпадений", "ui:composite")],
                        [("Охват и список", "ui:radar"), ("Диагностика", "ui:diagnostics")],
                    ]
                ),
            )
        elif command == "event" and raw:
            try:
                event_id = str(uuid.UUID(raw.strip()))
            except ValueError as exc:
                raise ConfigError("Событие не найдено: некорректная ссылка") from exc
            event = self.c.history.get("event-head:" + event_id)
            if event is None:
                await self.c.send(
                    update, "Событие недоступно в текущем сроке хранения.", keyboard([], back="history")
                )
                return True
            now = (
                self.c.market.server_now()
                if isinstance(self.c.market, BybitRadar)
                else int(time.time() * 1000)
            )
            state = self.delivery_note(event_id)
            await self.c.send(
                update,
                event_text(event, now) + "\nТип: " + observation_kind(event) + "\nTelegram: " + state,
                event_keyboard(event),
            )
        elif command in ("radar", "watchlist"):
            current = s.instruments()
            offset = int(raw) if command == "watchlist" and raw.isdecimal() else 0
            if offset >= max(len(current), 1):
                offset = 0
            text = source_summary(snapshot, self.c.store.get_runtime("ingestion", {}), time.time())
            text += "\n\nМой список:\n" + (
                "\n".join(
                    f"• {market(key.split('/')[0])}: {key.split('/')[1]}"
                    for key in current[offset : offset + 10]
                )
                or "Список пуст"
            )
            text += f"\n\nПороги монет списка умножаются на {amount(s.watchlist_multiplier)}. Сохранённое наблюдение не всегда отправляется в Telegram."
            if len(current) > 10:
                text += f"\nПоказаны {offset + 1}–{min(offset + 10, len(current))} из {len(current)}."
            pages = []
            if offset:
                pages.append(("← Предыдущие", f"ui:listpage:{max(offset - 10, 0)}"))
            if offset + 10 < len(current):
                pages.append(("Следующие →", f"ui:listpage:{offset + 10}"))
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
                        *([pages] if pages else []),
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
                    + (
                        [[("Вернуть свои пороги", "ui:restore:" + str(snapshot.revision))]]
                        if self.c.store.get_runtime(CUSTOM)
                        else []
                    ),
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
        elif command == "event" and not raw:
            await self.history(update)
        elif command == "outcomes" and not raw:
            await self.c.send(
                update,
                "📍 Результаты событий\n\nВыберите событие, затем нажмите «Результат». Сроки и исходная цена взяты из сохранённого события.",
            )
            await self.history(update)
        elif command == "analytics_home":
            await self.c.send(
                update,
                "📈 Аналитика\n\nРезультаты описывают движение после событий, не доходность стратегии. "
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
        elif command in ("analytics", "compare"):
            groups = tuple(raw.replace(",", " ").split())
            try:
                data = await asyncio.to_thread(
                    read_snapshot,
                    self.c.store.path,
                    self.c.store.owner_id,
                    lambda store: cohorts(store, groups, limit=20),
                )
            except ValueError as exc:
                raise ConfigError(
                    "Неизвестный разрез. Выберите детектор, профиль, версию, рынок или TF кнопками"
                ) from exc
            token = uuid.uuid4().hex[:12]
            self.statistics = {"token": token, "at": time.time(), "command": command, "data": data}
            await self.analytics_page(update, token, 0)
        elif command == "digest" and not raw:
            enabled = self.c.digest.preference()["enabled"]
            await self.c.send(
                update,
                f"📋 Дайджест: {'включён' if enabled else 'выключен'}\n"
                f"Интервал: {s.digest_interval_min} минут.\n\n"
                "• Независим от мгновенных уведомлений и заглушений.\n"
                "• После включения — следующий полный интервал UTC.",
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
            state = agent_state(d)
            await self.c.send(
                update,
                f"📨 Доставка контекста агенту: {state}\nОжидают: {amount(d.get('pending'))}; доставлены: {amount(d.get('delivered'))}.\n\n"
                "Ручной радар работает независимо. Ключи и подключение настраиваются в проекте агента. "
                "Просмотр не выполняет доставку повторно.",
                keyboard(
                    (
                        [
                            [
                                (
                                    "Выключить доставку" if d.get("enabled") else "Включить доставку",
                                    "ui:agenttoggle:" + ("off" if d.get("enabled") else "on"),
                                )
                            ]
                        ]
                        if d.get("configuration") == "READY"
                        else []
                    )
                    + [[("Подробности в отчёте", "ui:diag:file")]],
                    back="settings",
                ),
            )
        elif command == "mutes":
            offset = int(raw) if raw.isdecimal() else 0
            rows = self.c.store.db.execute(
                "SELECT symbol,mute_until FROM mutes WHERE mute_until>? ORDER BY symbol LIMIT 11 OFFSET ?",
                (time.time(), offset),
            ).fetchall()
            controls = [[("Снять: " + row[0], "ui:unmute:" + row[0])] for row in rows[:10]]
            text = "🔕 Заглушённые монеты\n\n" + (
                "\n".join(f"• {row[0]} до {when(row[1] * 1000)}" for row in rows[:10])
                or "Нет активных заглушений на этой странице."
            )
            text += "\n\nЗаглушение влияет только на Telegram. Расчёт и история продолжаются."
            pages = []
            if offset:
                pages.append(("← Предыдущие", f"ui:mutespage:{max(offset - 10, 0)}"))
            if len(rows) > 10:
                pages.append(("Следующие →", f"ui:mutespage:{offset + 10}"))
            if pages:
                controls.append(pages)
            await self.c.send(update, text, keyboard(controls, back="alerts"))
        elif command in ("enrichment", "derivatives") and not raw:
            from .render import enrichment_keyboard

            await self.c.send(
                update,
                f"🧩 Дополнительный контекст деривативов: {'включён' if s.enrichment_enabled else 'выключен'}\n\n"
                "Добавляет доступные OI (открытый интерес), funding и basis для соответствующей Spot-монеты. "
                "Это не переключатель основного Perpetual радара. Отсутствие контекста не блокирует детекторы.",
                enrichment_keyboard(snapshot),
            )
        else:
            return False
        return True

    async def callback(self, update: Update, parts: list[str]) -> None:
        from .render import TERMS

        action = parts[0] if parts else ""
        snapshot = self.c.store.snapshot()
        if action == "cancel" and self.c.store.get_runtime(INPUT):
            value = self.c.store.get_runtime(INPUT)
            self.cancel()
            await self.c.send(
                update, "Ввод отменён. Настройки сохранены.", keyboard([], back=self.input_back(value))
            )
        elif action in ("cancel", "home"):
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
        elif action == "composite":
            result = await self.c.market.snapshot(snapshot.settings)
            if self.c.store.snapshot().revision != snapshot.revision:
                raise RevisionConflict("Настройки изменились во время расчёта")
            lines = [
                "Разбор composite — не общий статус пяти детекторов",
                "До 6 оценок с наибольшим всплеском оборота; недостижение порога не является ошибкой источника.",
            ]
            for key, q in sorted(
                result.quotes.items(), key=lambda item: item[1]["turnover_spike_ratio"], reverse=True
            )[:6]:
                lines.append(
                    f"{key}\nЦена: {amount(q['price_change_tf'])}% · оборот ×{amount(q['turnover_spike_ratio'])}\n"
                    f"Оборот 24ч: {amount(q['turnover24h'], compact=True)} USDT · "
                    + ("условия выполнены" if q["meets_signal"] else "не все пороги достигнуты")
                    + " · "
                    + q.get("freshness", "свежесть неизвестна")
                )
            if not result.quotes:
                lines.append("Для выбранного охвата готовых оценок нет.")
            await self.c.send(update, "\n\n".join(lines), keyboard([], back="status"))
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
                "Это пример/карточка и рынок ввода монет без префикса. Основной радар обрабатывает оба рынка и все 4 TF.",
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
                settings = snapshot.settings.with_profile(parts[1])
                self.remember_custom()
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
        elif action == "historyinfo":
            await self.c.send(
                update,
                self.c.history_notice()
                + "\nСохранённое наблюдение не означает отправленное уведомление. Полная доступная выборка — через export.",
                keyboard([], back="history"),
            )
        elif action == "returnhistory" and len(parts) == 2:
            context = self.history_contexts.get(parts[1], {})
            await self.history(
                update, context.get("cursor", "") if time.time() - context.get("at", 0) < 900 else ""
            )
        elif action in ("open", "result") and len(parts) == 3:
            await self.show_event(update, parts[1], parts[2], result=action == "result")
        elif action in ("eventmore", "eventmute") and len(parts) in (2, 3):
            from .render import alert_keyboard

            event = self.c.history.get("event-head:" + str(uuid.UUID(parts[1])))
            if event is None:
                await self.c.send(update, "Событие уже недоступно.", keyboard([], back="history"))
            else:
                token = parts[2] if len(parts) == 3 else ""
                back = "open:" + parts[1] + ":" + token if token else None
                original = alert_keyboard(event["symbol"], event["market_type"]).inline_keyboard
                copied = [
                    [(b.text, b.callback_data) for b in row if isinstance(b.callback_data, str)]
                    for row in original
                ]
                rows: list[list[tuple[str, str]]] = (
                    [copied[1]]
                    if action == "eventmute"
                    else [
                        [("Монета", copied[2][0][1])],
                        [("Полные исходные данные", "ui:eventfile:" + parts[1])],
                        [("Пересчитать сравнение", "recompare:" + parts[1])],
                    ]
                )
                if not back:
                    rows.append([("← Событие", "event:" + parts[1])])
                await self.c.send(
                    update,
                    "Заглушение уведомлений" if action == "eventmute" else "Дополнительные действия события",
                    keyboard(rows, back=back),
                )
        elif action == "listpage" and len(parts) == 2 and parts[1].isdecimal():
            await self.command(update, "watchlist", parts[1])
        elif action == "mutespage" and len(parts) == 2 and parts[1].isdecimal():
            await self.command(update, "mutes", parts[1])
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
        elif action == "agenttoggle" and len(parts) == 2 and parts[1] in ("on", "off"):
            await self.c.command(update, "agent", parts[1])
        elif action == "groups" and len(parts) == 3 and parts[1] in ("analytics", "compare"):
            await self.c.command(update, parts[1], parts[2])
        elif action == "stats" and len(parts) == 3 and parts[2].isdecimal():
            await self.analytics_page(update, parts[1], int(parts[2]))
        elif action == "statsfile" and len(parts) == 2:
            if self.statistics.get("token") != parts[1] or time.time() - self.statistics.get("at", 0) > 300:
                raise ConfigError("Снимок статистики устарел")
            await self.document(update, self.statistics["data"], "cryptowatcher-statistics.json")
        elif action == "unmute" and len(parts) == 2:
            await self.c.command(update, "unmute", parts[1])
        elif action == "eventfile" and len(parts) == 2:
            try:
                event_id = str(uuid.UUID(parts[1]))
            except ValueError as exc:
                raise ConfigError("Некорректная ссылка на событие") from exc
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
                    diagnostic_section(data, "queues"),
                    keyboard([], back="diagnostics"),
                )
            elif parts[1] in ("ingestion", "storage"):
                await self.c.send(
                    update,
                    diagnostic_section(data, parts[1]),
                    keyboard([[("Полный JSON файлом", "ui:diag:file")]], back="diagnostics"),
                )
            else:
                raise ConfigError("Неизвестный раздел диагностики")
        elif action == "commands":
            from .bot import COMMANDS, MANUAL_FIELDS

            usage = {
                "setlist": "spot/BTC,linear/ETH",
                "profile": "normal|conservative|sensitive",
                "set": "price_1m 2.5",
                "mute": "spot/BTC 60",
                "unmute": "spot/BTC (или all)",
                "event": "EVENT_ID",
                "outcomes": "[EVENT_ID]",
                "horizons": "5,15,30,60",
                "analytics": "[detector,market,timeframe]",
                "compare": "[profile,version]",
                "recompare": "EVENT_ID",
                "digest": "on [минуты] | off",
                "enrichment": "on|off",
                "derivatives": "spot/BTC",
                "agent": "status|on|off|retry; replay EVENT_ID",
                "alerts": "on|off",
                "cancel": "",
            }

            await self.c.send(
                update,
                "Дополнительные команды\n"
                + "\n".join(
                    f"/{name} {usage.get(name, '')} — {label}"
                    for name, label in COMMANDS.items()
                    if not name.startswith("reset")
                )
                + "\nПоля /set: "
                + ", ".join(sorted(MANUAL_FIELDS))
                + "\nEVENT_ID берётся из подробностей события. В обычном меню внутренние ID вводить не нужно. Команды изменения требуют аргументов; обычные действия доступны кнопками.",
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
