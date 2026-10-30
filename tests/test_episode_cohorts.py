"""Descriptive groups share an immutable episode result without inflating N."""

from contextlib import closing

import pytest

from cryptowatcher.analytics import cohort_rows, cohorts
from cryptowatcher.events import EventEngine, identity
from cryptowatcher.market_storage import enable
from cryptowatcher.outcomes import MINUTE, OutcomeWorker
from cryptowatcher.sources import Candle, Sources
from cryptowatcher.storage import Store

from .test_events import NOW, observe, selected


@pytest.mark.parametrize("bounded", [False, True])
def test_episode_groups_count_confirmations_once_and_respect_causal_membership(store, bounded):
    if bounded:
        enable(store.db)
        store.bounded = True
    store.clock_ms = lambda: NOW
    worker = OutcomeWorker(store)
    engine = EventEngine(store, selected("price", "composite"), on_event=worker.admit)
    for market in ("spot", "linear"):
        observe(engine, market=market)
        observe(engine, NOW + MINUTE, market=market)
        observe(engine, NOW + 2 * MINUTE, market=market)
        observe(engine, NOW + 3 * MINUTE, tf="3m", market=market)
        observe(engine, NOW + 4 * MINUTE, close=104, market=market)
    events = engine.history.events()
    assert len({e["episode_id"] for e in events}) == 2
    assert len(events) > 4
    assert len(engine.history.items("outcome-job:")) == 8
    end = NOW + 60 * MINUTE
    for market in ("spot", "linear"):
        stream = f"{market}/BTCUSDT/1m"
        sources = Sources(store)
        sources.load(stream)
        sources.put(
            stream,
            [
                sources.windows[stream].get(t)
                or Candle.parse([t, 102, 104, 101, 103, 1, 400], "1m", "rest", end, end)
                for t in range(NOW, end, MINUTE)
            ],
        )
    worker.work(end)
    assert all(o["state"] == "COMPLETE" for _, o in engine.history.items("outcome-head:"))
    # A late-available confirmation must not leak into a historical cutoff.
    origin = next(e for e in events if e["market_type"] == "spot")
    with store.db:
        engine.history.version(
            "event",
            {
                **origin,
                "event_id": identity("late-available-confirmation"),
                "revision": 1,
                "detector_id": "volatility",
                "available_at_ms": end + 30 * MINUTE,
            },
        )
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        total = cohorts(reader, ("instrument",))
        assert total["sample_units_by_method"]["2"] == "UNIQUE_EPISODES"
        assert total["groups_overlap"] is True
        assert len(total["cohorts"]) == 4
        assert {r["sample_size"] for r in total["cohorts"]} == {2}
        grouped = cohorts(reader, ("detector", "timeframe", "market"), limit=100)["cohorts"]
        assert {r["sample_size"] for r in grouped} == {1}
        assert {r["group"]["detector"] for r in grouped} == {"price", "composite", "volatility"}
        assert {r["group"]["timeframe"] for r in grouped} == {"1m", "3m"}
        causal = cohorts(reader, ("detector",), cutoff_ms=end + MINUTE)["cohorts"]
        assert {r["group"]["detector"] for r in causal} == {"price", "composite"}
        assert {r["sample_size"] for r in causal} == {2}
        assert list(cohort_rows(reader, ("market",), start_ms=NOW + MINUTE, end_ms=NOW + 5 * MINUTE)) == []
        assert reader.db.total_changes == 0
