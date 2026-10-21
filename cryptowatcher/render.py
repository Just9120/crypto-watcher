"""Pure Telegram views for owner settings, versioned events and episode decisions."""

from __future__ import annotations

from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

from .config import OwnerSettings
from .detector_config import FAMILIES, effective_parameters
from .detectors import freshness
from .enrichment import freshness as metric_freshness
from .market import RadarResult
from .storage import Snapshot
from .telegram_views import FAMILY, METRIC, PROFILE, amount, event_summary, keyboard, settings_summary, when

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


def event_keyboard(event: dict[str, Any], *, history_token: str | None = None) -> InlineKeyboardMarkup:
    rows = alert_keyboard(event["symbol"], event["market_type"]).inline_keyboard
    eid = event["event_id"]
    suffix = ":" + history_token if history_token else ""
    return InlineKeyboardMarkup(
        [
            rows[0],
            [
                InlineKeyboardButton(
                    "Подробности", callback_data=("ui:open:" if history_token else "event:") + eid + suffix
                ),
                InlineKeyboardButton(
                    "Результат", callback_data=("ui:result:" if history_token else "outcomes:") + eid + suffix
                ),
            ],
            [
                InlineKeyboardButton("Заглушить", callback_data="ui:eventmute:" + eid + suffix),
                InlineKeyboardButton("Ещё", callback_data="ui:eventmore:" + eid + suffix),
            ],
            [
                InlineKeyboardButton(
                    "← История",
                    callback_data="ui:returnhistory:" + history_token if history_token else "ui:history",
                )
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
                    "Выключить контекст" if enabled else "Включить контекст",
                    callback_data="enrichment:" + ("off" if enabled else "on"),
                ),
                InlineKeyboardButton("Обновить", callback_data="enrichment:status"),
            ],
            *[[InlineKeyboardButton(key, callback_data="derivatives:" + key)] for key in keys],
            [InlineKeyboardButton("← Настройки", callback_data="ui:settings")],
        ]
    )


def digest_keyboard(enabled: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Выключить дайджест" if enabled else "Включить дайджест",
                    callback_data="digest:" + ("off" if enabled else "on"),
                ),
                InlineKeyboardButton("Обновить", callback_data="digest:status"),
            ]
        ]
    )


def outcome_text(event: dict[str, Any], outcomes: list[Any], horizons: tuple[int, ...]) -> str:
    lines = [
        f"{event['symbol']} · {market_label(OwnerSettings(bybit_category=event['market_type']))}",
        f"{FAMILY[event['detector_id']]} · {event['timeframe']} · результат наблюдения",
        f"Исходное время: {when(event['reference_time_ms'])}",
        f"Цена отсчёта: {amount(event['reference_price'])} USDT",
    ]
    states = {
        "COMPLETE": "готово",
        "INCOMPLETE": "данные неполные",
        "PENDING": "ожидание",
        "EXPIRED": "подробности истекли",
    }
    for horizon, outcome in zip(horizons, outcomes):
        if outcome is None:
            lines.append(f"{horizon} мин: ожидается срок или завершение расчёта; это не нулевой результат.")
            continue
        lines.append(f"{horizon} мин · {states.get(outcome['state'], outcome['state'])}")
        if outcome["state"] != "COMPLETE":
            lines.append(
                f"Свечи: {outcome.get('observed_candles', 'неизвестно')}/{outcome.get('expected_candles', 'неизвестно')}; "
                + str(outcome.get("recovery_reason", outcome.get("reason", "ожидается полное окно")))
            )
        else:
            lines.extend(
                f"{METRIC.get(name, name)}: {amount(value)}%" for name, value in outcome["metrics"].items()
            )
            classification = {
                "CONTINUATION": "движение продолжилось",
                "REVERSAL": "движение развернулось",
                "NEUTRAL_MOVEMENT": "в пределах шума",
                "UNCLASSIFIED": "недостаточно данных для оценки",
            }.get(outcome["classification"], outcome["classification"])
            lines.append(f"Оценка: {classification}; граница шума {amount(outcome['noise_threshold_pct'])}%")
        lines.append(f"Методика {outcome['methodology_version']}, версия результата {outcome['revision']}")
    lines.append(
        "Последующая аналитика не входила в исходное решение. Это не доходность стратегии и не торговая рекомендация."
    )
    return "\n".join(lines)


def event_text(event: dict[str, Any], now_ms: int) -> str:
    metrics = "\n".join(
        f"{METRIC.get(key, key)}: {amount(value)} {'×' if event['units'][key] == 'ratio' else event['units'][key]}"
        for key, value in event["metrics"].items()
    )
    context = event.get("enrichment", {"state": "UNAVAILABLE"})
    optional = "\n" + enrichment_text(context, now_ms) if context.get("metrics") else ""
    return (
        event_summary(event, now_ms)
        + "\n\nПодробности исходного наблюдения\n"
        + metrics
        + f"\nЦена отсчёта: {amount(event['reference_price'])} USDT"
        + f"\nПрофиль: {PROFILE.get(event['profile'], event['profile'])}; версия настроек {event['settings_revision']}"
        + f"\nВерсия детектора: {event['detector_version']}; версия события {event['revision']}"
        + f"\nEvent ID: {event['event_id']}\nEpisode ID: {event['episode_id']}"
        + "\nПолные точные значения, исходные свечи и происхождение данных доступны файлом."
        + optional
    )


