"""Owner-only Telegram entrypoints, revision-safe polling and diagnostic views."""

from __future__ import annotations

import json
import logging
import time
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

from .config import ConfigError, Infrastructure, authorized, normalize_pair, number
from .market import BybitREST, MarketError
from .render import TERMS, alert_keyboard, menu, quote_text, settings_keyboard, settings_text, status_text
from .storage import RevisionConflict, Store

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
    "radar_top": "Top100",
    "radar_custom": "Мой список",
    "sample_alert": "Пример alert",
    "mute": "Отключить alerts монеты",
    "unmute": "Включить alerts монеты",
    "alerts": "Включить/выключить alerts",
    "set": "Ручная настройка",
    "diagnostics": "Диагностика",
    "resetbase": "Устаревшее действие",
    "resetallbase": "Устаревшее действие",
}
MENU_ACTIONS = {
    "📊 Статус": "status",
    "⚙️ Настройки": "settings",
    "Радар: Top 100": "radar_top",
    "Радар: Мой список": "radar_custom",
    "Список": "watchlist",
    "Добавить монету": "addcoin",
    "Удалить монету": "removecoin",
    "Очистить список": "clearlist",
    "Термины": "terms",
    "🔔 Alerts": "alerts",
    "🔕 Mutes": "mutes",
    "Диагностика": "diagnostics",
}
MANUAL_FIELDS = {
    "signal_timeframe",
    "radar_poll_sec",
    "cooldown_min",
    "price_move_min",
    "turnover_spike_min",
    "liquidity_floor_24h",
    "sma_periods",
}


class Controller:
    def __init__(self, infrastructure: Infrastructure, store: Store, market: BybitREST) -> None:
        self.infrastructure = infrastructure
        self.store = store
        self.market = market

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

    def diagnostics(self) -> dict[str, Any]:
        return {
            "source_revision": self.infrastructure.source_revision,
            "configuration_revision": self.store.snapshot().revision,
            "telegram_alerts_enabled": self.store.enabled(),
            "last_poll": self.store.get_runtime("last_poll_status", {"state": "PENDING"}),
            "telegram_delivery": self.store.get_runtime("telegram_delivery", {"state": "PENDING"}),
            "capabilities": {
                "composite": True,
                "websocket": False,
                "episodes": False,
                "outcomes": False,
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
                update, "Мой список:\n" + (", ".join(snapshot.settings.custom_pairs) or "Список пуст")
            )
        elif command in ("addcoin", "removecoin"):
            self.store.put_runtime("pending_action", "add" if command == "addcoin" else "remove")
            await self.send(update, "Отправьте символы через запятую: BTC,ETH,SOL")
        elif command in ("setlist", "input"):
            action = "set" if command == "setlist" else self.store.get_runtime("pending_action", "")
            if action not in ("add", "remove", "set"):
                await self.send(update, "Откройте действие в меню или /help", menu())
                return
            parts = [part for part in raw.replace("\n", ",").replace(" ", ",").split(",") if part]
            if not parts:
                raise ConfigError("Invalid custom list")
            pairs = tuple(dict.fromkeys(normalize_pair(part) for part in parts))
            current_pairs = snapshot.settings.custom_pairs
            next_pairs = (
                pairs
                if action == "set"
                else tuple(dict.fromkeys((*current_pairs, *pairs)))
                if action == "add"
                else tuple(pair for pair in current_pairs if pair not in pairs)
            )
            values: dict[str, Any] = {"custom_pairs": next_pairs}
            if action == "set":
                values["alert_universe_mode"] = "custom"
            self.change(**values)
            self.store.put_runtime("pending_action", "")
            await self.send(update, "Список сохранён:\n" + (", ".join(next_pairs) or "Список пуст"))
        elif command == "clearlist":
            self.change(custom_pairs=())
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
            symbol = normalize_pair(parts[0])
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
            await self.send(update, f"Alerts {'ON' if enabled else 'OFF'}. Расчёт рынка продолжается.")
        elif command in ("resetbase", "resetallbase"):
            await self.send(
                update,
                "Reset baseline больше не используется. Composite считает движение закрытой свечи; настройки доступны в меню.",
            )
        elif command == "diagnostics":
            await self.send(update, json.dumps(self.diagnostics(), ensure_ascii=False, indent=2))
        elif command == "sample_alert":
            await self.send(
                update,
                "Пример: BTCUSDT · TF3m · +2.4% · оборот100000 USDT · x4.2. Для сигнала должны выполняться все три active thresholds.",
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
        if parts[0] == "st":
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
            symbol = normalize_pair(parts[3])
            if parts[1] == "mute" and len(parts) == 5:
                duration = number(parts[4], "mute seconds")
                self.store.mute(symbol, time.time() + duration)
                await self.send(update, "Mute сохранён.")
            elif parts[1] == "coin":
                snapshot = self.store.snapshot()
                result = await self.market.snapshot(
                    snapshot.settings.changed(alert_universe_mode="custom", custom_pairs=(symbol,))
                )
                if self.store.snapshot().revision != snapshot.revision:
                    await self.send(update, "Настройки изменились; повторите запрос.")
                elif symbol in result.quotes:
                    await self.send(
                        update,
                        quote_text(symbol, result.quotes[symbol], snapshot.settings),
                        alert_keyboard(symbol, snapshot.settings.bybit_category),
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
                "state": "DEGRADED" if result.errors else "OK",
                "at": now,
                "available": len(result.quotes),
                "requested": result.requested,
                "errors": result.errors,
                "configuration_revision": snapshot.revision,
            },
        )
        for symbol, quote in result.quotes.items():
            if self.store.snapshot().revision != snapshot.revision:
                break
            if not quote["meets_signal"] or not self.store.enabled() or self.store.muted(symbol, now):
                continue
            key = (
                f"last_alert:{snapshot.settings.bybit_category}:{symbol}:{snapshot.settings.signal_timeframe}"
            )
            sent = self.store.get_runtime(key, {})
            legacy = self.store.get_runtime("legacy_baselines", {}).get(f"BYBIT:{symbol}", {})
            last_alert = sent.get("at", legacy.get("last_alert", 0))
            if (
                now - last_alert < snapshot.settings.cooldown_min * 60
                or sent.get("candle_end_ms") == quote["candle_end_ms"]
            ):
                continue
            try:
                await context.bot.send_message(
                    self.infrastructure.delivery_chat_id,
                    quote_text(symbol, quote, snapshot.settings)
                    + f"\nSettings revision: {snapshot.revision}",
                    reply_markup=alert_keyboard(symbol, snapshot.settings.bybit_category),
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

    async def heartbeat(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.store.put_runtime(
            "heartbeat", {"at": time.time(), "revision": self.infrastructure.source_revision}
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
    return app
