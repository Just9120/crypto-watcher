import copy

import pytest

from cryptowatcher import condition_state as conditions
from cryptowatcher.config import OwnerSettings
from cryptowatcher.detectors import DetectorUnavailable, evaluate
from cryptowatcher.events import PREFIX, EventEngine, History
from cryptowatcher.expiry_index import hot_binding
from cryptowatcher.outcomes import OutcomeWorker
from cryptowatcher.storage import StateError, Store, encode
from cryptowatcher.storage_lifecycle import BoundedRetention
from tests.test_bounded_storage import bounded
from tests.test_events import NOW, window

STREAM = "spot/BTCUSDT/1m"
UP = f"condition:{STREAM}:price:UP"
DOWN = f"condition:{STREAM}:price:DOWN"
GROUP = conditions.GROUP + STREAM
DAY = 86_400_000


@pytest.fixture
def state(store):
    bounded(store)
    clock = [NOW]
    store.clock_ms = lambda: clock[0]
    return store, History(store), clock


def value(n):
    return {"last_ms": n, "non_signal_bars": 2, "blocked": False}


def group(history):
    return conditions.read(history, STREAM)


def test_migration_preserves_logical_reads_order_limits_counts_and_individual_expiry(state):
    store, history, clock = state
    with store.db:
        history.put(UP, value(1))
    original_expiry = hot_binding(store.db, PREFIX + UP)[0]
    clock[0] += 100
    with store.db:
        history.put(DOWN, value(2))
        history.put("another-key", {"v": 3})
    before_all = list(history.scan(""))
    before = history.items("condition:")
    count = history.count("")
    with store.db, conditions.batch(history, STREAM):
        history.put(DOWN, value(2))
        assert history.get(UP) == value(1)
        assert history.get(DOWN) == value(2)
    assert history.items("condition:") == before
    assert list(history.scan("")) == before_all
    assert history.count("") == count
    for prefix in ("", "c", "con", "condition", "condition:", UP, f"condition:{STREAM}:price:"):
        expected = [(k, v) for k, v in before_all if k.startswith(prefix)]
        assert history.count(prefix) == len(expected)
        assert history.items(prefix) == expected
        assert history.items(prefix, 1) == expected[:1]
        assert history.items(prefix, 0) == []
        assert history.items(prefix, reverse=True) == list(reversed(expected))
        assert history.items(prefix, 1, reverse=True) == list(reversed(expected))[:1]
    assert store.count_runtime(PREFIX + "condition:") == 0
    assert store.count_runtime(PREFIX + conditions.GROUP) == 1
    assert group(history)["states"]["price:UP"]["expires_ms"] == original_expiry
    assert (
        group(history)["states"]["price:DOWN"]["expires_ms"]
        == clock[0] + store.snapshot().settings.candle_retention_days * DAY
    )
    assert hot_binding(store.db, PREFIX + GROUP) == (original_expiry, "condition-state", None)
    fetched = history.get(UP)
    fetched["last_ms"] = 999
    assert history.get(UP) == value(1)
    for invalid in (-1, True, "1"):
        with pytest.raises(ValueError):
            history.items("condition:", invalid)


def test_partial_and_final_retention_neither_shorten_nor_renew_another_member(state):
    store, history, clock = state
    with store.db:
        history.put(UP, value(1))
    first = clock[0] + store.snapshot().settings.candle_retention_days * DAY
    clock[0] += 100
    with store.db, conditions.batch(history, STREAM):
        history.put(DOWN, value(2))
    last = clock[0] + store.snapshot().settings.candle_retention_days * DAY
    clock[0] = first
    BoundedRetention(store).work(first)
    assert history.get(UP) is None
    assert history.get(DOWN) == value(2)
    assert hot_binding(store.db, PREFIX + GROUP) == (last, "condition-state", None)
    assert list(group(history)["states"]) == ["price:DOWN"]
    telemetry = store.get_runtime("market_retention")
    assert telemetry["condition_members_expired"] == 1
    assert telemetry["condition_groups_updated"] == 1
    assert telemetry["removed_services"] == 0
    clock[0] = last
    BoundedRetention(store).work(last)
    assert history.get(DOWN) is None and group(history) is None
    assert hot_binding(store.db, PREFIX + GROUP) is None
    telemetry = store.get_runtime("market_retention")
    assert telemetry["condition_members_expired"] == 1
    assert telemetry["condition_groups_updated"] == 0
    assert telemetry["removed_services"] == 1
    assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not store.db.execute("PRAGMA foreign_key_check").fetchall()