def enrichment_text(value: dict[str, Any], now_ms: int) -> str:
    states = {
        "DISABLED": "выключен",
        "UNAVAILABLE": "недоступен",
        "N/A": "не применяется",
        "FRESH": "свежий",
        "STALE": "устарел",
        "PARTIAL": "неполный",
        "MISSING": "нет данных",
        "COMPLETE": "полный",
    }
    names = {
        "open_interest_base": "Открытый интерес",
        "funding_rate_pct": "Funding",
        "next_funding_ms": "Следующий funding",
        "snapshot_basis_pct": "Разница Spot/Perpetual",
        "oi_change_pct_5min": "Изменение OI за 5 мин",
        "oi_change_base_5min": "Изменение OI в монетах за 5 мин",
    }
    lines = ["Дополнительный контекст деривативов: " + states.get(value["state"], value["state"])]
    if value.get("metrics"):
        lines.append(f"{value['spot_instrument']} ↔ {value['perpetual_instrument']}")
        for name, metric in value["metrics"].items():
            state = metric_freshness(metric, now_ms)
            if (
                metric.get("unit") == "UTC_MS"
                and metric.get("value") is not None
                and metric["value"] <= now_ms
            ):
                state = "STALE"
            text = when(metric["value"]) if metric.get("unit") == "UTC_MS" else amount(metric.get("value"))
            unit = (
                metric.get("base_asset", "")
                if metric.get("unit") == "BASE"
                else ""
                if metric.get("unit") == "UTC_MS"
                else metric.get("unit", "")
            )
            lines.append(f"{names.get(name, name)}: {text} {unit} · {states.get(state, state)}")
            lines.append(
                f"Источник {metric['source']}, рынок {when(metric.get('market_observation_ms'))}, доступно {when(metric.get('available_at_ms'))}"
            )
        lines.append(
            "Basis — разница двух снимков цен, не одновременные сделки. Контекст не участвует в срабатывании детекторов."
        )
    return "\n".join(lines)


def notification_text(event: dict[str, Any], candidate: dict[str, Any], now_ms: int) -> str:
    episode = candidate["episode_snapshot"]
    reasons = {
        "first_detection": "Первое уведомление",
        "new_detector": "Новое семейство подтверждения",
        "new_timeframe": "Новый подтверждающий TF",
        "strengthening": "Усиление эпизода: интенсивность выросла не менее чем в 1,5 раза",
    }
    active = [
        key
        for key, value in episode["confirmations"].items()
        if freshness(value, now_ms) == "FRESH" and value["settings_revision"] == event["settings_revision"]
    ]
    expired = sum(
        freshness(value, now_ms) != "FRESH" and value["settings_revision"] == event["settings_revision"]
        for value in episode["confirmations"].values()
    )
    other_settings = sum(
        value["settings_revision"] != event["settings_revision"]
        for value in episode["confirmations"].values()
    )
    confirmations = ", ".join(f"{FAMILY.get(key.split('/')[0], key)} {key.split('/')[-1]}" for key in active)
    return (
        reasons[candidate["reason"]]
        + "\n"
        + event_summary(event, now_ms)
        + f"\nАктуальные подтверждения: {confirmations or 'нет'}."
        + (f" Устаревших подтверждений этой настройки: {expired}." if expired else "")
        + (f" Из других версий настроек: {other_settings}." if other_settings else "")
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
        f"{symbol} · {market_label(settings)}\nЦена: {amount(quote['price'])} USDT"
        + f"\nTF {quote['signal_timeframe']} · {quote['price_change_tf']:+.1f}%"
        + f"\nОборот свечи: {amount(quote['current_tf_turnover'], compact=True)} USDT"
        + f"\nСредний оборот ({int(periods)} свечей): {amount(quote['sma_tf_turnover'], compact=True)} USDT"
        + f"\nВсплеск оборота: ×{amount(quote['turnover_spike_ratio'])}"
        + f"\nОборот 24ч: {amount(quote['turnover24h'], compact=True)} USDT"
        + "\nComposite: "
        + ("условия выполнены" if quote["meets_signal"] else "не все пороги достигнуты")
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
        + f"\nПороги: ≥ {amount(s.price_move_min)}%, ≥ ×{amount(s.turnover_spike_min)}, "
        f"≥ {amount(s.liquidity_floor_24h)} USDT. Все три условия одновременно."
        f"\nВерсия настроек: {snapshot.revision}"
    )


def status_text(snapshot: Snapshot, result: RadarResult, enabled: bool) -> str:
    s = snapshot.settings
    return (
        "📊 Радар Bybit\n"
        f"Мгновенные уведомления: {'включены' if enabled else 'выключены'}\n"
        "Разрешены в Telegram: "
        + (", ".join(FAMILY[f] for f in s.alert_detectors) or "нет")
        + (
            f"\nОграничений служебной оценки composite: {len(result.errors)}. Подробнее — «Разбор composite»."
            if result.errors
            else ""
        )
    )


TERMS = (
    "Composite: движение цены + спайк оборота + ликвидность.\n"
    "Движение цены — изменение close относительно previous close закрытой свечи.\n"
    "Оборот — объём в USDT; объём в базовом asset имеет другую единицу.\n"
    "Спайк — оборот текущей закрытой свечи / средний оборот предыдущих candles.\n"
    "Ликвидность — оборот инструмента за 24 часа.\n"
    "TF определяет размер свечи; 1m/3m/5m/15m поступают одновременно через WebSocket.\n"
    "Служебная сводка обновляется отдельно от сигналов WebSocket; выбранные TF/рынок определяют только карточку.\n"
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
