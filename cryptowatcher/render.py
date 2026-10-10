"""Pure Telegram views for owner settings, versioned events and episode decisions."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

from .config import TIMEFRAMES, OwnerSettings
from .detector_config import FAMILIES, effective_parameters
from .detectors import freshness
from .market import RadarResult
from .storage import Snapshot

SAMPLE_SYMBOL = "SOLUSDT"


def market_label(settings: OwnerSettings) -> str:
    return "Spot USDT" if settings.bybit_category == "spot" else "USDT Perpetual"


def settings_text(snapshot: Snapshot) -> str:
    s = snapshot.settings
    parameters = effective_parameters(s)
    prices = ", ".join(f"{tf}: ≥ {parameters['price_' + tf]:g}%" for tf in TIMEFRAMES)
    return (
        f"⚙️ Bybit Spot USDT + USDT Perpetual · пять detectors\n"
        f"Режим: {'Широкий universe' if s.alert_universe_mode == 'top' else 'Мой список'}\n"
        f"Проверка сигналов: {s.radar_poll_sec}s\nStreams: 1m/3m/5m/15m одновременно\n"
        f"Карточка/пример: {market_label(s)} · {s.signal_timeframe}\n"
        f"Движение цены: {prices}\nСпайк оборота: ≥ x{parameters['turnover_ratio']:g}\n"
        f"Ликвидность 24ч: ≥ {s.liquidity_floor_24h:,.0f} USDT\nSMA periods: {int(parameters['turnover_lookback'])}\n"
        f"Composite: все три условия одновременно.\nEpisodes: новый detector/TF либо strength×1.5; coalesce2s.\n"
        f"Profile: {s.profile}\nTelegram detectors: {', '.join(s.alert_detectors) or 'выключены'}\n"
        f"Watchlist sensitivity multiplier: {s.watchlist_multiplier:g}\n"
        f"Configuration revision: {snapshot.revision}"
        + "\nActual detector parameters:\n"
        + ", ".join(f"{key}={value:g}" for key, value in effective_parameters(s).items())
    )


def menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["📊 Статус", "⚙️ Настройки"],
            ["Радар: Universe", "Радар: Мой список"],
            ["Список", "Добавить монету"],
            ["Удалить монету", "Очистить список"],
            ["🔔 Alerts", "🔕 Mutes"],
            ["Термины", "Диагностика"],
            ["Detectors", "Profiles"],
            ["История"],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def settings_keyboard(snapshot: Snapshot) -> InlineKeyboardMarkup:
    s = snapshot.settings
    parameters = effective_parameters(s)
    rows: list[list[InlineKeyboardButton]] = []
    groups = (
        ("Проверка рынка", "rdr", (60, 90, 120, 180), s.radar_poll_sec),
        ("Таймфрейм карточки (все streams активны)", "sgtf", TIMEFRAMES, s.signal_timeframe),
        (
            "Движение цены (%) — manual для всех TF",
            "pm",
            (1.0, 1.5, 2.0, 3.0),
            parameters[f"price_{s.signal_timeframe}"],
        ),
        ("Спайк оборота (x)", "sp", (2.0, 3.0, 4.0, 5.0), parameters["turnover_ratio"]),
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
    rows.append(
        [
            InlineKeyboardButton("Profiles", callback_data="deck:profiles"),
            InlineKeyboardButton("Detectors", callback_data="deck:detectors"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def detectors_keyboard(snapshot: Snapshot) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"{'🔔' if family in snapshot.settings.alert_detectors else '🔕'} {family}",
                    callback_data=f"detector:{family}:{'off' if family in snapshot.settings.alert_detectors else 'on'}",
                )
            ]
            for family in FAMILIES
        ]
    )


def profiles_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(name, callback_data="profile:" + name)]
            for name in ("normal", "conservative", "sensitive")
        ]
    )


def history_keyboard(events: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"{e['symbol']} {e['market_type']} {e['detector_id']} {e['timeframe']}",
                    callback_data="event:" + e["event_id"],
                )
            ]
            for e in events[:12]
        ]
    )


def event_keyboard(event: dict[str, Any]) -> InlineKeyboardMarkup:
    rows = alert_keyboard(event["symbol"], event["market_type"]).inline_keyboard
    return InlineKeyboardMarkup(
        [*rows, [InlineKeyboardButton("Событие", callback_data="event:" + event["event_id"])]]
    )


def event_text(event: dict[str, Any], now_ms: int) -> str:
    market = "Spot USDT" if event["market_type"] == "spot" else "USDT Perpetual"
    observed = datetime.fromtimestamp(event["market_observation_ms"] / 1000, timezone.utc).isoformat()
    metrics = "\n".join(f"{key}: {value:g} {event['units'][key]}" for key, value in event["metrics"].items())
    return (
        f"{event['symbol']} · {market}\n{event['detector_id']} v{event['detector_version']} · TF {event['timeframe']} · {event['direction']}\n"
        f"Strength: {event['strength']:.2f} относительно threshold\n{metrics}\n"
        f"Freshness: {freshness(event, now_ms)} · {event['completeness']}\n"
        f"Наблюдение: {observed}\nReference close: {event['reference_price']:g} USDT\n"
        f"Profile: {event['profile']} · Settings revision: {event['settings_revision']}\n"
        f"Event: {event['event_id']} · revision {event['revision']}\n"
        f"Episode: {event['episode_id']}\n"
        "Сохранённые metrics/source inputs относятся к исходному наблюдению."
    )


def notification_text(event: dict[str, Any], candidate: dict[str, Any], now_ms: int) -> str:
    episode = candidate["episode_snapshot"]
    reasons = {
        "first_detection": "первое уведомление",
        "new_detector": "новое семейство detector",
        "new_timeframe": "новый подтверждающий TF",
        "strengthening": "strength выросла ≥1.5×",
    }
    confirmations = ", ".join(
        f"{key}(rev{value['settings_revision']},{freshness(value, now_ms)})"
        for key, value in episode["confirmations"].items()
    )
    return (
        event_text(event, now_ms) + f"\nПричина: {reasons[candidate['reason']]}\n"
        f"Episode revision: {episode['revision']}\nПодтверждения: {confirmations}\nDelivery: {candidate['delivery_id']}"
    )


def trade_url(symbol: str, category: str) -> str:
    if category == "linear":
        return f"https://www.bybit.com/trade/usdt/{symbol}"
    return f"https://www.bybit.com/trade/spot/{symbol[:-4]}/USDT"


def alert_keyboard(symbol: str, category: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"Bybit {symbol}", url=trade_url(symbol, category))],
            [
                InlineKeyboardButton("🔕 1h", callback_data=f"al:mute:BYBIT:{category}:{symbol}:3600"),
                InlineKeyboardButton("🔕 24h", callback_data=f"al:mute:BYBIT:{category}:{symbol}:86400"),
            ],
            [InlineKeyboardButton("Монета", callback_data=f"al:coin:BYBIT:{category}:{symbol}")],
        ]
    )


def quote_text(symbol: str, quote: dict[str, Any], settings: OwnerSettings) -> str:
    periods = quote.get("effective_settings", {}).get(
        "sma_periods", effective_parameters(settings)["turnover_lookback"]
    )
    return (
        f"{symbol} · {market_label(settings)}\n"
        f"Цена: {quote['price']:g} USDT\n"
        f"TF {quote['signal_timeframe']} · {quote['price_change_tf']:+.1f}%\n"
        f"Оборот TF: {quote['current_tf_turnover']:,.1f} USDT\n"
        f"Средний оборот ({int(periods)} candles): {quote['sma_tf_turnover']:,.1f} USDT\n"
        f"Спайк оборота: x{quote['turnover_spike_ratio']:.1f}\n"
        f"Оборот 24ч: {quote['turnover24h']:,.1f} USDT\n"
        f"Composite: {'сработал' if quote['meets_signal'] else 'условия не выполнены'}"
    )


def sample_alert_text(snapshot: Snapshot) -> str:
    parameters = effective_parameters(snapshot.settings)
    s = snapshot.settings.changed(
        price_move_min=parameters[f"price_{snapshot.settings.signal_timeframe}"],
        turnover_spike_min=parameters["turnover_ratio"],
        sma_periods=parameters["turnover_lookback"],
    )
    spike = max(s.turnover_spike_min + 0.2, 2.0)
    turnover = 1_850_000.0
    quote = {
        "price": 182.45,
        "signal_timeframe": s.signal_timeframe,
        "price_change_tf": max(s.price_move_min + 0.3, 2.3),
        "current_tf_turnover": turnover,
        "sma_tf_turnover": turnover / spike,
        "turnover_spike_ratio": spike,
        "turnover24h": max(s.liquidity_floor_24h, 8_500_000.0),
        "meets_signal": True,
    }
    return (
        "🧪 Пример алерта\nДемонстрационные данные, не рыночное событие.\n"
        + quote_text(SAMPLE_SYMBOL, quote, s)
        + f"\nПороги: ≥ {s.price_move_min:g}%, ≥ x{s.turnover_spike_min:g}, "
        f"≥ {s.liquidity_floor_24h:,.0f} USDT. Все три условия одновременно."
        f"\nConfiguration revision: {snapshot.revision}"
    )


def status_text(snapshot: Snapshot, result: RadarResult, enabled: bool) -> str:
    text = settings_text(snapshot) + f"\n\nAlerts: {'ON' if enabled else 'OFF'}\n"
    text += f"Данные: {len(result.quotes)}/{result.requested}; ошибки/неполнота: {len(result.errors)}\n"
    ranked = sorted(result.quotes.items(), key=lambda item: item[1]["turnover_spike_ratio"], reverse=True)[
        :12
    ]
    for symbol, q in ranked:
        s = (
            OwnerSettings.from_payload(q["effective_settings"])
            if "effective_settings" in q
            else snapshot.settings
        )
        text += (
            f"\n{symbol} · {q.get('freshness', 'N/A')}\nДвижение цены ({q['signal_timeframe']}): {q['price_change_tf']:+.2f}% "
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
    "TF определяет размер свечи; 1m/3m/5m/15m поступают одновременно через WebSocket.\n"
    "Проверка сигналов — отдельная cadence, selected TF/market выбирают карточку.\n"
    "Universe основан на metadata/liquidity; watchlist имеет market identity и отдельный multiplier.\n"
    "Price impulse: abs(close/previous close−1)×100 ≥ threshold TF.\n"
    "Turnover spike: текущий оборот / среднее предыдущего lookback ≥ ratio; NEUTRAL.\n"
    "Range breakout: close выше/ниже предыдущего range с buffer×ATR14; ATR14 — среднее предыдущих14 true ranges.\n"
    "Volatility expansion: текущий true range / среднее предыдущих ranges ≥ ratio; NEUTRAL.\n"
    "Текущая candle исключается из baseline. Strength — отношение интенсивности к threshold; composite берёт минимум price/spike strength.\n"
    "Profiles меняются только кнопкой владельца; custom thresholds сохраняются при обновлении. Telegram selection не выключает history.\n"
    "Episodes объединяют instrument/market/direction; новый detector/TF либо strength×1.5 дают уведомление. Coalesce2s; re-arm после2 полных non-signal bars.\n"
    "Freshness считается от market time: обычно fresh1TF/expire2TF; breakout2TF/4TF. Retry/receipt не освежают event.\n"
    "Положительное/отрицательное движение описывает рынок.\n"
    "Radar не даёт торговых полномочий и не использует trading API credentials."
)
