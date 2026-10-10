from dataclasses import replace

import pytest

from cryptowatcher.config import ConfigError, OwnerSettings
from cryptowatcher.detector_config import FAMILIES, effective_parameters
from cryptowatcher.detectors import DetectorUnavailable, atr14, evaluate, freshness
from cryptowatcher.market import evaluate_composite
from cryptowatcher.sources import Candle

NOW = 1_800_000_000_000


def series(tf="3m", close=102, turnover=1200):
    step = int(tf[:-1]) * 60_000
    return [
        Candle.parse(
            [
                NOW - (24 - i) * step,
                100,
                max(101, close) if i == 23 else 101,
                min(99, close) if i == 23 else 99,
                close if i == 23 else 100,
                1,
                turnover if i == 23 else 400,
            ],
            tf,
            "fixture",
            NOW,
            NOW,
        )
        for i in range(24)
    ]


@pytest.mark.parametrize("tf", ["1m", "3m", "5m", "15m"])
@pytest.mark.parametrize("close,direction", [(102, "UP"), (98, "DOWN")])
def test_custom_composite_preserves_legacy_equality(tf, close, direction):
    candles = series(tf, close)
    settings = OwnerSettings(signal_timeframe=tf)
    detection = evaluate("composite", candles, settings, "spot/BTCUSDT", 1_000_000)
    legacy = evaluate_composite(
        [c.row() for c in reversed(candles)],
        {"lastPrice": str(close), "turnover24h": "1000000"},
        settings,
        NOW,
    )
    assert legacy["meets_signal"] == detection.signal is True
    assert detection.direction == direction and detection.strength == pytest.approx(1)
    assert detection.metrics["turnover_mean"] == 400
    assert detection.metrics["turnover_ratio"] == 3


def test_five_families_and_previous_only_metrics():
    candles = series(close=103)
    candles[-1] = replace(candles[-1], high=110, low=90)
    settings = OwnerSettings()
    results = {f: evaluate(f, candles, settings, "linear/BTCUSDT", 1_000_000) for f in FAMILIES}
    assert all(d.signal for d in results.values())
    assert results["turnover"].direction == results["volatility"].direction == "NEUTRAL"
    assert results["breakout"].metrics["range_high"] == 101
    assert results["breakout"].metrics["atr14"] == 2
    assert results["volatility"].metrics["true_range_mean"] == 2
    assert results["volatility"].metrics["true_range"] == 20
    assert atr14(candles) == 2  # Current extreme range is excluded.
    for detection in results.values():
        assert detection.inputs[-1] is candles[-1]


def test_formula_boundaries_and_detector_isolation():
    settings = OwnerSettings().with_profile("normal")
    candles = series(close=101.2)
    # Exact range+buffer is not strictly above the breakout boundary.
    assert not evaluate("breakout", candles, settings, "spot/BTCUSDT", 1e6).signal
    assert not evaluate("price", candles, settings, "spot/BTCUSDT", 1e6).signal
    zero = [replace(c, turnover=0) for c in candles[:-1]] + candles[-1:]
    with pytest.raises(DetectorUnavailable, match="zero_turnover"):
        evaluate("turnover", zero, settings, "spot/BTCUSDT", 1e6)
    assert evaluate("price", zero, settings.with_parameter("price_3m", 1), "spot/BTCUSDT", 1e6).signal
    with pytest.raises(DetectorUnavailable, match="insufficient"):
        evaluate("breakout", candles[:3], settings, "spot/BTCUSDT", 1e6)
    gap = candles[:10] + candles[11:]
    with pytest.raises(DetectorUnavailable, match="candle_gap"):
        evaluate("volatility", gap, settings, "spot/BTCUSDT", 1e6)


def test_profiles_are_explicit_persistable_and_manual_edits_are_custom():
    legacy = OwnerSettings.from_payload({"price_move_min": 3.7, "turnover_spike_min": 4.2})
    assert legacy.profile == "custom" and legacy.alert_detectors == ("composite",)
    assert effective_parameters(legacy)["price_1m"] == 3.7
    assert effective_parameters(legacy)["turnover_ratio"] == 4.2
    conservative = legacy.with_profile("conservative")
    assert effective_parameters(conservative)["price_15m"] == 6
    assert effective_parameters(conservative)["turnover_lookback"] == 12
    assert OwnerSettings.from_payload(conservative.payload()) == conservative
    manual = conservative.with_parameter("breakout_lookback", 28).changed(price_move_min=2.8)
    assert manual.profile == "custom"
    assert effective_parameters(manual)["breakout_lookback"] == 28
    assert all(effective_parameters(manual)[f"price_{tf}"] == 2.8 for tf in ("1m", "3m", "5m", "15m"))
    watch = conservative.changed(watchlist=("linear/BTCUSDT",))
    assert effective_parameters(watch, "linear/BTCUSDT")["price_1m"] == 1.125
    assert effective_parameters(watch, "spot/BTCUSDT")["price_1m"] == 1.5


@pytest.mark.parametrize(
    "key,value",
    [
        ("unknown", 2),
        ("turnover_ratio", 1),
        ("volatility_ratio", "nan"),
        ("breakout_lookback", 1.5),
        ("price_1m", 0),
        ("breakout_buffer", True),
    ],
)
def test_invalid_manual_parameters_preserve_current(key, value):
    settings = OwnerSettings()
    with pytest.raises(ConfigError):
        settings.with_parameter(key, value)
    assert settings.detector_parameters == ()


def test_type_tf_freshness_uses_immutable_market_time():
    event = {"market_observation_ms": NOW, "fresh_until_ms": NOW + 180_000, "expires_at_ms": NOW + 360_000}
    assert freshness(event, NOW - 1) == "FUTURE"
    assert freshness(event, NOW + 180_000) == "FRESH"
    assert freshness(event, NOW + 180_001) == "STALE"
    assert freshness({**event, "received_ms": NOW + 1_000_000}, NOW + 360_001) == "EXPIRED"
