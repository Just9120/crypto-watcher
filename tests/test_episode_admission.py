"""Meaningful episode updates, a single immutable outcome origin and bounded liveness."""

from contextlib import closing

import pytest

from cryptowatcher.config import OwnerSettings
from cryptowatcher.detectors import Detection
from cryptowatcher.events import EventEngine
from cryptowatcher.market_storage import enable
from cryptowatcher.outcomes import OutcomeWorker
from cryptowatcher.sources import Candle
from cryptowatcher.storage import StateError, Store

from .test_events import NOW, window


@pytest.mark.parametrize("bounded", [False, True])
def test_repeated_candles_keep_episode_alive_without_research_and_new_tf_keeps_origin(tmp_path, bounded):
    path = tmp_path / "episode.db"
    clock = [NOW]
    captures = []
    with closing(Store(path, 233382575)) as store:
        store.initialize(OwnerSettings(active_detectors=("price",), alert_detectors=()))
        if bounded:
            enable(store.db)
            store.bounded = True
        store.clock_ms = lambda: clock[0]
        worker = OutcomeWorker(store)

        def signal(family, candles, *args):
            return Detection(family, True, "UP", 1, {}, tuple(candles[-2:]))

        def context():
            captures.append(clock[0])
            return {"ticker": None, "instrument": None}, {"state": "DISABLED"}

        engine = EventEngine(store, signal, on_event=worker.admit)
        candles = window()
        for index in range(3):
            if index:
                previous = candles[-1]
                clock[0] += 60_000
                candles = [
                    *candles[1:],
                    Candle.parse(
                        [previous.end, 102, 103, 101, 102, 1, 400],
                        "1m",
                        "websocket",
                        clock[0],
                        clock[0],
                    ),
                ]
            engine.observe(
                "spot/BTCUSDT/1m", candles, 1e6, 0.01, clock[0], live=True, context_provider=context
            )
        original = engine.history.events()[0]
        assert len(engine.history.events()) == 1
        assert captures == [NOW]
        assert engine.history.get("episode-head:" + original["episode_id"])["revision"] == 1
        engine.tick(clock[0] + 1)
        assert len(engine.history.items("active:")) == 1  # Not closed from stale immutable revision.
        clock[0] += 60_000
        engine.observe("spot/BTCUSDT/3m", window(now=clock[0], tf="3m"), 1e6, 0.01, clock[0], live=True)
        events = engine.history.events()
        assert len(events) == 2
        assert len({e["episode_id"] for e in events}) == 1
        assert {e["reference_time_ms"] for e in events} == {NOW}
        assert {e["reference_price"] for e in events} == {102}
        assert len(engine.history.items("outcome-job:")) == 4
        assert not engine.history.items("comparison-job:")
        saved_episode = engine.history.get("episode-head:" + original["episode_id"])
        with pytest.raises(StateError, match="Episode outcome reference is immutable"), store.db:
            engine.history.version(
                "episode",
                {**saved_episode, "revision": saved_episode["revision"] + 1, "reference_price": 999},
            )
        assert engine.history.get(f"event:{original['event_id']}:{original['revision']:010d}") == original
    with closing(Store(path, 233382575)) as restored:
        restarted = EventEngine(restored, signal, on_event=OutcomeWorker(restored).admit)
        restarted.tick(clock[0] + 360_000)
        episode = restarted.history.get("episode-head:" + original["episode_id"])
        assert episode["state"] == "CLOSED"
        assert episode["origin_event_id"] == original["event_id"]
        assert len(restarted.history.items("outcome-job:")) == 4
