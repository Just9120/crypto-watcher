"""Human-readable Telegram panels. Stored values and machine contracts stay unchanged."""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .config import TIMEFRAMES
from .detector_config import effective_parameters, preset
from .detectors import freshness
from .storage import Snapshot
from .storage_meter import BUDGET

FAMILY = {
    "price": "Движение цены",
    "turnover": "Всплеск оборота",
    "breakout": "Пробой диапазона",
    "volatility": "Расширение диапазона",
    "composite": "Совпадение цены и оборота",
}
PROFILE = {
    "custom": "Свои настройки",
    "normal": "Обычный",
    "conservative": "Строгий",
    "sensitive": "Чувствительный",
}
DIRECTION = {"UP": "вверх", "DOWN": "вниз", "NEUTRAL": "без направления"}
FIELDS = {
    **{f"price_{tf}": f"Движение цены {tf}, %" for tf in TIMEFRAMES},
    "price_move_min": "Цена: применить ко всем TF, %",
    "turnover_ratio": "Всплеск оборота, ×",
    "turnover_lookback": "Окно среднего оборота, свечей",
    "breakout_lookback": "Окно пробоя, свечей",
    "breakout_buffer": "Отступ пробоя, ×ATR14",
    "volatility_ratio": "Расширение диапазона, ×",
    "volatility_lookback": "Окно диапазона, свечей",
    "liquidity_floor_24h": "Минимальный оборот 24ч, USDT",
    "watchlist_multiplier": "Множитель порогов моего списка",
    "radar_poll_sec": "Обновление служебной сводки, секунд",
    "digest_interval_min": "Интервал дайджеста, минут",
    "outcome_horizons": "Сроки оценки результата, минут",
}
METRIC = {
    "price_return": "Изменение цены",
    "price_threshold": "Порог цены",
    "turnover": "Оборот свечи",
    "turnover_mean": "Обычный оборот",
    "turnover_ratio": "К обычному обороту",
    "turnover_threshold": "Порог оборота",
    "liquidity_24h": "Оборот 24ч",
    "liquidity_floor": "Минимум 24ч",
    "atr14": "Средний истинный диапазон ATR14",
    "range_high": "Верх диапазона",
    "range_low": "Низ диапазона",
    "true_range": "Истинный диапазон",
    "range_mean": "Обычный диапазон",
    "true_range_mean": "Обычный истинный диапазон",
    "breakout_buffer": "Отступ пробоя",
    "breakout_distance": "Расстояние пробоя",
    "volatility_threshold": "Порог расширения диапазона",
    "volatility_ratio": "К обычному диапазону",
    "return_pct": "Изменение цены",
    "price_return_pct": "Изменение цены",
    "directed_return_pct": "Движение по направлению",
    "max_rise_pct": "Максимальный рост",
    "max_fall_pct": "Максимальное падение",
    "range_pct": "Общий диапазон",
    "directional_return_pct": "Движение по направлению",
    "mfe_pct": "Максимальное движение в сторону",
    "mae_pct": "Максимальное движение против",
}
FRESHNESS = {
    "FRESH": "свежее",
    "STALE": "устаревает",
    "EXPIRED": "срок истёк",
    "FUTURE": "время источника впереди текущего",
}
STATE = {
    "OK": "работает",
    "FAILED": "сбой",
    "FRESH": "сводка свежая",
    "STALE": "сводка устарела",
    "PENDING": "ожидается результат",
    "DEGRADED": "работает с ограничениями",
    "WARMING": "готовит данные",
    "COMPLETE": "завершено",
}


def amount(value: Any, *, compact: bool = False) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "неизвестно"
    if compact:
        for scale, label in ((1e9, "млрд"), (1e6, "млн"), (1e3, "тыс.")):
            if abs(value) >= scale:
                return f"{value / scale:,.2f}".rstrip("0").rstrip(".").replace(",", " ") + " " + label
    text = f"{value:,.8f}".rstrip("0").rstrip(".")
    if value and text in ("0", "-0"):
        text = format(Decimal(str(value)), "f")
    return text.replace(",", " ")