def test_rollback_and_old_reader_preserve_original_legacy_and_group_snapshots(state):
    store, history, _ = state
    with store.db:
        history.put(UP, value(1))
    old = Store(store.path, store.owner_id, readonly=True)
    try:
        old.db.execute("BEGIN")
        assert History(old).get(UP) == value(1)
        with pytest.raises(RuntimeError, match="interrupted"):
            with store.db, conditions.batch(history, STREAM):
                history.put(UP, value(2))
                with conditions.exposed(history):
                    assert History(store).get(UP) == value(2)
                    assert History(old).get(UP) == value(1)
                raise RuntimeError("interrupted migration")
        assert group(history) is None and history.get(UP) == value(1)
        with store.db, conditions.batch(history, STREAM):
            history.put(UP, value(3))
        assert History(old).get(UP) == value(1)
        old.db.rollback()
        old.db.execute("BEGIN")
        assert History(old).get(UP) == value(3)
        with store.db:
            history.put(UP, value(4))
        assert History(old).get(UP) == value(3)
        old.db.rollback()
        assert History(old).get(UP) == value(4)
        assert old.db.total_changes == 0
    finally:
        old.close()


@pytest.mark.parametrize("existing_group", [False, True])
def test_unexposed_reentrant_mutation_is_detected_and_rolled_back(state, existing_group):
    store, history, _ = state
    with store.db:
        history.put(UP, value(1))
    if existing_group:
        with store.db, conditions.batch(history, STREAM):
            history.put(UP, value(1))
    before = list(history.scan(""))
    with pytest.raises(StateError, match="Concurrent"):
        with store.db, conditions.batch(history, STREAM):
            history.put(UP, value(2))
            History(store).put(DOWN, value(3))
    assert list(history.scan("")) == before
    assert history._condition_batch is None


def test_exposed_callbacks_see_prior_writes_and_their_changes_are_not_overwritten(state):
    store, history, _ = state
    checked = []

    def callback(event):
        reader = History(store)
        if event["detector_id"] == "price":
            current = reader.get(UP)
            assert current["last_ms"] == NOW
            reader.put(UP, {**current, "non_signal_bars": 7})
            checked.append(True)

    engine = EventEngine(store, on_event=callback)
    engine.observe(STREAM, window(), 1e6, 0.01, NOW, live=True)
    assert checked and history.get(UP)["non_signal_bars"] == 7
    assert len(engine.history.events()) == 4

    def broken(_):
        assert History(store).get(UP)["last_ms"] == NOW + 120_000
        raise RuntimeError("callback after publication")

    engine.on_event = broken
    # A repeated unchanged candle updates liveness without invoking on_event.
    engine.observe(STREAM, window(NOW + 60_000), 1e6, 0.01, NOW + 60_000, live=True)
    assert len(engine.history.events()) == 4
    before = list(history.scan(""))
    with pytest.raises(RuntimeError, match="callback after publication"):
        engine.observe(STREAM, window(NOW + 120_000, close=110), 1e6, 0.01, NOW + 120_000, live=True)
    assert list(history.scan("")) == before


def test_unknown_legacy_fields_stay_legacy_and_unknown_group_format_is_preserved(state):
    store, history, _ = state
    unknown = {**value(1), "future_field": {"precision": 1.23456789012345}}
    with store.db:
        history.put(UP, unknown)
    with store.db, conditions.batch(history, STREAM):
        current = history.get(UP)
        current["blocked"] = True
        history.put(UP, current)
    assert history.get(UP) == {**unknown, "blocked": True}
    assert group(history) is None
    future = {"schema": "cryptowatcher.condition-state.future", "states": {}}
    with store.db:
        store.db.execute("INSERT INTO runtime VALUES(?,?)", (PREFIX + GROUP, encode(future)))
    with pytest.raises(StateError, match="Unknown grouped"):
        history.get(UP)
    assert store.get_runtime(PREFIX + GROUP) == future


def test_logical_delete_reopen_and_mixed_owner_conflict_keep_data_and_exact_expiry(state):
    store, history, _ = state
    with store.db, conditions.batch(history, STREAM):
        history.put(UP, value(1))
        history.put(DOWN, value(2))
    original = copy.deepcopy(group(history))
    other = Store(store.path, store.owner_id, readonly=True)
    try:
        assert History(other).items("condition:") == history.items("condition:")
    finally:
        other.close()
    with pytest.raises(StateError, match="Conflicting mixed"):
        with store.db:
            history._put_raw(UP, value(1))
            history.get(UP)
    assert group(history) == original
    with store.db:
        history.delete(UP)
    assert history.get(UP) is None and history.get(DOWN) == value(2)
    assert group(history)["states"]["price:DOWN"] == original["states"]["price:DOWN"]
    with store.db:
        history.delete(DOWN)
    assert group(history) is None
    assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not store.db.execute("PRAGMA foreign_key_check").fetchall()


