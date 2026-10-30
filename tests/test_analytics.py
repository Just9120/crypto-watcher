from dataclasses import asdict

import pytest

from cryptowatcher.analytics import ComparisonWorker, as_of, cohorts, comparison_dataset, decision_context
from cryptowatcher.config import OwnerSettings
from cryptowatcher.detector_config import effective_parameters
from cryptowatcher.events import EventEngine, History
from cryptowatcher.outcomes import DAY, MINUTE, OutcomeWorker
from cryptowatcher.source_context import save
from cryptowatcher.sources import Candle, Sources
from cryptowatcher.storage import Store, encode

NOW = 1_800_000_000_000


def series(store, days=2):
    start = NOW - days * DAY - 20 * MINUTE
    candles = [
        Candle.parse([t, 100, 101, 99, 100, 1, 400], "1m", "rest", t + MINUTE, t + MINUTE)
        for t in range(start, NOW + MINUTE, MINUTE)
    ]
    if store.bounded:
        Sources(store).put("spot/BTCUSDT/1m", candles)
        return candles
    with store.db:
        store.db.executemany(
            "INSERT INTO runtime VALUES (?,?)",
            ((Sources.prefix("spot/BTCUSDT/1m") + f"{c.start:016d}", encode(asdict(c))) for c in candles),
        )
    return candles


def event(store, family="price"):
    settings = store.snapshot().settings.changed(outcome_horizons=(5, 15))
    return {
        "event_id": "fixture",
        "revision": 1,
        "episode_id": "episode",
        "available_at_ms": NOW,
        "reference_time_ms": NOW,
        "reference_price": 100,
        "reference_atr14": 2,
        "direction": "UP",
        "continuation_threshold_pct": 99,
        "timeframe": "1m",
        "symbol": "BTCUSDT",
        "market_type": "spot",
        "detector_id": family,
        "detector_version": "1",
        "settings_snapshot": settings.payload(),
        "effective_parameters": effective_parameters(settings, "spot/BTCUSDT"),
        "metrics": {"liquidity_24h": 1e20},
        "strength": 2,
        "source_inputs": [],
    }


@pytest.mark.parametrize("bounded", [False, True])
def test_prior_complete_matching_quartile_nonoverlap_and_frozen_context(tmp_path, bounded):
    store = Store(tmp_path / "state.sqlite", 233382575)
    store.initialize(OwnerSettings())
    if bounded:
        from cryptowatcher.market_storage import enable

        enable(store.db)
        store.bounded = True
    series(store)
    result = comparison_dataset(store, event(store))
    assert result["sample_size"] > 0 and result["sample_size"] <= 20
    windows = sorted(
        (s["reference_time_ms"], s["reference_time_ms"] + 15 * MINUTE) for s in result["samples"]
    )
    assert all(end < NOW for _, end in windows)
    assert all(begin >= end for (_, end), (begin, _) in zip(windows, windows[1:]))
    for sample in result["samples"]:
        assert sample["reference_time_ms"] // 3_600_000 % 24 == NOW // 3_600_000 % 24
        assert sample["quartile"] == result["event_quartile"]
        assert sample["outcomes"]["5"]["noise_threshold_pct"] is None
        assert sample["outcomes"]["5"]["classification"] == "UNCLASSIFIED"
        assert sample["outcomes"]["15"]["observed_candles"] == 15
    future_version = event(store)
    future_version["detector_version"] = "unimplemented"
    assert comparison_dataset(store, future_version)["limitations"] == ["FROZEN_DETECTOR_VERSION_UNAVAILABLE"]


@pytest.mark.parametrize("bounded", [False, True])
def test_composite_never_uses_future_liquidity_or_original_event_noise(tmp_path, bounded):
    store = Store(tmp_path / "state.sqlite", 233382575)
    store.initialize(OwnerSettings())
    if bounded:
        from cryptowatcher.market_storage import enable

        enable(store.db)
        store.bounded = True
    series(store)
    value = event(store, "composite")
    assert comparison_dataset(store, value)["sample_size"] == 0
    with store.db:
        save(
            store,
            "ticker",
            "spot/BTCUSDT",
            {
                "available_at_ms": NOW,
                "market_observation_ms": NOW,
                "values": {"turnover24h": "1000000"},
            },
        )
    assert comparison_dataset(store, value)["sample_size"] == 0
    prior = NOW - DAY
    with store.db:
        for offset in range(-60, 61, 5):
            t = prior + offset * MINUTE
            save(
                store,
                "ticker",
                "spot/BTCUSDT",
                {
                    "available_at_ms": t,
                    "market_observation_ms": t,
                    "values": {"turnover24h": "1000000"},
                },
            )
        save(
            store,
            "instrument",
            "spot/BTCUSDT",
            {
                "available_at_ms": prior - DAY,
                "market_observation_ms": prior - DAY,
                "tick_size": 0.01,
            },
        )
    result = comparison_dataset(store, value)
    assert result["sample_size"] > 0
    sample = result["samples"][0]
    assert sample["outcomes"]["5"]["noise_threshold_pct"] == pytest.approx(0.2)
    assert sample["source_context"]["ticker"]["available_at_ms"] <= sample["reference_time_ms"]


def test_workers_restart_protection_cohorts_and_causal_revisions(tmp_path):
    path = tmp_path / "state.sqlite"
    store = Store(path, 233382575)
    store.initialize(OwnerSettings())
    worker = OutcomeWorker(store)
    candles = series(store)
    settings = store.snapshot().settings.changed(outcome_horizons=(5,))
    store.update(store.snapshot().revision, settings)
    # A live price event with full prior ATR; comparisons exclude detector signals.
    last = candles[-2]
    spike = Candle.parse([last.start, 100, 105, 99, 102, 1, 1200], "1m", "websocket", NOW, NOW)
    engine = EventEngine(store, on_event=worker.admit)
    engine.observe("spot/BTCUSDT/1m", candles[-25:-2] + [spike], 1e6, 0.01, NOW, live=True)
    history = History(store)
    produced = history.items("event-head:")[0][1]
    comparison = ComparisonWorker(store)
    while history.items("comparison-pending:"):
        comparison.work(NOW)
    assert not history.items("comparison-head:")  # No automatic N20 research.
    assert history.items("outcome-job:") and history.items("protected:")
    assert decision_context(history, produced["event_id"], NOW - 1)["event"] is None
    initial = decision_context(history, produced["event_id"], NOW)
    assert initial["episode"]["available_at_ms"] <= NOW and "outcome" not in initial
    later = {**produced, "revision": 2, "available_at_ms": NOW + MINUTE, "strength": 9}
    with store.db:
        history.version("event", later)
    assert as_of(history, "event", produced["event_id"], NOW)["revision"] == 1
    assert as_of(history, "event", produced["event_id"], NOW + MINUTE)["revision"] == 2
    store.close()
    store = Store(path, 233382575)
    assert ComparisonWorker(store).history.items("comparison-pending:") == []
    # No fabricated returns for uncompleted horizons.
    assert cohorts(store)["cohorts"] == []
    with pytest.raises(ValueError):
        cohorts(store, ("secret",))
