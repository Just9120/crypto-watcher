"""Versioned deterministic parameters; presets require an explicit owner operation."""

from __future__ import annotations

import math
from typing import Any

FAMILIES = ("price", "turnover", "breakout", "volatility", "composite")
PROFILE_MULTIPLIERS = {"normal": 1.0, "conservative": 1.5, "sensitive": 0.75}
PARAMETERS = {
    "price_1m": 1.0,
    "price_3m": 2.0,
    "price_5m": 2.5,
    "price_15m": 4.0,
    "turnover_ratio": 3.0,
    "turnover_lookback": 12,
    "breakout_lookback": 20,
    "breakout_buffer": 0.1,
    "volatility_ratio": 2.0,
    "volatility_lookback": 20,
}


def validate_parameters(values: Any) -> tuple[tuple[str, float], ...]:
    if not isinstance(values, (list, tuple)):
        raise ValueError("Invalid detector_parameters")
    parsed: dict[str, float] = {}
    for item in values:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError("Invalid detector_parameters")
        key, value = item
        if not isinstance(key, str) or key not in PARAMETERS or key in parsed or isinstance(value, bool):
            raise ValueError("Invalid detector parameter")
        try:
            number = float(value)
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError("Invalid detector parameter") from exc
        if not math.isfinite(number) or number <= 0:
            raise ValueError("Invalid detector parameter")
        if key.endswith("_ratio") and number <= 1:
            raise ValueError("Detector ratio must exceed one")
        if key.endswith("_lookback") and not number.is_integer():
            raise ValueError("Detector lookback must be a positive integer")
        parsed[key] = number
    return tuple(sorted(parsed.items()))


def effective_parameters(settings: Any, identity: str = "") -> dict[str, float]:
    result = dict(PARAMETERS)
    # Legacy persisted thresholds are a custom profile, never silently replaced on upgrade.
    for tf in ("1m", "3m", "5m", "15m"):
        result[f"price_{tf}"] = settings.price_move_min
    result["turnover_ratio"] = settings.turnover_spike_min
    result["turnover_lookback"] = settings.sma_periods
    result.update(dict(settings.detector_parameters))
    if identity and identity in settings.instruments():
        for key in result:
            if not key.endswith("_lookback"):
                result[key] *= settings.watchlist_multiplier
    return result


def preset(name: str) -> tuple[tuple[str, float], ...]:
    if name not in PROFILE_MULTIPLIERS:
        raise ValueError("Unknown sensitivity profile")
    multiplier = PROFILE_MULTIPLIERS[name]
    return tuple(
        (key, float(value) if key.endswith("_lookback") else value * multiplier)
        for key, value in PARAMETERS.items()
    )


def validate_effective_configuration(settings: Any) -> None:
    """Admission guard only: historical settings remain readable unchanged."""
    if settings.candle_retention_days != 3:
        raise ValueError("Общее окно свечей — 72 часа (3 суток); прежние настройки требуют явного перехода")
    if settings.detail_retention_days != 30:
        raise ValueError("Срок подробностей — 30 дней")
    if max(settings.outcome_horizons) > 72 * 60:
        raise ValueError("Срок оценки результата не может превышать 72 часа (4320 минут)")
    for instrument in ("", *settings.instruments()):
        values = effective_parameters(settings, instrument)
        for key in ("turnover_ratio", "volatility_ratio"):
            if values[key] <= 1:
                raise ValueError(
                    f"{instrument or 'Общие пороги'}: {key} должен быть больше 1 с учётом множителя"
                )
        required = max(
            16,
            values["turnover_lookback"] + 1,
            values["breakout_lookback"] + 1,
            values["volatility_lookback"] + 2,
        )
        if required * 15 > 72 * 60:
            raise ValueError("Lookback с текущей свечой не может превышать 72 часа на 15m")
        if required > 256:
            raise ValueError(f"Для расчёта требуется {int(required)} свечей; доступное окно — 256")
