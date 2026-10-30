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


@pytest.mark.parametrize("bounded", [False, True])
def test_historical_recovery_repeats_share_episode_and_use_market_and_receipt_clocks(tmp_path, bounded):
    from .test_events import observe, selected

    path = tmp_path / "recovery.db"
    with closing(Store(path, 233382575)) as store:
        store.initialize(OwnerSettings(active_detectors=("price", "composite")))
        if bounded:
            enable(store.db)
            store.bounded = True
        store.clock_ms = lambda: NOW
        worker = OutcomeWorker(store)
        closed = []
        engine = EventEngine(
            store, selected("price", "composite"), on_event=worker.admit, on_episode=closed.append
        )
        past = NOW - 120 * 60_000
        for index in range(4):
            engine.observe(
                "spot/BTCUSDT/1m", window(past + index * 60_000), 1e6, 0.01, NOW + index * 1000, live=False
            )
        engine.observe(
            "spot/BTCUSDT/3m", window(past + 3 * 60_000, tf="3m"), 1e6, 0.01, NOW + 4000, live=False
        )
        events = engine.history.events()
        assert len(events) == 4 and len({e["episode_id"] for e in events}) == 1
        assert all(not e["live"] and e["reference_time_ms"] == past for e in events)
        assert len(engine.history.items("outcome-job:")) == 4
        assert not engine.history.items("comparison-job:")
        assert not engine.history.items("active:") and not engine.history.items("pending:telegram:")
        engine.tick(NOW + 5000)
        episode_id = events[0]["episode_id"]
        assert engine.history.get("episode-head:" + episode_id)["state"] == "HISTORICAL"
        assert closed == []  # No new automatic historical closure delivery.
    with closing(Store(path, 233382575)) as reopened:
        reopened.clock_ms = lambda: NOW + 7 * 60_000
        restarted = EventEngine(
            reopened, selected("price", "composite"), on_event=OutcomeWorker(reopened).admit
        )
        restarted.tick(NOW + 7 * 60_000)
        episode = restarted.history.get("episode-head:" + episode_id)
        assert episode["state"] == "CLOSED" and episode["close_reason"] == "inactivity"
        assert not restarted.history.items("historical-active:")
        # Historical expiry did not impose a live re-arm block.
        observe(restarted, NOW + 8 * 60_000)
        assert any(e["live"] for e in restarted.history.events())


@pytest.mark.parametrize("bounded", [False, True])
def test_fresh_historical_episode_promotes_once_and_notifies_without_new_outcome_set(store, bounded):
    from .test_events import observe, selected

    if bounded:
        enable(store.db)
        store.bounded = True
    store.clock_ms = lambda: NOW
    worker = OutcomeWorker(store)
    engine = EventEngine(store, selected("price", "composite"), on_event=worker.admit)
    observe(engine, live=False)
    origin = engine.history.events()[0]
    observe(engine, NOW + 60_000)
    events = engine.history.events()
    assert len(events) == 4 and len({e["episode_id"] for e in events}) == 1
    assert {e["reference_time_ms"] for e in events} == {NOW}
    assert len(engine.history.items("outcome-job:")) == 4
    assert len(engine.history.items("active:")) == 1
    assert not engine.history.items("historical-active:")
    assert len(engine.history.items("pending:telegram:")) == 1
    assert all(e["reference_price"] == origin["reference_price"] for e in events)
    observe(engine, NOW + 120_000)
    assert len(engine.history.events()) == 4  # Promotion was a single meaningful transition.


@pytest.mark.parametrize("bounded", [False, True])
def test_awaiting_rearm_repeats_are_not_per_candle_research(store, bounded):
    from .test_events import observe, selected

    if bounded:
        enable(store.db)
        store.bounded = True
    store.clock_ms = lambda: NOW
    worker = OutcomeWorker(store)
    engine = EventEngine(store, selected("composite"), on_event=worker.admit)
    for minute in range(66):
        observe(engine, NOW + minute * 60_000)
    events = engine.history.events()
    assert len(events) == 2
    assert {e["observation_reason"] for e in events} == {"live_observation", "awaiting_rearm"}
    assert len(engine.history.items("outcome-job:")) == 8
    assert not engine.history.items("active:")
    assert len(engine.history.items("suppressed-active:")) == 1
    for minute in (66, 67):
        observe(engine, NOW + minute * 60_000, close=100)
    observe(engine, NOW + 68 * 60_000)
    assert engine.history.events(1)[0]["live"]
    assert len({e["episode_id"] for e in engine.history.events()}) == 3


@pytest.mark.parametrize("bounded", [False, True])
def test_notification_only_revision_and_duplicate_bar_do_not_create_historical_study(store, bounded):
    from .test_events import observe, selected

    if bounded:
        enable(store.db)
        store.bounded = True
    store.clock_ms = lambda: NOW
    engine = EventEngine(store, selected("price", "composite"), on_event=OutcomeWorker(store).admit)
    observe(engine)
    before = engine.history.events()
    settings = store.snapshot()
    store.update(settings.revision, settings.settings.changed(alert_detectors=()))
    observe(engine)  # Same already closed bar with a new owner revision.
    observe(engine, NOW + 60_000)
    assert engine.history.events() == before
    assert len(engine.history.items("outcome-job:")) == 4
    assert not engine.history.items("historical-active:")