def when(milliseconds: Any) -> str:
    if (
        isinstance(milliseconds, bool)
        or not isinstance(milliseconds, (int, float))
        or not math.isfinite(milliseconds)
        or milliseconds <= 0
    ):
        return "время неизвестно"
    try:
        return datetime.fromtimestamp(milliseconds / 1000, timezone(timedelta(hours=3))).strftime(
            "%d.%m.%Y %H:%M:%S МСК"
        )
    except (ValueError, OverflowError, OSError):
        return "время вне диапазона"


def market(value: str) -> str:
    return {"spot": "Spot USDT", "linear": "USDT Perpetual"}.get(value, value)


def keyboard(rows: list[list[tuple[str, str]]], *, back: str | None = "home") -> InlineKeyboardMarkup:
    result = [[InlineKeyboardButton(label, callback_data=data) for label, data in row] for row in rows]
    if back:
        result.append(
            [
                InlineKeyboardButton(
                    "← Главное меню" if back == "home" else "← Назад", callback_data="ui:" + back
                )
            ]
        )
    return InlineKeyboardMarkup(result)


def split_message(text: str, limit: int = 3500) -> list[str]:
    """Split on paragraphs/lines/words and count Telegram's UTF-16 limit."""
    if limit < 128:
        raise ValueError("Message limit too small")
    chunks: list[str] = []
    current = ""
    size = 0
    for token in re.findall(r"\S+\s*|\s+", text):
        units = len(token.encode("utf-16-le")) // 2
        if size + units <= limit:
            current += token
            size += units
            continue
        if current:
            chunks.append(current.rstrip())
            current, size = "", 0
        if units > limit:
            # Only an individually oversized unbroken token needs a character fallback.
            for char in token:
                char_size = len(char.encode("utf-16-le")) // 2
                if size + char_size > limit:
                    chunks.append(current)
                    current, size = "", 0
                current += char
                size += char_size
        else:
            current, size = token, units
    if current or not chunks:
        chunks.append(current.rstrip())
    return chunks


def settings_summary(snapshot: Snapshot) -> str:
    s = snapshot.settings
    p = effective_parameters(s)
    prices = " · ".join(f"{tf}: {amount(p['price_' + tf])}%" for tf in TIMEFRAMES)
    return (
        "⚙️ Настройки радара\n"
        f"Профиль: {PROFILE[s.profile]}\n\n"
        "🎚 Пороги срабатывания\n"
        f"Цена: {prices}\n"
        f"Оборот: ×{amount(p['turnover_ratio'])}; среднее за {int(p['turnover_lookback'])} свечей\n"
        f"Минимальный оборот 24ч: {amount(s.liquidity_floor_24h, compact=True)} USDT\n"
        f"\nРежим: {'Весь подходящий рынок' if s.alert_universe_mode == 'top' else 'Мой список: ' + str(len(s.instruments()))}\n"
        "Возможности: Spot USDT + USDT Perpetual · 1m / 3m / 5m / 15m · 5 детекторов\n"
        "Параметры каждого детектора и технические значения доступны ниже."
    )


def parameters_text(snapshot: Snapshot) -> str:
    s = snapshot.settings
    p = effective_parameters(s)
    return (
        "🎚 Параметры детекторов\n"
        + "\n".join(f"• {FIELDS.get(key, key)}: {amount(value)}" for key, value in p.items())
        + f"\n\nМой список: множитель {amount(s.watchlist_multiplier)} к порогам, окна неизменны."
        + f"\nСлужебная сводка: каждые {s.radar_poll_sec} с; это не частота WebSocket и не TF."
        + f"\nКарточка/рынок ввода без префикса: {market(s.bybit_category)}, {s.signal_timeframe}."
        + "\nВсе четыре TF работают одновременно. Выбор карточки не отключает другие TF."
        + f"\n\nВерсия настроек: {snapshot.revision}\nТочные ключи: "
        + ", ".join(f"{key}={value!r}" for key, value in p.items())
    )


