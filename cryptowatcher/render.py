"""Pure Telegram views for the currently implemented composite capabilities."""

from __future__ import annotations

from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

from .config import TIMEFRAMES, OwnerSettings
from .market import RadarResult
from .storage import Snapshot


def market_label(settings: OwnerSettings) -> str:
    return "Spot USDT" if settings.bybit_category == "spot" else "USDT Perpetual"


def settings_text(snapshot: Snapshot) -> str:
    s = snapshot.settings
    return (
        f"⚙️ Bybit {market_label(s)} · composite\n"
        f"Режим: {'Top 100' if s.alert_universe_mode == 'top' else 'Мой список'}\n"
        f"Проверка рынка: {s.radar_poll_sec}s\nТаймфрейм сигнала: {s.signal_timeframe}\n"
        f"Движение цены: ≥ {s.price_move_min:g}%\nСпайк оборота: ≥ x{s.turnover_spike_min:g}\n"
        f"Ликвидность 24ч: ≥ {s.liquidity_floor_24h:,.0f} USDT\nSMA periods: {s.sma_periods}\n"
        f"Все три условия выполняются одновременно.\nCooldown: {s.cooldown_min}min\n"
        f"Configuration revision: {snapshot.revision}"
    )


def menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["📊 Статус", "⚙️ Настройки"],
            ["Радар: Top 100", "Радар: Мой список"],
            ["Список", "Добавить монету"],
            ["Удалить монету", "Очистить список"],
            ["🔔 Alerts", "🔕 Mutes"],
            ["Термины", "Диагностика"],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def settings_keyboard(snapshot: Snapshot) -> InlineKeyboardMarkup:
    s = snapshot.settings
    rows: list[list[InlineKeyboardButton]] = []
    groups = (
        ("Проверка рынка", "rdr", (60, 90, 120, 180), s.radar_poll_sec),
        ("Таймфрейм сигнала", "sgtf", TIMEFRAMES, s.signal_timeframe),
        ("Движение цены (%)", "pm", (1.0, 1.5, 2.0, 3.0), s.price_move_min),
        ("Спайк оборота (x)", "sp", (2.0, 3.0, 4.0, 5.0), s.turnover_spike_min),
        (
            "Ликвидность 24ч (USDT)",
            "lq",
            (1_000_000, 5_000_000, 10_000_000, 25_000_000),
            s.liquidity_floor_24h,
        ),
    )
    for label, key, values, active in groups:
        rows.append([InlineKeyboardButton(label, callback_data="st:noop")])
        rows.append(
            [
                InlineKeyboardButton(
                    f"{'✅ ' if value == active else ''}{value:g}"
                    if isinstance(value, (float, int))
                    else f"{'✅ ' if value == active else ''}{value}",
                    callback_data=f"st:{key}:{value}",
                )
                for value in values
            ]
        )
    rows.append(
        [
            InlineKeyboardButton("Ручное значение", callback_data="st:manual"),
            InlineKeyboardButton("Термины", callback_data="st:help"),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton("Статус", callback_data="st:status"),
            InlineKeyboardButton("Обновить", callback_data="st:refresh"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def trade_url(symbol: str, category: str) -> str:
    if category == "linear":
        return f"https://www.bybit.com/trade/usdt/{symbol}"
    return f"https://www.bybit.com/trade/spot/{symbol[:-4]}/USDT"


def alert_keyboard(symbol: str, category: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"Bybit {symbol}", url=trade_url(symbol, category))],
            [
                InlineKeyboardButton("🔕 1h", callback_data=f"al:mute:BYBIT:{symbol}:3600"),
                InlineKeyboardButton("🔕 24h", callback_data=f"al:mute:BYBIT:{symbol}:86400"),
            ],
            [InlineKeyboardButton("Монета", callback_data=f"al:coin:BYBIT:{symbol}")],
        ]
    )


def quote_text(symbol: str, quote: dict[str, Any], settings: OwnerSettings) -> str:
    return (
        f"{symbol} · {market_label(settings)}\n"
        f"Цена: {quote['price']:g} USDT\n"
        f"TF {quote['signal_timeframe']} · {quote['price_change_tf']:+.1f}%\n"
        f"Оборот TF: {quote['current_tf_turnover']:,.1f} USDT\n"
        f"Средний оборот ({settings.sma_periods} candles): {quote['sma_tf_turnover']:,.1f} USDT\n"
        f"Спайк оборота: x{quote['turnover_spike_ratio']:.1f}\n"
        f"Оборот 24ч: {quote['turnover24h']:,.1f} USDT\n"
        f"Composite: {'сработал' if quote['meets_signal'] else 'условия не выполнены'}"
    )


def status_text(snapshot: Snapshot, result: RadarResult, enabled: bool) -> str:
    text = settings_text(snapshot) + f"\n\nAlerts: {'ON' if enabled else 'OFF'}\n"
    text += f"Данные: {len(result.quotes)}/{result.requested}; ошибки/неполнота: {len(result.errors)}\n"
    ranked = sorted(result.quotes.items(), key=lambda item: item[1]["turnover_spike_ratio"], reverse=True)[
        :12
    ]
    for symbol, q in ranked:
        s = snapshot.settings
        text += (
            f"\n{symbol}\nДвижение цены ({s.signal_timeframe}): {q['price_change_tf']:+.2f}% "
            f"{'✅' if abs(q['price_change_tf']) >= s.price_move_min else '❌'}\n"
            f"Спайк оборота: x{q['turnover_spike_ratio']:.2f} "
            f"{'✅' if q['turnover_spike_ratio'] >= s.turnover_spike_min else '❌'}\n"
            f"Ликвидность 24ч: {q['turnover24h']:,.0f} USDT "
            f"{'✅' if q['turnover24h'] >= s.liquidity_floor_24h else '❌'}\n"
            f"Итог: {'сигнал есть' if q['meets_signal'] else 'сигнала нет'}\n"
        )
    if result.errors:
        text += "\nНедостаток данных ограничивает расчёт; details в диагностике."
    return text


TERMS = (
    "Composite: движение цены + спайк оборота + ликвидность.\n"
    "Движение цены — изменение close относительно previous close закрытой свечи.\n"
    "Оборот — объём в USDT; объём в базовом asset имеет другую единицу.\n"
    "Спайк — оборот текущей закрытой свечи / средний оборот предыдущих candles.\n"
    "Ликвидность — оборот инструмента за 24 часа.\n"
    "TF определяет размер свечи, проверка рынка — частоту REST polling.\n"
    "Положительное/отрицательное движение описывает рынок.\n"
    "Radar не даёт торговых полномочий и не использует trading API credentials."
)
