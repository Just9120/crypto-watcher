"""Owner-only Telegram entrypoints, revision-safe polling and diagnostic views."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from telegram import BotCommand, BotCommandScopeChat, InlineKeyboardMarkup, Update
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
from .analytics import ComparisonWorker
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
    alert_keyboard,
    enrichment_keyboard,
    enrichment_text,
    event_keyboard,
    menu,
    notification_text,
    outcome_text,
    profiles_keyboard,
    quote_text,
    sample_alert_text,
    settings_keyboard,
    settings_text,
)
from .source_context import context as public_context
from .storage import RevisionConflict, Store
from .telegram_delivery import TelegramDelivery
from .telegram_ui import ControlDeck
from .telegram_views import split_message, when

logger = logging.getLogger(__name__)
COMMANDS = {
    "start": "Открыть главное меню",
    "help": "Помощь",
    "status": "Рынок и состояние данных",
    "settings": "Активные настройки",
    "terms": "Метрики и объяснения",
    "watchlist": "Мой список",
    "setlist": "Заменить список",
    "addcoin": "Добавить монету",
    "removecoin": "Удалить монету",
    "clearlist": "Очистить список",
    "radar_top": "Весь подходящий рынок",
    "radar_custom": "Мой список",
    "sample_alert": "Пример уведомления",
    "mute": "Заглушить уведомления монеты",
    "unmute": "Снять заглушение монеты",
    "alerts": "Управление уведомлениями",
    "set": "Ручная настройка",
    "diagnostics": "Диагностика",
    "agent": "Доставка рыночного контекста",
    "profiles": "Профили чувствительности",
    "profile": "Применить профиль",
    "detectors": "Уведомления по детекторам",
    "history": "История событий",
    "event": "Сохранённое событие",
    "outcomes": "Последующее движение события",
    "horizons": "Сроки оценки результата",
    "analytics": "Статистика детекторов",
    "compare": "Сравнение профилей и версий",
    "recompare": "Пересчитать сравнение события",
    "enrichment": "Дополнительный контекст деривативов",
    "derivatives": "Контекст деривативов Spot-монеты",
    "digest": "Управление дайджестом",
    "cancel": "Отменить ввод",
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
    "📡 Радар": "radar",
    "🗂 История": "history",
    "📈 Аналитика": "analytics_home",
    "❔ Помощь": "help",
}
PUBLISHED_COMMANDS = (
    "start",
    "status",
    "watchlist",
    "history",
    "analytics",
    "settings",
    "alerts",
    "help",
    "diagnostics",
)
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
        store.put_runtime("runtime_instance", {"id": uuid.uuid4().hex, "started_ms": int(time.time() * 1000)})
        self.market = market
        self.history = History(store)
        self.ui = ControlDeck(self)
        self.agent = AgentDelivery(store, getattr(market, "client", None))
        self.recovery = AnalyticsRecovery(store, market) if isinstance(market, BybitRadar) else None
        self.outcomes = OutcomeWorker(
            store, self.recovery.request if self.recovery else None, on_outcome=self.agent.outcome
        )
        self.retention = Retention(store)
        self.digest = Digest(store, infrastructure.delivery_chat_id)
        self.analytics_lock = asyncio.Lock()
        self.retention_lock = asyncio.Lock()
        self.repack_lock = asyncio.Lock()
        from .shared_record_worker import Worker as SharedRecordWorker

        self.shared_records = SharedRecordWorker(store, scalar_units=True)
        self.recovery_lock = asyncio.Lock()
        self.maintenance_started_at = time.time()
        self.first_ready_poll = False
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
        chunks = split_message(text)
        query = update.callback_query
        if (
            query is not None
            and str(query.data or "").startswith("ui:")
            and not str(query.data).startswith(("ui:eventfile:", "ui:statsfile:", "ui:diag:file"))
            and isinstance(markup, InlineKeyboardMarkup)
            and len(chunks) == 1
        ):
            try:
                await query.edit_message_text(chunks[0], reply_markup=markup)
                return
            except BadRequest as exc:
                reason = str(exc).lower()
                if "message is not modified" in reason:
                    return
                if "message to edit not found" not in reason and "message can't be edited" not in reason:
                    raise
        for index, chunk in enumerate(chunks):
            await message.reply_text(chunk, reply_markup=markup if index == len(chunks) - 1 else None)

    def change(self, **values: Any) -> None:
        snapshot = self.store.snapshot()
        self.store.update(snapshot.revision, snapshot.settings.changed(**values))
        self.store.put_runtime("last_poll_ts", 0)

    def history_notice(self) -> str:
        if not self.store.bounded:
            return ""
        generation = self.store.get_runtime("history_generation", {})
        start = generation.get("started_ms")
        since = when(start)
        return (
            f"Период новой истории: с {since}. Старая тестовая история недоступна.\n"
            f"Подробности: {self.store.snapshot().settings.detail_retention_days} дней; сводные результаты: 12 месяцев. "
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
                self.ui.cancel()
                await self.callback(update, context)
            elif update.effective_message and update.effective_message.text:
                text = update.effective_message.text.strip()
                if text.startswith("/"):
                    parts = text.split(maxsplit=1)
                    command = parts[0][1:].split("@", 1)[0].lower()
                    raw = parts[1] if len(parts) > 1 else ""
                    if command != "input":
                        self.ui.cancel()
                elif text in MENU_ACTIONS:
                    command, raw = MENU_ACTIONS[text], ""
                    self.ui.cancel()
                else:
                    if await self.ui.input(update, text):
                        return
                    command, raw = "input", text
                await self.command(update, command, raw)
        except (ConfigError, RevisionConflict) as exc:
            await self.send(update, f"Действие не выполнено: {exc}. Активные значения сохранены.")
        except (MarketError, TelegramError):
            await self.send(update, "Данные/доставка временно недоступны. Подробнее в диагностике.")

    async def command(self, update: Update, command: str, raw: str = "") -> None:
        if await self.ui.command(update, command, raw):
            return
        snapshot = self.store.snapshot()
        if command in ("setlist", "input"):
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
                    (
                        p.split("/")[1]
                        for p in next_pairs
                        if p.startswith(snapshot.settings.bybit_category + "/")
                    )
                ),
            }
            if action == "set":
                values["alert_universe_mode"] = "custom"
            self.store.update(snapshot.revision, snapshot.settings.changed(**values))
            self.store.put_runtime("last_poll_ts", 0)
            self.store.put_runtime("pending_action", "")
            await self.send(update, "Список сохранён:\n" + (", ".join(next_pairs) or "Список пуст"))
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
                await self.send(update, "Все заглушения сняты.")
                return
            symbol = instrument_key(parts[0], snapshot.settings.bybit_category)
            duration = number(parts[1], "mute minutes") * 60 if len(parts) == 2 else 3600
            self.store.mute(symbol, 0 if command == "unmute" else time.time() + duration)
            await self.send(update, "Заглушение сохранено." if command == "mute" else "Заглушение снято.")
        elif command == "alerts":
            if raw and raw.lower() not in ("on", "off"):
                raise ConfigError("Invalid alerts preference")
            enabled = raw.lower() == "on" if raw else not self.store.enabled()
            self.store.set_enabled(enabled)
            if enabled:
                self.delivery.resume()
            await self.send(
                update,
                f"Мгновенные уведомления {('включены' if enabled else 'выключены')}. Расчёт рынка продолжается.",
            )
            await self.ui.command(update, "alerts", "")
        elif command in ("resetbase", "resetallbase"):
            await self.send(
                update,
                "Reset baseline больше не используется. Composite считает движение закрытой свечи; настройки доступны в меню.",
            )
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
            await self.ui.command(update, "agent", "status")
        elif command in ("profiles", "profile"):
            if command == "profile" and raw:
                selected = snapshot.settings.with_profile(raw.strip().lower())
                self.ui.remember_custom()
                self.store.update(snapshot.revision, selected)
                self.store.put_runtime("last_poll_ts", 0)
            await self.send(update, settings_text(self.store.snapshot()), profiles_keyboard())
        elif command == "horizons":
            if raw:
                parts = raw.replace(",", " ").split()
                if not parts or any(not part.isdecimal() for part in parts):
                    raise ConfigError("Horizons: положительные целые минуты через запятую")
                self.change(outcome_horizons=tuple(int(part) for part in parts))
            await self.send(
                update,
                "Сроки оценки результата, минут: "
                + ", ".join(map(str, self.store.snapshot().settings.outcome_horizons))
                + "\nИзменение применяется только к новым событиям; исходная цена, время и настройки прежних событий сохраняются.",
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
            await self.ui.command(update, "enrichment", "")
        elif command == "derivatives":
            key = instrument_key(raw or "spot/BTC")
            now = self.market.server_now() if isinstance(self.market, BybitRadar) else int(time.time() * 1000)
            await self.send(
                update,
                enrichment_text(self.enrichment_context(key, now), now)
                + "\nЭто текущий снимок, отдельно от контекста сохранённого события.",
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
        elif command == "digest":
            now = (
                self.market.server_now() or int(time.time() * 1000)
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
            await self.ui.command(update, "digest", "")
        elif command == "sample_alert":
            await self.send(
                update,
                sample_alert_text(snapshot),
                alert_keyboard(SAMPLE_SYMBOL, snapshot.settings.bybit_category),
            )

    async def callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        await query.answer()
        parts = str(query.data or "").split(":")
        if parts[0] == "ui":
            await self.ui.callback(update, parts[1:])
        elif parts[0] == "deck" and len(parts) == 2 and parts[1] in ("profiles", "detectors"):
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
                await self.ui.callback(update, ["parameters"])
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
                await self.send(update, "Заглушение сохранено.")
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

    async def warmup_poll(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        if isinstance(self.market, BybitRadar) and not self.first_ready_poll:
            await self.poll(context, warmup=True)

    async def poll(self, context: ContextTypes.DEFAULT_TYPE, *, warmup: bool = False) -> None:
        now = time.time()
        snapshot = self.store.snapshot()
        last_poll = self.store.get_runtime("last_poll_ts", 0)
        interval = min(5, snapshot.settings.radar_poll_sec) if warmup else snapshot.settings.radar_poll_sec
        if now - last_poll < interval:
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
        if isinstance(self.market, BybitRadar) and self.store.get_runtime("last_poll_status")["state"] in (
            "OK",
            "DEGRADED",
        ):
            self.first_ready_poll = True
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
                async with self.retention_lock:
                    self.retention_once(now)
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
        if not self.maintenance_ready():
            return
        from .catalogue_repack import busy
        from .storage_repack import async_work

        async with self.repack_lock:
            try:
                retention_slices = await self.retention_priority()
                defer_compaction = (
                    self.retention_pressure_current()
                    and self.store.get_runtime("storage_repack", {}).get("initial_reclaim_complete") is True
                )
                slices = 1 if defer_compaction else 4 if self.repack_boost_ready() else 1
                if not defer_compaction:
                    await self.shared_records.step(self.market.server_now)
                deadline = time.monotonic() + 0.2
                for index in range(slices):
                    if (index or retention_slices) and (
                        time.monotonic() >= deadline
                        or not self.maintenance_ready()
                        or not self.repack_boost_ready()
                    ):
                        break
                    state = await async_work(
                        self.store,
                        self.market.server_now(),
                        slices=1,
                        **({"defer_compaction": True} if defer_compaction else {}),
                    )
                    if not busy(state):
                        break
                self.store.put_runtime(
                    "repack_worker",
                    {
                        "state": "OK",
                        "at_ms": self.market.server_now(),
                        "compaction_deferred_for_retention": defer_compaction,
                    },
                )
            except Exception as exc:
                self.store.put_runtime(
                    "repack_worker",
                    {"state": "FAILED", "error_class": type(exc).__name__, "at_ms": now},
                )
                logger.error("Repack worker failure: %s", type(exc).__name__)

    def retention_once(self, now: int) -> dict[str, Any]:
        try:
            self.retention.work(now)
        except Exception as exc:
            self.store.put_runtime(
                "retention_worker", {"state": "FAILED", "error_class": type(exc).__name__, "at_ms": now}
            )
            raise
        value = self.store.get_runtime("market_retention", {})
        self.store.put_runtime(
            "retention_worker",
            {"state": "OK", "at_ms": now, "expired_events": value.get("expired_events", 0), "slices": 1},
        )
        return value

    def retention_backlog_current(self) -> bool:
        if not isinstance(self.market, BybitRadar):
            return False
        value = self.store.get_runtime("market_retention", {})
        stamp = value.get("at_ms")
        return (
            value.get("state") == "OK"
            and type(stamp) is int
            and 0 <= self.market.server_now() - stamp <= 60000
            and any(
                type(value.get(key)) is int and value[key] > 0
                for key in ("expired_events", "remaining_due_batch")
            )
        )

    def retention_pressure_current(self) -> bool:
        """Only an outstanding due batch may delay otherwise useful packing."""
        value = self.store.get_runtime("market_retention", {})
        remaining = value.get("remaining_due_batch")
        return self.retention_backlog_current() and type(remaining) is int and remaining > 0

    async def retention_priority(self) -> int:
        """Borrow the existing maintenance turn for proven expiry work, yielding per call."""
        if (
            not isinstance(self.market, BybitRadar)
            or self.retention_lock.locked()
            or self.analytics_lock.locked()
            or not self.retention_backlog_current()
            or not self.repack_boost_ready()
            or self.store.get_runtime("storage_repack", {}).get("initial_reclaim_complete") is not True
            or self.store.get_runtime("retention_worker", {}).get("state") == "FAILED"
        ):
            return 0
        async with self.retention_lock:
            began = time.monotonic()
            expired = 0
            slices = 0
            for index in range(4):
                if (
                    self.analytics_lock.locked()
                    or not self.maintenance_ready()
                    or not self.repack_boost_ready()
                    or not self.retention_backlog_current()
                    or index
                    and time.monotonic() - began >= 0.5
                ):
                    break
                value = self.retention_once(self.market.server_now())
                count = value.get("expired_events", 0)
                expired += count
                slices += 1
                await asyncio.sleep(0)
                if not count:
                    break
            self.store.put_runtime(
                "retention_worker",
                {
                    "state": "OK",
                    "at_ms": self.market.server_now(),
                    "expired_events": expired,
                    "slices": slices,
                    "elapsed_seconds": round(time.monotonic() - began, 6),
                    "mode": "MAINTENANCE_PRIORITY",
                },
            )
            return slices

    def repack_boost_ready(self) -> bool:
        """Use spare post-startup time, while retaining each40ms checkpoint/yield."""
        if not isinstance(self.market, BybitRadar) or time.time() - self.maintenance_started_at < 600:
            return False
        ingestion = self.store.get_runtime("ingestion", {})
        poll = self.store.get_runtime("last_poll_status", {})
        analytics = self.store.get_runtime("analytics", {})
        requested, available = poll.get("requested"), poll.get("available")
        return (
            type(requested) is int
            and requested > 0
            and type(available) is int
            and available >= requested * 0.95
            and 0 <= time.time() - ingestion.get("at", 0) <= 5
            and 0 <= time.time() - poll.get("at", 0) <= 120
            and analytics.get("state") == "OK"
            and 0 <= self.market.server_now() - analytics.get("at_ms", 0) <= 60000
            and not self.market._working
            and not any(v[0] < 20 for v in self.market._pending.values())
        )

    async def backfill_job(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.recovery is None or not isinstance(self.market, BybitRadar) or self.recovery_lock.locked():
            return
        now = self.market.server_now()
        if now <= 0:
            return
        if not self.maintenance_ready():
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

    def maintenance_ready(self) -> bool:
        """Give startup/current recovery the disk as well as the REST priority."""
        ingestion = self.store.get_runtime("ingestion", {})
        poll = self.store.get_runtime("last_poll_status", {})
        usable = poll.get("state") in ("OK", "DEGRADED") and (
            poll.get("state") == "OK" or poll.get("available", 0) > 0
        )
        if usable and poll.get("at", 0) >= self.maintenance_started_at:
            self.first_ready_poll = True
        return (
            self.first_ready_poll
            and (poll.get("available", 0) > 0 or poll.get("requested") == 0)
            and 0 <= time.time() - ingestion.get("at", 0) <= 30
            and all(
                ingestion.get(k, 1) == 0
                for k in (
                    "bootstrap_pending_streams",
                    "blocked_streams",
                    "recovery_pending",
                    "recovery_working",
                )
            )
        )

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
                if command in PUBLISHED_COMMANDS
            ],
            scope=BotCommandScopeChat(controller.infrastructure.delivery_chat_id),
        )
        controller.store.put_runtime(
            "heartbeat", {"at": time.time(), "revision": controller.infrastructure.source_revision}
        )
        if isinstance(controller.market, BybitRadar):
            await controller.market.start()

    async def shutdown(app: Application[Any, Any, Any, Any, Any, Any]) -> None:
        await controller.shared_records.close()
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
    app.job_queue.run_repeating(
        controller.warmup_poll,
        interval=5,
        first=2,
        job_kwargs={"max_instances": 1, "coalesce": True, "misfire_grace_time": 30},
    )
    app.job_queue.run_repeating(controller.heartbeat, interval=20, first=1)
    app.job_queue.run_repeating(controller.deliver, interval=2, first=2)
    app.job_queue.run_repeating(controller.analytics_job, interval=5, first=5)
    app.job_queue.run_repeating(
        controller.repack_job,
        interval=1,
        first=3,
        job_kwargs={"max_instances": 1, "coalesce": True, "misfire_grace_time": 30},
    )
    app.job_queue.run_repeating(
        controller.backfill_job,
        interval=1,
        first=4,
        job_kwargs={"max_instances": 1, "coalesce": True, "misfire_grace_time": 30},
    )
    app.job_queue.run_repeating(controller.enrichment_job, interval=2, first=6)
    app.job_queue.run_repeating(controller.agent_job, interval=2, first=3)
    return app