def profile_text(snapshot: Snapshot, name: str | None = None) -> str:
    s = snapshot.settings
    if name is None:
        return (
            f"🎛 Профили чувствительности\nАктивен: {PROFILE[s.profile]}\n\n"
            "• Обычный — базовые пороги.\n• Строгий — пороги ×1,5.\n• Чувствительный — пороги ×0,75.\n\n"
            "Выбор сначала показывает значения. Применение заменяет пороги и окна формул всех пяти детекторов. "
            "Если активны свои пороги, перед применением они сохраняются для возврата. "
            "Чувствительность не означает прибыльность."
        )
    p = dict(preset(name))
    return (
        f"{PROFILE[name]}: значения после применения\n"
        + "\n".join(f"{FIELDS.get(key, key)}: {amount(value)}" for key, value in p.items())
        + "\nИзменятся только пороги/окна детекторов; список, доставка и минимальный оборот 24ч сохраняются."
    )


def observation_kind(event: dict[str, Any]) -> str:
    reason = event.get("observation_reason", "")
    return {
        "historical_observation": "историческое наблюдение",
        "awaiting_rearm": "повтор до нового независимого движения",
        "live_observation": "живое наблюдение",
    }.get(reason, "живое наблюдение" if event.get("live") else "историческое/подавленное наблюдение")


def event_summary(event: dict[str, Any], now_ms: int) -> str:
    direction = DIRECTION.get(event["direction"], event["direction"])
    lines = [
        f"{event['symbol']} · {market(event['market_type'])}",
        f"{FAMILY.get(event['detector_id'], event['detector_id'])} · {event['timeframe']} · {direction}",
        "",
    ]
    for key, value in event["metrics"].items():
        if key.endswith(("threshold", "floor")) or key in ("atr14", "range_high", "range_low"):
            continue
        unit = event.get("units", {}).get(key, "")
        display_value = round(value, 2) if unit in ("%", "ratio") and abs(value) >= 0.01 else value
        lines.append(
            f"• {METRIC.get(key, key)}: {amount(display_value, compact=unit == 'USDT')} {'×' if unit == 'ratio' else unit}".rstrip()
        )
    lines += [
        f"Интенсивность: ×{amount(round(event['strength'], 2))} от порога",
        "",
        f"Свежесть: {FRESHNESS.get(freshness(event, now_ms), 'свежесть неизвестна')} · "
        + ("данные полные" if event.get("completeness") == "COMPLETE" else "данные неполные"),
        "Время наблюдения: " + when(event.get("market_observation_ms")),
    ]
    context = event.get("enrichment", {})
    if context.get("metrics"):
        lines.append("Есть дополнительный контекст деривативов — в подробностях.")
    return "\n".join(lines)


def source_summary(snapshot: Snapshot, ingestion: dict[str, Any], now: float) -> str:
    s = snapshot.settings
    age = now - ingestion.get("at", 0)
    fresh = 0 <= age <= 30
    scope = (
        "весь подходящий рынок"
        if s.alert_universe_mode == "top"
        else f"мой список, {len(s.instruments())} инструментов"
    )
    result = [f"Выбранный охват: {scope}"]
    if s.alert_universe_mode == "custom" and not s.instruments():
        result.append("Список пуст: выбранных инструментов нет. Добавьте монету или выберите весь рынок.")
    if not ingestion or "active_streams" not in ingestion:
        result.append("Состояние источников пока неизвестно.")
    else:
        result.append(
            f"Источник: {amount(ingestion['active_streams'])} потоков инструмент/рынок/TF; "
            + (f"сводка {amount(round(max(age, 0), 1))} с назад" if fresh else "сводка устарела")
        )
        result.append(
            f"Пробелы: {amount(ingestion.get('historical_gaps_pending'))}; "
            f"заблокировано: {amount(ingestion.get('blocked_streams'))}; "
            f"восстановление — ожидают: {amount(ingestion.get('recovery_pending'))}, "
            f"в работе: {amount(ingestion.get('recovery_working'))}."
        )
    if ingestion.get("configuration_revision") != snapshot.revision:
        result.append("Применение текущего охвата к источнику еще не подтверждено этой сводкой.")
    discovery = ingestion.get("discovery")
    if not isinstance(discovery, dict) or set(discovery) != {"spot", "linear"}:
        result.append("Готовность метаданных и цен обоих рынков этой сводкой не подтверждена.")
    elif any(not item.get("metadata_ready") or not item.get("tickers_ready") for item in discovery.values()):
        result.append("Начальная подготовка: метаданные или цены ещё не получены для всех рынков.")
    if ingestion.get("market_errors"):
        result.append("Есть ограничения источника; причины доступны в диагностике.")
    return "\n".join(result)


