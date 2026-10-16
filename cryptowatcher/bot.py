"""Owner-only Telegram entrypoints, revision-safe polling and diagnostic views."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

from telegram import BotCommand, BotCommandScopeChat, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .agent_delivery import AgentDelivery
from .analytics import ComparisonWorker, cohorts
from .analytics_recovery import AnalyticsRecovery
from .config import (
    ConfigError,
    Infrastructure,
    authorized,
    instrument_key,
    normalize_pair,
    number,
)
from .detector_config import FAMILIES, PARAMETERS
from .digest import Digest
from .enrichment_runtime import Enrichment
from .events import EventEngine, History
from .ingestion import BybitRadar
from .market import BybitREST, MarketError
from .outcomes import OutcomeWorker, Retention
from .render import (
    SAMPLE_SYMBOL,
    TERMS,
    alert_keyboard,
    detectors_keyboard,
    digest_keyboard,
    enrichment_keyboard,
    enrichment_text,
    event_keyboard,
    event_text,
    history_keyboard,
    menu,
    notification_text,
    outcome_text,
    profiles_keyboard,
    quote_text,
    sample_alert_text,
    settings_keyboard,
    settings_text,
    status_text,
)
from .source_context import context as public_context
from .storage import RevisionConflict, Store
from .telegram_delivery import TelegramDelivery

logger = logging.getLogger(__name__)
COMMANDS = {
    "start": "Открыть control deck",
    "help": "Помощь",
    "status": "Рынок и состояние данных",
    "settings": "Активные настройки",
    "terms": "Метрики и объяснения",
    "watchlist": "Мой список",
    "setlist": "Заменить список",
    "addcoin": "Добавить монету",
    "removecoin": "Удалить монету",
    "clearlist": "Очистить список",
    "radar_top": "Широкий universe",
    "radar_custom": "Мой список",
    "sample_alert": "Пример alert",
    "mute": "Отключить alerts монеты",
    "unmute": "Включить alerts монеты",
    "alerts": "Включить/выключить alerts",
    "set": "Ручная настройка",
    "diagnostics": "Диагностика",
    "agent": "Доставка рыночного контекста",
    "profiles": "Sensitivity profiles",
    "profile": "Выбрать profile",
    "detectors": "Telegram detectors",
    "history": "История событий",
    "event": "Сохранённое событие",
    "outcomes": "Последующее движение события",
    "horizons": "Настроить outcome horizons",
    "analytics": "Статистика detectors",
    "compare": "Сравнение profiles и versions",
    "recompare": "Пересчитать prior comparators события",
    "enrichment": "Включить derivatives context",
    "derivatives": "Derivatives context Spot монеты",
    "digest": "Управление дайджестом",
    "resetbase": "Устаревшее действие",
    "resetallbase": "Устаревшее действие",
}
MENU_ACTIONS = {
    "📊 Статус": "status",
    "⚙️ Настройки": "settings",
    "Радар: Top 100": "radar_top",
    "Радар: Universe": "radar_top",
    "Радар: Мой список": "radar_custom",
    "Список": "watchlist",
    "Добавить монету": "addcoin",
    "Удалить монету": "removecoin",
    "Очистить список": "clearlist",
    "Термины": "terms",
    "🔔 Alerts": "alerts",
    "🔕 Mutes": "mutes",
    "Диагностика": "diagnostics",
    "Detectors": "detectors",
    "Profiles": "profiles",
    "История": "history",
    "Outcomes": "outcomes",
    "Статистика": "analytics",
    "Digest": "digest",
    "Derivatives": "derivatives",
    "Сравнение": "compare",
    "Trading agent": "agent",
}
MANUAL_FIELDS = {
    "watchlist_multiplier",
    "signal_timeframe",
    "radar_poll_sec",
    "price_move_min",
    "turnover_spike_min",
    "liquidity_floor_24h",
    "sma_periods",
    "digest_interval_min",
    "detail_retention_days",
    "candle_retention_days",
}
MANUAL_FIELDS.update(PARAMETERS)


class Controller:
    def __init__(self, infrastructure: Infrastructure, store: Store, market: BybitREST) -> None:
        self.infrastructure = infrastructure
        self.store = store
        self.market = market
        self.history = History(store)
        self.agent = AgentDelivery(store, getattr(market, "client", None))
        self.recovery = AnalyticsRecovery(store, market) if isinstance(market, BybitRadar) else None
        self.outcomes = OutcomeWorker(
            store, self.recovery.request if self.recovery else None, on_outcome=self.agent.outcome
        )
        self.retention = Retention(store)
        self.digest = Digest(store, infrastructure.delivery_chat_id)
        self.analytics_lock = asyncio.Lock()
        self.repack_lock = asyncio.Lock()
        self.recovery_lock = asyncio.Lock()
        self.enrichment = Enrichment(store, market) if isinstance(market, BybitRadar) else None
        self.engine = (
            EventEngine(
                store,
                source_revision=infrastructure.source_revision,
                on_event=self.admit_event,
                on_episode=self.agent.episode_closed,
            )
            if isinstance(market, BybitRadar)
            else None
        )
        self.delivery = TelegramDelivery(
            store,
            infrastructure.delivery_chat_id,
            notification_text,
            event_keyboard,
            self.engine.reconcile_notifications if self.engine else None,
        )
        if isinstance(market, BybitRadar) and self.engine is not None:
            engine = self.engine
            market.on_observation = lambda stream, candles, liquidity, tick, now, live: engine.observe(
                stream,
                candles,
                liquidity,
                tick,
                now,
                live=live,
                source_context=public_context(store, stream.rsplit("/", 1)[0], now),
                enrichment=self.enrichment_context(stream.rsplit("/", 1)[0], candles[-1].end),
            )

    def admit_event(self, event: dict[str, Any]) -> None:
        self.outcomes.admit(event)
        self.agent.admit(event)

    def enrichment_context(self, key: str, cutoff: int) -> dict[str, Any]:
        if self.enrichment is None:
            return {"state": "N/A", "reason": "REALTIME_RADAR_REQUIRED", "detector_input": False}
        try:
            return self.enrichment.context(key, cutoff)
        except Exception as exc:
            return {"state": "INVALID", "reason": type(exc).__name__, "detector_input": False}

    async def send(self, update: Update, text: str, markup: Any = None) -> None:
        message = update.effective_message
        if message is None:
            return
        chunks = [text[i : i + 1800] for i in range(0, len(text), 1800)] or [""]
        for index, chunk in enumerate(chunks):
            await message.reply_text(chunk, reply_markup=markup if index == len(chunks) - 1 else None)

    def change(self, **values: Any) -> None:
        snapshot = self.store.snapshot()
        self.store.update(snapshot.revision, snapshot.settings.changed(**values))
        self.store.put_runtime("last_poll_ts", 0)

    def history_notice(self) -> str:
        if not self.store.bounded:
            return ""
        from datetime import datetime, timezone

        generation = self.store.get_runtime("history_generation", {})
        start = generation.get("started_ms")
        since = (
            datetime.fromtimestamp(start / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            if start
            else "UNSET"
        )
        return (
            f"Период новой истории: с {since}. Старая тестовая история недоступна.\n"
            f"Подробности: {self.store.snapshot().settings.detail_retention_days} дней; quality summaries: 12 месяцев. "
            "Доступная глубина и N ограничены накопленными данными.\n"
        )

    def diagnostics(self) -> dict[str, Any]:
        return {
            "storage": self.store.get_runtime("storage_report", {"state": "PENDING"}),
            "source_revision": self.infrastructure.source_revision,
            "configuration_revision": self.store.snapshot().revision,
            "telegram_alerts_enabled": self.store.enabled(),
            "last_poll": self.store.get_runtime("last_poll_status", {"state": "PENDING"}),
            "telegram_delivery": self.store.get_runtime("telegram_delivery", {"state": "PENDING"}),
            "ingestion": self.store.get_runtime("ingestion", {"state": "PENDING"}),
            "radar": self.history.diagnostics(),
            "outcomes": self.outcomes.diagnostics(),
            "analytics": self.store.get_runtime("analytics", {"state": "PENDING"}),
            "analytics_recovery": self.recovery.diagnostics() if self.recovery else {"state": "N/A"},
            "digest": self.digest.diagnostics(),
            "agent_delivery": self.agent.diagnostics(),
            "enrichment": self.enrichment.diagnostics() if self.enrichment else {"state": "N/A"},
            "capabilities": {
                "composite": True,
                "websocket": isinstance(self.market, BybitRadar),
                "detectors": list(FAMILIES) if self.engine else ["composite"],
                "episodes": self.engine is not None,
                "outcomes": True,
                "derivatives_enrichment": self.enrichment is not None,
                "agent_delivery": False,
            },
        }

    async def handle(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not authorized(update, self.infrastructure):
            if update.callback_query:
                await update.callback_query.answer("Нет доступа", show_alert=True)
            return
        try:
            if update.callback_query:
                await self.callback(update, context)
            elif update.effective_message and update.effective_message.text:
                text = update.effective_message.text.strip()
                if text.startswith("/"):
                    parts = text.split(maxsplit=1)
                    command = parts[0][1:].split("@", 1)[0].lower()
                    raw = parts[1] if len(parts) > 1 else ""
                else:
                    command, raw = MENU_ACTIONS.get(text, "input"), text
                await self.command(update, command, raw)
        except (ConfigError, RevisionConflict) as exc:
            await self.send(update, f"Настройка отклонена: {exc}. Активные значения сохранены.")
        except (MarketError, TelegramError):
            await self.send(update, "Данные/доставка временно недоступны. Подробнее в диагностике.")

    async def command(self, update: Update, command: str, raw: str = "") -> None:
        if command == "start":
            self.store.set_enabled(True)
            await self.send(update, "Control deck владельца", menu())
            command = "settings"
        snapshot = self.store.snapshot()
        if command == "settings":
            await self.send(update, settings_text(snapshot), settings_keyboard(snapshot))
        elif command == "help":
            await self.send(
                update,
                "Используйте меню. Ручной параметр: /set price_move_min 2.5\nСписок: /setlist BTC,ETH\nMute: /mute BTC 60 (минуты); /unmute BTC\n/alerts on или /alerts off\nРучные поля: "
                + ", ".join(sorted(MANUAL_FIELDS)),
                menu(),
            )
        elif command == "terms":
            await self.send(update, TERMS, menu())
        elif command == "watchlist":
            await self.send(
                update,
                "Мой список (market/symbol):\n"
                + (", ".join(snapshot.settings.instruments()) or "Список пуст"),
            )
        elif command in ("addcoin", "removecoin"):
            self.store.put_runtime("pending_action", "add" if command == "addcoin" else "remove")
            await self.send(
                update,
                "Отправьте market/symbol через запятую: spot/BTC,linear/ETH. Без market — выбранный рынок карточки.",
            )
        elif command in ("setlist", "input"):
            action = "set" if command == "setlist" else self.store.get_runtime("pending_action", "")
            if action not in ("add", "remove", "set"):
                await self.send(update, "Откройте действие в меню или /help", menu())
                return
            parts = [part for part in raw.replace("\n", ",").replace(" ", ",").split(",") if part]
            if not parts:
                raise ConfigError("Invalid custom list")
            pairs = tuple(
                dict.fromkeys(instrument_key(part, snapshot.settings.bybit_category) for part in parts)
            )
            if action != "remove" and isinstance(self.market, BybitRadar):
                await self.market.validate_instruments(pairs)
            current_pairs = snapshot.settings.instruments()
            next_pairs = (
                pairs
                if action == "set"
                else tuple(dict.fromkeys((*current_pairs, *pairs)))
                if action == "add"
                else tuple(pair for pair in current_pairs if pair not in pairs)
            )
            values: dict[str, Any] = {
                "watchlist": next_pairs,
                "custom_pairs": tuple(
                    p.split("/")[1]
                    for p in next_pairs
                    if p.startswith(snapshot.settings.bybit_category + "/")
                ),
            }
            if action == "set":
                values["alert_universe_mode"] = "custom"
            self.store.update(snapshot.revision, snapshot.settings.changed(**values))
            self.store.put_runtime("last_poll_ts", 0)
            self.store.put_runtime("pending_action", "")
            await self.send(update, "Список сохранён:\n" + (", ".join(next_pairs) or "Список пуст"))
        elif command == "clearlist":
            self.change(custom_pairs=(), watchlist=())
            self.store.put_runtime("pending_action", "")
            await self.send(update, "Пользовательский список очищен.")
        elif command in ("radar_top", "radar_custom"):
            self.change(alert_universe_mode="top" if command == "radar_top" else "custom")
            await self.send(update, settings_text(self.store.snapshot()))
        elif command == "set":
            parts = raw.split()
            if len(parts) != 2 or parts[0] not in MANUAL_FIELDS:
                await self.send(
                    update, "Формат: /set price_move_min 2.5. Поля: " + ", ".join(sorted(MANUAL_FIELDS))
                )
                return
            if parts[0] in PARAMETERS:
                self.store.update(snapshot.revision, snapshot.settings.with_parameter(parts[0], parts[1]))
                self.store.put_runtime("last_poll_ts", 0)
            else:
                self.change(**{parts[0]: parts[1]})
            await self.send(update, settings_text(self.store.snapshot()))
        elif command in ("mute", "unmute"):
            parts = raw.split()
            if not parts or len(parts) > 2:
                raise ConfigError("Invalid mute command")
            if command == "unmute" and parts == ["all"]:
                with self.store.db:
                    self.store.db.execute("DELETE FROM mutes")
                await self.send(update, "Все mutes сняты.")
                return
            symbol = instrument_key(parts[0], snapshot.settings.bybit_category)
            duration = number(parts[1], "mute minutes") * 60 if len(parts) == 2 else 3600
            self.store.mute(symbol, 0 if command == "unmute" else time.time() + duration)
            await self.send(update, "Mute сохранён." if command == "mute" else "Mute снят.")
        elif command == "mutes":
            rows = self.store.db.execute(
                "SELECT symbol FROM mutes WHERE mute_until>? ORDER BY symbol", (time.time(),)
            ).fetchall()
            await self.send(update, "Активные mutes:\n" + (", ".join(row[0] for row in rows) or "Нет"))
        elif command == "alerts":
            if raw and raw.lower() not in ("on", "off"):
                raise ConfigError("Invalid alerts preference")
            enabled = raw.lower() == "on" if raw else not self.store.enabled()
            self.store.set_enabled(enabled)
            if enabled:
                self.delivery.resume()
            await self.send(update, f"Alerts {'ON' if enabled else 'OFF'}. Расчёт рынка продолжается.")
        elif command in ("resetbase", "resetallbase"):
            await self.send(
                update,
                "Reset baseline больше не используется. Composite считает движение закрытой свечи; настройки доступны в меню.",
            )
        elif command == "diagnostics":
            from .analytics_worker import read_snapshot
            from .diagnostics import snapshot as diagnostic_snapshot

            result = await asyncio.to_thread(
                read_snapshot, self.store.path, self.store.owner_id, diagnostic_snapshot
            )
            await self.send(update, json.dumps(result, ensure_ascii=False, indent=2))
        elif command == "agent":
            parts = raw.strip().split()
            action = parts[0].lower() if parts else "status"
            now = self.market.server_now() if isinstance(self.market, BybitRadar) else 0
            if action in ("on", "off") and len(parts) == 1:
                self.agent.enable(action == "on")
            elif action == "retry" and len(parts) == 1 and now > 0:
                self.agent.retry(now)
            elif action == "replay" and len(parts) == 2 and now > 0:
                self.agent.replay(parts[1], now)
            elif action != "status" or len(parts) > 1:
                raise ConfigError("Используйте /agent status|on|off|retry или /agent replay EVENT_ID")
            from .analytics_worker import read_snapshot
            from .diagnostics import snapshot as diagnostic_snapshot

            result = await asyncio.to_thread(
                read_snapshot, self.store.path, self.store.owner_id, diagnostic_snapshot
            )
            await self.send(update, json.dumps(result["agent_delivery"], ensure_ascii=False, indent=2))
        elif command in ("profiles", "profile"):
            if command == "profile" and raw:
                self.store.update(snapshot.revision, snapshot.settings.with_profile(raw.strip().lower()))
                self.store.put_runtime("last_poll_ts", 0)
            await self.send(update, settings_text(self.store.snapshot()), profiles_keyboard())
        elif command == "detectors":
            await self.send(
                update,
                "Все пять families сохраняют history. Кнопки выбирают Telegram alerts; новые families изначально silent.\n"
                + settings_text(snapshot),
                detectors_keyboard(snapshot),
            )
        elif command == "history":
            events = self.history.events(12)
            await self.send(
                update,
                self.history_notice()
                + (
                    "Последние сохранённые события, включая historical/suppressed и muted observations.\n"
                    + "\n".join(
                        f"{e['symbol']} · {e['market_type']} · {e['detector_id']} · {e['timeframe']} · {e['direction']} · strength {e['strength']:.2f}"
                        for e in events
                    )
                    if events
                    else "История пока пуста."
                ),
                history_keyboard(events),
            )
        elif command == "event":
            try:
                event_id = str(uuid.UUID(raw.strip()))
            except ValueError as exc:
                raise ConfigError("Invalid event ID") from exc
            event = self.history.get("event-head:" + event_id)
            if event:
                now_ms = (
                    self.market.server_now()
                    if isinstance(self.market, BybitRadar)
                    else int(time.time() * 1000)
                )
                await self.send(update, event_text(event, now_ms), event_keyboard(event))
            else:
                await self.send(update, self.history_notice() + "Событие не найдено.")
        elif command == "horizons":
            if raw:
                parts = raw.replace(",", " ").split()
                if not parts or any(not part.isdecimal() for part in parts):
                    raise ConfigError("Horizons: положительные целые минуты через запятую")
                self.change(outcome_horizons=tuple(int(part) for part in parts))
            await self.send(
                update,
                "Outcome horizons, min: "
                + ", ".join(map(str, self.store.snapshot().settings.outcome_horizons))
                + "\n/horizons 5,15,30,60. Изменение применяется к новым событиям; старые reference/settings сохраняются.",
            )
        elif command == "outcomes":
            if raw:
                try:
                    event_id = str(uuid.UUID(raw.strip()))
                except ValueError as exc:
                    raise ConfigError("Invalid event ID") from exc
                events = [self.history.get("event-head:" + event_id)]
            else:
                events = self.history.events(5)
            if not events or events[0] is None:
                await self.send(update, self.history_notice() + "История пока пуста или событие не найдено.")
            else:
                for event in events:
                    from .config import OwnerSettings
                    from .events import identity
                    from .outcomes import METHOD

                    horizons = OwnerSettings.from_payload(event["settings_snapshot"]).outcome_horizons
                    outcomes = [
                        self.history.get(
                            "outcome-head:" + identity(f"outcome/{event['event_id']}/{h}/{METHOD}")
                        )
                        for h in horizons
                    ]
                    await self.send(update, outcome_text(event, outcomes, horizons), event_keyboard(event))
        elif command == "enrichment":
            if raw:
                if raw.lower() not in ("on", "off"):
                    raise ConfigError("Формат /enrichment on или /enrichment off")
                self.change(enrichment_enabled=raw.lower() == "on")
            await self.send(
                update,
                f"Optional derivatives context: {'ON' if self.store.snapshot().settings.enrichment_enabled else 'OFF'}\n"
                "Ticker1min · OI comparison5min · exact asset identity. При gaps/ошибках Spot продолжает работать.\n"
                "/enrichment on · /enrichment off · /derivatives spot/BTC\n"
                + settings_text(self.store.snapshot()),
                enrichment_keyboard(self.store.snapshot()),
            )
        elif command == "derivatives":
            key = instrument_key(raw or "spot/BTC")
            now = self.market.server_now() if isinstance(self.market, BybitRadar) else int(time.time() * 1000)
            await self.send(
                update,
                enrichment_text(self.enrichment_context(key, now), now)
                + "\n/enrichment on · /enrichment off · /derivatives spot/BTC. Текущий snapshot, отдельно от event history.",
                enrichment_keyboard(self.store.snapshot()),
            )
        elif command == "recompare":
            try:
                event_id = str(uuid.UUID(raw))
                now = (
                    self.market.server_now()
                    if isinstance(self.market, BybitRadar)
                    else int(time.time() * 1000)
                )
                if now <= 0:
                    raise ValueError("Clock pending")
                state = ComparisonWorker(self.store).requeue(event_id, now)
            except ValueError as exc:
                raise ConfigError(
                    "Формат /recompare event_uuid; событие должно быть сохранено и clock доступен"
                ) from exc
            await self.send(
                update,
                f"Comparator {state}. Frozen event/settings/reference и предыдущие revisions сохранены. Только prior windows; thresholds не меняются.",
            )
        elif command in ("analytics", "compare"):
            groups = (
                tuple(raw.replace(",", " ").split())
                if raw
                else ("detector", "profile", "version", "settings", "market", "timeframe", "direction")
            )
            try:
                from .analytics_worker import read_snapshot

                statistics = await asyncio.to_thread(
                    read_snapshot,
                    self.store.path,
                    self.store.owner_id,
                    lambda store: cohorts(store, groups, limit=20),
                )
            except ValueError as exc:
                raise ConfigError(
                    "Groups: detector/profile/version/settings/market/timeframe/instrument/universe/direction/strength"
                ) from exc
            lines = [
                "Статистика detectors · return %, описательная оценка информативности",
                f"Неполных outcomes: {statistics['incomplete_outcomes']}. Horizons считаются отдельно.",
            ]
            for row in statistics["cohorts"]:
                lines.append(
                    f"{row['group']}\nN={row['sample_size']} · mean {row['mean']:.3f}% · median {row['median']:.3f}% · p10/p90 {row['p10']:.3f}/{row['p90']:.3f}%\nno-signal N={row['comparator_sample_size']} · mean={row['comparator_mean']} · {row['classifications']}\nFrozen noise: {row['noise_min_pct']}…{row['noise_max_pct']}% · N={row['noise_sample_size']}"
                )
            if statistics["truncated"]:
                lines.append("Показаны первые20 cohorts. Полная выборка доступна через CLI export CSV.")
            lines.append(
                "Малое N/неполная история ограничивают выводы; события могут быть зависимы. Это не доходность стратегии и не рекомендация."
            )
            if command == "compare":
                lines += [
                    "Сравниваются только реально сохранённые profiles/settings. Автоматического изменения thresholds нет.",
                    "/recompare event_uuid — явный новый расчёт comparators при новых исходных данных. Предыдущие revisions сохраняются.",
                    "Текущие settings:\n" + settings_text(self.store.snapshot()),
                ]
            await self.send(update, "\n".join(lines))
        elif command == "digest":
            now = (
                (self.market.server_now() or int(time.time() * 1000))
                if isinstance(self.market, BybitRadar)
                else int(time.time() * 1000)
            )
            parts = raw.lower().split()
            if parts:
                if parts[0] not in ("on", "off") or len(parts) > 2:
                    raise ConfigError("Формат /digest on [interval_min] или /digest off")
                if len(parts) == 2:
                    self.change(digest_interval_min=parts[1])
                self.digest.configure(parts[0] == "on", now)
            enabled = self.digest.preference()["enabled"]
            await self.send(
                update,
                f"Digest {'ON' if enabled else 'OFF'} · interval {self.store.snapshot().settings.digest_interval_min} min\nНезависим от alerts/mutes. После включения — следующий полный UTC interval. /digest on 60 · /digest off",
                digest_keyboard(enabled),
            )
        elif command == "sample_alert":
            await self.send(
                update,
                sample_alert_text(snapshot),
                alert_keyboard(SAMPLE_SYMBOL, snapshot.settings.bybit_category),
            )
        elif command == "status":
            result = await self.market.snapshot(snapshot.settings)
            current = self.store.snapshot()
            if current.revision != snapshot.revision:
                await self.send(
                    update,
                    "Настройки изменились во время запроса; данные прежней revision не применены.\n"
                    + settings_text(current),
                )
                return
            await self.send(update, status_text(snapshot, result, self.store.enabled()))

    async def callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        await query.answer()
        parts = str(query.data or "").split(":")
        if parts[0] == "deck" and len(parts) == 2 and parts[1] in ("profiles", "detectors"):
            await self.command(update, parts[1])
        elif parts[0] in ("profile", "event", "outcomes", "derivatives", "recompare") and len(parts) == 2:
            await self.command(update, parts[0], parts[1])
        elif parts[0] == "enrichment" and len(parts) == 2 and parts[1] in ("on", "off", "status"):
            await self.command(update, "enrichment", "" if parts[1] == "status" else parts[1])
        elif parts[0] == "digest" and len(parts) == 2 and parts[1] in ("on", "off", "status"):
            await self.command(update, "digest", "" if parts[1] == "status" else parts[1])
        elif parts[0] == "detector" and len(parts) == 3:
            if parts[1] not in FAMILIES or parts[2] not in ("on", "off"):
                raise ConfigError("Invalid detector selection")
            snapshot = self.store.snapshot()
            selected = set(snapshot.settings.alert_detectors)
            selected.add(parts[1]) if parts[2] == "on" else selected.discard(parts[1])
            self.store.update(
                snapshot.revision,
                snapshot.settings.changed(alert_detectors=tuple(f for f in FAMILIES if f in selected)),
            )
            await self.command(update, "detectors")
        elif parts[0] == "st":
            action = parts[1] if len(parts) > 1 else ""
            if action == "noop":
                return
            if action in ("src", "core", "cmctop", "tf", "thr", "top"):
                await self.command(update, "resetbase")
                return
            if action == "help":
                await self.command(update, "terms")
                return
            if action == "manual":
                await self.command(update, "help")
                return
            if action == "status":
                await self.command(update, "status")
                return
            fields = {
                "rdr": "radar_poll_sec",
                "sgtf": "signal_timeframe",
                "pm": "price_move_min",
                "sp": "turnover_spike_min",
                "lq": "liquidity_floor_24h",
            }
            if action in fields and len(parts) == 3:
                self.change(**{fields[action]: parts[2]})
            elif action != "refresh":
                raise ConfigError("Unknown settings action")
            snapshot = self.store.snapshot()
            try:
                await query.edit_message_text(
                    settings_text(snapshot), reply_markup=settings_keyboard(snapshot)
                )
            except BadRequest as exc:
                if "message is not modified" not in str(exc).lower():
                    raise
        elif parts[0] == "al" and len(parts) >= 4:
            if parts[2] != "BYBIT":
                await self.command(update, "resetbase")
                return
            snapshot = self.store.snapshot()
            if parts[3] in ("spot", "linear") and len(parts) >= 5:
                category, symbol = parts[3], normalize_pair(parts[4])
                duration_index = 5
            else:
                # Preserve old callbacks: their original rendered market is the identity.
                text = str(getattr(getattr(query, "message", None), "text", ""))
                category = "linear" if "USDT Perpetual" in text else "spot" if "Spot USDT" in text else ""
                if not category:
                    await self.send(
                        update, "У этого старого сообщения не указан рынок. Откройте новый пример/карточку."
                    )
                    return
                symbol, duration_index = normalize_pair(parts[3]), 4
            identity = instrument_key(f"{category}/{symbol}")
            if parts[1] == "mute" and len(parts) == duration_index + 1:
                duration = number(parts[duration_index], "mute seconds")
                self.store.mute(identity, time.time() + duration)
                await self.send(update, "Mute сохранён.")
            elif parts[1] == "coin":
                card_settings = (
                    snapshot.settings.changed(bybit_category=category)
                    if isinstance(self.market, BybitRadar)
                    else snapshot.settings.changed(
                        alert_universe_mode="custom",
                        custom_pairs=(),
                        watchlist=(identity,),
                        bybit_category=category,
                    )
                )
                result = await self.market.snapshot(card_settings)
                quote = result.quotes.get(
                    f"{identity}/{snapshot.settings.signal_timeframe}", result.quotes.get(symbol)
                )
                if self.store.snapshot().revision != snapshot.revision:
                    await self.send(update, "Настройки изменились; повторите запрос.")
                elif quote:
                    await self.send(
                        update,
                        quote_text(symbol, quote, snapshot.settings.changed(bybit_category=category)),
                        alert_keyboard(symbol, category),
                    )
                else:
                    await self.send(update, "Данные инструмента неполны/недоступны.")
        elif parts[0] == "rb":
            await self.command(update, "resetbase")

    async def poll(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        now = time.time()
        snapshot = self.store.snapshot()
        last_poll = self.store.get_runtime("last_poll_ts", 0)
        if now - last_poll < snapshot.settings.radar_poll_sec:
            return
        self.store.put_runtime("last_poll_ts", now)
        try:
            result = await self.market.snapshot(snapshot.settings)
        except MarketError as exc:
            self.store.put_runtime("last_poll_status", {"state": "DEGRADED", "reason": str(exc), "at": now})
            return
        if self.store.snapshot().revision != snapshot.revision:
            self.store.put_runtime(
                "last_poll_status", {"state": "DISCARDED", "reason": "configuration_changed", "at": now}
            )
            return
        self.store.put_runtime(
            "last_poll_status",
            {
                "state": "WARMING"
                if isinstance(self.market, BybitRadar) and not self.market.delivery_ready(result)
                else "DEGRADED"
                if result.errors
                else "OK",
                "at": now,
                "available": len(result.quotes),
                "requested": result.requested,
                "errors": result.errors,
                "configuration_revision": snapshot.revision,
            },
        )
        if self.engine is not None:
            return  # Persisted observation producer and durable episode queue own live delivery.
        for item, quote in result.quotes.items():
            if self.store.snapshot().revision != snapshot.revision:
                break
            symbol = quote.get("symbol", item)
            category = quote.get("market", snapshot.settings.bybit_category)
            tf = quote["signal_timeframe"]
            identity = instrument_key(f"{category}/{symbol}")
            if (
                not quote["meets_signal"]
                or not self.store.enabled()
                or self.store.muted(identity, now)
                or (category == snapshot.settings.bybit_category and self.store.muted(symbol, now))
                or quote.get("live") is False
                or quote.get("freshness", "FRESH") != "FRESH"
                or (
                    isinstance(self.market, BybitRadar)
                    and not 0 <= self.market.server_now() - quote["candle_end_ms"] <= int(tf[:-1]) * 60_000
                )
            ):
                continue
            key = f"last_alert:{category}:{symbol}:{tf}"
            sent = self.store.get_runtime(key, {})
            legacy = (
                self.store.get_runtime("legacy_baselines", {}).get(f"BYBIT:{symbol}", {})
                if category == snapshot.settings.bybit_category
                else {}
            )
            last_alert = sent.get("at", legacy.get("last_alert", 0))
            if (
                now - last_alert < snapshot.settings.cooldown_min * 60
                or sent.get("candle_end_ms") == quote["candle_end_ms"]
            ):
                continue
            try:
                await context.bot.send_message(
                    self.infrastructure.delivery_chat_id,
                    quote_text(
                        symbol, quote, snapshot.settings.changed(bybit_category=category, signal_timeframe=tf)
                    )
                    + f"\nSettings revision: {snapshot.revision}",
                    reply_markup=alert_keyboard(symbol, category),
                )
            except TelegramError as exc:
                self.store.put_runtime(
                    "telegram_delivery",
                    {"state": "FAILED", "error_class": type(exc).__name__, "at": time.time()},
                )
                continue
            self.store.put_runtime(
                key,
                {
                    "at": time.time(),
                    "candle_end_ms": quote["candle_end_ms"],
                    "settings_revision": snapshot.revision,
                },
            )
            self.store.put_runtime("telegram_delivery", {"state": "DELIVERED", "at": time.time()})

    async def deliver(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.engine is None or not isinstance(self.market, BybitRadar):
            return
        now_ms = self.market.server_now()
        if now_ms <= 0:
            return  # Establish exchange clock after restart before deciding pending expiry.
        self.engine.tick(now_ms)
        await self.delivery.send_due(context.bot, now_ms, clock=self.market.server_now)
        self.digest.schedule(self.market.server_now())
        await self.digest.send_due(context.bot, self.market.server_now())

    async def analytics_job(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not isinstance(self.market, BybitRadar) or self.recovery is None:
            return
        now = self.market.server_now()
        if now <= 0:
            return
        async with self.analytics_lock:
            try:
                started = time.monotonic()
                processed_outcomes = await self.outcomes.async_work(self.market.server_now)
                processed_comparisons = await ComparisonWorker(self.store).async_work(self.market.server_now)
                self.retention.work(now)
                from .storage_meter import sample

                sample(self.store, now)
                self.store.put_runtime(
                    "analytics",
                    {
                        "state": "OK",
                        "at_ms": self.market.server_now(),
                        "processed_outcomes": processed_outcomes,
                        "processed_comparisons": processed_comparisons,
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                    },
                )
            except Exception as exc:
                self.store.put_runtime(
                    "analytics", {"state": "FAILED", "error_class": type(exc).__name__, "at_ms": now}
                )
                logger.error("Analytics worker failure: %s", type(exc).__name__)

    async def repack_job(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not isinstance(self.market, BybitRadar) or self.repack_lock.locked():
            return
        now = self.market.server_now()
        if now <= 0:
            return
        from .storage_repack import async_work

        async with self.repack_lock:
            try:
                await async_work(self.store, now)
                self.store.put_runtime("repack_worker", {"state": "OK", "at_ms": self.market.server_now()})
            except Exception as exc:
                self.store.put_runtime(
                    "repack_worker",
                    {"state": "FAILED", "error_class": type(exc).__name__, "at_ms": now},
                )
                logger.error("Repack worker failure: %s", type(exc).__name__)

    async def backfill_job(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.recovery is None or not isinstance(self.market, BybitRadar) or self.recovery_lock.locked():
            return
        now = self.market.server_now()
        if now <= 0:
            return
        async with self.recovery_lock:
            try:
                await self.recovery.work(now)
                self.store.put_runtime(
                    "analytics_recovery_worker", {"state": "OK", "at_ms": self.market.server_now()}
                )
            except Exception as exc:
                self.store.put_runtime(
                    "analytics_recovery_worker",
                    {"state": "FAILED", "error_class": type(exc).__name__, "at_ms": now},
                )
                logger.error("Backfill worker failure: %s", type(exc).__name__)

    async def enrichment_job(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.enrichment is None or not isinstance(self.market, BybitRadar):
            return
        try:
            await self.enrichment.work(self.market.server_now())
        except Exception as exc:
            self.store.put_runtime("enrichment", {"state": "FAILED", "error_class": type(exc).__name__})
            logger.error("Optional enrichment failure: %s", type(exc).__name__)

    async def agent_job(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not isinstance(self.market, BybitRadar):
            return
        now = self.market.server_now()
        if now <= 0:
            return
        try:
            await self.agent.work(now)
        except Exception as exc:
            self.agent._diagnostic("FAILED_" + type(exc).__name__, now)
            logger.error("Optional agent delivery failure: %s", type(exc).__name__)

    async def heartbeat(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.store.put_runtime(
            "heartbeat",
            {
                "at": time.time(),
                "revision": self.infrastructure.source_revision,
                "components_healthy": (
                    not isinstance(self.market, BybitRadar) or self.market.components_healthy()
                )
                and self.store.get_runtime("analytics", {}).get("state") != "FAILED",
            },
        )


def application(controller: Controller) -> Application[Any, Any, Any, Any, Any, Any]:
    async def initialize(app: Application[Any, Any, Any, Any, Any, Any]) -> None:
        identity = await app.bot.get_me()
        if identity.id != controller.infrastructure.expected_bot_id:
            raise ConfigError("Telegram bot identity mismatch")
        await app.bot.set_my_commands(
            [
                BotCommand(command, label)
                for command, label in COMMANDS.items()
                if not command.startswith("reset")
            ],
            scope=BotCommandScopeChat(controller.infrastructure.delivery_chat_id),
        )
        controller.store.put_runtime(
            "heartbeat", {"at": time.time(), "revision": controller.infrastructure.source_revision}
        )
        if isinstance(controller.market, BybitRadar):
            await controller.market.start()

    async def shutdown(app: Application[Any, Any, Any, Any, Any, Any]) -> None:
        await controller.market.close()

    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error("Telegram handler failure: %s", type(context.error).__name__)

    app = (
        Application.builder()
        .token(controller.infrastructure.bot_token)
        .post_init(initialize)
        .post_shutdown(shutdown)
        .build()
    )
    app.add_handler(CommandHandler(list(COMMANDS), controller.handle))
    app.add_handler(CallbackQueryHandler(controller.handle))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, controller.handle))
    app.add_error_handler(error_handler)
    if app.job_queue is None:
        raise ConfigError("Telegram job-queue dependency missing")
    app.job_queue.run_repeating(controller.poll, interval=controller.infrastructure.engine_interval, first=1)
    app.job_queue.run_repeating(controller.heartbeat, interval=20, first=1)
    app.job_queue.run_repeating(controller.deliver, interval=2, first=2)
    app.job_queue.run_repeating(controller.analytics_job, interval=5, first=5)
    app.job_queue.run_repeating(
        controller.repack_job, interval=1, first=3, job_kwargs={"max_instances": 1, "coalesce": True}
    )
    app.job_queue.run_repeating(
        controller.backfill_job, interval=1, first=4, job_kwargs={"max_instances": 1, "coalesce": True}
    )
    app.job_queue.run_repeating(controller.enrichment_job, interval=2, first=6)
    app.job_queue.run_repeating(controller.agent_job, interval=2, first=3)
    return app
