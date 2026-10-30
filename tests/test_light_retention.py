"""72-hour general data and 30-day ownership of actually used source inputs."""

from cryptowatcher.events import EventEngine
from cryptowatcher.market_storage import enable, read, read_context
from cryptowatcher.outcomes import DAY, Retention
from cryptowatcher.source_context import context, save
from cryptowatcher.sources import Candle, Sources

from .test_events import NOW, selected


def test_general_candle_and_context_expire_exactly_at_72h_while_used_inputs_survive(store):
    enable(store.db)
    store.bounded = True
    clock = [NOW]
    store.clock_ms = lambda: clock[0]
    unused = Candle.parse([NOW - 60_000, 100, 101, 99, 100, 1, 400], "1m", "rest", NOW, NOW)
    Sources(store).put("spot/ETHUSDT/1m", [unused])
    with store.db:
        for symbol in ("BTCUSDT", "ETHUSDT"):
            for kind in ("ticker", "instrument"):
                save(
                    store,
                    kind,
                    "spot/" + symbol,
                    {
                        "available_at_ms": NOW,
                        "market_observation_ms": NOW,
                        "values": {"turnover24h": "2000000"} if kind == "ticker" else {"tickSize": "0.01"},
                    },
                )
    engine = EventEngine(store, selected("price"))
    from .test_events import window

    engine.observe(
        "spot/BTCUSDT/1m",
        window(),
        1e6,
        0.01,
        NOW,
        live=True,
        source_context=context(store, "spot/BTCUSDT", NOW),
    )
    frozen = engine.history.events()[0]
    retention = Retention(store)
    clock[0] = NOW + 3 * DAY - 1
    retention.work(clock[0])
    assert read(store.db, "spot/ETHUSDT/1m")
    assert read_context(store.db, "ticker", "spot/ETHUSDT", clock[0]) is not None
    clock[0] += 1
    retention.work(clock[0])
    assert read(store.db, "spot/ETHUSDT/1m") == []
    assert read_context(store.db, "ticker", "spot/ETHUSDT", clock[0]) is None
    assert engine.history.get(f"event:{frozen['event_id']}:0000000001") == frozen
    assert read(store.db, "spot/BTCUSDT/1m")
    assert read_context(store.db, "ticker", "spot/BTCUSDT", clock[0]) is not None
    clock[0] = NOW + 30 * DAY - 1
    retention.work(clock[0])
    assert engine.history.get(f"event:{frozen['event_id']}:0000000001") == frozen
    clock[0] += 2
    for _ in range(20):
        retention.work(clock[0])
        if not engine.history.count("event-head:"):
            break
    assert not engine.history.count("event-head:")
    assert read(store.db, "spot/BTCUSDT/1m") == []
    assert read_context(store.db, "ticker", "spot/BTCUSDT", clock[0]) is None
    assert store.db.execute("SELECT count(*) FROM market_episode_results").fetchone()[0] == 4


def test_72h_outcome_boundary_reads_complete_frozen_range_and_excludes_future_candle(store):
    import pytest

    from cryptowatcher.outcomes import MINUTE, OutcomeWorker

    from .test_events import observe

    enable(store.db)
    store.bounded = True
    clock = [NOW]
    store.clock_ms = lambda: clock[0]
    before = store.snapshot()
    store.update(before.revision, before.settings.changed(outcome_horizons=(4320,), alert_detectors=()))
    worker = OutcomeWorker(store)
    engine = EventEngine(store, selected("price"), on_event=worker.admit)
    observe(engine)
    origin = engine.history.events()[0]
    end = NOW + 4320 * MINUTE
    inputs = [
        Candle.parse([t, 102, 106, 100, 105 if t == end - MINUTE else 103, 1, 400], "1m", "rest", end, end)
        for t in range(NOW, end, MINUTE)
    ]
    outside = Candle.parse([end, 105, 1000, 100, 999, 1, 400], "1m", "rest", end + MINUTE, end + MINUTE)
    Sources(store).put("spot/BTCUSDT/1m", [*inputs, outside])
    clock[0] = end
    Retention(store).work(clock[0])
    worker.work(clock[0])
    result = engine.history.items("outcome-head:")[0][1]
    assert result["state"] == "COMPLETE"
    assert result["expected_candles"] == result["observed_candles"] == 4320
    assert result["window_start_ms"] == origin["reference_time_ms"] == NOW
    assert result["window_end_ms"] == end
    assert result["metrics"]["price_return_pct"] == pytest.approx((105 / 102 - 1) * 100)
    assert result["metrics"]["mfe_pct"] == pytest.approx((106 / 102 - 1) * 100)
    assert result["metrics"]["mae_pct"] == pytest.approx((100 / 102 - 1) * 100)
    assert len(result["source_inputs"]) == 4320
    assert max(c["start"] for c in result["source_inputs"]) == end - MINUTE