def diagnostic_summary(data: dict[str, Any]) -> str:
    ingestion = data.get("ingestion", {})
    analytics = data.get("analytics", {})
    storage = data.get("storage", {})
    agent = data.get("agent_delivery", {})
    lines = [
        "🩺 Диагностика — сводка",
        f"Снята: {when(data.get('at_ms'))}",
        "",
        f"Источники: {STATE.get(ingestion.get('diagnostic_state'), 'неизвестно')}, возраст {amount(ingestion.get('diagnostic_age_seconds'))} с",
        f"Потоки: {amount(ingestion.get('active_streams'))}; пробелы: {amount(ingestion.get('historical_gaps_pending'))}",
        f"Последний цикл аналитики: {STATE.get(analytics.get('state'), 'неизвестно')}; {when(analytics.get('at_ms'))}",
    ]
    if storage.get("working_bytes") is not None:
        lines.append(
            f"База + журналы сейчас: {amount(round(storage['working_bytes'] / 1e9, 2))} ГБ / "
            f"{amount(storage.get('budget_bytes', BUDGET) / 1e9)} ГБ"
        )
    if storage.get("projected_working_bytes") is not None:
        lines.append(
            f"Модель заполненной базы: {amount(round(storage['projected_working_bytes'] / 1e9, 2))} ГБ; "
            "это прогноз, не текущий размер и не доказанная верхняя граница."
        )
    if storage.get("indexes", {}).get("state") == "STALE":
        lines.append("Отдельный снимок индексов устарел; индексы уже входят в размер базы.")
    if data.get("radar", {}).get("detector_errors"):
        lines.append(
            "Есть сообщения о недоступных расчётах детекторов; это не обязательно сбой подключения. См. полный отчёт."
        )
    lines += [
        "",
        "Контекст деривативов (необязательно): " + toggle_state(data.get("enrichment", {}).get("enabled")),
        "Доставка агенту: " + agent_state(agent),
        "Дайджест: " + toggle_state(data.get("digest", {}).get("preferences", {}).get("enabled")),
        "Ожидающие задачи включают еще не наступившие сроки. Число pending не равно числу просроченных. "
        "Подробности очередей и точные данные — в полном отчёте.",
    ]
    return "\n".join(lines)


def toggle_state(value: Any) -> str:
    if type(value) is not bool:
        return "неизвестно"
    return "включён" if value else "выключен"


def agent_state(value: dict[str, Any]) -> str:
    if value.get("configuration") == "UNSET":
        return "не подключена"
    if value.get("configuration") == "BLOCKED":
        return "ошибка настройки подключения"
    if value.get("configuration") != "READY" or type(value.get("enabled")) is not bool:
        return "состояние неизвестно"
    return "включена" if value["enabled"] else "выключена"


