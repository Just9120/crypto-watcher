"""Pure Telegram views for owner settings, versioned events and episode decisions."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

from .config import OwnerSettings
from .detector_config import FAMILIES, effective_parameters
from .detectors import freshness
from .enrichment import freshness as metric_freshness
from .market import RadarResult
from .storage import Snapshot
from .telegram_views import FAMILY, PROFILE, event_summary, keyboard, settings_summary, when

SAMPLE_SYMBOL = "SOLUSDT"


def market_label(settings: OwnerSettings) -> str:
    return "Spot USDT" if settings.bybit_category == "spot" else "USDT Perpetual"


def settings_text(snapshot: Snapshot) -> str:
    return settings_summary(snapshot)


def menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["📊 Статус", "📡 Радар"],
            ["🗂 История", "📈 Аналитика"],
            ["⚙️ Настройки", "❔ Помощь"],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def settings_keyboard(snapshot: Snapshot) -> InlineKeyboardMarkup:
    return keyboard(
        [
            [("Профили", "ui:profiles"), ("Пороги детекторов", "ui:parameters")],
            [("Уведомления", "ui:alerts"), ("Семейства уведомлений", "ui:detectors")],
            [("Контекст деривативов", "ui:enrichment"), ("Доставка агенту", "ui:agent")],
            [("Карточка и ввод", "ui:card"), ("Сроки результатов", "ui:field:outcome_horizons")],
        ]
    )


def detectors_keyboard(snapshot: Snapshot) -> InlineKeyboardMarkup:
    return keyboard(
        [
            [
                (
                    ("Включены: " if family in snapshot.settings.alert_detectors else "Выключены: ")
                    + FAMILY[family],
                    f"detector:{family}:{'off' if family in snapshot.settings.alert_detectors else 'on'}",
                )
            ]
            for family in FAMILIES
        ],
        back="settings",
    )


def profiles_keyboard() -> InlineKeyboardMarkup:
    return keyboard(
        [[(PROFILE[name], "ui:profile:" + name)] for name in ("normal", "conservative", "sensitive")],
        back="settings",
    )


def history_keyboard(events: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    return keyboard(
        [
            [
                (
                    f"{when(e['market_observation_ms'])} · {e['symbol']} · {e['timeframe']}",
                    "event:" + e["event_id"],
                )
            ]
            for e in events[:6]
        ]
    )


def event_keyboard(event: dict[str, Any]) -> InlineKeyboardMarkup:
    rows = alert_keyboard(event["symbol"], event["market_type"]).inline_keyboard
    return InlineKeyboardMarkup(
        [
            *rows,
            [InlineKeyboardButton("Пересчитать сравнение", callback_data="recompare:" + event["event_id"])],
            [
                InlineKeyboardButton(
                    "Полные исходные данные", callback_data="ui:eventfile:" + event["event_id"]
                )
            ],
            [
                InlineKeyboardButton("Подробности", callback_data="event:" + event["event_id"]),
                InlineKeyboardButton("Результат", callback_data="outcomes:" + event["event_id"]),
            ],
        ]
    )


def enrichment_keyboard(snapshot: Snapshot) -> InlineKeyboardMarkup:
    enabled = snapshot.settings.enrichment_enabled
    keys = [key for key in snapshot.settings.instruments() if key.startswith("spot/")][:5] or [
        "spot/BTCUSDT",
        "spot/ETHUSDT",
        "spot/SOLUSDT",
    ]
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Выключить context" if enabled else "Включить context",
                    callback_data="enrichment:" + ("off" if enabled else "on"),
                ),
                InlineKeyboardButton("Обновить", callback_data="enrichment:status"),
            ],
            *[[InlineKeyboardButton(key, callback_data="derivatives:" + key)] for key in keys],
        ]
    )


def digest_keyboard(enabled: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Выключить digest" if enabled else "Включить digest",
                    callback_data="digest:" + ("off" if enabled else "on"),
                ),
                InlineKeyboardButton("Обновить", callback_data="digest:status"),
            ]
        ]
    )


def outcome_text(event: dict[str, Any], outcomes: list[Any], horizons: tuple[int, ...]) -> str:
    lines = [
        f"{event['market_type']}/{event['symbol']} · {event['detector_id']} · TF {event['timeframe']} · {event['direction']}",
        f"Event {event['event_id']} · immutable reference {event['reference_price']:g} USDT",
        "Reference time: "
        + datetime.fromtimestamp(event["reference_time_ms"] / 1000, timezone.utc).isoformat(),
    ]
    for horizon, outcome in zip(horizons, outcomes):
        if outcome is None:
            lines.append(f"{horizon} min: PENDING (horizon или очередь ещё не завершены)")
            continue
        lines.append(
            f"{horizon} min · {outcome['state']} · methodology v{outcome['methodology_version']} · revision {outcome['revision']}"
        )
        if outcome["state"] != "COMPLETE":
            lines.append(
                f"Данные {outcome.get('observed_candles', 'UNSET')}/{outcome.get('expected_candles', 'UNSET')}; {outcome.get('recovery_reason', outcome.get('reason', 'ожидается полное окно'))}. Return не подменяется нулём."
            )
        else:
            lines += [f"{name}: {value:.3f}%" for name, value in outcome["metrics"].items()]
            lines.append(
                f"{outcome['classification']} · {outcome['classification_reason']} · noise={outcome['noise_threshold_pct']}"
            )
    lines.append("Late analytics; не входили в исходный decision context. Не торговая рекомендация.")
    return "\n".join(lines)


def event_text(event: dict[str, Any], now_ms: int) -> str:
    market = "Spot USDT" if event["market_type"] == "spot" else "USDT Perpetual"
    observed = datetime.fromtimestamp(event["market_observation_ms"] / 1000, timezone.utc).isoformat()
    metrics = "\n".join(f"{key}: {value:g} {event['units'][key]}" for key, value in event["metrics"].items())
    context = event.get("enrichment", {"state": "UNAVAILABLE"})
    optional = (
        "\n" + enrichment_text(context, now_ms)
        if context["state"] not in ("DISABLED", "N/A", "UNAVAILABLE")
        else ""
    )
    return (
        f"{event['symbol']} · {market}\n{event['detector_id']} v{event['detector_version']} · TF {event['timeframe']} · {event['direction']}\n"
        f"Strength: {event['strength']:.2f} относительно threshold\n{metrics}\n"
        f"Freshness: {freshness(event, now_ms)} · {event['completeness']}\n"
        f"Наблюдение: {observed}\nReference close: {event['reference_price']:g} USDT\n"
        f"Profile: {event['profile']} · Settings revision: {event['settings_revision']}\n"
        f"Event: {event['event_id']} · revision {event['revision']}\n"
        f"Episode: {event['episode_id']}\n"
        "Сохранённые metrics/source inputs относятся к исходному наблюдению." + optional
    )


def enrichment_text(value: dict[str, Any], now_ms: int) -> str:
    lines = [
        f"Optional derivatives context · {value['state']} · {value.get('reason', value.get('mapping', ''))}"
    ]
    if value.get("metrics"):
        lines.append(
            f"{value['spot_instrument']} ↔ {value['perpetual_instrument']} · method v{value['methodology_version']}"
        )
        for name, metric in value["metrics"].items():
            state = metric_freshness(metric, now_ms)
            if (
                metric.get("unit") == "UTC_MS"
                and metric.get("value") is not None
                and metric["value"] <= now_ms
            ):
                state = "STALE"
            amount = f"{metric['value']:g}" if metric.get("value") is not None else "UNSET"
            lines.append(
                f"{name}: {amount} {metric['unit']} {metric.get('base_asset', '')} · {state}\n"
                f"source {metric['source']} · market time {metric.get('market_observation_ms')} · available {metric.get('available_at_ms')}"
            )
        lines.append(
            "Basis — два REST price snapshots, timestamps приведены; это не одновременные trades. Context не участвует в trigger."
        )
    return "\n".join(lines)


def notification_text(event: dict[str, Any], candidate: dict[str, Any], now_ms: int) -> str:
    episode = candidate["episode_snapshot"]
    reasons = {
        "first_detection": "Первое уведомление",
        "new_detector": "Новое семейство подтверждения",
        "new_timeframe": "Новый подтверждающий TF",
        "strengthening": "Усиление эпизода: интенсивность выросла не менее чем в1,5раза",
    }
    active = [key for key, value in episode["confirmations"].items() if freshness(value, now_ms) == "FRESH"]
    expired = len(episode["confirmations"]) - len(active)
    confirmations = ", ".join(f"{FAMILY.get(key.split('/')[0], key)} {key.split('/')[-1]}" for key in active)
    return (
        reasons[candidate["reason"]]
        + "\n"
        + event_summary(event, now_ms)
        + f"\nАктуальные подтверждения: {confirmations or 'нет'}."
        + (f" Ранее подтверждений: {expired}; их свежесть истекла." if expired else "")
        + "\nПодробности, исходные данные и последующее движение — по кнопкам."
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
    s = snapshot.settings
    return (
        "📊 Радар Bybit\n"
        "Рынки: Spot USDT + USDT Perpetual\n"
        "Свечи: 1m / 3m / 5m / 15m одновременно\n"
        "Детекторы: цена, оборот, пробой, диапазон, совпадение\n"
        f"Охват: {'весь подходящий рынок' if s.alert_universe_mode == 'top' else 'мой список: ' + str(len(s.instruments()))}\n"
        f"Мгновенные уведомления: {'включены' if enabled else 'выключены'}\n"
        "Разрешены в Telegram: "
        + (", ".join(FAMILY[f] for f in s.alert_detectors) or "нет")
        + f"\nСлужебный composite-снимок: {len(result.quotes)}/{result.requested} оценок инструмент/рынок/TF; неполных/ошибок: {len(result.errors)}."
        + "\nЭта оценка не означает наличие или отсутствие событий остальных детекторов."
    )


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
    "\nDerivatives context optional: OI linear в BASE (обе стороны), funding в %, next funding UTC ms, basis в % по Spot/Perpetual price snapshots."
    "\nMissing/stale context не блокирует detectors; события сохраняют context, доступный до observation cutoff."
    "\nСравнение profiles/versions использует только сохранённые outcomes с N/completeness; ручной recompare сохраняет предыдущие revisions. Thresholds меняет только владелец."
)
