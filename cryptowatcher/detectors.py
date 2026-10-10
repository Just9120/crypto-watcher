"""Five pure detector families, with previous-only baselines and reproducible inputs."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .config import OwnerSettings
from .detector_config import FAMILIES, effective_parameters
from .sources import Candle

VERSIONS = {family: "1" for family in FAMILIES}
UNITS = {
    "price_return": "%",
    "price_threshold": "%",
    "turnover": "USDT",
    "turnover_mean": "USDT",
    "turnover_ratio": "ratio",
    "turnover_threshold": "ratio",
    "liquidity_24h": "USDT",
    "liquidity_floor": "USDT",
    "atr14": "USDT",
    "true_range": "USDT",
    "true_range_mean": "USDT",
    "volatility_ratio": "ratio",
    "volatility_threshold": "ratio",
    "range_high": "USDT",
    "range_low": "USDT",
    "breakout_buffer": "USDT",
    "breakout_distance": "USDT",
}


class DetectorUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class Detection:
    detector: str
    signal: bool
    direction: str
    strength: float
    metrics: dict[str, float]
    inputs: tuple[Candle, ...]


def true_range(candle: Candle, previous: Candle) -> float:
    return max(candle.high - candle.low, abs(candle.high - previous.close), abs(candle.low - previous.close))


def baseline(candles: list[Candle], count: int) -> list[Candle]:
    if len(candles) < count:
        raise DetectorUnavailable("insufficient_history")
    window = candles[-count:]
    if any(b.start - a.start != b.step for a, b in zip(window, window[1:])):
        raise DetectorUnavailable("candle_gap")
    return window


def atr14(candles: list[Candle]) -> float:
    window = baseline(candles, 16)  # Previous 14 TR each require its own previous close.
    return sum(true_range(window[i], window[i - 1]) for i in range(1, 15)) / 14


def evaluate(
    family: str, candles: list[Candle], settings: OwnerSettings, identity: str, liquidity: float
) -> Detection:
    if family not in FAMILIES:
        raise DetectorUnavailable("unknown_detector")
    parameters = effective_parameters(settings, identity)
    current = baseline(candles, 2)[-1]
    price_return = 100 * (current.close / candles[-2].close - 1)
    metrics: dict[str, float] = {}
    direction = "NEUTRAL"
    signal = False
    strength = 0.0
    count = 2
    if family in ("price", "composite"):
        threshold = parameters[f"price_{current.step // 60_000}m"]
        metrics.update(price_return=price_return, price_threshold=threshold)
        strength = abs(price_return) / threshold
        signal = abs(price_return) >= threshold
        direction = "UP" if price_return > 0 else "DOWN" if price_return < 0 else "NEUTRAL"
    if family in ("turnover", "composite"):
        count = int(parameters["turnover_lookback"]) + 1
        window = baseline(candles, count)
        mean = sum(c.turnover for c in window[:-1]) / (count - 1)
        if mean <= 0:
            raise DetectorUnavailable("zero_turnover_baseline")
        threshold = parameters["turnover_ratio"]
        if threshold <= 1:
            raise DetectorUnavailable("invalid_effective_turnover_ratio")
        ratio = current.turnover / mean
        metrics.update(
            turnover=current.turnover, turnover_mean=mean, turnover_ratio=ratio, turnover_threshold=threshold
        )
        if family == "composite":
            if not math.isfinite(liquidity) or liquidity < 0:
                raise DetectorUnavailable("invalid_liquidity")
            metrics.update(liquidity_24h=liquidity, liquidity_floor=settings.liquidity_floor_24h)
            signal = signal and ratio >= threshold and liquidity >= settings.liquidity_floor_24h
            strength = min(strength, ratio / threshold)
        else:
            signal, strength = ratio >= threshold, ratio / threshold
    if family == "breakout":
        count = max(int(parameters["breakout_lookback"]) + 1, 16)
        window = baseline(candles, count)
        previous = window[-int(parameters["breakout_lookback"]) - 1 : -1]
        high, low = max(c.high for c in previous), min(c.low for c in previous)
        atr = atr14(candles)
        buffer = parameters["breakout_buffer"] * atr
        if buffer <= 0:
            raise DetectorUnavailable("zero_atr_baseline")
        up, down = current.close - high, low - current.close
        distance = max(up, down)
        signal = current.close > high + buffer or current.close < low - buffer
        strength = max(0, distance / buffer)
        direction = "UP" if up > down else "DOWN"
        metrics.update(
            range_high=high, range_low=low, atr14=atr, breakout_buffer=buffer, breakout_distance=distance
        )
    if family == "volatility":
        count = int(parameters["volatility_lookback"]) + 2
        window = baseline(candles, count)
        mean = sum(true_range(window[i], window[i - 1]) for i in range(1, len(window) - 1)) / (
            len(window) - 2
        )
        if mean <= 0:
            raise DetectorUnavailable("zero_true_range_baseline")
        threshold = parameters["volatility_ratio"]
        if threshold <= 1:
            raise DetectorUnavailable("invalid_effective_volatility_ratio")
        tr = true_range(current, candles[-2])
        ratio = tr / mean
        signal, strength = ratio >= threshold, ratio / threshold
        metrics.update(
            true_range=tr, true_range_mean=mean, volatility_ratio=ratio, volatility_threshold=threshold
        )
    if not all(math.isfinite(v) for v in (*metrics.values(), strength)):
        raise DetectorUnavailable("nonfinite_metric")
    return Detection(family, signal, direction, strength, metrics, tuple(baseline(candles, count)))


def freshness(event: dict[str, Any], now_ms: int) -> str:
    age = now_ms - event["market_observation_ms"]
    if age < 0:
        return "FUTURE"
    if now_ms <= event["fresh_until_ms"]:
        return "FRESH"
    return "STALE" if now_ms <= event["expires_at_ms"] else "EXPIRED"
