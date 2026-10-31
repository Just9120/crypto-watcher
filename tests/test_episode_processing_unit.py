"""Episode clock projections and frame-owned inputs preserve full public history."""

import hashlib
from dataclasses import asdict

import pytest

from cryptowatcher.events import HEAD_FIELDS, PREFIX, EventEngine, History
from cryptowatcher.storage import RUNTIME_UPSERT, StateError, encode
from tests.test_bounded_storage import bounded
from tests.test_events import NOW, window


@pytest.mark.parametrize("compact", [False, True])
def test_lifecycle_projection_checks_fresh_identity_and_keeps_full_public_body(store, compact):
    if compact:
        bounded(store)
    engine = EventEngine(store)
    engine.observe("spot/BTCUSDT/1m", window(), 1e6, 0.01, NOW, live=True)
    event = engine.history.events()[0]
    key = "episode-head:" + event["episode_id"]
    full = engine.history.get(key)
    clock = engine.history.episode_clock(key)
    fields = set(HEAD_FIELDS["episode"]) | {"origin_event_id", "reference_price", "reference_time_ms"}
    assert clock == {k: v for k, v in full.items() if k in fields}
    assert "confirmations" not in clock
    clock["started_ms"] = 0
    assert History(store).get(key) == full
    revision = f"episode:{event['episode_id']}:{full['revision']:010d}"
    # A changed fresh head cannot use a previous valid projection as proof.
    raw_head = store.get_runtime(PREFIX + key)
    with store.db:
        store.db.execute(RUNTIME_UPSERT, (PREFIX + key, encode({**raw_head, "strength": 999})))
    if compact:
        with pytest.raises(StateError, match="conflicting history head"):
            engine.history.episode_clock(key)
    with store.db:
        store.db.execute(RUNTIME_UPSERT, (PREFIX + key, encode(raw_head)))
        store.db.execute("DELETE FROM runtime WHERE key=?", (PREFIX + revision,))
    with pytest.raises(StateError, match="conflicting history head"):
        engine.history.episode_clock(key)


def test_frame_inputs_keep_exact_canonical_hash_and_independent_callback_values(store):
    bounded(store)
    seen = []

    def callback(event):
        expected = [asdict(c) for c in window()[-len(event["source_inputs"]) :]]
        assert event["source_inputs"] == expected
        assert event["source_window_id"] == hashlib.sha256(encode(expected).encode()).hexdigest()
        seen.append(event["event_id"])
        event["source_inputs"][0]["close"] = 777

    engine = EventEngine(store, on_event=callback)
    engine.observe("spot/BTCUSDT/1m", window(), 1e6, 0.01, NOW, live=True)
    assert len(seen) == 4
    for event in engine.history.events():
        assert event["source_inputs"][0]["close"] != 777
        assert (
            event["source_window_id"] == hashlib.sha256(encode(event["source_inputs"]).encode()).hexdigest()
        )


def test_multi_tf_closure_keeps_callback_rearm_changes_and_rolls_back_as_one_unit(store):
    bounded(store)
    engine = EventEngine(store)
    for tf in ("1m", "3m"):
        engine.observe(f"spot/BTCUSDT/{tf}", window(tf=tf), 1e6, 0.01, NOW, live=True)
    before = list(store.db.iterdump())

    def fail(episode):
        assert episode["state"] == "CLOSED" and episode["confirmations"]
        raise RuntimeError("closure failed")

    engine.on_episode = fail
    with pytest.raises(RuntimeError, match="closure failed"):
        engine.tick(NOW + 3_600_000)
    assert list(store.db.iterdump()) == before
    assert engine.history._condition_batch is None
    seen = []

    def changed(episode):
        seen.append(episode["episode_id"])
        for condition in episode["conditions"]:
            current = History(store).get(condition)
            History(store).put(condition, {**current, "non_signal_bars": 7, "blocked": False})

    engine.on_episode = changed
    engine.tick(NOW + 3_600_000)
    assert seen and not engine.history.items("active:")
    for _, condition in engine.history.items("condition:"):
        # A full close callback remains an exposed mutation boundary.
        if condition.get("non_signal_bars") == 7:
            assert condition["blocked"] is False
    assert all(value["state"] == "CLOSED" for _, value in engine.history.items("episode-head:"))


def test_full_legacy_head_still_checks_non_clock_body_fields(store):
    from tests.test_catalogue_write import cold, head_key

    coordinate, body, _ = next(v for v in cold(store) if v[0].kind == "episode")
    before = list(store.db.iterdump())
    with pytest.raises(StateError, match="Conflicting new history head projection"):
        store.put_runtime(head_key(coordinate), {**body, "confirmations": {"injected": True}})
    assert list(store.db.iterdump()) == before