def diagnostic_section(data: dict[str, Any], section: str) -> str:
    if section == "storage":
        value = data.get("storage", {})
        lines = ["Хранение — факт и модель", "Снимок: " + when(value.get("at_ms"))]
        for key, label in (
            ("working_bytes", "База и журналы сейчас"),
            ("target_bytes", "Желаемый объём"),
            ("budget_bytes", "Допустимый предел"),
            ("projected_working_bytes", "Модель заполненной базы"),
        ):
            lines.append(
                f"{label}: "
                + (
                    amount(value[key] / 1e9) + " ГБ"
                    if isinstance(value.get(key), (int, float))
                    else "не измерено"
                )
            )
        for key, label in (
            ("database", "Файл базы, включая индексы"),
            ("wal", "Журнал WAL"),
            ("shm", "Служебный SHM"),
        ):
            value_bytes = value.get("files", {}).get(key)
            lines.append(
                f"{label}: "
                + (
                    amount(value_bytes / 1e6) + " МБ"
                    if isinstance(value_bytes, (int, float))
                    else "не измерено"
                )
            )
        lines.append(
            "Модель не равна занятому диску и не является доказанной верхней границей. Bootstrap, короткое окно и изменения конфигурации ограничивают выводы. Индексы не прибавляются к базе повторно; backups учитываются отдельно."
        )
        return "\n".join(lines)
    if section == "queues":
        outcomes = data.get("outcomes", {})
        analytics = data.get("analytics", {})
        return (
            "Очереди — снимок "
            + when(data.get("at_ms"))
            + f"\nРезультаты событий ожидают: {amount(outcomes.get('pending'))}"
            + f"\nСравнения ожидают: {amount(outcomes.get('comparison_pending'))}"
            + f"\nВосстановление данных: {amount(data.get('analytics_recovery', {}).get('pending'))}"
            + f"\nРабочий цикл: {STATE.get(analytics.get('state'), 'неизвестно')}; {when(analytics.get('at_ms'))}"
            + f"\nЗа этот цикл: результатов {amount(analytics.get('processed_outcomes'))}, сравнений {amount(analytics.get('processed_comparisons'))}."
            + "\nОжидание включает будущие сроки. Просрочено и возраст самой старой задачи: не измерены этим снимком. Неполный результат не подменяется нулём."
        )
    value = data.get("ingestion", {})
    return (
        "Источники — снимок "
        + when(data.get("at_ms"))
        + f"\n{STATE.get(value.get('diagnostic_state'), 'состояние неизвестно')}; возраст сводки {amount(value.get('diagnostic_age_seconds'))} с"
        + f"\nПотоков монета/рынок/TF: {amount(value.get('active_streams'))}"
        + f"\nМетаданные Spot: {amount(value.get('eligible_metadata', {}).get('spot'))}; Perpetual: {amount(value.get('eligible_metadata', {}).get('linear'))}"
        + f"\nНе хватает начальной истории: {amount(value.get('bootstrap_pending_streams'))}; пробелы: {amount(value.get('historical_gaps_pending'))}"
        + f"\nЗаблокировано: {amount(value.get('blocked_streams'))}; восстановление: {amount(value.get('recovery_pending'))}"
        + "\nСвежесть сводки не доказывает свежесть каждой свечи. Точные состояния соединений и причины доступны в полном файле."
    )


HELP = (
    "❔ Помощь\nРадар находит необычную активность; он не выставляет ордера.\n\n"
    "• Статус — работа источников и доставки.\n• Радар — охват и мой список.\n"
    "• История — сохранённые наблюдения и их результаты.\n• Аналитика — статистика и дайджест.\n"
    "• Настройки — профили, пороги и уведомления.\n\n"
    "Начните с примера или выберите понятие. Команды остаются дополнительным способом управления."
)
TERMS_SHORT = (
    "📖 Как читать сигналы\n\n"
    "• Цена: изменение закрытой свечи к предыдущей; рост и падение различаются.\n"
    "• Всплеск оборота: обычно 100 тыс. USDT, сейчас 300 тыс. — это ×3. База — предыдущие свечи, текущая исключена.\n"
    "• Пробой: цена закрылась за прежним диапазоном с отступом от обычной волатильности.\n"
    "• Расширение диапазона: свеча необычно широкая; само по себе не задаёт направление.\n"
    "• Совпадение: одновременно выполнены условия цены, оборота и минимального оборота 24ч.\n\n"
    "Интенсивность ×2: превышение порога вдвое, не вероятность успеха.\n"
    "Эпизод объединяет подтверждения. Новое семейство/TF или усиление объясняют повторное уведомление.\n"
    "Свежесть: для обычного события 1m свежее окно — 1 минута, истечение через 2; для пробоя — 2/4 минуты. "
    "Поздняя доставка не обновляет исходное время.\n"
    "История включает наблюдения без Telegram-уведомления. Результаты — последующее движение, не доходность стратегии."
)