@pytest.mark.parametrize("unknown", [None, {}, {"schema": "future", "states": {}}])
def test_present_unknown_group_is_never_treated_as_missing_or_overwritten(state, unknown):
    store, history, _ = state
    with store.db:
        store.db.execute("INSERT INTO runtime VALUES(?,?)", (PREFIX + GROUP, encode(unknown)))
    with pytest.raises(StateError, match="Unknown grouped"):
        with store.db, conditions.batch(history, STREAM):
            history.put(UP, value(1))
    assert store.get_runtime(PREFIX + GROUP, "missing") == unknown


def test_invalid_group_owner_write_or_expiry_is_rejected_without_changing_existing_state(state):
    store, history, _ = state
    with store.db, conditions.batch(history, STREAM):
        history.put(UP, value(1))
    original = list(history.scan(""))
    with pytest.raises(StateError, match="owner"):
        with store.db:
            history.put(conditions.GROUP + "unknown/OTHER/1m", copy.deepcopy(group(history)))
    with pytest.raises(StateError, match="owner"):
        conditions.expire(history, UP, NOW + 99 * DAY)
    assert list(history.scan("")) == original


def test_full_legacy_and_grouped_detector_episode_and_consumer_results_match_across_markets_tf_and_failures(
    state, tmp_path, monkeypatch
):
    store, grouped, clock = state
    legacy_store = Store(tmp_path / "legacy-conditions.sqlite3", store.owner_id)
    legacy_store.initialize(OwnerSettings())
    legacy_store.clock_ms = lambda: clock[0]
    phase = [0]

    def detector(family, *args):
        if phase[0] in (3, 5) and family == "volatility":
            raise DetectorUnavailable("controlled family recovery")
        return evaluate(family, *args)

    closed = [[], []]
    engines = []
    try:
        for index, current in enumerate((legacy_store, store)):
            worker = OutcomeWorker(current)
            engines.append(
                EventEngine(
                    current,
                    detector,
                    source_revision="a" * 40,
                    on_event=worker.admit,
                    on_episode=lambda episode, i=index: closed[i].append(copy.deepcopy(episode)),
                )
            )
        legacy = engines[0].history
        expected_expiry = {}
        original_put = legacy.put

        def track_legacy_expiry(key, value):
            if key.startswith("condition:"):
                expected_expiry[key] = (
                    legacy_store.clock_ms() + legacy_store.snapshot().settings.candle_retention_days * DAY
                )
            original_put(key, value)

        monkeypatch.setattr(legacy, "put", track_legacy_expiry)
        for step in range(9):
            phase[0] = step
            clock[0] = NOW + step * 900_000
            if step == 4:
                for current in (legacy_store, store):
                    before = current.snapshot()
                    current.update(before.revision, before.settings.changed(price_move_min=1.5))
            for market in ("spot", "linear"):
                for tf in ("1m", "3m", "5m", "15m"):
                    candles = window(
                        clock[0], tf, close=(103 if step % 3 == 1 else 97 if step % 3 == 2 else 100)
                    )
                    for engine in engines:
                        engine.observe(
                            f"{market}/BTCUSDT/{tf}",
                            candles,
                            1e6,
                            0.01,
                            clock[0],
                            live=step % 4 != 0,
                        )
            assert grouped.items("condition:") == legacy.items("condition:")
        clock[0] += 3_600_000
        for engine in engines:
            engine.tick(clock[0])
        assert closed[0] == closed[1]
        for prefix in (
            "condition:",
            "diagnostic:",
            "event-head:",
            "episode-head:",
            "active:",
            "history:",
            "outcome-job:",
            "comparison-job:",
            "outbox:telegram:",
            "pending:telegram:",
        ):
            assert grouped.items(prefix) == legacy.items(prefix), prefix
        for key, _ in grouped.items("condition:"):
            stream, member = conditions.coordinate(key)
            assert conditions.read(grouped, stream)["states"][member]["expires_ms"] == expected_expiry[key]
        for current in (legacy_store, store):
            assert current.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert not current.db.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        legacy_store.close()
